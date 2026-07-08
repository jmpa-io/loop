#!/usr/bin/env python3
# loop-status.py — print current loop state to stdout.
import json, sys, os

path = "loop-run-state.json"
if not os.path.exists(path):
    print("loop-run-state.json not found — loop has not been started yet.")
    sys.exit(0)

with open(path) as f:
    r = json.load(f)

print(f"Status:    {r.get('status')}")
print(f"Last run:  {r.get('last_run_log')} ({r.get('last_result')})")
print(f"Completed: {r.get('completed_targets')}")
print(f"Failed:    {r.get('failed_targets')}")
print(f"Attempts:  {r.get('attempts')}")
ha = r.get("human_action")
if ha:
    print(f"NEEDS HUMAN: {ha}")
