#!/usr/bin/env bash
# homelab-loop.sh — dependency-aware deployment loop.
#
# Targets and their dependency graph live in loop-state.json under 'deps'.
# A target runs only when ALL its dependencies have succeeded.
# If a dependency permanently failed (max retries), the target is SKIPPED
# (not blocked) — so independent targets like deploy-obs-stack and deploy-pbs
# can still run even when deploy-k3s is broken.
#
# Example dependency graph (defined in loop-state.json):
#   proxmox-host-preflight → (none)
#   nas-preflight          → (none)
#   deploy-k3s             → proxmox-host-preflight
#   infra-report           → (none)
#   deploy-k3s-apps        → deploy-k3s
#   deploy-nfs             → deploy-k3s
#   deploy-obs-stack       → (none)
#   deploy-pbs             → (none)
#   deploy-dns             → deploy-k3s-apps
#   k3s-report             → deploy-k3s
#
# Ownership:
#   loop-run-state.json — written/pushed by this script (homelab laptop)
#   loop-state.json     — written/pushed by OpenCode (Mac)

set -uo pipefail
# Note: -e (errexit) is intentionally NOT set — the loop swallows many errors
# with '|| true' to keep running even when individual targets fail.
# Critical state-file reads have explicit error guards below.

REPO_DIR="$(dirname "$(realpath "$0")")/.."
REPO_DIR="$(realpath "$REPO_DIR")"
RUN_STATE="$REPO_DIR/loop-run-state.json"
OC_STATE="$REPO_DIR/loop-state.json"
BRANCH="$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD)"
FIX_POLL_INTERVAL=10

log() { echo "$(date '+%H:%M:%S') [loop] $*"; }
log_ok() { echo "$(date '+%H:%M:%S') [loop] ✓ $*"; }
log_fail() { echo "$(date '+%H:%M:%S') [loop] ✗ $*"; }
log_skip() { echo "$(date '+%H:%M:%S') [loop] ⊘ $*"; }
log_wait() { echo "$(date '+%H:%M:%S') [loop] ⏳ $*"; }

# ── Helpers ────────────────────────────────────────────────────────────────────

get_run() {
  python3 -c "
import json
with open('$RUN_STATE') as f: s=json.load(f)
v=s.get('$1')
print('' if v is None else str(v).lower() if isinstance(v,bool) else str(v))
" 2>/dev/null || echo ""
}

get_oc() {
  python3 -c "
import json
with open('$OC_STATE') as f: s=json.load(f)
v=s.get('$1')
print('' if v is None else str(v).lower() if isinstance(v,bool) else str(v))
" 2>/dev/null || echo ""
}

push_run_state() {
  cd "$REPO_DIR" || return
  git add loop-run-state.json runs/ 2>/dev/null || true
  git diff --staged --quiet && return 0
  git commit -m "loop: $1" 2>/dev/null || true
  # Pull --rebase before push — prevents conflicts when both sides push to main.
  # -X theirs: if loop-run-state.json conflicts, keep our (laptop) version.
  git pull origin "$BRANCH" --rebase -X theirs --quiet 2>/dev/null || true
  # Retry push once after rebase in case of a race condition
  git push origin "$BRANCH" 2>/dev/null || {
    git pull origin "$BRANCH" --rebase -X theirs --quiet 2>/dev/null || true
    git push origin "$BRANCH" 2>/dev/null || true
  }
}

git_pull() {
  cd "$REPO_DIR" || return
  # Preserve loop-run-state.json across the pull.
  #
  # Root cause of the deploy-uptime-kuma false-failure:
  # --autostash stashes loop-run-state.json, then the stash pop merges it back.
  # .gitattributes has "merge=ours" for this file, but during stash pop "our side"
  # is the JUST-PULLED remote version, not the stash. So the stash is discarded,
  # and the pulled (potentially older) remote version replaces our local state.
  # This can silently remove completed_targets entries, causing targets to re-run.
  #
  # Fix: copy loop-run-state.json to a tmp file before pulling. After the pull,
  # merge the two versions by taking the UNION of completed_targets (never shrink it).
  local _rstate_tmp
  _rstate_tmp="$(mktemp /tmp/loop-run-state-backup.XXXXXX.json)"
  cp "$RUN_STATE" "$_rstate_tmp" 2>/dev/null || true

  git pull origin "$BRANCH" --rebase --autostash -X theirs --quiet 2>/dev/null || true

  # After the pull: restore any completed_targets that were lost.
  # This is safe — if a target was in completed_targets it really did complete.
  if [[ -s "$_rstate_tmp" ]]; then
    python3 -c "
import json, sys
try:
    with open('$RUN_STATE') as f: current = json.load(f)
    with open('$_rstate_tmp') as f: backup = json.load(f)
    # Union of completed_targets — never lose a completed target due to a pull
    current_completed = set(current.get('completed_targets', []))
    backup_completed  = set(backup.get('completed_targets', []))
    merged_completed = list(backup_completed | current_completed)
    if merged_completed != current.get('completed_targets', []):
        current['completed_targets'] = merged_completed
    # Clean failed_targets — remove anything that is now in completed (local or remote)
    # This prevents stale failed entries from persisting after a successful run
    all_completed = backup_completed | current_completed
    current_failed = set(current.get('failed_targets', []))
    backup_failed  = set(backup.get('failed_targets', []))
    # Take intersection of both failed sets, minus anything completed
    merged_failed = list((current_failed & backup_failed) - all_completed)
    if merged_failed != current.get('failed_targets', []):
        current['failed_targets'] = merged_failed
    with open('$RUN_STATE', 'w') as f: json.dump(current, f, indent=2)
except Exception:
    pass
" 2>/dev/null
  fi
  rm -f "$_rstate_tmp" 2>/dev/null || true
}

check_aws() {
  # Always use jmpa profile — never inherit $AWS_PROFILE from the environment.
  # The shell may have AWS_PROFILE=privas-dev-admin or similar set.
  aws --profile jmpa sts get-caller-identity --region ap-southeast-2 &>/dev/null
}

# Try to auto-refresh AWS SSO credentials without user interaction.
# Works if the SSO session cache exists and just needs a token refresh.
# Returns 0 if credentials are now valid, 1 if manual login still needed.
try_refresh_aws() {
  # Always use jmpa profile
  if timeout 30 aws sso login --profile jmpa --no-browser &>/dev/null 2>&1; then
    check_aws && return 0
  fi
  if check_aws; then return 0; fi
  return 1
}

# ── AWS credential wait ────────────────────────────────────────────────────────

wait_for_aws() {
  # First: try auto-refresh (works if SSO cache exists, no browser needed)
  log_wait "AWS credentials expired — attempting auto-refresh..."
  if try_refresh_aws; then
    log_ok "AWS credentials auto-refreshed successfully"
    return 0
  fi

  # Auto-refresh failed — needs human
  log_fail "AWS credentials expired — auto-refresh failed. Run: aws sso login then make loop-ack"
  python3 -c "
import json
with open('$RUN_STATE') as f: r=json.load(f)
r['status']='needs_human'
r['human_action']='AWS SSO credentials expired. Run: aws sso login — then run: make loop-ack'
with open('$RUN_STATE','w') as f: json.dump(r,f,indent=2)
" 2>/dev/null
  push_run_state "blocked — AWS credentials expired — needs human"
  while true; do
    sleep $FIX_POLL_INTERVAL
    git_pull
    if [[ "$(get_oc 'fix_pushed')" == "true" ]]; then
      if check_aws; then
        log_ok "AWS credentials valid — resuming"
        python3 -c "
import json
with open('$RUN_STATE') as f: r=json.load(f)
r['status']='running'
r['human_action']=None
with open('$RUN_STATE','w') as f: json.dump(r,f,indent=2)
with open('$OC_STATE') as f: s=json.load(f)
s['fix_pushed']=False
with open('$OC_STATE','w') as f: json.dump(s,f,indent=2)
" 2>/dev/null
        push_run_state "resuming — AWS credentials refreshed"
        return 0
      else
        log_fail "AWS still invalid — waiting again"
        python3 -c "
import json
with open('$OC_STATE') as f: s=json.load(f)
s['fix_pushed']=False
with open('$OC_STATE','w') as f: json.dump(s,f,indent=2)
" 2>/dev/null
      fi
    fi
    log_wait "Waiting for AWS credentials refresh..."
  done
}

# ── Dependency resolution ──────────────────────────────────────────────────────
# Returns:
#   "ready"   — all deps completed successfully
#   "blocked" — at least one dep has permanently failed (max retries hit)
#   "waiting" — deps exist but haven't completed or failed yet

dep_status() {
  local target="$1"
  python3 - <<PYEOF
import json, sys
with open('$RUN_STATE') as f: r=json.load(f)
with open('$OC_STATE') as f: s=json.load(f)
deps = s.get('deps', {}).get('$target', [])
completed = set(r.get('completed_targets', []))
failed    = set(r.get('failed_targets', []))
if any(d in failed for d in deps):
    print('blocked')
elif all(d in completed for d in deps):
    print('ready')
else:
    print('waiting')
PYEOF
}

# ── Run a single target ────────────────────────────────────────────────────────

run_target() {
  local target="$1"

  # Guard: skip targets already in completed_targets.
  # The snapshot normally excludes completed targets from ready, but a git pull
  # mid-loop can briefly surface a stale loop-run-state.json where a completed
  # target is missing. This backstop prevents a false-failure re-run.
  local already_done
  already_done="$(python3 -c "
import json
with open('$RUN_STATE') as f: r=json.load(f)
print('true' if '$target' in r.get('completed_targets', []) else 'false')
" 2>/dev/null || echo "false")"
  if [[ "$already_done" == "true" ]]; then
    log_ok "$target — already in completed_targets, skipping (idempotent guard)"
    return 0
  fi

  local max
  max="$(get_run 'max_attempts')"
  max="${max:-200}" # default 200 if state file missing/corrupted
  local attempt
  attempt="$(python3 -c "
import json
with open('$RUN_STATE') as f: r=json.load(f)
print(r.get('attempts', {}).get('$target', 1))
" 2>/dev/null || echo 1)"

  log "▶ $target (attempt $attempt/$max)"

  # Clear any stale fix_pushed signal before running.
  # If fix_pushed=true is left over from a previous cycle, wait_for_fix() would
  # instantly return on the next failure without OpenCode ever seeing the new log.
  python3 -c "
import json
with open('$OC_STATE') as f: s=json.load(f)
if s.get('fix_pushed'):
    s['fix_pushed']=False
    with open('$OC_STATE','w') as f: json.dump(s,f,indent=2)
" 2>/dev/null

  # Per-run AWS check
  if ! check_aws; then
    wait_for_aws
  fi

  if make "$target"; then
    log_ok "$target succeeded"
    python3 -c "
import json
with open('$RUN_STATE') as f: r=json.load(f)
r.setdefault('completed_targets', [])
if '$target' not in r['completed_targets']:
    r['completed_targets'].append('$target')
r.setdefault('failed_targets', [])
if '$target' in r.get('failed_targets', []):
    r['failed_targets'].remove('$target')
r.setdefault('attempts', {})
r['attempts']['$target'] = 1
r['last_result'] = 'success'
r['last_run_log'] = '$target'
with open('$RUN_STATE','w') as f: json.dump(r,f,indent=2)
" 2>/dev/null
    push_run_state "$target succeeded"
    return 0

  else
    # Check for AWS expiry — escalate immediately, do NOT burn retries
    local latest_log
    # shellcheck disable=SC2012
    latest_log="$(ls -1t "$REPO_DIR/runs/${target}-"*.log 2>/dev/null | head -1 | xargs basename 2>/dev/null)"
    if [[ -n "$latest_log" ]] && grep -q "LOOP_SIGNAL: AWS_EXPIRED" "$REPO_DIR/runs/$latest_log" 2>/dev/null; then
      wait_for_aws
      return 1
    fi

    # Check for hardware-only blockers — retrying will never fix these
    if [[ -n "$latest_log" ]]; then
      local hw_blocker=""
      local log_path="$REPO_DIR/runs/$latest_log"
      if grep -qE "No route to host.*192\.168\.1\.2|NAS.*unreachable|Connection refused.*192\.168\.1\.2" "$log_path" 2>/dev/null; then
        hw_blocker="NAS (192.168.1.2) is unreachable — check if it is powered on and on the network"
      elif grep -qE "No space left on device" "$log_path" 2>/dev/null; then
        hw_blocker="Disk full on a Proxmox host — free up space manually"
      elif grep -qE "USB.*disconnect|usbcore.*disconnect" "$log_path" 2>/dev/null; then
        hw_blocker="USB WiFi adapter disconnected on server-3 — re-plug or reboot required"
      elif grep -qE "Hardware Error|Machine Check Exception" "$log_path" 2>/dev/null; then
        hw_blocker="Hardware error on a Proxmox host — inspect server hardware"
      elif grep -qE "NEEDS_HUMAN: cert-manager ClusterIssuer cannot start" "$log_path" 2>/dev/null; then
        hw_blocker="cert-manager CA secret missing — run: make bootstrap-homelab-ca then: make loop-ack"
      fi
      if [[ -n "$hw_blocker" ]]; then
        log_fail "Hardware blocker in $latest_log — escalating to NEEDS_HUMAN"
        log_fail "$hw_blocker"
        python3 -c "
import json
with open('$RUN_STATE') as f: r=json.load(f)
r['status']='needs_human'
r['human_action']='Hardware blocker: $hw_blocker — fix manually then run: make loop-ack'
with open('$RUN_STATE','w') as f: json.dump(r,f,indent=2)
" 2>/dev/null
        push_run_state "$target — hardware blocker — needs human"
        return 1
      fi
    fi

    log_fail "$target failed (attempt $attempt/$max)"
    python3 -c "
import json
with open('$RUN_STATE') as f: r=json.load(f)
r.setdefault('attempts', {})
r['attempts']['$target'] = r['attempts'].get('$target', 1) + 1
r['last_result'] = 'failed'
r['last_run_log'] = '$target'
with open('$RUN_STATE','w') as f: json.dump(r,f,indent=2)
" 2>/dev/null
    push_run_state "$target failed — attempt $attempt"

    if [[ "$attempt" -ge "$max" ]]; then
      log_fail "$target hit max retries — marking permanently failed, independent targets can continue"
      python3 -c "
import json
with open('$RUN_STATE') as f: r=json.load(f)
r.setdefault('failed_targets', [])
if '$target' not in r['failed_targets']:
    r['failed_targets'].append('$target')
r['attempts']['$target'] = 1
with open('$RUN_STATE','w') as f: json.dump(r,f,indent=2)
" 2>/dev/null
      push_run_state "$target permanently failed after $max retries"
      return 1
    fi

    # Signal OpenCode (for visibility) then wait a fixed delay and retry.
    # No fix_pushed handshake needed — the loop pulls latest code before every
    # retry, so any fix pushed to the repo will be picked up automatically.
    # loop-ack is only needed for genuine human actions (e.g. aws sso login).
    python3 -c "
import json
with open('$OC_STATE') as f: s=json.load(f)
s['fix_pushed']=False
s['waiting_for_fix']=True
with open('$OC_STATE','w') as f: json.dump(s,f,indent=2)
" 2>/dev/null
    cd "$REPO_DIR" || return
    git add loop-state.json 2>/dev/null || true
    git diff --staged --quiet || git commit -m "loop: waiting_for_fix=true — $target attempt $attempt" 2>/dev/null || true
    git push origin "$BRANCH" 2>/dev/null || {
      git pull origin "$BRANCH" --rebase -X theirs --quiet 2>/dev/null || true
      git add loop-state.json 2>/dev/null || true
      git push origin "$BRANCH" 2>/dev/null || true
    }
    push_run_state "$target failed — attempt $attempt — waiting for OpenCode"

    log_wait "Waiting 60s before retry (OpenCode may be pushing a fix)..."
    sleep 60
    git_pull
    python3 -c "
import json
with open('$OC_STATE') as f: s=json.load(f)
s['waiting_for_fix']=False
s['fix_pushed']=False
with open('$OC_STATE','w') as f: json.dump(s,f,indent=2)
" 2>/dev/null
    log_ok "Retrying $target"
    return 1
  fi
}

# ── Pre-flight ─────────────────────────────────────────────────────────────────

cd "$REPO_DIR" || exit 1
log "Starting homelab loop (dependency-aware) — branch: $BRANCH"
git submodule update --init --recursive --quiet 2>/dev/null || true

# Register the 'ours' merge driver — required for .gitattributes merge=ours to work.
# Without this, git falls back to its default 3-way merge and ignores the attribute.
# 'driver = true' means: always keep OUR version of the file during a merge conflict.
git config merge.ours.driver true 2>/dev/null || true

if ! check_aws; then
  log_fail "AWS credentials expired — run: aws --profile jmpa sso login"
  exit 1
fi
log_ok "AWS credentials OK"

# Run inventory — print the actual error if it fails so the cause is visible
if ! inventory_out="$(python3 inventory/main.py 2>&1)"; then
  log_fail "Inventory failed — check SSM / AWS credentials:"
  echo "$inventory_out" | tail -10 | sed 's/^/  /'
  exit 1
fi
log_ok "Inventory OK"

# ── Initialise run state ───────────────────────────────────────────────────────

python3 - <<PYEOF
import json, os
with open('$OC_STATE') as f: s=json.load(f)
targets = s.get('targets', [])
run_state_path = '$RUN_STATE'

if not os.path.exists(run_state_path):
    r = {
        'status': 'running',
        'targets': targets,
        'completed_targets': [],
        'failed_targets': [],
        'attempts': {},
        'max_attempts': s.get('max_attempts', 10),
        'last_result': None,
        'last_run_log': None,
        'human_action': None,
    }
else:
    with open(run_state_path) as f: r=json.load(f)
    r.setdefault('failed_targets', [])
    r.setdefault('attempts', {})
    r['targets'] = targets
    r['max_attempts'] = s.get('max_attempts', 10)
    # Preserve needs_human across restarts — the human hasn't acted yet.
    # Only auto-clear to 'running' if status is idle/None (fresh start).
    if r.get('status') in ('idle', None):
        r['status'] = 'running'
        r['human_action'] = None
    # Remove legacy sequential fields
    for k in ['current_target', 'current_index', 'current_attempt']:
        r.pop(k, None)

with open(run_state_path, 'w') as f: json.dump(r, f, indent=2)
PYEOF

push_run_state "initialised dependency-aware loop"
log "Loop initialised. Ready targets will run now."

# ── Main loop ──────────────────────────────────────────────────────────────────

while true; do
  git_pull

  # Check if needs_human (e.g. AWS expired, hardware blocker)
  if [[ "$(get_run 'status')" == "needs_human" ]]; then
    log_wait "Loop paused — waiting for human: $(get_run 'human_action')"
    sleep $FIX_POLL_INTERVAL
    git_pull
    if [[ "$(get_oc 'fix_pushed')" == "true" ]]; then
      # Determine blocker type — AWS needs a credential check before resuming,
      # hardware blockers just need the human ack (fix_pushed=true is enough).
      human_action="$(python3 -c "
import json
with open('$RUN_STATE') as f: r=json.load(f)
print(r.get('human_action') or '')
" 2>/dev/null)"
      aws_ok=true
      if echo "$human_action" | grep -qi "aws\|sso\|credential"; then
        check_aws || aws_ok=false
      fi
      if $aws_ok; then
        python3 -c "
import json
with open('$RUN_STATE') as f: r=json.load(f)
r['status']='running'; r['human_action']=None
with open('$RUN_STATE','w') as f: json.dump(r,f,indent=2)
with open('$OC_STATE') as f: s=json.load(f)
s['fix_pushed']=False
with open('$OC_STATE','w') as f: json.dump(s,f,indent=2)
" 2>/dev/null
        push_run_state "resuming after human ack"
        log_ok "Resumed — human action acknowledged"
      else
        log_fail "AWS credentials still invalid — run: aws sso login, then: make loop-ack"
        python3 -c "
import json
with open('$OC_STATE') as f: s=json.load(f)
s['fix_pushed']=False
with open('$OC_STATE','w') as f: json.dump(s,f,indent=2)
" 2>/dev/null
      fi
    fi
    continue
  fi

  # Build target status snapshot
  snapshot="$(
    python3 - <<PYEOF
import json
with open('$OC_STATE') as f: s=json.load(f)
with open('$RUN_STATE') as f: r=json.load(f)
targets   = s.get('targets', [])
deps_map  = s.get('deps', {})
completed = set(r.get('completed_targets', []))
failed    = set(r.get('failed_targets', []))
attempts  = r.get('attempts', {})
max_att   = r.get('max_attempts', 10)
done      = completed | failed

ready   = []
waiting = []
skipped = []

# Cascade blocked status: process targets in order so that if a dep is
# skipped (blocked by a failed grandparent), its dependents are also skipped.
# We expand 'failed' with each newly-skipped target as we go.
effective_failed = set(failed)
for t in targets:
    if t in done:
        continue
    deps = deps_map.get(t, [])
    blocked_by = [d for d in deps if d in effective_failed]
    pending    = [d for d in deps if d not in completed and d not in effective_failed]
    if blocked_by:
        skipped.append((t, 'dep failed: ' + ','.join(blocked_by)))
        effective_failed.add(t)  # cascade: this target's deps are also effectively blocked
    elif pending:
        waiting.append((t, 'waiting: ' + ','.join(pending)))
    else:
        ready.append(t)

print('READY:'   + ' '.join(ready))
print('WAITING:' + ' '.join(t for t,_ in waiting))
print('SKIPPED:' + ' '.join(t for t,_ in skipped))
print('DONE:'    + ' '.join(done))
for t, reason in skipped:
    print(f'SKIP_REASON:{t}={reason}')
PYEOF
  )"

  ready="$(echo "$snapshot" | grep '^READY:' | cut -d: -f2 | tr -s ' ')"
  waiting="$(echo "$snapshot" | grep '^WAITING:' | cut -d: -f2 | tr -s ' ')"
  skipped="$(echo "$snapshot" | grep '^SKIPPED:' | cut -d: -f2 | tr -s ' ')"
  done_set="$(echo "$snapshot" | grep '^DONE:' | cut -d: -f2 | tr -s ' ')"

  # Print skip reasons
  while IFS= read -r line; do
    [[ "$line" == SKIP_REASON:* ]] && log_skip "${line#SKIP_REASON:}"
  done <<<"$snapshot"

  # Log waiting targets (dep not done yet)
  for t in $waiting; do
    log_wait "$t — waiting for dependency"
  done

  # Check if everything is done
  all_targets="$(python3 -c "
import json
with open('$OC_STATE') as f: s=json.load(f)
print(' '.join(s.get('targets', [])))
" 2>/dev/null)"

  all_done=true
  for t in $all_targets; do
    in_done=false
    for d in $done_set; do
      [[ "$t" == "$d" ]] && in_done=true && break
    done
    for s in $skipped; do
      [[ "$t" == "$s" ]] && in_done=true && break
    done
    $in_done || {
      all_done=false
      break
    }
  done

  if $all_done && [[ -z "$ready" ]]; then
    failed_list="$(get_run 'failed_targets' | tr -d "[]'\"")"
    python3 -c "
import json
with open('$RUN_STATE') as f: r=json.load(f)
r['status'] = 'completed' if not r.get('failed_targets') else 'completed_with_failures'
with open('$RUN_STATE','w') as f: json.dump(r,f,indent=2)
" 2>/dev/null
    if [[ -z "$failed_list" || "$failed_list" == "[]" ]]; then
      log_ok "All targets completed successfully!"
    else
      log_ok "Loop complete with some failures/skips."
      log "Failed: $failed_list"
      log "Skipped (dep failed): $skipped"
    fi
    push_run_state "loop complete"
    exit 0
  fi

  if [[ -z "$ready" ]]; then
    log_wait "Nothing ready to run — waiting for dependencies to resolve..."
    sleep $FIX_POLL_INTERVAL
    continue
  fi

  # Run each ready target
  for target in $ready; do
    run_target "$target" || true
    # Stop launching more targets if we entered needs_human mid-loop
    # (e.g. a hardware blocker fired on one target — don't start others)
    if [[ "$(get_run 'status')" == "needs_human" ]]; then
      log_wait "Loop entered needs_human — stopping ready-target run"
      break
    fi
  done

done
