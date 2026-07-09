#!/usr/bin/env python3
"""
bin/opencode_loop.py — receiver side AI self-healing loop.

Polls sender-state.json (written by the sender) for failures.
When a target fails, invokes OpenCode to diagnose and fix the code,
then sets fix_pushed=true in receiver-state.json so the sender retries.

File ownership:
    sender-state.json   — receiver reads only (sender owns it)
    receiver-state.json — receiver writes (this script owns it)
"""

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lib

REPO = lib.repo_root()
POLL_INTERVAL = 10  # seconds between polls
MAX_ERRORS = 3  # unclear OpenCode responses before force-unblocking
TIMEOUT_SECS = 1800  # 30 min max per OpenCode invocation


# ---------------------------------------------------------------------------
# Pure functions (testable without I/O)
# ---------------------------------------------------------------------------


def should_invoke_opencode(
    sender_state: dict,
    receiver_state: dict,
    last_processed_log: str,
    latest_log: str,
) -> tuple[bool, str]:
    """
    Decide whether to invoke OpenCode for the current failure.

    Returns (should_invoke, reason_if_not).
    Pure function — no I/O, fully testable.
    """
    status = sender_state.get("status", "")
    last_result = sender_state.get("last_result", "")
    target = sender_state.get("last_run_log", "")

    if status in ("completed", "completed_with_failures"):
        return False, "loop complete"

    if status == "needs_human":
        return False, "waiting for human action"

    if last_result != "failed":
        return False, f"last result is '{last_result}' — not a failure"

    if lib.is_already_completed(target, sender_state):
        return False, f"{target} already completed — stale failed state"

    if not latest_log:
        return False, f"{target} failed but no log found yet"

    if latest_log == last_processed_log and receiver_state.get("fix_pushed"):
        return (
            False,
            f"{target} — fix already pushed, waiting for sender to produce a new log",
        )

    if not lib.sender_needs_fix(sender_state):
        return False, f"{target} — sender not in a fixable state"

    return True, ""


def build_opencode_prompt(
    target: str,
    latest_log: str,
    log_content: str,
    prev_logs_content: str,
) -> str:
    """
    Build the OpenCode prompt for a failed target.
    Pure function — no I/O, fully testable.
    """
    return f"""You are the deployment agent. A make target has failed and you must diagnose and fix the code.

## Repo structure (understand this before touching anything)
- .loop/bin/loop.py             — sender loop (do NOT modify while loop is running)
- .loop/bin/opencode_loop.py    — receiver loop (this script — do NOT self-modify)
- receiver-state.json           — YOUR file: targets, deps, fix signals (you may update fix_pushed)
- sender-state.json             — SENDER'S file: runtime state — DO NOT COMMIT THIS FILE
- loop-context.md               — Shared brain: full history of every failure and fix applied
- runs/                         — Run logs committed by the sender — read to diagnose failures

## Critical rules
- NEVER commit sender-state.json
- NEVER commit files in runs/ or dist/
- NEVER run live infrastructure commands (make <deploy target>, kubectl, ansible-playbook, etc.)
- NEVER hardcode credentials

## Files to read first (in this order)
1. loop-context.md — FULL FILE. Read BEFORE writing any fix. Same fix twice = wrong approach.
2. runs/{latest_log} — FULL FILE. Look for the FIRST error line, not the last symptom.
3. sender-state.json — note attempt count and completed/failed targets for context.

## Previous attempts on this target
{prev_logs_content}

## Instructions
IMPORTANT: If the failing target is 'loop-test-fail' — deliberate test target. Output RETRY immediately.

For real failures:
1. Read all three files above in full before writing any code.
2. Identify ROOT CAUSE — first error in the log, not the cascading symptoms.
3. Cross-check loop-context.md: if same error recurred after a prior fix, use a completely different approach.
4. Fix minimum necessary files. Prefer editing existing files over creating new ones.
5. Validate your changes before committing.
6. Update loop-context.md: add a bullet under ### {target} with today's date, error seen, and fix applied.
7. Commit and push your changes (NOT sender-state.json, NOT runs/).

Output exactly one of these as the LAST LINE: RETRY, SUCCESS, or NEEDS_HUMAN
- RETRY: fix pushed, sender should retry
- SUCCESS: target already succeeded (no fix needed)
- NEEDS_HUMAN: same error 3+ times with no new fix, OR requires physical action, OR requires credentials you cannot provide
"""


def set_fix_pushed(repo: Path, branch: str, message: str) -> None:
    """Set fix_pushed=true in receiver-state.json and push."""
    rc = lib.load_receiver_state(repo)
    rc["fix_pushed"] = True
    rc["last_fix"] = message
    lib.save_receiver_state(repo, rc)
    lib.git(repo, "add", "receiver-state.json")
    lib.git(repo, "commit", "-m", f"loop: fix_pushed=true — {message}")
    lib.git(
        repo,
        "pull",
        "origin",
        branch,
        "--rebase",
        "--autostash",
        "-X",
        "theirs",
        "--quiet",
    )
    r = lib.git(repo, "push", "origin", branch)
    if r.returncode != 0:
        lib.git(
            repo,
            "pull",
            "origin",
            branch,
            "--rebase",
            "--autostash",
            "-X",
            "theirs",
            "--quiet",
        )
        lib.git(repo, "push", "origin", branch)


def notify_human(sender_state: dict, target: str, latest_log: str) -> None:
    """Show a macOS notification and print a banner for human action required."""
    human_action = sender_state.get("human_action", f"Check runs/{latest_log}")
    print()
    print("════════════════════════════════════════════════════════════")
    print("  ⚠️  ACTION REQUIRED")
    print("════════════════════════════════════════════════════════════")
    print(f"  {human_action}")
    print("════════════════════════════════════════════════════════════")
    print("  Once done, run: make loop-reset")
    print("════════════════════════════════════════════════════════════")
    subprocess.run(
        [
            "osascript",
            "-e",
            f'display notification "{human_action}" with title "Loop" subtitle "Action Required" sound name "Sosumi"',
        ],
        capture_output=True,
    )


def gather_previous_logs(runs_dir: Path, target: str, logs: list) -> str:
    """Gather content from previous run logs and last OpenCode log for context."""
    prev = ""
    for pf in list(logs[1:4]):
        prev += f"\n--- Previous run log for {target}: {pf.name} ---\n"
        prev += pf.read_text(errors="replace")[-3000:]

    oc_logs = sorted(
        runs_dir.glob("opencode-loop-*.log"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if oc_logs:
        prev += f"\n--- Last OpenCode invocation ({oc_logs[0].name}) ---\n"
        prev += oc_logs[0].read_text(errors="replace")[-2000:]

    return prev


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main() -> None:
    lib.register_ours_driver(REPO)
    branch = lib.current_branch(REPO)

    lib.log(f"Starting receiver loop — polling every {POLL_INTERVAL}s")

    errors = 0
    last_processed_log = ""
    sender_state_path = REPO / "sender-state.json"
    runs_dir = REPO / "runs"

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

        if not sender_state_path.exists():
            lib.log_wait("sender-state.json not found yet — sender loop hasn't started")
            time.sleep(POLL_INTERVAL)
            continue

        sender = lib.load_sender_state(REPO)
        receiver = lib.load_receiver_state(REPO)

        status = sender.get("status", "")
        target = sender.get("last_run_log", "")

        # ── Human action notification ────────────────────────────────────────
        if sender.get("human_action"):
            notify_human(sender, target, "")
            time.sleep(POLL_INTERVAL)
            continue

        # ── Loop complete ────────────────────────────────────────────────────
        if status in ("completed", "completed_with_failures"):
            lib.log_ok("Loop complete! All targets finished.")
            sys.exit(0)

        # ── Find latest log for this target ──────────────────────────────────
        logs = (
            sorted(
                runs_dir.glob(f"{target}-*.log"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if target
            else []
        )
        latest_log = logs[0].name if logs else ""

        # ── Decide whether to invoke OpenCode ───────────────────────────────
        should_invoke, reason = should_invoke_opencode(
            sender, receiver, last_processed_log, latest_log
        )

        if not should_invoke:
            lib.log(
                f"Standing by — {reason} | target: {target or 'none'} | status: {status}"
            )
            time.sleep(POLL_INTERVAL)
            continue

        lib.log(f"⚡ {target} failed — invoking OpenCode to fix (log: {latest_log})")
        last_processed_log = latest_log
        oc_log = runs_dir / f"opencode-loop-{time.strftime('%Y%m%d-%H%M%S')}.log"

        prev_logs_content = gather_previous_logs(runs_dir, target, logs)
        log_content = logs[0].read_text(errors="replace") if logs else ""
        prompt = build_opencode_prompt(
            target, latest_log, log_content, prev_logs_content
        )

        result = subprocess.run(
            ["opencode", "run", "--dangerously-skip-permissions", prompt],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECS,
            cwd=str(REPO),
        )
        output = result.stdout + result.stderr
        print(output, flush=True)
        runs_dir.mkdir(exist_ok=True)
        oc_log.write_text(output)

        # Trim context if needed
        trim_script = Path(__file__).resolve().parent / "trim_loop_context.py"
        if trim_script.exists():
            subprocess.run(
                [sys.executable, str(trim_script)], cwd=str(REPO), capture_output=True
            )

        # Commit OpenCode log and context (receiver owns these)
        lib.git(
            REPO, "add", str(oc_log), "loop-context.md", "docs/loop-context-archive.md"
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
            lib.log_ok(f"{target} — fix pushed, sender will retry")
        elif last_word == "NEEDS_HUMAN":
            lib.log_fail(f"{target} — OpenCode cannot fix automatically")
            sender = lib.load_sender_state(REPO)
            sender["human_action"] = (
                f"OpenCode could not fix {target} automatically. "
                f"Check runs/{latest_log}. Fix manually then run: make loop-reset"
            )
            sender["status"] = "needs_human"
            # NOTE: we write sender-state.json here only to surface the human_action
            # message — this is the one exception to strict ownership, and only for
            # the needs_human escalation path.
            lib.save_sender_state(REPO, sender)
            lib.git(REPO, "add", "sender-state.json")
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
                    "Too many unclear responses — forcing fix_pushed=true to unblock sender"
                )
                set_fix_pushed(
                    branch, f"forced unblock after {MAX_ERRORS} unclear responses"
                )
                errors = 0

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
