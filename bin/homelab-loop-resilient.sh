#!/usr/bin/env bash
# homelab-loop-resilient.sh — wraps homelab-loop with auto-restart and AWS refresh.
#
# Keeps homelab-loop running even if it crashes. Refreshes AWS credentials
# before each restart. If AWS cannot be refreshed, waits and retries.
#
# All output is tee'd to runs/resilient.log AND committed to git after every
# iteration so the Mac can see what's happening without needing terminal access.
#
# Usage:
#   make homelab-loop-resilient

set -uo pipefail

REPO_DIR="$(dirname "$(realpath "$0")")/.."
REPO_DIR="$(realpath "$REPO_DIR")"
RESTART_DELAY=10
AWS_RETRY_DELAY=60 # check every 60s when waiting for AWS auth
LOG_FILE="$REPO_DIR/runs/resilient.log"
BRANCH="$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main")"

mkdir -p "$REPO_DIR/runs"

# Tee all output to the log file so it gets committed and the Mac can read it
exec > >(tee -a "$LOG_FILE") 2>&1

log() { echo "$(date '+%H:%M:%S') [resilient] $*"; }

push_log() {
  cd "$REPO_DIR" || return
  git add runs/resilient.log 2>/dev/null || true
  git diff --staged --quiet && return 0
  git commit -m "loop: resilient — $1" 2>/dev/null || true
  git push origin "$BRANCH" 2>/dev/null || {
    git pull origin "$BRANCH" --rebase -X theirs --quiet 2>/dev/null || true
    git push origin "$BRANCH" 2>/dev/null || true
  }
}

log "Starting resilient homelab loop"
log "Auto-restarts on crash. Ctrl+C to stop."
log "Log file: $LOG_FILE"

# Register the 'ours' merge driver — required for .gitattributes merge=ours to work.
# Without this, git ignores the merge=ours attribute for loop-run-state.json and
# loop-state.json, falling back to a 3-way merge that can produce conflicts.
git -C "$REPO_DIR" config merge.ours.driver true 2>/dev/null || true

# Force jmpa profile regardless of any inherited AWS_PROFILE env var
export AWS_PROFILE=jmpa

while true; do
  # Check AWS credentials — wait indefinitely if expired, don't run at all
  while ! bash "$REPO_DIR/bin/aws-refresh.sh"; do
    log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    log "AWS credentials expired."
    log "Run: aws --profile jmpa sso login"
    log "Checking again in ${AWS_RETRY_DELAY}s..."
    log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    push_log "waiting for AWS credentials"
    sleep $AWS_RETRY_DELAY
  done

  log "Pulling latest code from origin/main..."
  # --autostash: stashes any local changes (e.g. loop-run-state.json written by
  # homelab-loop.sh) before rebasing, then restores them after. This prevents
  # "cannot rebase: You have unstaged changes" from blocking the pull every time.
  if git -C "$REPO_DIR" pull origin "$BRANCH" --rebase --autostash --quiet 2>/dev/null; then
    log "Pull OK — $(git -C "$REPO_DIR" rev-parse --short HEAD)"
  else
    log "Pull failed (will continue with current code)"
  fi
  git -C "$REPO_DIR" submodule update --init --recursive --quiet 2>/dev/null || true

  push_log "starting homelab-loop"

  log "Starting homelab-loop..."
  # Capture exit code without losing it to '|| true' (which always sets $?=0)
  set +e
  bash "$REPO_DIR/bin/homelab-loop.sh"
  EXIT_CODE=$?
  set -e

  log "homelab-loop exited (code: $EXIT_CODE)"
  push_log "homelab-loop exited (code: $EXIT_CODE)"

  # If it exited cleanly (all targets completed), stop the resilient wrapper too.
  if [[ $EXIT_CODE -eq 0 ]]; then
    log "All targets completed — stopping resilient wrapper"
    push_log "all targets completed"
    exit 0
  fi

  log "Restarting in ${RESTART_DELAY}s... (Ctrl+C to stop)"
  sleep $RESTART_DELAY
done
