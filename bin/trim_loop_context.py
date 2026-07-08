#!/usr/bin/env python3
"""
bin/trim_loop_context.py — cap loop-context.md at MAX_LINES lines.

If loop-context.md exceeds MAX_LINES, the oldest content is prepended to
docs/loop-context-archive.md and removed from loop-context.md.

Usage:
    python3 .loop/bin/trim_loop_context.py            # trim if needed
    python3 .loop/bin/trim_loop_context.py --dry-run  # preview only
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lib

REPO = lib.repo_root()
CONTEXT_FILE = REPO / "loop-context.md"
ARCHIVE_FILE = REPO / "docs" / "loop-context-archive.md"
MAX_LINES = 500


def main() -> None:
    dry_run = "--dry-run" in sys.argv

    if not CONTEXT_FILE.exists():
        print("loop-context.md not found — nothing to trim")
        sys.exit(0)

    lines = CONTEXT_FILE.read_text().splitlines()
    current = len(lines)

    if current <= MAX_LINES:
        print(
            f"loop-context.md is {current} lines — under limit ({MAX_LINES}), no trim needed"
        )
        sys.exit(0)

    archive_count = current - MAX_LINES
    print(
        f"loop-context.md is {current} lines — trimming to {MAX_LINES} (archiving first {archive_count} lines)"
    )

    if dry_run:
        print(f"[dry-run] Would archive lines 1–{archive_count} to {ARCHIVE_FILE}")
        print(
            f"[dry-run] Would keep lines {archive_count + 1}–{current} in loop-context.md"
        )
        sys.exit(0)

    to_archive = "\n".join(lines[:archive_count])
    to_keep = "\n".join(lines[archive_count:])

    date_str = datetime.now().strftime("%Y-%m-%d")
    header = f"# loop-context archive — entries trimmed on {date_str} (lines 1–{archive_count} from loop-context.md)"

    ARCHIVE_FILE.parent.mkdir(exist_ok=True)
    if ARCHIVE_FILE.exists():
        existing = ARCHIVE_FILE.read_text()
        ARCHIVE_FILE.write_text(f"{header}\n\n{to_archive}\n\n---\n\n{existing}")
    else:
        ARCHIVE_FILE.write_text(f"{header}\n\n{to_archive}")

    CONTEXT_FILE.write_text(to_keep)
    print(
        f"Done — loop-context.md trimmed to {len(to_keep.splitlines())} lines, archive at {ARCHIVE_FILE}"
    )


if __name__ == "__main__":
    main()
