#!/usr/bin/env python3
"""
bin/loop_resilient.py — crash-resilient wrapper around loop.py.

Keeps loop.py running even if it crashes. Pulls latest code before each
restart. All output is tee'd to runs/resilient.log so the Mac can read it
via git without needing terminal access.

No hardcoded pre-flight checks — the consuming repo is responsible for
ensuring credentials and tooling are valid before calling loop-start.
"""

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lib

REPO = lib.repo_root()
RESTART_DELAY = 10  # seconds between restarts
LOG_FILE = REPO / "runs" / "resilient.log"
LOOP_SCRIPT = Path(__file__).resolve().parent / "loop.py"


def push_log(branch: str, message: str) -> None:
    lib.git(REPO, "add", "runs/resilient.log")
    r = lib.git(REPO, "diff", "--staged", "--quiet")
    if r.returncode == 0:
        return
    lib.git(REPO, "commit", "-m", f"loop: resilient — {message}")
    r = lib.git(REPO, "push", "origin", branch)
    if r.returncode != 0:
        lib.git(REPO, "pull", "origin", branch, "--rebase", "-X", "theirs", "--quiet")
        lib.git(REPO, "push", "origin", branch)


def main() -> None:
    lib.register_ours_driver(REPO)
    branch = lib.current_branch(REPO)

    (REPO / "runs").mkdir(exist_ok=True)

    lib.log("Starting resilient loop")
    lib.log("Auto-restarts on crash. Ctrl+C to stop.")
    lib.log(f"Log file: {LOG_FILE}")

    with open(LOG_FILE, "a") as log_fh:

        def tee(msg: str) -> None:
            print(msg, flush=True)
            log_fh.write(msg + "\n")
            log_fh.flush()

        while True:
            tee(f"{lib._ts()} [resilient] Pulling latest code...")
            lib.git(
                REPO, "pull", "origin", branch, "--rebase", "--autostash", "--quiet"
            )
            lib.git(REPO, "submodule", "update", "--init", "--recursive", "--quiet")
            push_log(branch, "starting loop")

            tee(f"{lib._ts()} [resilient] Starting loop...")
            result = subprocess.run([sys.executable, str(LOOP_SCRIPT)], cwd=str(REPO))
            exit_code = result.returncode

            tee(f"{lib._ts()} [resilient] loop exited (code: {exit_code})")
            push_log(branch, f"loop exited (code: {exit_code})")

            if exit_code == 0:
                tee(f"{lib._ts()} [resilient] All targets completed — stopping")
                push_log(branch, "all targets completed")
                sys.exit(0)

            tee(
                f"{lib._ts()} [resilient] Restarting in {RESTART_DELAY}s... (Ctrl+C to stop)"
            )
            time.sleep(RESTART_DELAY)


if __name__ == "__main__":
    main()
