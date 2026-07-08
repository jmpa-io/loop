#!/usr/bin/env python3
"""
bin/opencode_loop.py — Mac-side AI self-healing loop.

Polls loop-run-state.json (written by the runner) for failures.
When a target fails, invokes OpenCode to diagnose and fix the code,
then sets fix_pushed=true so the runner retries.

File ownership:
    loop-run-state.json — runner owns this. Mac only writes human_action on
                          NEEDS_HUMAN (one-way; runner overwrites on ack).
    loop-state.json     — Mac owns this. Writes fix_pushed, opencode_last_fix.
"""

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lib

REPO = lib.repo_root()
POLL_INTERVAL = 10  # seconds
MAX_ERRORS = 3  # unclear responses before force-unblocking
TIMEOUT_SECS = 1800  # 30 min max per OpenCode invocation

# Path to this script's directory — used to reference sibling scripts in prompt
LOOP_BIN = Path(__file__).resolve().parent.relative_to(REPO)


def set_fix_pushed(branch: str, message: str) -> None:
    oc = lib.load_oc_state(REPO)
    oc["fix_pushed"] = True
    oc["opencode_last_fix"] = message
    lib.save_oc_state(REPO, oc)
    lib.git(REPO, "add", "loop-state.json")
    lib.git(REPO, "commit", "-m", f"loop: fix_pushed=true — {message}")
    lib.git(
        REPO,
        "pull",
        "origin",
        branch,
        "--rebase",
        "--autostash",
        "-X",
        "theirs",
        "--quiet",
    )
    r = lib.git(REPO, "push", "origin", branch)
    if r.returncode != 0:
        lib.git(
            REPO,
            "pull",
            "origin",
            branch,
            "--rebase",
            "--autostash",
            "-X",
            "theirs",
            "--quiet",
        )
        lib.git(REPO, "push", "origin", branch)


def main() -> None:
    lib.register_ours_driver(REPO)
    branch = lib.current_branch(REPO)

    lib.log(f"Starting OpenCode loop — polling every {POLL_INTERVAL}s")

    errors = 0
    last_processed_log = ""
    run_state_path = REPO / "loop-run-state.json"

    while True:
        lib.git(
            REPO,
            "pull",
            "origin",
            branch,
            "--rebase",
            "--autostash",
            "-X",
            "theirs",
            "--quiet",
        )

        if not run_state_path.exists():
            lib.log_wait(
                "loop-run-state.json not found yet — runner loop hasn't started"
            )
            time.sleep(POLL_INTERVAL)
            continue

        run = lib.load_run_state(REPO)
        status = run.get("status", "")
        target = run.get("last_run_log", "")
        last_result = run.get("last_result", "")
        human_action = run.get("human_action") or ""

        # Human action notification
        if human_action:
            print()
            print("════════════════════════════════════════════════════════════")
            print("  ⚠️  ACTION REQUIRED")
            print("════════════════════════════════════════════════════════════")
            print(f"  {human_action}")
            print("════════════════════════════════════════════════════════════")
            print("  Once done, run: make loop-reset")
            print("════════════════════════════════════════════════════════════")
            # macOS notification
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    f'display notification "{human_action}" with title "Loop" subtitle "Action Required" sound name "Sosumi"',
                ],
                capture_output=True,
            )
            time.sleep(POLL_INTERVAL)
            continue

        if status in ("completed", "completed_with_failures"):
            lib.log_ok("Loop complete! All targets finished.")
            sys.exit(0)

        if status == "needs_human":
            lib.log_wait("Waiting for human action — run: make loop-reset when done")
            time.sleep(POLL_INTERVAL)
            continue

        if last_result != "failed":
            lib.log(
                f"Runner is running — last: {target or 'none'} | result: {last_result or 'pending'} | status: {status}"
            )
            time.sleep(POLL_INTERVAL)
            continue

        # Guard: stale failed state (target already completed)
        if lib.is_already_completed(target, run):
            lib.log(f"Skipping — {target} is already completed (stale failed state)")
            time.sleep(POLL_INTERVAL)
            continue

        # Find latest log for this target
        runs_dir = REPO / "runs"
        logs = sorted(
            runs_dir.glob(f"{target}-*.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not logs:
            lib.log_wait(f"{target} failed but no log found yet — waiting...")
            time.sleep(POLL_INTERVAL)
            continue

        latest_log = logs[0].name
        oc = lib.load_oc_state(REPO)
        waiting = oc.get("waiting_for_fix", False)
        fix_pushed = oc.get("fix_pushed", False)

        if latest_log == last_processed_log and fix_pushed:
            lib.log_wait(
                f"{target} — fix already pushed, waiting for runner to produce a new log..."
            )
            time.sleep(POLL_INTERVAL)
            continue

        if not waiting:
            lib.log(
                f"Runner is actively running {target} (not paused) — standing by..."
            )
            time.sleep(POLL_INTERVAL)
            continue

        lib.log(f"⚡ {target} failed — invoking OpenCode to fix (log: {latest_log})")
        last_processed_log = latest_log
        oc_log = runs_dir / f"opencode-loop-{time.strftime('%Y%m%d-%H%M%S')}.log"

        # Gather previous logs for context
        prev_logs = ""
        for pf in list(logs[1:4]):
            prev_logs += f"\n--- Previous run log for {target}: {pf.name} ---\n"
            prev_logs += pf.read_text(errors="replace")[-3000:]

        oc_logs = sorted(
            runs_dir.glob("opencode-loop-*.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if oc_logs:
            prev_logs += f"\n--- Last OpenCode invocation ({oc_logs[0].name}) ---\n"
            prev_logs += oc_logs[0].read_text(errors="replace")[-2000:]

        prompt = f"""You are the deployment agent. A make target has failed and you must diagnose and fix the code.

## Repo structure (understand this before touching anything)
- .loop/bin/loop.py          — runner loop (do NOT modify while loop is running)
- .loop/bin/opencode_loop.py — Mac fixer loop (this script — do NOT self-modify)
- loop-state.json            — YOUR file: targets, deps, fix signals (you may update opencode_last_fix)
- loop-run-state.json        — RUNNER'S file: runtime state — DO NOT COMMIT THIS FILE
- loop-context.md            — Shared brain: full history of every failure and fix applied
- runs/                      — Run logs committed by the runner — read to diagnose failures

## Critical rules
- NEVER commit loop-run-state.json
- NEVER commit files in runs/ or dist/
- NEVER run live infrastructure commands (make <deploy target>, kubectl, ansible-playbook, etc.)
- NEVER hardcode credentials

## Files to read first (in this order)
1. loop-context.md — FULL FILE. Read BEFORE writing any fix. Same fix twice = wrong approach.
2. runs/{latest_log} — FULL FILE. Look for the FIRST error line, not the last symptom.
3. loop-run-state.json — note attempt count and completed/failed targets for context.

## Previous OpenCode attempts on this target
{prev_logs}

## Instructions
IMPORTANT: If the failing target is 'loop-test-fail' — deliberate test target. Output RETRY immediately.

For real failures:
1. Read all three files above in full before writing any code.
2. Identify ROOT CAUSE — first error in the log, not the cascading symptoms.
3. Cross-check loop-context.md: if same error recurred after a prior fix, use a completely different approach.
4. Fix minimum necessary files. Prefer editing existing files over creating new ones.
5. Validate your changes before committing.
6. Update loop-context.md: add a bullet under ### {target} with today's date, error seen, and fix applied.
7. Commit and push your changes (NOT loop-run-state.json, NOT runs/).

Output exactly one of these as the LAST LINE: RETRY, SUCCESS, or NEEDS_HUMAN
- RETRY: fix pushed, runner should retry
- SUCCESS: target already succeeded (no fix needed)
- NEEDS_HUMAN: same error 3+ times with no new fix, OR requires physical action, OR requires credentials you cannot provide
"""

        result = subprocess.run(
            ["opencode", "run", "--dangerously-skip-permissions", prompt],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECS,
            cwd=str(REPO),
        )
        output = result.stdout + result.stderr
        print(output, flush=True)
        oc_log.write_text(output)

        # Trim context if needed
        trim_script = Path(__file__).resolve().parent / "trim_loop_context.py"
        if trim_script.exists():
            subprocess.run(
                [sys.executable, str(trim_script)], cwd=str(REPO), capture_output=True
            )

        lib.git(
            REPO,
            "add",
            str(oc_log),
            "loop-state.json",
            "loop-context.md",
            "docs/loop-context-archive.md",
        )
        lib.git(REPO, "commit", "-m", f"loop: opencode log for {target}")
        lib.git(
            REPO,
            "pull",
            "origin",
            branch,
            "--rebase",
            "--autostash",
            "-X",
            "theirs",
            "--quiet",
        )
        lib.git(REPO, "push", "origin", branch)

        last_word = lib.parse_last_word(output)
        lib.log(f"OpenCode result: '{last_word or 'UNCLEAR'}'")

        if last_word in ("SUCCESS", "RETRY"):
            set_fix_pushed(branch, f"{target} fixed by OpenCode")
            errors = 0
            lib.log_ok(f"{target} — fix pushed, runner will retry")
        elif last_word == "NEEDS_HUMAN":
            lib.log_fail(f"{target} — OpenCode cannot fix automatically")
            run = lib.load_run_state(REPO)
            run["human_action"] = (
                f"OpenCode could not fix {target} automatically. "
                f"Check runs/{latest_log}. Fix manually then run: make loop-reset"
            )
            run["status"] = "needs_human"
            lib.save_run_state(REPO, run)
            lib.git(REPO, "add", "loop-run-state.json", "loop-state.json")
            lib.git(REPO, "commit", "-m", f"loop: needs human — {target}")
            lib.git(
                REPO,
                "pull",
                "origin",
                branch,
                "--rebase",
                "--autostash",
                "-X",
                "theirs",
                "--quiet",
            )
            lib.git(REPO, "push", "origin", branch)
        else:
            errors += 1
            lib.log_fail(
                f"OpenCode output unclear ({errors}/{MAX_ERRORS} unclear responses)"
            )
            if errors >= MAX_ERRORS:
                lib.log_fail(
                    "Too many unclear responses — forcing fix_pushed=true to unblock runner"
                )
                set_fix_pushed(
                    branch, f"forced unblock after {MAX_ERRORS} unclear responses"
                )
                errors = 0

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
