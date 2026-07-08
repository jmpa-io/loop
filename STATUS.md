# Status

Last updated: 2026-07-08

---

## What this is

A dependency-aware deployment loop with OpenCode self-healing. Two processes
run on two separate machines and coordinate entirely through git commits — no
sockets, no HTTP.

```
[Sender — runner machine]               [Receiver — Mac]
loop.py  ────── git push ──────────►  opencode_loop.py
  runs make targets in dep order          watches for failures
  writes loop-run-state.json              invokes OpenCode to fix
  reads loop-state.json                   pushes fix, sets fix_pushed=true
  retries on fix  ◄──── git pull ──────
```

`loop_resilient.py` wraps `loop.py` — auto-restarts it on crash, pulls latest
code before each restart. `make loop-start` always runs `loop_resilient.py`.

---

## Current state

### What is done

- **Full Python rewrite** — all scripts rewritten from bash to Python. No bash
  remaining.
- **No hardcoded infrastructure** — no IP addresses, no specific tooling
  paths, no AWS-specific calls. Blocker patterns are configurable in
  `loop-state.json["blocker_patterns"]`.
- **Submodule-ready** — designed to be added as `.loop` to any repo.
  `REPO_DIR` is derived from `Path(__file__).resolve().parent.parent.parent`
  so scripts always find the consuming repo root regardless of where the
  submodule is mounted.
- **98 passing tests** — unit tests for all pure logic, integration tests for
  `loop_reset.py`, `loop_ack.py`, `loop_status.py`, and
  `trim_loop_context.py` against real temp git repos.
- **81% coverage on `lib.py`** — all state-transition logic, dependency
  resolution, blocker detection, and result parsing is tested. The 19% not
  covered is git network I/O (`git_pull`, `push_run_state`) which requires
  live git remotes.

### What is NOT done yet

1. **`loop-start-sender` / `loop-start-receiver` split** — currently there is
   only `loop-start` which starts `loop_resilient.py` (the sender/runner).
   The receiver (`opencode_loop.py`) has no make target. These need to become:
   - `make loop-start-sender` — starts `loop_resilient.py` in tmux
   - `make loop-start-receiver` — starts `opencode_loop.py` in tmux
   - `make loop-attach` — attaches to whichever session is running on this
     machine

2. **`loop-stop` / `loop-pause` signals** — the state functions exist in
   `lib.py` (`should_stop`, `should_pause`, `apply_stop_signal`,
   `apply_pause_signal`, `clear_signals`) and `loop.py` already reads the
   stop/pause signals on every iteration. But there are no scripts or make
   targets to actually _write_ those signals and push them. Needed:
   - `bin/loop_stop.py` — kills local tmux immediately, commits `stop=true`
     to git so the other machine exits on next pull
   - `bin/loop_pause.py` — kills local tmux, commits `pause=true` to git so
     the other machine finishes its current target then waits
   - `make loop-stop` — currently just kills local tmux, does not signal the
     other machine
   - `make loop-pause` — does not exist yet

3. **`loop-context.md` auto-creation** — this file must exist in the consuming
   repo for `opencode_loop.py` to work (OpenCode reads and appends to it).
   Currently the loop does not create it. On first run against a repo that
   doesn't have it, OpenCode will fail to read it and may produce garbage
   output. Needs: `loop.py` (or `loop_resilient.py`) creates a minimal
   `loop-context.md` at startup if it doesn't exist, seeded from
   `loop-state.json` (target table, max attempts).

4. **`loop-status.py` (old stub)** — `bin/loop-status.py` (with a hyphen) is
   a dead file left over from the bash era. It has 0% coverage and should be
   deleted.

5. **`stop` and `pause` fields missing from `loop-state.json` template** —
   the template `loop-state.json` doesn't include `stop` or `pause` fields.
   They should be present and default to `false` so the schema is explicit.

6. **README still references old make targets** — the README still shows
   `loop-start` instead of the planned `loop-start-sender` /
   `loop-start-receiver`. Needs updating once those targets are built.

---

## File map

### In this repo (`.loop/`)

| File | Purpose |
|---|---|
| `bin/lib.py` | Shared library — all pure logic: state I/O, git ops, dep resolution, blocker detection, signal helpers, state transitions |
| `bin/loop.py` | Core loop — runs `make <target>` in dep order, retries, signals OpenCode on failure |
| `bin/loop_resilient.py` | Crash-resilient wrapper — restarts `loop.py` on crash, pulls latest code first |
| `bin/loop_ack.py` | Acknowledges a `needs_human` pause — sets `fix_pushed=true`, resumes loop |
| `bin/loop_reset.py` | Resets all state — clears completed/failed/attempts, pushes to git |
| `bin/loop_status.py` | Prints current status from `loop-run-state.json` |
| `bin/opencode_loop.py` | Receiver — polls for failures, invokes OpenCode to fix, pushes fix signal |
| `bin/trim_loop_context.py` | Caps `loop-context.md` at 500 lines, archives overflow |
| `bin/loop-status.py` | **Dead file** — leftover stub, should be deleted |
| `Makefile` | Provides `loop-start`, `loop-stop`, `loop-attach`, `loop-status`, `loop-reset`, `loop-test` via `include .loop/Makefile` |
| `loop-state.json` | Template — copy to consuming repo root, edit targets/deps/blocker_patterns |
| `loop-run-state.json` | Template — copy to consuming repo root, written at runtime by `loop.py` |
| `tests/test_loop.py` | 98 tests — unit + integration |

### Generated in the consuming repo

| File | Owner | Purpose |
|---|---|---|
| `loop-state.json` | You + Mac | Config: targets, deps, max attempts, blocker patterns, fix signals |
| `loop-run-state.json` | Runner | Runtime state: completed/failed targets, attempt counts, status |
| `loop-context.md` | You + OpenCode | OpenCode's memory — failure history and fixes. You create it; OpenCode appends to it. Auto-created on startup once #3 above is implemented. |
| `runs/` | Runner | Per-target logs, resilient wrapper log, OpenCode invocation logs |
| `docs/loop-context-archive.md` | Auto | Overflow from `loop-context.md` when it exceeds 500 lines |

---

## Next steps (in order)

1. Delete `bin/loop-status.py` (dead stub)
2. Add `stop` and `pause` to `loop-state.json` template
3. Write `bin/loop_stop.py` and `bin/loop_pause.py`
4. Split `loop-start` → `loop-start-sender` + `loop-start-receiver` in Makefile
5. Add `loop-stop` (with git signal) and `loop-pause` to Makefile
6. Auto-create `loop-context.md` at startup in `loop_resilient.py`
7. Update README to reflect final make targets
8. Write tests for `loop_stop.py` and `loop_pause.py`
