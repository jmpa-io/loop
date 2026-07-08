# loop

Dependency-aware deployment loop with OpenCode self-healing. Drop it into any repo as a `.loop` submodule, include one line in your Makefile, and you get a full autonomous deployment loop.

## How it works

Two processes run concurrently and communicate entirely through git commits — no sockets, no HTTP.

```
[Runner machine]                         [Mac]
loop.py  ──────── git push ──────────►  opencode_loop.py
  runs make targets in dep order           watches for failures
  writes loop-run-state.json               invokes OpenCode to diagnose + fix
  reads loop-state.json                    pushes fix, sets fix_pushed=true
  retries on fix  ◄──── git pull ───────
```

`loop_resilient.py` wraps `loop.py` — if it crashes or exits non-zero, it pulls latest code and restarts it automatically. `make loop-start` always runs `loop_resilient.py`.

---

## Files generated in your repo

These files are created in your **repo root** (not inside `.loop/`) when the loop runs.

### State files (committed to git)

| File | Owner | What it is |
|---|---|---|
| `loop-state.json` | You + Mac | Configuration: target list, dependency graph, max attempts, blocker patterns, and fix signals written by OpenCode |
| `loop-run-state.json` | Runner | Runtime state: which targets completed/failed, attempt counts, current status, human action message |
| `loop-context.md` | You + OpenCode | Shared brain — OpenCode reads this in full on every fix invocation. Contains the target table, known issues, and a history of every failure + fix applied. Prevents OpenCode repeating the same broken fix twice. You create this file; OpenCode appends to it. |

### Generated directories (not committed)

| Path | What it is |
|---|---|
| `runs/` | Per-target run logs written by the runner. Named `<target>-<timestamp>.log`. OpenCode reads these to diagnose failures. Also contains `resilient.log` (output of the resilient wrapper) and `opencode-loop-<timestamp>.log` (output of each OpenCode fix invocation). Add `runs/` to your `.gitignore`. |
| `docs/loop-context-archive.md` | Auto-generated when `loop-context.md` exceeds 500 lines. The oldest entries are moved here to keep the active context file small enough for OpenCode to read in full without burning tokens. |

---

## Adding to your repo

### 1. Add the submodule

```bash
git submodule add https://github.com/jmpa-io/loop .loop
git submodule update --init --recursive
```

### 2. Add to your Makefile

```makefile
include .loop/Makefile
```

All loop make targets are now available.

### 3. Copy the template state files

```bash
cp .loop/loop-state.json loop-state.json
cp .loop/loop-run-state.json loop-run-state.json
```

Edit `loop-state.json` — set your `targets`, `deps`, and `max_attempts`.

### 4. Create loop-context.md

Create a `loop-context.md` in your repo root. Minimum content:

```markdown
# Deployment Loop — Shared Context

## Current State
- Status: idle

## Target Queue
| Target | Depends on | Description |
|--------|-----------|-------------|
| `build` | — | Build the project |
| `test` | `build` | Run tests |
| `deploy` | `test` | Deploy to production |

## OpenCode Instructions
Read this file before every fix. Do not repeat a fix already listed below.

## Loop Parameters
- Max attempts per target: 10
- Context file size cap: 500 lines (overflow archived to docs/loop-context-archive.md)

## Known Issues & Fixes Applied
<!-- OpenCode appends entries here after every fix -->
```

### 5. Add to .gitattributes

```
loop-state.json     merge=ours
loop-run-state.json merge=ours
```

### 6. Add to .gitignore

```
runs/
```

---

## Make targets

| Target | What it does |
|---|---|
| `make loop-start` | Start the resilient loop in a detached tmux session |
| `make loop-stop` | Kill the tmux session |
| `make loop-attach` | Attach to the running tmux session to watch output |
| `make loop-status` | Print current status, completed/failed targets, attempt counts |
| `make loop-reset` | Clear all state — all targets re-run from scratch on next `loop-start` |
| `make loop-test` | Run the unit test suite |

---

## loop-state.json schema

```json
{
  "fix_pushed": false,
  "waiting_for_fix": false,
  "opencode_last_fix": null,
  "targets": ["build", "test", "deploy"],
  "deps": {
    "test": ["build"],
    "deploy": ["test"]
  },
  "max_attempts": 10,
  "human_action": null,
  "blocker_patterns": [
    {
      "pattern": "No route to host.*192\\.168\\.1\\.1",
      "message": "NAS is unreachable — check power and network"
    }
  ]
}
```

| Field | Who writes it | Meaning |
|---|---|---|
| `targets` | You | Ordered list of make targets to run |
| `deps` | You | Dependency graph — a target only runs when all its deps have completed |
| `max_attempts` | You | Max retries per target before it is marked permanently failed |
| `blocker_patterns` | You | Regex patterns to match against run logs. On match, loop pauses immediately and asks for human intervention instead of retrying. Add your own infrastructure-specific patterns here. |
| `fix_pushed` | Mac (`opencode_loop.py`) | Set to true when OpenCode has pushed a fix and the runner should retry |
| `waiting_for_fix` | Runner (`loop.py`) | Set to true when the runner is paused waiting for a fix |
| `opencode_last_fix` | Mac (`opencode_loop.py`) | Free-text description of the last fix applied |
| `human_action` | Either | Non-null message when human intervention is required |

---

## Scripts

All scripts live in `.loop/bin/` and are called via the Makefile. You do not need to call them directly.

| Script | Runs on | What it does |
|---|---|---|
| `bin/lib.py` | — | Shared library: state I/O, git ops, dependency resolution, blocker detection, result parsing. Imported by all other scripts. |
| `bin/loop.py` | Runner | Core loop — reads targets from `loop-state.json`, runs `make <target>` in dependency order, retries on failure, signals OpenCode when stuck |
| `bin/loop_resilient.py` | Runner | Wrapper around `loop.py` — restarts it if it crashes, pulls latest code before each restart. This is what `make loop-start` runs. |
| `bin/loop_reset.py` | Mac | Resets both state files to blank, commits and pushes — all targets will re-run from scratch |
| `bin/loop_status.py` | Either | Prints current status, completed/failed targets, attempt counts from `loop-run-state.json` |
| `bin/loop_ack.py` | Mac | Acknowledges a `needs_human` pause — sets `fix_pushed=true` and resumes the loop. Used when a human action was required (e.g. credential refresh). |
| `bin/opencode_loop.py` | Mac | Polls `loop-run-state.json` for failures, invokes OpenCode to diagnose and fix, pushes the fix, and sets `fix_pushed=true` so the runner retries |
| `bin/trim_loop_context.py` | Mac | Caps `loop-context.md` at 500 lines — archives overflow to `docs/loop-context-archive.md`. Called automatically by `opencode_loop.py` after every fix. |

---

## Requirements

- `python3` (3.9+)
- `git`
- `make` (targets invoked via `make <target>`)
- `tmux` (for `make loop-start`)
- `opencode` CLI (for `opencode_loop.py` — only needed on the Mac side)

---

## Updating the submodule

```bash
git submodule update --remote .loop
git add .loop
git commit -m "chore: bump .loop submodule"
```
