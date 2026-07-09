#!/usr/bin/env python3
"""
bin/sender.py — dependency-aware deployment loop (sender side).

Reads targets and their dependency graph from receiver-state.json.
A target runs only when ALL its dependencies have succeeded.
If a dependency permanently failed (max retries), the target is SKIPPED
so independent targets can still run.

File ownership:
    sender-state.json   — this script writes it (sender)
    receiver-state.json — this script reads it only (receiver owns it)
"""

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lib

REPO = lib.repo_root()
SENDER_STATE_PATH = REPO / "sender-state.json"
RECEIVER_STATE_PATH = REPO / "receiver-state.json"
FIX_POLL = int(os.environ.get("LOOP_POLL_INTERVAL", "10"))  # seconds between polls


# ---------------------------------------------------------------------------
# Run a single target
# ---------------------------------------------------------------------------


def run_target(target: str, branch: str) -> bool:
    """
    Run `make <target>` once. Returns True on success, False on failure.
    Handles retry signalling, blocker detection, and state updates.
    """
    sender = lib.load_sender_state(REPO)
    receiver = lib.load_receiver_state(REPO)

    # Idempotency guard — never re-run a completed target
    if lib.is_already_completed(target, sender):
        lib.log_ok(f"{target} — already completed, skipping")
        return True

    max_att = sender.get("max_attempts", receiver.get("max_attempts", 10))
    attempt = sender.get("attempts", {}).get(target, 1)
    lib.log(f"▶ {target} (attempt {attempt}/{max_att})")

    result = subprocess.run(["make", target], cwd=str(REPO))

    if result.returncode == 0:
        lib.log_ok(f"{target} succeeded")
        sender = lib.apply_target_success(lib.load_sender_state(REPO), target)
        lib.save_sender_state(REPO, sender)
        lib.push_sender_state(REPO, branch, f"{target} succeeded")
        return True

    # ── Failure path ────────────────────────────────────────────────────────

    # Check for configurable blockers in the latest run log
    runs_dir = REPO / "runs"
    logs = sorted(
        runs_dir.glob(f"{target}-*.log"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    blocker_patterns = receiver.get("blocker_patterns", [])
    if logs and blocker_patterns:
        log_content = logs[0].read_text(errors="replace")
        blocker_msg = lib.check_hardware_blocker(log_content, blocker_patterns)
        if blocker_msg:
            lib.log_fail(f"Blocker detected in {logs[0].name}: {blocker_msg}")
            sender = lib.load_sender_state(REPO)
            sender["status"] = "needs_human"
            sender["human_action"] = (
                f"{blocker_msg} — fix manually then run: make loop-reset"
            )
            lib.save_sender_state(REPO, sender)
            lib.push_sender_state(REPO, branch, f"{target} — blocker — needs human")
            return False

    sender, permanently_failed = lib.apply_target_failure(
        lib.load_sender_state(REPO), target, max_att
    )
    lib.save_sender_state(REPO, sender)

    if permanently_failed:
        lib.log_fail(f"{target} hit max retries — marking permanently failed")
        lib.push_sender_state(
            REPO, branch, f"{target} permanently failed after {max_att} retries"
        )
        return False

    lib.log_fail(f"{target} failed (attempt {attempt}/{max_att})")
    lib.push_sender_state(REPO, branch, f"{target} failed — attempt {attempt}")

    # Wait for receiver to push a fix, polling git
    lib.log_wait(f"Waiting for receiver to push a fix for {target}...")
    for _ in range(18):  # poll for up to 3 minutes (18 x 10s)
        time.sleep(FIX_POLL)
        lib.git_pull(REPO, branch)
        receiver = lib.load_receiver_state(REPO)
        if receiver.get("fix_pushed"):
            lib.log_ok(f"Receiver pushed a fix for {target} — retrying")
            return False
        if lib.should_stop(receiver):
            lib.log("Stop signal received while waiting for fix — exiting")
            sys.exit(0)

    lib.log_wait(f"No fix received after 3 minutes — retrying {target} anyway")
    return False


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------


def initialise(branch: str) -> None:
    receiver = lib.load_receiver_state(REPO)
    existing = lib.load_sender_state(REPO) if SENDER_STATE_PATH.exists() else None
    sender = lib.initialise_sender_state(existing, receiver)
    lib.save_sender_state(REPO, sender)
    lib.push_sender_state(REPO, branch, "initialised loop")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main() -> None:
    lib.register_ours_driver(REPO)
    branch = lib.current_branch(REPO)

    lib.log(f"Starting sender loop — branch: {branch}")
    lib.git(REPO, "submodule", "update", "--init", "--recursive", "--quiet")

    receiver = lib.load_receiver_state(REPO)
    if not receiver.get("targets"):
        lib.log_fail("receiver-state.json has no targets — nothing to do")
        sys.exit(1)

    initialise(branch)
    lib.log("Loop initialised. Ready targets will run now.")

    while True:
        lib.git_pull(REPO, branch)
        sender = lib.load_sender_state(REPO)
        receiver = lib.load_receiver_state(REPO)

        # ── Stop / pause signals ────────────────────────────────────────────
        if lib.should_stop(receiver):
            lib.log("Stop signal received — exiting")
            sys.exit(0)

        if lib.should_pause(receiver):
            lib.log_wait("Pause signal received — waiting for resume...")
            time.sleep(FIX_POLL)
            continue

        # ── Needs-human pause ───────────────────────────────────────────────
        if sender.get("status") == "needs_human":
            lib.log_wait(
                f"Loop paused — waiting for human: {sender.get('human_action')}"
            )
            time.sleep(FIX_POLL)
            lib.git_pull(REPO, branch)
            receiver = lib.load_receiver_state(REPO)
            if receiver.get("fix_pushed"):
                sender = lib.load_sender_state(REPO)
                sender["status"] = "running"
                sender["human_action"] = None
                lib.save_sender_state(REPO, sender)
                lib.push_sender_state(REPO, branch, "resuming after human ack")
                lib.log_ok("Resumed — human action acknowledged")
            continue

        # ── Build snapshot ───────────────────────────────────────────────────
        snapshot = lib.build_snapshot(sender, receiver)
        ready = snapshot["ready"]
        waiting = snapshot["waiting"]
        skipped = snapshot["skipped"]

        for t, reason in skipped:
            lib.log_skip(f"{t}: {reason}")
        for t, reason in waiting:
            lib.log_wait(f"{t} — {reason}")

        # ── All done? ────────────────────────────────────────────────────────
        if lib.all_targets_done(sender, receiver):
            failed_list = sender.get("failed_targets", [])
            sender["status"] = (
                "completed" if not failed_list else "completed_with_failures"
            )
            lib.save_sender_state(REPO, sender)
            if not failed_list:
                lib.log_ok("All targets completed successfully!")
            else:
                lib.log_ok("Loop complete with some failures/skips.")
                lib.log(f"Failed: {failed_list}")
                lib.log(f"Skipped: {[t for t, _ in skipped]}")
            lib.push_sender_state(REPO, branch, "loop complete")
            sys.exit(0)

        if not ready:
            lib.log_wait("Nothing ready to run — waiting for dependencies...")
            time.sleep(FIX_POLL)
            continue

        # ── Run ready targets ────────────────────────────────────────────────
        for target in ready:
            run_target(target, branch)
            sender = lib.load_sender_state(REPO)
            if sender.get("status") == "needs_human":
                lib.log_wait("Loop entered needs_human — stopping ready-target run")
                break


if __name__ == "__main__":
    main()
