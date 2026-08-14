# loop/Makefile — included by consuming repos via: include .loop/Makefile
#
# Provides make targets for the AI self-healing deployment loop.
# State files (sender-state.json, receiver-state.json) live in the consuming
# repo root, not inside this submodule.
#
# Usage in consuming repo's Makefile:
#   include .loop/Makefile

# Path to this Makefile's directory — works whether included or run directly.
LOOP_DIR := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))

loop-debug: ## Loop: print shell/tmux diagnostics to help debug make loop-start-* failures.
loop-debug:
	@echo "=== loop-debug ==="
	@echo ""
	@echo "--- Environment ---"
	@echo "make SHELL   : $(SHELL)"
	@echo "sh path      : $$(command -v sh)"
	@echo "sh version   : $$(/bin/sh --version 2>&1 | head -1 || echo unknown)"
	@echo "bash path    : $$(command -v bash || echo 'not found')"
	@echo "USER         : $$(id -un)"
	@echo "PWD          : $$PWD"
	@echo "LOOP_DIR     : $(LOOP_DIR)"
	@echo "TERM         : $${TERM:-unset}"
	@echo "TMUX         : $${TMUX:-unset}"
	@echo ""
	@echo "--- tmux ---"
	@echo "tmux path    : $$(command -v tmux || echo 'not found')"
	@echo "tmux version : $$(tmux -V 2>&1 || echo 'tmux not found')"
	@echo "tmux socket  : $$(tmux -L default ls 2>&1 || echo 'no sessions on default socket')"
	@echo "tmux tmpdir  : $${TMUX_TMPDIR:-/tmp} (uid=$$(id -u))"
	@echo "socket path  : /tmp/tmux-$$(id -u)/default"
	@ls -la /tmp/tmux-$$(id -u)/ 2>&1 || echo "  socket dir does not exist yet"
	@echo ""
	@echo "--- tmux new-session test (from make shell) ---"
	@tmux -L default new-session -d -s loop-debug-test "sleep 10" 2>&1 \
		&& echo "  OK — tmux new-session works from make" \
		|| echo "  FAIL — tmux new-session DOES NOT work from make (this is the bug)"
	@tmux -L default ls 2>&1 || true
	@tmux -L default kill-session -t loop-debug-test 2>/dev/null || true
	@echo ""
	@echo "--- pgrep ---"
	@echo "sender       : $$(pgrep -fa sender_resilient.py 2>/dev/null || echo 'not running')"
	@echo "receiver     : $$(pgrep -fa receiver.py 2>/dev/null || echo 'not running')"
	@echo ""
	@echo "--- python3 ---"
	@echo "python3 path : $$(command -v python3 || echo 'not found')"
	@echo "python3 ver  : $$(python3 --version 2>&1 || echo 'not found')"
	@echo "=================="

loop-start-sender: ## Loop: start the sender. Optionally pass TARGETS="build test deploy" to set targets first.
loop-start-sender:
	@if [ -n "$(TARGETS)" ]; then \
		echo "Setting targets: $(TARGETS)"; \
		python3 $(LOOP_DIR)bin/loop_targets.py $(TARGETS); \
	fi
	@SESSION="homelab-loop"; \
	SCRIPT="$(LOOP_DIR)bin/sender_resilient.py"; \
	TMUX="tmux -L default"; \
	if pgrep -f "sender_resilient.py" > /dev/null 2>&1; then \
		echo "Sender loop is already running (PID: $$(pgrep -f sender_resilient.py | tr '\n' ' '))"; \
		echo "Use 'make loop-attach' to watch it, or 'make loop-stop' to stop it first."; \
	elif $$TMUX has-session -t "$$SESSION" 2>/dev/null; then \
		echo "Stale tmux session found — killing and restarting..."; \
		$$TMUX kill-session -t "$$SESSION" 2>/dev/null || true; \
		$$TMUX new-session -d -s "$$SESSION" -c "$$PWD" "python3 $$SCRIPT"; \
		echo "Sender started. Run 'make loop-attach' to watch it."; \
	else \
		echo "Starting sender loop..."; \
		$$TMUX new-session -d -s "$$SESSION" -c "$$PWD" "python3 $$SCRIPT"; \
		echo "Sender started. Run 'make loop-attach' to watch it."; \
	fi

loop-start-receiver: ## Loop: start the receiver. Optionally pass TARGETS="build test deploy" to set targets first.
loop-start-receiver:
	@if [ -n "$(TARGETS)" ]; then \
		echo "Setting targets: $(TARGETS)"; \
		python3 $(LOOP_DIR)bin/loop_targets.py $(TARGETS); \
	fi
	@SESSION="homelab-loop-receiver"; \
	SCRIPT="$(LOOP_DIR)bin/receiver.py"; \
	TMUX="tmux -L default"; \
	if pgrep -f "receiver.py" > /dev/null 2>&1; then \
		echo "Receiver loop is already running (PID: $$(pgrep -f receiver.py | tr '\n' ' '))"; \
		echo "Use 'make loop-attach' to watch it, or 'make loop-stop' to stop it first."; \
	elif $$TMUX has-session -t "$$SESSION" 2>/dev/null; then \
		echo "Stale tmux session found — killing and restarting..."; \
		$$TMUX kill-session -t "$$SESSION" 2>/dev/null || true; \
		$$TMUX new-session -d -s "$$SESSION" -c "$$PWD" "python3 $$SCRIPT"; \
		echo "Receiver started. Run 'make loop-attach' to watch it."; \
	else \
		echo "Starting receiver loop..."; \
		$$TMUX new-session -d -s "$$SESSION" -c "$$PWD" "python3 $$SCRIPT"; \
		echo "Receiver started. Run 'make loop-attach' to watch it."; \
	fi

loop-attach: ## Loop: attach to the running tmux session to watch output.
loop-attach:
	@TMUX="tmux -L default"; \
	if $$TMUX has-session -t homelab-loop 2>/dev/null; then \
		$$TMUX attach -t homelab-loop; \
	elif $$TMUX has-session -t homelab-loop-receiver 2>/dev/null; then \
		$$TMUX attach -t homelab-loop-receiver; \
	else \
		echo "No loop session running. Run 'make loop-start-sender' or 'make loop-start-receiver' first."; \
	fi

loop-stop: ## Loop: stop both sides immediately and signal the remote machine to stop.
loop-stop:
	@python3 $(LOOP_DIR)bin/loop_stop.py

loop-pause: ## Loop: pause after the current target completes. Run 'make loop-ack' to resume.
loop-pause:
	@python3 $(LOOP_DIR)bin/loop_pause.py

loop-targets: ## Loop: set targets in receiver-state.json. Usage: make loop-targets TARGETS="build test deploy"
loop-targets:
	@if [ -z "$(TARGETS)" ]; then \
		echo "Usage: make loop-targets TARGETS=\"build test deploy\""; \
		exit 1; \
	fi
	@python3 $(LOOP_DIR)bin/loop_targets.py $(TARGETS)

loop-status: ## Loop: show current status, completed/failed targets, and attempt counts.
loop-status:
	@python3 $(LOOP_DIR)bin/loop_status.py

loop-reset: ## Loop: reset all state — all targets re-run from scratch on next loop-start-sender.
loop-reset:
	@python3 $(LOOP_DIR)bin/loop_reset.py

loop-ack: ## Loop: acknowledge a human action / resume after pause.
loop-ack:
	@python3 $(LOOP_DIR)bin/loop_ack.py

loop-test: ## Loop: run unit and integration tests.
loop-test:
	@python3 $(LOOP_DIR)tests/test_loop.py
	@python3 $(LOOP_DIR)tests/test_integration.py

.PHONY += loop-debug loop-start-sender loop-start-receiver loop-attach loop-stop loop-pause loop-targets loop-status loop-reset loop-ack loop-test
