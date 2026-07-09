# Status

Last updated: 2026-07-09

---

## What this is

A dependency-aware deployment loop with OpenCode self-healing. Two processes
run on two separate machines and coordinate entirely through git commits — no
sockets, no HTTP.

```
[Sender — runner machine]               [Receiver — Mac]
sender.py  ──── git push ──────────►  receiver.py
  runs make targets in dep order          watches sender-state.json for failures
  writes sender-state.json                invokes OpenCode to diagnose + fix
  reads receiver-state.json               pushes fix, sets fix_pushed=true
  retries on fix  ◄──── git pull ──────
```

File ownership is strict:
- `sender-state.json` — written only by the sender
- `receiver-state.json` — written only by the receiver

This eliminates merge conflicts entirely.

`sender_resilient.py wraps sender.py` — auto-restarts on crash, pulls latest
code before each restart. `make loop-start-sender` always runs `sender_resilient.py`.

Single machine: run `make loop-start-sender` and `make loop-start-receiver` in
separate terminals on the same machine.

---

## Current state

### What is done

- **Full Python implementation** — all scripts in Python, no bash
- **Strict file ownership** — sender writes sender-state.json only, receiver writes receiver-state.json only
- **No waiting_for_fix flag** — receiver infers sender needs fix from sender-state.json directly via `lib.sender_needs_fix()`
- **Renamed state files** — `loop-state.json` → `receiver-state.json`, `loop-run-state.json` → `sender-state.json`
- **Extracted pure functions in receiver.py** — `should_invoke_opencode()`, `build_opencode_prompt()`, `gather_previous_logs()`, `set_fix_pushed()`, `notify_human()` — all testable without subprocess
- **stop/pause signals** — `make loop-stop` and `make loop-pause` kill local tmux and push signal via git
- **loop-start-sender / loop-start-receiver split** — separate make targets for each side
- **Auto-create loop-context.md** — created at startup by `sender_resilient.py` if missing
- **148 passing tests, 76% coverage** — up from 42% at previous session

### Coverage breakdown

| File | Coverage |
|---|---|
| `lib.py` | 86% |
| `sender.py` | 76% |
| `sender_resilient.py` | 93% |
| `loop_status.py` | 95% |
| `trim_loop_context.py` | 97% |
| `receiver.py` | 51% |
| `loop_ack.py` | 71% |
| `loop_stop.py` | 70% |
| `loop_pause.py` | 70% |
| `loop_reset.py` | 73% |

### Remaining coverage gaps

The uncovered lines in `loop_ack.py`, `loop_stop.py`, `loop_pause.py`, and `loop_reset.py` are all the push/retry-on-conflict paths (lines after the first successful git push). These require a live git remote to test properly.

`receiver.py` main() loop body (lines 210-373) — the full end-to-end invocation path including subprocess OpenCode call, git commit, and push. Covered via pure function tests instead.

---

## File map

### In this repo (`.loop/`)

| File | Purpose |
|---|---|
| `bin/lib.py` | Shared library — all pure logic: state I/O, git ops, dep resolution, blocker detection, signal helpers, state transitions |
| `bin/sender.py` | Core sender loop — runs `make <target>` in dep order, retries, polls receiver for fix signal |
| `bin/sender_resilient.py` | Crash-resilient wrapper — restarts `sender.py` on crash, pulls latest code first, auto-creates `loop-context.md` |
| `bin/receiver.py` | Receiver — polls sender-state.json for failures, invokes OpenCode to fix, sets fix_pushed in receiver-state.json |
| `bin/loop_ack.py` | Acknowledges human action / resumes after pause |
| `bin/loop_pause.py` | Sets pause signal in receiver-state.json, kills local tmux |
| `bin/loop_stop.py` | Sets stop signal in receiver-state.json, kills local tmux |
| `bin/loop_reset.py` | Resets both state files to blank, commits and pushes |
| `bin/loop_status.py` | Prints current status from sender-state.json |
| `bin/trim_loop_context.py` | Caps loop-context.md at 500 lines, archives overflow |
| `Makefile` | loop-start-sender, loop-start-receiver, loop-attach, loop-stop, loop-pause, loop-status, loop-reset, loop-ack, loop-test |
| `receiver-state.json` | Template — copy to consuming repo root, edit targets/deps/blocker_patterns |
| `sender-state.json` | Template — copy to consuming repo root |
| `tests/test_loop.py` | 148 tests — unit + integration |

### Generated in the consuming repo

| File | Owner | Purpose |
|---|---|---|
| `receiver-state.json` | Receiver | Config: targets, deps, max attempts, blocker patterns, fix signals, stop/pause |
| `sender-state.json` | Sender | Runtime state: completed/failed targets, attempt counts, status |
| `loop-context.md` | You + OpenCode | OpenCode's memory — failure history and fixes. Auto-created on startup. |
| `runs/` | Sender | Per-target logs, resilient wrapper log, OpenCode invocation logs |
| `docs/loop-context-archive.md` | Auto | Overflow from loop-context.md when it exceeds 500 lines |

---

## Next steps (in order)

1. Update the homelab repo to use new file names (receiver-state.json, sender-state.json)
2. Push coverage higher on receiver.py main() — consider integration test with mocked opencode binary
3. Push coverage on ack/stop/pause/reset retry paths — requires live git remote or test git server
