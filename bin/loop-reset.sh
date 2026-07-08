#!/usr/bin/env bash
# loop-reset.sh — reset loop state so all targets re-run from scratch.

set -uo pipefail
REPO="$(dirname "$(realpath "$0")")/.."
cd "$REPO" || exit 1
BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)"

python3 - <<'PYEOF'
import json, sys

with open('loop-state.json') as f: ls = json.load(f)
ls.update({'fix_pushed': False, 'waiting_for_fix': False, 'opencode_last_fix': None, 'human_action': None})
with open('loop-state.json', 'w') as f: json.dump(ls, f, indent=2)

with open('loop-run-state.json') as f: r = json.load(f)
r.update({'status': 'running', 'completed_targets': [], 'failed_targets': [],
          'attempts': {}, 'last_result': None, 'last_run_log': None, 'human_action': None})
for k in ['current_target', 'current_index', 'current_attempt']:
    r.pop(k, None)
with open('loop-run-state.json', 'w') as f: json.dump(r, f, indent=2)
print('Loop state reset.')
PYEOF

git add loop-state.json loop-run-state.json
if git diff --staged --quiet; then
  echo "Nothing changed — already reset."
  exit 0
fi
git commit -m "loop: reset — all targets will re-run from scratch"
# Pull before push — the loop may have pushed state since we last pulled
git pull origin "$BRANCH" --rebase -X theirs --quiet 2>/dev/null || true
git push origin "$BRANCH" || {
  git pull origin "$BRANCH" --rebase -X theirs --quiet 2>/dev/null || true
  git push origin "$BRANCH"
}
echo "Done — loop will restart from the beginning on next run."
