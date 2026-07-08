#!/usr/bin/env bash
# opencode-loop.sh — runs on this Mac.
#
# FILE OWNERSHIP — separation to reduce git conflicts:
#   loop-run-state.json — Primarily written by Arch laptop. Mac writes ONLY:
#                         - human_action + status=needs_human on NEEDS_HUMAN output
#                         These are one-way writes; the laptop overwrites on ack.
#
#   loop-state.json     — WRITTEN by this script ONLY
#                         We write: fix_pushed, opencode_last_fix

set -uo pipefail

REPO_DIR="$(dirname "$(realpath "$0")")/.."
REPO_DIR="$(realpath "$REPO_DIR")"
RUN_STATE="$REPO_DIR/loop-run-state.json"
OC_STATE="$REPO_DIR/loop-state.json"
BRANCH="$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'main')"
POLL_INTERVAL=10
MAX_ERRORS=3

log() { echo "$(date '+%H:%M:%S') [opencode] $*"; }
log_ok() { echo "$(date '+%H:%M:%S') [opencode] ✓ $*"; }
log_fail() { echo "$(date '+%H:%M:%S') [opencode] ✗ $*"; }
log_wait() { echo "$(date '+%H:%M:%S') [opencode] ⏳ $*"; }

# Read from loop-run-state.json (laptop's file — READ ONLY for us)
get_run() {
	python3 -c "
import json
with open('$RUN_STATE') as f: s=json.load(f)
v=s.get('$1')
print('' if v is None else str(v).lower() if isinstance(v,bool) else str(v))
" 2>/dev/null || echo ""
}

# Write fix_pushed to loop-state.json (our file ONLY)
set_fix_pushed() {
	python3 -c "
import json
with open('$OC_STATE') as f: s=json.load(f)
s['fix_pushed']=True
s['opencode_last_fix']='$1'
with open('$OC_STATE','w') as f: json.dump(s,f,indent=2)
" 2>/dev/null
	cd "$REPO_DIR" || return
	git add loop-state.json
	git commit -m "loop: fix_pushed=true — $1" 2>/dev/null || true
	git pull origin "$BRANCH" --rebase --autostash -X theirs --quiet 2>/dev/null || true
	git push origin "$BRANCH" 2>/dev/null || {
		git pull origin "$BRANCH" --rebase --autostash -X theirs --quiet 2>/dev/null || true
		git push origin "$BRANCH" 2>/dev/null || true
	}
}

git_pull() {
	cd "$REPO_DIR" || return
	git pull origin "$BRANCH" --rebase --autostash -X theirs --quiet 2>/dev/null || true
}

log "Starting OpenCode loop — polling every ${POLL_INTERVAL}s"
log "Watching: loop-run-state.json (laptop) + loop-state.json (mac)"

# Register the 'ours' merge driver — required for .gitattributes merge=ours to work.
# Without this, git falls back to its default 3-way merge and ignores the attribute.
# 'driver = true' means: always keep OUR version of the file during a merge conflict.
git -C "$REPO_DIR" config merge.ours.driver true 2>/dev/null || true

errors=0
last_processed_log=""

while true; do
	git_pull

	if [[ ! -f "$RUN_STATE" ]]; then
		log_wait "loop-run-state.json not found yet — laptop loop hasn't started"
		sleep $POLL_INTERVAL
		continue
	fi

	status="$(get_run 'status')"
	target="$(get_run 'last_run_log')"
	last_result="$(get_run 'last_result')"

	# Human action — show notification and wait
	human_action="$(python3 -c "
import json
with open('$RUN_STATE') as f: s=json.load(f)
v=s.get('human_action')
print('' if v is None else str(v))
" 2>/dev/null || echo "")"

	if [[ -n "$human_action" ]]; then
		echo ""
		echo "════════════════════════════════════════════════════════════"
		echo "  ⚠️  ACTION REQUIRED"
		echo "════════════════════════════════════════════════════════════"
		echo "  $human_action"
		echo "════════════════════════════════════════════════════════════"
		echo "  Once done, run: make loop-ack"
		echo "════════════════════════════════════════════════════════════"
		printf '\a'
		osascript -e "display notification \"$human_action\" with title \"Homelab Loop\" subtitle \"Action Required\" sound name \"Sosumi\"" 2>/dev/null || true
		sleep $POLL_INTERVAL
		continue
	fi

	[[ "$status" == "completed" || "$status" == "completed_with_failures" ]] && {
		log_ok "Loop complete! All targets finished."
		exit 0
	}
	[[ "$status" == "needs_human" ]] && {
		log_wait "Waiting for human action — run: make loop-ack when done"
		sleep $POLL_INTERVAL
		continue
	}

	if [[ "$last_result" != "failed" ]]; then
		log "Laptop loop is running — last target: ${target:-none} | result: ${last_result:-pending} | status: $status"
		sleep $POLL_INTERVAL
		continue
	fi

	# Guard: if target is already in completed_targets, the 'failed' result is stale
	# (race between homelab-loop writing success and opencode-loop reading state).
	already_done="$(python3 -c "
import json
with open('$RUN_STATE') as f: r=json.load(f)
print('true' if '$target' in r.get('completed_targets', []) else 'false')
" 2>/dev/null || echo "false")"
	if [[ "$already_done" == "true" ]]; then
		log "Skipping — $target is already in completed_targets (stale failed state from previous attempt)"
		sleep $POLL_INTERVAL
		continue
	fi

	# Find latest log for this target
	# shellcheck disable=SC2012
	latest_log="$(ls -1t "$REPO_DIR/runs/${target}-"*.log 2>/dev/null | head -1 | xargs basename 2>/dev/null)"
	if [[ -z "$latest_log" ]]; then
		log_wait "$target failed but no log found yet — waiting..."
		sleep $POLL_INTERVAL
		continue
	fi

	# Check if loop is waiting for a fix (read from OC_STATE — homelab-loop.sh sets this there)
	waiting="$(python3 -c "
import json
with open('$OC_STATE') as f: s=json.load(f)
print(str(s.get('waiting_for_fix', False)).lower())
" 2>/dev/null || echo "false")"
	fix_pushed_val="$(python3 -c "
import json
with open('$OC_STATE') as f: s=json.load(f)
print(str(s.get('fix_pushed', False)).lower())
" 2>/dev/null || echo "false")"

	if [[ "$latest_log" == "$last_processed_log" ]] && [[ "$fix_pushed_val" == "true" ]]; then
		log_wait "$target — fix already pushed, waiting for laptop to retry and produce a new log..."
		sleep $POLL_INTERVAL
		continue
	fi

	if [[ "$waiting" != "true" ]]; then
		log "Laptop is actively running $target (not paused for a fix) — standing by..."
		sleep $POLL_INTERVAL
		continue
	fi

	log "⚡ $target failed — invoking OpenCode to fix (log: $latest_log)"
	last_processed_log="$latest_log"
	oc_log="$REPO_DIR/runs/opencode-loop-$(date '+%Y%m%d-%H%M%S').log"

	prev_logs=""
	# Find previous run logs for THIS specific target (not other targets' OpenCode sessions)
	# This gives OpenCode context about what has already been tried for this exact failure
	# shellcheck disable=SC2012
	prev_target_logs="$(ls -1t "$REPO_DIR/runs/${target}-"*.log 2>/dev/null | tail -n +2 | head -3 | xargs -I{} basename {} 2>/dev/null)"
	for pf in $prev_target_logs; do
		prev_logs="$prev_logs
--- Previous run log for $target: $pf ---
$(tail -30 "$REPO_DIR/runs/$pf" 2>/dev/null)"
	done
	# Also include the last OpenCode invocation log (to avoid repeating a broken fix)
	# shellcheck disable=SC2012
	last_oc_log="$(ls -1t "$REPO_DIR/runs/opencode-loop-"*.log 2>/dev/null | head -1 | xargs basename 2>/dev/null)"
	if [[ -n "$last_oc_log" ]]; then
		prev_logs="$prev_logs
--- Last OpenCode invocation ($last_oc_log) ---
$(tail -20 "$REPO_DIR/runs/$last_oc_log" 2>/dev/null)"
	fi

	cd "$REPO_DIR" || return
	# timeout 30m: prevents opencode-loop from hanging indefinitely if opencode stalls.
	# If it times out, result will be empty and the unclear-response counter fires.
	result=$(timeout 1800 opencode run --dangerously-skip-permissions "
You are the homelab deployment agent. A make target has failed and you must diagnose and fix the code.

## Repo structure (understand this before touching anything)
- loop/bin/homelab-loop.sh     — Arch laptop executor loop (do NOT modify while loop is running)
- loop/bin/opencode-loop.sh    — Mac fixer loop (this script — do NOT self-modify)
- loop-state.json         — YOUR file: targets, deps, fix signals (you may update opencode_last_fix)
- loop-run-state.json     — LAPTOP'S file: runtime state — DO NOT COMMIT THIS FILE
- loop-context.md         — Shared brain: full history of every failure and fix applied
- runs/                   — Run logs committed by the laptop — read to diagnose failures
- playbooks/              — Top-level Ansible playbooks (loop targets call these)
- services/vms/k3s/       — k3s VM provisioning playbooks
- services/lxc/           — LXC service playbooks (observability, nginx, PBS)
- services/proxmox-community-scripts/ — Community script LXC deployments
- scripts/                — Shell scripts called via ansible.builtin.script (no Jinja2 in body)
- roles/                  — Ansible roles (create-vm, proxmox-community-script, etc)
- inventory/main.py       — Dynamic inventory reading from AWS SSM

## Critical rules — violations cause silent failures
- NEVER commit loop-run-state.json — owned by the running loop, your commit will conflict
- NEVER commit files in runs/ or dist/
- NEVER run make, ansible-playbook, kubectl, or any live infrastructure command
- NEVER hardcode credentials — all secrets are in AWS SSM (ap-southeast-2)
- k3s playbooks use root_playbook_directory (NOT playbook_dir) for vars_files paths
- Shell blocks containing regex {}, jsonpath {}, or bare \$() must be extracted to scripts/ — Ansible argument splitter treats { as Jinja2 and fails at load time (exit 4) before any task runs

## Validation commands (run these after every fix before committing)
  python3 -c \"import yaml; yaml.safe_load(open('<changed_file>'))\"
  ANSIBLE_COLLECTIONS_PATH=\$(pwd)/vendor/collections ANSIBLE_COLLECTIONS_SCAN_SYS_PATH=false ansible-playbook --syntax-check -i localhost, -e root_playbook_directory=\$(pwd) <changed_playbook>

## Files to read first (in this order — do not skip any)
1. loop-context.md — FULL FILE. Read BEFORE writing any fix. Same fix twice = wrong approach.
2. runs/$latest_log — FULL FILE. Look for the FIRST error line, not the last symptom.
3. loop-run-state.json — note attempt count and completed/failed targets for context.

## Previous OpenCode attempts on this target
$prev_logs

## Instructions
IMPORTANT: If the failing target is 'loop-test-fail' — deliberate test target. Output RETRY immediately.

For real failures:
1. Read all three files above in full before writing any code.
2. Identify ROOT CAUSE — first error in the log, not the cascading symptoms.
3. Cross-check loop-context.md: if same error recurred after a prior fix, that fix was wrong — use a completely different approach.
4. Fix minimum necessary files. Prefer editing existing files over creating new ones.
5. Run the validation commands above. Fix any errors before committing.
6. Update loop-context.md: add a bullet under ### $target with today's date, error seen, and fix applied.
7. Commit: git add <only changed files — NOT loop-run-state.json, NOT runs/> && git commit -m 'fix($target): <description>'
8. Push: git pull --rebase --autostash origin $BRANCH && git push origin $BRANCH

Output exactly one of these as the LAST LINE: RETRY, SUCCESS, or NEEDS_HUMAN
- RETRY: fix pushed, laptop should retry
- SUCCESS: target already succeeded (no fix needed)
- NEEDS_HUMAN: same error 3+ times with no new fix, OR requires physical hardware, OR requires credentials you cannot provide
" 2>&1)

	echo "$result" | tee "$oc_log"
	cd "$REPO_DIR" || return

	# Trim loop-context.md if it has grown beyond 500 lines
	bash "$(dirname "$(realpath "$0")")/trim-loop-context.sh" 2>/dev/null || true

	git add "$oc_log" loop-state.json loop-context.md docs/loop-context-archive.md 2>/dev/null || true
	git commit -m "loop: opencode log for $target" 2>/dev/null || true
	git pull origin "$BRANCH" --rebase --autostash -X theirs --quiet 2>/dev/null || true
	git push origin "$BRANCH" 2>/dev/null || true

	last_word="$(echo "$result" | tr -s ' \n' '\n' | grep -E '^(SUCCESS|RETRY|NEEDS_HUMAN)$' | tail -1)"
	if [[ -z "$last_word" ]]; then
		echo "$result" | grep -qi "success" && last_word="SUCCESS" || true
		echo "$result" | grep -qi "retry\|fix.*push\|push.*fix" && last_word="RETRY" || true
		echo "$result" | grep -qi "needs.human\|cannot fix\|human intervention" && last_word="NEEDS_HUMAN" || true
	fi

	log "OpenCode result: '${last_word:-UNCLEAR}'"

	case "$last_word" in
	SUCCESS | RETRY)
		set_fix_pushed "$target fixed by OpenCode"
		errors=0
		log_ok "$target — fix pushed, laptop will retry"
		;;
	NEEDS_HUMAN)
		log_fail "$target — OpenCode cannot fix automatically, human intervention needed"
		# NOTE: We write human_action to loop-run-state.json here as an exception
		# because it's a status field the laptop needs to display. This is safe
		# because it's a one-way write (we don't read it back) and the laptop
		# will overwrite it when it acks.
		python3 -c "
import json
with open('$RUN_STATE') as f: r=json.load(f)
r['human_action']='OpenCode could not fix $target automatically. Check runs/$latest_log. Fix manually then run: make loop-ack'
r['status']='needs_human'
with open('$RUN_STATE','w') as f: json.dump(r,f,indent=2)
" 2>/dev/null
		cd "$REPO_DIR" || return
		git add loop-run-state.json loop-state.json 2>/dev/null || true
		git commit -m "loop: needs human — $target" 2>/dev/null || true
		git pull origin "$BRANCH" --rebase --autostash -X theirs --quiet 2>/dev/null || true
		git push origin "$BRANCH" 2>/dev/null || true
		;;
	*)
		((errors++)) || true
		log_fail "OpenCode output unclear — could not determine fix outcome ($errors/$MAX_ERRORS unclear responses)"
		if [[ $errors -ge $MAX_ERRORS ]]; then
			log_fail "Too many unclear responses — forcing fix_pushed=true to unblock loop"
			set_fix_pushed "forced unblock after $MAX_ERRORS unclear responses"
			errors=0
		fi
		;;
	esac

	sleep $POLL_INTERVAL
done
