#!/usr/bin/env python3
"""
bin/loop_status.py — print current loop state to stdout.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lib

REPO = lib.repo_root()


def main() -> None:
    path = REPO / "loop-run-state.json"
    if not path.exists():
        print("loop-run-state.json not found — loop has not been started yet.")
        sys.exit(0)

    r = lib.load_run_state(REPO)
    print(f"Status:    {r.get('status')}")
    print(f"Last run:  {r.get('last_run_log')} ({r.get('last_result')})")
    print(f"Completed: {r.get('completed_targets')}")
    print(f"Failed:    {r.get('failed_targets')}")
    print(f"Attempts:  {r.get('attempts')}")
    ha = r.get("human_action")
    if ha:
        print(f"NEEDS HUMAN: {ha}")


if __name__ == "__main__":
    main()
