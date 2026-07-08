#!/usr/bin/env bash
# loop-ack.sh — run on the Mac to acknowledge a human action and resume the loop.
# Handles git conflicts automatically by retrying until the push succeeds.

set -uo pipefail

REPO_DIR="$(dirname "$(realpath "$0")")/../.."
REPO_DIR="$(realpath "$REPO_DIR")"
BRANCH="$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD)"

apply_ack() {
  python3 -c "
import json
with open('$REPO_DIR/loop-state.json') as f: s=json.load(f)
s['fix_pushed']=True
s['waiting_for_fix']=False
with open('$REPO_DIR/loop-state.json','w') as f: json.dump(s,f,indent=2)
with open('$REPO_DIR/loop-run-state.json') as f: r=json.load(f)
r['human_action']=None
r['status']='running'
with open('$REPO_DIR/loop-run-state.json','w') as f: json.dump(r,f,indent=2)
"
}

cd "$REPO_DIR" || exit 1

for attempt in 1 2 3 4 5; do
  # Pull latest so we're not behind
  git pull origin "$BRANCH" --rebase --quiet 2>/dev/null || true

  # Apply ack values (always re-apply after pull so they survive rebase)
  apply_ack

  git add loop-state.json loop-run-state.json
  git diff --staged --quiet && {
    echo "Already acked — nothing to commit"
    exit 0
  }
  git commit -m "loop: human ack — resuming" 2>/dev/null || true

  if git push origin "$BRANCH" 2>/dev/null; then
    echo "Ack sent — loop will resume"
    exit 0
  fi

  echo "Push conflict — retrying ($attempt/5)..."
  sleep 2
done

echo "Failed to push ack after 5 attempts — run: make loop-ack again"
exit 1
