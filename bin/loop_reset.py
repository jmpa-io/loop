#!/usr/bin/env python3
"""
bin/loop_reset.py -- reset loop state so all targets re-run from scratch.

Clears completed_targets, failed_targets, attempts, and fix signals in both
state files, then commits and pushes.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lib

REPO = lib.repo_root()


def main() -> None:
    branch = lib.current_branch(REPO)

    rc = lib.load_receiver_state(REPO)
    rc.update(
        {
            "fix_pushed": False,
            "last_fix": None,
            "human_action": None,
            "stop": False,
            "pause": False,
        }
    )
    lib.save_receiver_state(REPO, rc)

    sender = lib.load_sender_state(REPO)
    sender.update(
        {
            "status": "running",
            "completed_targets": [],
            "failed_targets": [],
            "attempts": {},
            "last_result": None,
            "last_run_log": None,
            "human_action": None,
        }
    )
    for k in ("current_target", "current_index", "current_attempt"):
        sender.pop(k, None)
    lib.save_sender_state(REPO, sender)

    print("Loop state reset.")

    lib.git(REPO, "add", "receiver-state.json", "sender-state.json")
    r = lib.git(REPO, "diff", "--staged", "--quiet")
    if r.returncode == 0:
        print("Nothing changed -- already reset.")
        sys.exit(0)

    lib.git(REPO, "commit", "-m", "loop: reset -- all targets will re-run from scratch")
    lib.git(REPO, "pull", "origin", branch, "--rebase", "-X", "theirs", "--quiet")
    r = lib.git(REPO, "push", "origin", branch)
    if r.returncode != 0:
        lib.git(REPO, "pull", "origin", branch, "--rebase", "-X", "theirs", "--quiet")
        lib.git(REPO, "push", "origin", branch)

    print("Done -- loop will restart from the beginning on next run.")


if __name__ == "__main__":
    main()
