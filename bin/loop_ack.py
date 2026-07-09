#!/usr/bin/env python3
"""
bin/loop_ack.py — acknowledge a human action and resume the loop.

Sets fix_pushed=true in receiver-state.json and clears human_action in
sender-state.json, then commits and pushes so the sender picks it up.
Retries up to 5 times on push conflict.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lib

REPO = lib.repo_root()


def apply_ack() -> None:
    rc = lib.load_receiver_state(REPO)
    rc["fix_pushed"] = True
    lib.save_receiver_state(REPO, rc)

    sender = lib.load_sender_state(REPO)
    sender["human_action"] = None
    sender["status"] = "running"
    lib.save_sender_state(REPO, sender)


def main() -> None:
    branch = lib.current_branch(REPO)

    for attempt in range(1, 6):
        lib.git(REPO, "pull", "origin", branch, "--rebase", "--quiet")
        apply_ack()

        lib.git(REPO, "add", "receiver-state.json", "sender-state.json")
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
