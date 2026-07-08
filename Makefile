# loop/Makefile — included by consuming repos via: include .loop/Makefile
#
# Provides make targets for the AI self-healing deployment loop.
# State files (loop-state.json, loop-run-state.json) live in the consuming
# repo root, not inside this submodule.
#
# Usage in consuming repo's Makefile:
#   include .loop/Makefile

# Path to this Makefile's directory — works whether included or run directly.
LOOP_DIR := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))

loop-start: ## Loop: start the resilient loop in a detached tmux session.
loop-start:
	@SESSION="homelab-loop"; \
	SCRIPT="$(LOOP_DIR)bin/loop_resilient.py"; \
	if pgrep -f "loop_resilient.py" > /dev/null 2>&1; then \
		echo "Loop is already running (PID: $$(pgrep -f loop_resilient.py | tr '\n' ' '))"; \
		echo "Use 'make loop-attach' to watch it, or 'make loop-stop' to stop it first."; \
	elif tmux has-session -t "$$SESSION" 2>/dev/null; then \
		echo "Stale tmux session found — killing and restarting..."; \
		tmux kill-session -t "$$SESSION" 2>/dev/null || true; \
		tmux new-session -d -s "$$SESSION" -c "$$PWD" "python3 $$SCRIPT"; \
		echo "Loop started. Run 'make loop-attach' to watch it."; \
	else \
		echo "Starting loop..."; \
		tmux new-session -d -s "$$SESSION" -c "$$PWD" "python3 $$SCRIPT"; \
		echo "Loop started. Run 'make loop-attach' to watch it."; \
	fi

loop-stop: ## Loop: stop the running loop tmux session.
loop-stop:
	@tmux kill-session -t homelab-loop 2>/dev/null && echo "Loop stopped." || echo "No loop session running."

loop-attach: ## Loop: attach to the running tmux session to watch output.
loop-attach:
	@tmux attach -t homelab-loop 2>/dev/null || echo "No loop session running. Run 'make loop-start' first."

loop-status: ## Loop: show current status, completed/failed targets, and attempt counts.
loop-status:
	@python3 $(LOOP_DIR)bin/loop_status.py

loop-reset: ## Loop: reset all state — all targets re-run from scratch on next loop-start.
loop-reset:
	@python3 $(LOOP_DIR)bin/loop_reset.py

loop-test: ## Loop: run unit tests.
loop-test:
	@python3 $(LOOP_DIR)tests/test_loop.py

.PHONY += loop-start loop-stop loop-attach loop-status loop-reset loop-test
