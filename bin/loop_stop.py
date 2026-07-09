#!/usr/bin/env python3
"""
bin/loop_stop.py — signal both sides of the loop to stop immediately.

Kills the local tmux session (if any), sets stop=true in loop-state.json,
then commits and pushes so the remote machine exits on its next git pull.
Retries up to 5 times on push conflict.
"""

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lib

REPO = lib.repo_root()
SESSION = "homelab-loop"


def kill_local_tmux() -> None:
    r = subprocess.run(
        ["tmux", "kill-session", "-t", SESSION],
        capture_output=True,
    )
    if r.returncode == 0:
        print(f"Killed local tmux session '{SESSION}'")
    else:
        print(f"No local tmux session '{SESSION}' running")


def main() -> None:
    kill_local_tmux()

    branch = lib.current_branch(REPO)

    for attempt in range(1, 6):
        lib.git(REPO, "pull", "origin", branch, "--rebase", "--quiet")

        oc = lib.load_receiver_state(REPO)
        oc = lib.apply_stop_signal(oc)
        lib.save_receiver_state(REPO, oc)

        lib.git(REPO, "add", "receiver-state.json")
        r = lib.git(REPO, "diff", "--staged", "--quiet")
        if r.returncode == 0:
            print("Stop signal already set — nothing to commit")
            sys.exit(0)

        lib.git(REPO, "commit", "-m", "loop: stop signal set")
        r = lib.git(REPO, "push", "origin", branch)
        if r.returncode == 0:
            print("Stop signal pushed — remote loop will exit on next pull")
            sys.exit(0)

        print(f"Push conflict — retrying ({attempt}/5)...")
        time.sleep(2)

    print("Failed to push stop signal after 5 attempts")
    sys.exit(1)


if __name__ == "__main__":
    main()
