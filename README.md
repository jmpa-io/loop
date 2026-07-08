# loop

Dependency-aware deployment loop with OpenCode self-healing. Drop it into any repo as a `.loop` submodule, include one line in your Makefile, and you get a full autonomous deployment loop.

## What it does

Two processes run concurrently and communicate via two JSON state files committed to git:

```
[Runner — e.g. Arch laptop]              [Mac]
homelab-loop.sh  ──── git push ────►  opencode-loop.sh
  runs make targets in dep order          watches for failures
  writes loop-run-state.json              invokes OpenCode to diagnose + fix
  reads loop-state.json                   pushes fix, sets fix_pushed=true
  retries on fix ◄──── git pull ──────
```

No sockets, HTTP, or side-channels — coordination happens entirely through git commits.

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

That's it. All loop targets are now available in your repo.

### 3. Add state files to your repo root

Copy the template state files:

```bash
cp .loop/loop-state.json loop-state.json
cp .loop/loop-run-state.json loop-run-state.json
```

Edit `loop-state.json` to define your targets and dependencies (see schema below).

### 4. Add merge drivers to .gitattributes

```
# loop-state.json — owned EXCLUSIVELY by Mac (OpenCode).
loop-state.json     merge=ours

# loop-run-state.json — owned EXCLUSIVELY by the runner.
loop-run-state.json merge=ours
```

### 5. Add runs/ to .gitignore

```
runs/
```

---

## Make targets

All targets are provided by `include .loop/Makefile`.

| Target | Runs on | What it does |
|---|---|---|
| `make loop-start` | Runner | Start resilient loop in a detached tmux session |
| `make loop-resilient` | Runner | Run resilient wrapper directly (auto-restart + AWS refresh) |
| `make loop-run` | Runner | Run core loop directly (no auto-restart) |
| `make loop-attach` | Runner | Attach to the running tmux session |
| `make loop-stop` | Runner | Kill the tmux session |
| `make loop-logs` | Either | Tail the latest run log |
| `make loop-status` | Either | Print current status, completed/failed targets, attempts |
| `make loop-ack` | Mac | Acknowledge a human action and resume the loop |
| `make loop-reset` | Mac | Reset all state — all targets re-run from scratch |
| `make loop-opencode` | Mac | Start the OpenCode monitoring loop |
| `make loop-trim-context` | Mac | Trim loop-context.md to 500 lines, archive overflow |

---

## loop-state.json schema

Edit this in your repo root to configure targets and dependencies:

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
  "human_action": null
}
```

| Field | Who writes it | Meaning |
|---|---|---|
| `targets` | You (manually) | Ordered list of make targets to run |
| `deps` | You (manually) | Dependency graph — target only runs when all deps completed |
| `max_attempts` | You (manually) | Max retries per target before permanent failure |
| `fix_pushed` | Mac (opencode-loop.sh) | true when OpenCode has pushed a fix and runner should retry |
| `waiting_for_fix` | Runner (homelab-loop.sh) | true when runner is paused waiting for a fix |
| `opencode_last_fix` | Mac (opencode-loop.sh) | Free-text description of last fix applied |
| `human_action` | Either | Non-null message when human intervention is required |

---

## Scripts

| Script | Runs on | Purpose |
|---|---|---|
| `bin/homelab-loop.sh` | Runner | Core dependency-aware loop |
| `bin/homelab-loop-resilient.sh` | Runner | Crash-resilient wrapper with AWS credential refresh |
| `bin/opencode-loop.sh` | Mac | Polls for failures, invokes OpenCode to fix |
| `bin/loop-ack.sh` | Mac | Acknowledges a human action, resumes loop |
| `bin/loop-reset.sh` | Mac | Resets all state so targets re-run from scratch |
| `bin/trim-loop-context.sh` | Mac | Caps loop-context.md at 500 lines |

---

## Requirements

- `bash`, `python3`, `git`
- `make` (targets are invoked via `make <target>`)
- `aws` CLI with SSO profile `jmpa` (for AWS credential checks)
- `opencode` CLI (for `make loop-opencode`)
- `tmux` (for `make loop-start`)

---

## Updating the submodule

```bash
git submodule update --remote .loop
git add .loop
git commit -m "chore: bump loop submodule"
```
