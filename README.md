# loop

Dependency-aware deployment loop with OpenCode self-healing. Drop it into any repo as a `.loop` submodule, include one line in your Makefile, and you get a full autonomous deployment loop.

## How it works

Two processes run on two machines and communicate entirely through git commits — no sockets, no HTTP.

```
[Sender — runner machine]               [Receiver — Mac]
loop.py  ────── git push ──────────►  opencode_loop.py
  runs make targets in dep order          watches sender-state.json for failures
  writes sender-state.json                invokes OpenCode to diagnose + fix
  reads receiver-state.json               pushes fix, sets fix_pushed=true
  retries on fix  ◄──── git pull ──────
```

**File ownership is strict — no shared writes:**

| File | Written by | Read by |
|---|---|---|
| `sender-state.json` | Sender only | Receiver |
| `receiver-state.json` | Receiver only | Sender |

This eliminates merge conflicts. Each machine only writes its own file.

`loop_resilient.py` wraps `loop.py` — if it crashes or exits non-zero, it pulls latest code and restarts automatically. `make loop-start-sender` always runs `loop_resilient.py`.

**Single machine:** Run both `make loop-start-sender` and `make loop-start-receiver` in separate terminals on the same machine — they communicate through the same git repo on disk.

---

## Files generated in your repo

These files are created in your **repo root** (not inside `.loop/`) when the loop runs.

### State files (committed to git)

| File | Owner | What it is |
|---|---|---|
| `receiver-state.json` | Receiver (brain) | Config + signals: target list, dependency graph, max attempts, blocker patterns, fix signals, stop/pause |
| `sender-state.json` | Sender (runner) | Runtime state: which targets completed/failed, attempt counts, current status, human action message |
| `loop-context.md` | You + OpenCode | Shared brain — OpenCode reads this in full on every fix invocation. Auto-created at startup if missing. |

### Generated directories (not committed)

| Path | What it is |
|---|---|
| `runs/` | Per-target run logs written by the sender. Named `<target>-<timestamp>.log`. OpenCode reads these to diagnose failures. Also contains `resilient.log` and `opencode-loop-<timestamp>.log`. Add `runs/` to your `.gitignore`. |
| `docs/loop-context-archive.md` | Auto-generated when `loop-context.md` exceeds 500 lines. Oldest entries are archived here. |

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
cp .loop/receiver-state.json receiver-state.json
cp .loop/sender-state.json sender-state.json
```

Edit `receiver-state.json` — set your `targets`, `deps`, and `max_attempts`.

### 4. Add to .gitattributes

```
receiver-state.json merge=ours
sender-state.json   merge=ours
```

### 5. Add to .gitignore

```
runs/
```

---

## Make targets

| Target | What it does |
|---|---|
| `make loop-start-sender` | Start the sender (runner/executor) in a detached tmux session |
| `make loop-start-receiver` | Start the receiver (OpenCode fixer) in a detached tmux session |
| `make loop-attach` | Attach to the running sender or receiver tmux session |
| `make loop-stop` | Kill local tmux session and signal the remote machine to stop immediately |
| `make loop-pause` | Kill local tmux session and signal the remote machine to pause after current target |
| `make loop-status` | Print current status, completed/failed targets, attempt counts |
| `make loop-reset` | Clear all state — all targets re-run from scratch on next `loop-start-sender` |
| `make loop-ack` | Acknowledge a human action or resume after a pause |
| `make loop-test` | Run the unit test suite |

---

## receiver-state.json schema

```json
{
  "fix_pushed": false,
  "last_fix": null,
  "targets": ["build", "test", "deploy"],
  "deps": {
    "test": ["build"],
    "deploy": ["test"]
  },
  "max_attempts": 10,
  "blocker_patterns": [
    {
      "pattern": "No route to host.*192\\.168\\.1\\.1",
      "message": "NAS is unreachable — check power and network"
    }
  ],
  "stop": false,
  "pause": false
}
```

| Field | Who writes it | Meaning |
|---|---|---|
| `targets` | You | Ordered list of make targets to run |
| `deps` | You | Dependency graph — a target only runs when all its deps have completed |
| `max_attempts` | You | Max retries per target before it is marked permanently failed |
| `blocker_patterns` | You | Regex patterns matched against run logs. On match, loop escalates to `needs_human` instead of retrying. |
| `fix_pushed` | Receiver | Set to true when OpenCode has pushed a fix and the sender should retry |
| `last_fix` | Receiver | Free-text description of the last fix applied |
| `stop` | `loop_stop.py` | Set to true by `make loop-stop` — both sides exit on next pull |
| `pause` | `loop_pause.py` | Set to true by `make loop-pause` — sender finishes current target then waits |

## sender-state.json schema

```json
{
  "status": "idle",
  "targets": [],
  "completed_targets": [],
  "failed_targets": [],
  "attempts": {},
  "max_attempts": 10,
  "last_result": null,
  "last_run_log": null,
  "human_action": null
}
```

| Field | Meaning |
|---|---|
| `status` | `running`, `needs_human`, `completed`, `completed_with_failures`, or `idle` |
| `completed_targets` | Targets that succeeded |
| `failed_targets` | Targets that hit max attempts and are permanently failed |
| `attempts` | Per-target attempt counts |
| `last_result` | `success` or `failed` |
| `last_run_log` | Name of the target that last ran |
| `human_action` | Non-null message when human intervention is required |

---

## Scripts

All scripts live in `.loop/bin/` and are called via the Makefile.

| Script | Runs on | What it does |
|---|---|---|
| `bin/lib.py` | — | Shared library: state I/O, git ops, dependency resolution, blocker detection, result parsing |
| `bin/loop.py` | Sender | Core loop — reads targets from `receiver-state.json`, runs `make <target>` in dependency order, writes results to `sender-state.json`, polls for fix from receiver |
| `bin/loop_resilient.py` | Sender | Crash-resilient wrapper — restarts `loop.py` on crash, pulls latest code first, auto-creates `loop-context.md`. This is what `make loop-start-sender` runs. |
| `bin/opencode_loop.py` | Receiver | Polls `sender-state.json` for failures, invokes OpenCode to diagnose and fix, writes fix signal to `receiver-state.json`. This is what `make loop-start-receiver` runs. |
| `bin/loop_stop.py` | Either | Kills local tmux session, sets `stop=true` in `receiver-state.json`, pushes — both sides exit on next pull |
| `bin/loop_pause.py` | Either | Kills local tmux session, sets `pause=true` in `receiver-state.json`, pushes — sender finishes current target then waits |
| `bin/loop_reset.py` | Either | Resets both state files, commits and pushes — all targets re-run from scratch |
| `bin/loop_status.py` | Either | Prints current status from `sender-state.json` |
| `bin/loop_ack.py` | Either | Sets `fix_pushed=true` in `receiver-state.json`, clears `human_action` in `sender-state.json` — resumes the sender |
| `bin/trim_loop_context.py` | Receiver | Caps `loop-context.md` at 500 lines, archives overflow. Called automatically by `opencode_loop.py` after every fix. |

---

## Requirements

- `python3` (3.9+)
- `git`
- `make`
- `tmux` — required on both machines. Install with `brew install tmux` (macOS) or `apt install tmux` (Linux).
- `opencode` CLI — only needed on the receiver side for `loop-start-receiver`

---

## Updating the submodule

```bash
git submodule update --remote .loop
git add .loop
git commit -m "chore: bump .loop submodule"
```
