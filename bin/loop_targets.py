#!/usr/bin/env python3
"""
bin/loop_targets.py -- update the targets list in receiver-state.json.

Usage:
    python3 .loop/bin/loop_targets.py build test deploy
    python3 .loop/bin/loop_targets.py "build test deploy"   # space-separated string also works

Overwrites the targets list in receiver-state.json while preserving all
other fields (deps, max_attempts, blocker_patterns, etc).

If receiver-state.json does not exist, creates it from the template
at .loop/receiver-state.json.

Exits 1 if no targets are provided.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lib

REPO = lib.repo_root()
TEMPLATE = Path(__file__).resolve().parent.parent / "receiver-state.json"


def parse_targets(args: list[str]) -> list[str]:
    """
    Accept targets as either:
      - multiple args:       build test deploy
      - a single space-sep:  "build test deploy"
      - a single comma-sep:  "build,test,deploy"
    Returns a deduplicated list preserving order.
    """
    raw = []
    for a in args:
        # split on comma or whitespace
        import re
        raw.extend(re.split(r"[,\s]+", a.strip()))
    targets = [t for t in raw if t]
    # deduplicate preserving order
    seen = set()
    result = []
    for t in targets:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result


def load_or_create_receiver_state(repo: Path) -> dict:
    rc_path = repo / "receiver-state.json"
    if rc_path.exists():
        return lib.load_receiver_state(repo)
    # Bootstrap from template
    if TEMPLATE.exists():
        import json
        return json.loads(TEMPLATE.read_text())
    # Bare minimum
    return {
        "fix_pushed": False,
        "last_fix": None,
        "targets": [],
        "deps": {},
        "max_attempts": 10,
        "blocker_patterns": [],
        "stop": False,
        "pause": False,
    }


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print("Usage: loop_targets.py <target> [<target> ...]")
        print("Example: loop_targets.py build test deploy")
        sys.exit(1)

    targets = parse_targets(args)
    if not targets:
        print("Error: no targets provided")
        sys.exit(1)

    rc = load_or_create_receiver_state(REPO)
    old_targets = rc.get("targets", [])
    rc["targets"] = targets
    lib.save_receiver_state(REPO, rc)

    if old_targets != targets:
        print(f"Targets updated: {old_targets} -> {targets}")
    else:
        print(f"Targets unchanged: {targets}")


if __name__ == "__main__":
    main()
