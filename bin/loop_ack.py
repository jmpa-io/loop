#!/usr/bin/env python3
"""
bin/loop_ack.py — acknowledge a human action and resume the loop.

Sets fix_pushed=true in loop-state.json and clears human_action in
loop-run-state.json, then commits and pushes so the runner picks it up.
Retries up to 5 times on push conflict.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lib

REPO = lib.repo_root()


def apply_ack() -> None:
    oc = lib.load_oc_state(REPO)
    oc["fix_pushed"] = True
    oc["waiting_for_fix"] = False
    lib.save_oc_state(REPO, oc)

    run = lib.load_run_state(REPO)
    run["human_action"] = None
    run["status"] = "running"
    lib.save_run_state(REPO, run)


def main() -> None:
    branch = lib.current_branch(REPO)

    for attempt in range(1, 6):
        lib.git(REPO, "pull", "origin", branch, "--rebase", "--quiet")
        apply_ack()

        lib.git(REPO, "add", "loop-state.json", "loop-run-state.json")
        r = lib.git(REPO, "diff", "--staged", "--quiet")
        if r.returncode == 0:
            print("Already acked — nothing to commit")
            sys.exit(0)

        lib.git(REPO, "commit", "-m", "loop: human ack — resuming")
        r = lib.git(REPO, "push", "origin", branch)
        if r.returncode == 0:
            print("Ack sent — loop will resume")
            sys.exit(0)

        print(f"Push conflict — retrying ({attempt}/5)...")
        time.sleep(2)

    print("Failed to push ack after 5 attempts — run: make loop-reset")
    sys.exit(1)


if __name__ == "__main__":
    main()
