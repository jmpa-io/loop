#!/usr/bin/env bash
# trim-loop-context.sh — cap loop-context.md at MAX_LINES lines.
#
# If loop-context.md exceeds MAX_LINES, the oldest content (everything before
# the most recent MAX_LINES lines) is prepended to docs/loop-context-archive.md
# and removed from loop-context.md.
#
# This keeps the file small enough for the OpenCode agent to read in full on
# every invocation without burning excessive context tokens.
#
# Usage:
#   bash bin/trim-loop-context.sh           # trims if needed, silent if not
#   bash bin/trim-loop-context.sh --dry-run # shows what would be trimmed

set -uo pipefail

REPO_DIR="$(dirname "$(realpath "$0")")/../.."
REPO_DIR="$(realpath "$REPO_DIR")"
CONTEXT_FILE="$REPO_DIR/loop-context.md"
ARCHIVE_FILE="$REPO_DIR/docs/loop-context-archive.md"
MAX_LINES=500
DRY_RUN=false

[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

if [[ ! -f "$CONTEXT_FILE" ]]; then
  echo "loop-context.md not found — nothing to trim"
  exit 0
fi

current_lines=$(wc -l <"$CONTEXT_FILE")

if [[ "$current_lines" -le "$MAX_LINES" ]]; then
  echo "loop-context.md is $current_lines lines — under limit ($MAX_LINES), no trim needed"
  exit 0
fi

# How many lines to archive (everything except the last MAX_LINES lines)
archive_lines=$((current_lines - MAX_LINES))

echo "loop-context.md is $current_lines lines — trimming to $MAX_LINES (archiving first $archive_lines lines)"

if $DRY_RUN; then
  echo "[dry-run] Would archive lines 1–$archive_lines to $ARCHIVE_FILE"
  echo "[dry-run] Would keep lines $((archive_lines + 1))–$current_lines in loop-context.md"
  exit 0
fi

# Extract the lines to archive
to_archive=$(head -n "$archive_lines" "$CONTEXT_FILE")

# Prepend to archive file (newest at bottom, oldest at top — chronological)
archive_header="# loop-context archive — entries trimmed on $(date '+%Y-%m-%d') (lines 1–$archive_lines from loop-context.md)"
if [[ -f "$ARCHIVE_FILE" ]]; then
  # Prepend: new archived block goes ABOVE existing archive content
  tmp=$(mktemp)
  {
    echo "$archive_header"
    echo ""
    echo "$to_archive"
    echo ""
    echo "---"
    echo ""
    cat "$ARCHIVE_FILE"
  } >"$tmp"
  mv "$tmp" "$ARCHIVE_FILE"
else
  mkdir -p "$(dirname "$ARCHIVE_FILE")"
  {
    echo "$archive_header"
    echo ""
    echo "$to_archive"
  } >"$ARCHIVE_FILE"
fi

# Keep only the last MAX_LINES lines in loop-context.md
tmp=$(mktemp)
tail -n "$MAX_LINES" "$CONTEXT_FILE" >"$tmp"
mv "$tmp" "$CONTEXT_FILE"

echo "Done — loop-context.md trimmed to $(wc -l <"$CONTEXT_FILE") lines, archive at $ARCHIVE_FILE"
