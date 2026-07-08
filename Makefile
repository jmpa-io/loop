# loop/Makefile — included by consuming repos via: include .loop/Makefile
#
# Provides all make targets for the AI self-healing deployment loop.
# State files (loop-state.json, loop-run-state.json) live in the consuming
# repo root, not inside this submodule.
#
# Usage in consuming repo's Makefile:
#   include .loop/Makefile
#
# All targets are prefixed to avoid collisions with your own targets.

# Path to this Makefile's directory — works whether included or run directly.
LOOP_DIR := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))

loop-start: ## Loop: start the resilient homelab loop in a detached tmux session.
loop-start:
	@SESSION="homelab-loop"; \
	if pgrep -f "homelab-loop-resilient.sh" > /dev/null 2>&1; then \
		echo "homelab-loop-resilient is already running (PID: $$(pgrep -f homelab-loop-resilient.sh | tr '\n' ' '))"; \
		echo "Use 'make loop-attach' to watch it, or 'make loop-stop' to stop it first."; \
	elif tmux has-session -t "$$SESSION" 2>/dev/null; then \
		echo "tmux session exists but no loop running — killing stale session and restarting..."; \
		tmux kill-session -t "$$SESSION" 2>/dev/null || true; \
		tmux new-session -d -s "$$SESSION" -c "$$PWD" "make loop-resilient"; \
		echo "Loop started. Run 'make loop-attach' to watch it."; \
	else \
		echo "Starting resilient loop..."; \
		tmux new-session -d -s "$$SESSION" -c "$$PWD" "make loop-resilient"; \
		echo "Loop started. Run 'make loop-attach' to watch it."; \
	fi

loop-resilient: ## Loop: run the resilient wrapper (auto-restart + AWS refresh) directly.
loop-resilient:
	@bash $(LOOP_DIR)bin/homelab-loop-resilient.sh

loop-run: ## Loop: run the core loop directly (no auto-restart wrapper).
loop-run:
	@bash $(LOOP_DIR)bin/homelab-loop.sh

loop-attach: ## Loop: attach to the running tmux session to watch it.
loop-attach:
	@tmux attach -t homelab-loop 2>/dev/null || echo "No homelab-loop session running. Run 'make loop-start' first."

loop-stop: ## Loop: stop the homelab loop tmux session.
loop-stop:
	@tmux kill-session -t homelab-loop 2>/dev/null && echo "Loop stopped." || echo "No homelab-loop session running."

loop-logs: ## Loop: tail the latest run log.
loop-logs:
	@latest=$$(ls -t runs/*.log 2>/dev/null | head -1); \
	if [ -z "$$latest" ]; then echo "No log files in runs/ yet."; exit 0; fi; \
	tail -f "$$latest"

loop-status: ## Loop: show current loop state (status, last target, completed/failed).
loop-status:
	@python3 -c " \
import json, sys, os; \
path = 'loop-run-state.json'; \
if not os.path.exists(path): \
    print('loop-run-state.json not found — loop has not been started yet.'); sys.exit(0); \
r = json.load(open(path)); \
print(f\"Status:    {r.get('status')}\"); \
print(f\"Last run:  {r.get('last_run_log')} ({r.get('last_result')})\"); \
print(f\"Completed: {r.get('completed_targets')}\"); \
print(f\"Failed:    {r.get('failed_targets')}\"); \
print(f\"Attempts:  {r.get('attempts')}\"); \
ha = r.get('human_action'); \
print(f\"NEEDS HUMAN: {ha}\") if ha else None; \
"

loop-ack: ## Loop: acknowledge a human action — resumes a paused loop.
loop-ack:
	@bash $(LOOP_DIR)bin/loop-ack.sh

loop-reset: ## Loop: reset all state — all targets re-run from scratch on next start.
loop-reset:
	@bash $(LOOP_DIR)bin/loop-reset.sh

loop-opencode: ## Loop: start the OpenCode monitoring loop on this Mac (fixes failures automatically).
loop-opencode:
	@bash $(LOOP_DIR)bin/opencode-loop.sh

loop-trim-context: ## Loop: trim loop-context.md to 500 lines, archiving overflow.
loop-trim-context:
	@bash $(LOOP_DIR)bin/trim-loop-context.sh

.PHONY += loop-start loop-resilient loop-run loop-attach loop-stop loop-logs loop-status loop-ack loop-reset loop-opencode loop-trim-context
