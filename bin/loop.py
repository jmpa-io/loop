#!/usr/bin/env python3
"""
bin/loop.py — dependency-aware deployment loop.

Reads targets and their dependency graph from loop-state.json.
A target runs only when ALL its dependencies have succeeded.
If a dependency permanently failed (max retries), the target is SKIPPED
so independent targets can still run.

State files:
    loop-run-state.json — written by this script (runner machine)
    loop-state.json     — written by OpenCode (Mac)
"""

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lib

REPO = lib.repo_root()
RUN_STATE_PATH = REPO / "loop-run-state.json"
OC_STATE_PATH = REPO / "loop-state.json"
FIX_POLL = 10  # seconds between polls when waiting


# ---------------------------------------------------------------------------
# Human wait
# ---------------------------------------------------------------------------


def wait_for_human(branch: str, message: str) -> None:
    """Block until fix_pushed=true is set in loop-state.json."""
    lib.log_fail(f"Needs human: {message}")
    run = lib.load_run_state(REPO)
    run["status"] = "needs_human"
    run["human_action"] = message
    lib.save_run_state(REPO, run)
    lib.push_run_state(REPO, branch, f"blocked — needs human: {message}")

    while True:
        time.sleep(FIX_POLL)
        lib.git_pull(REPO, branch)
        oc = lib.load_oc_state(REPO)
        if oc.get("fix_pushed"):
            lib.log_ok("Human action acknowledged — resuming")
            run = lib.load_run_state(REPO)
            run["status"] = "running"
            run["human_action"] = None
            lib.save_run_state(REPO, run)
            oc["fix_pushed"] = False
            lib.save_oc_state(REPO, oc)
            lib.push_run_state(REPO, branch, "resuming after human ack")
            return
        lib.log_wait("Waiting for human action and fix_pushed=true...")


# ---------------------------------------------------------------------------
# Run a single target
# ---------------------------------------------------------------------------


def run_target(target: str, branch: str) -> bool:
    """
    Run `make <target>` once. Returns True on success, False on failure.
    Handles retry signalling, blocker detection, and state updates.
    """
    run = lib.load_run_state(REPO)
    oc = lib.load_oc_state(REPO)

    # Idempotency guard — never re-run a completed target
    if lib.is_already_completed(target, run):
        lib.log_ok(f"{target} — already completed, skipping")
        return True

    max_att = run.get("max_attempts", oc.get("max_attempts", 10))
    attempt = run.get("attempts", {}).get(target, 1)
    lib.log(f"▶ {target} (attempt {attempt}/{max_att})")

    # Clear stale fix_pushed before running
    if oc.get("fix_pushed"):
        oc["fix_pushed"] = False
        lib.save_oc_state(REPO, oc)

    result = subprocess.run(["make", target], cwd=str(REPO))

    if result.returncode == 0:
        lib.log_ok(f"{target} succeeded")
        run = lib.apply_target_success(lib.load_run_state(REPO), target)
        lib.save_run_state(REPO, run)
        lib.push_run_state(REPO, branch, f"{target} succeeded")
        return True

    # ── Failure path ────────────────────────────────────────────────────────

    # Check for configurable blockers in the latest run log
    runs_dir = REPO / "runs"
    logs = sorted(
        runs_dir.glob(f"{target}-*.log"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    blocker_patterns = oc.get("blocker_patterns", [])
    if logs and blocker_patterns:
        log_content = logs[0].read_text(errors="replace")
        blocker_msg = lib.check_hardware_blocker(log_content, blocker_patterns)
        if blocker_msg:
            lib.log_fail(f"Blocker detected in {logs[0].name}: {blocker_msg}")
            run = lib.load_run_state(REPO)
            run["status"] = "needs_human"
            run["human_action"] = (
                f"{blocker_msg} — fix manually then run: make loop-reset"
            )
            lib.save_run_state(REPO, run)
            lib.push_run_state(REPO, branch, f"{target} — blocker — needs human")
            return False

    run, permanently_failed = lib.apply_target_failure(
        lib.load_run_state(REPO), target, max_att
    )
    lib.save_run_state(REPO, run)

    if permanently_failed:
        lib.log_fail(f"{target} hit max retries — marking permanently failed")
        lib.push_run_state(
            REPO, branch, f"{target} permanently failed after {max_att} retries"
        )
        return False

    lib.log_fail(f"{target} failed (attempt {attempt}/{max_att})")
    lib.push_run_state(REPO, branch, f"{target} failed — attempt {attempt}")

    # Signal OpenCode then wait before retrying
    oc = lib.load_oc_state(REPO)
    oc["fix_pushed"] = False
    oc["waiting_for_fix"] = True
    lib.save_oc_state(REPO, oc)
    lib.git(REPO, "add", "loop-state.json")
    r = lib.git(REPO, "diff", "--staged", "--quiet")
    if r.returncode != 0:
        lib.git(
            REPO,
            "commit",
            "-m",
            f"loop: waiting_for_fix=true — {target} attempt {attempt}",
        )
        lib.git(REPO, "push", "origin", branch)

    lib.push_run_state(
        REPO, branch, f"{target} failed — attempt {attempt} — waiting for OpenCode"
    )

    lib.log_wait("Waiting 60s before retry (OpenCode may be pushing a fix)...")
    time.sleep(60)
    lib.git_pull(REPO, branch)

    oc = lib.load_oc_state(REPO)
    oc["waiting_for_fix"] = False
    oc["fix_pushed"] = False
    lib.save_oc_state(REPO, oc)

    lib.log_ok(f"Retrying {target}")
    return False


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------


def initialise(branch: str) -> None:
    oc = lib.load_oc_state(REPO)
    existing = lib.load_run_state(REPO) if RUN_STATE_PATH.exists() else None
    run = lib.initialise_run_state(existing, oc)
    lib.save_run_state(REPO, run)
    lib.push_run_state(REPO, branch, "initialised loop")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main() -> None:
    lib.register_ours_driver(REPO)
    branch = lib.current_branch(REPO)

    lib.log(f"Starting loop — branch: {branch}")
    lib.git(REPO, "submodule", "update", "--init", "--recursive", "--quiet")

    oc = lib.load_oc_state(REPO)
    if not oc.get("targets"):
        lib.log_fail("loop-state.json has no targets — nothing to do")
        sys.exit(1)

    initialise(branch)
    lib.log("Loop initialised. Ready targets will run now.")

    while True:
        lib.git_pull(REPO, branch)
        run = lib.load_run_state(REPO)
        oc = lib.load_oc_state(REPO)

        # ── Stop / pause signals ────────────────────────────────────────────
        if lib.should_stop(oc):
            lib.log("Stop signal received — exiting")
            oc = lib.clear_signals(oc)
            lib.save_oc_state(REPO, oc)
            sys.exit(0)

        if lib.should_pause(oc):
            lib.log_wait("Pause signal received — waiting for resume...")
            time.sleep(FIX_POLL)
            continue

        # ── Needs-human pause ───────────────────────────────────────────────
        if run.get("status") == "needs_human":
            lib.log_wait(f"Loop paused — waiting for human: {run.get('human_action')}")
            time.sleep(FIX_POLL)
            lib.git_pull(REPO, branch)
            oc = lib.load_oc_state(REPO)
            if oc.get("fix_pushed"):
                run = lib.load_run_state(REPO)
                run["status"] = "running"
                run["human_action"] = None
                lib.save_run_state(REPO, run)
                oc["fix_pushed"] = False
                lib.save_oc_state(REPO, oc)
                lib.push_run_state(REPO, branch, "resuming after human ack")
                lib.log_ok("Resumed — human action acknowledged")
            continue

        # ── Build snapshot ───────────────────────────────────────────────────
        snapshot = lib.build_snapshot(run, oc)
        ready = snapshot["ready"]
        waiting = snapshot["waiting"]
        skipped = snapshot["skipped"]

        for t, reason in skipped:
            lib.log_skip(f"{t}: {reason}")
        for t, reason in waiting:
            lib.log_wait(f"{t} — {reason}")

        # ── All done? ────────────────────────────────────────────────────────
        if lib.all_targets_done(run, oc):
            failed_list = run.get("failed_targets", [])
            run["status"] = (
                "completed" if not failed_list else "completed_with_failures"
            )
            lib.save_run_state(REPO, run)
            if not failed_list:
                lib.log_ok("All targets completed successfully!")
            else:
                lib.log_ok("Loop complete with some failures/skips.")
                lib.log(f"Failed: {failed_list}")
                lib.log(f"Skipped: {[t for t, _ in skipped]}")
            lib.push_run_state(REPO, branch, "loop complete")
            sys.exit(0)

        if not ready:
            lib.log_wait("Nothing ready to run — waiting for dependencies...")
            time.sleep(FIX_POLL)
            continue

        # ── Run ready targets ────────────────────────────────────────────────
        for target in ready:
            run_target(target, branch)
            run = lib.load_run_state(REPO)
            if run.get("status") == "needs_human":
                lib.log_wait("Loop entered needs_human — stopping ready-target run")
                break


if __name__ == "__main__":
    main()
