# loop

Dependency-aware deployment loop with OpenCode self-healing.

Runs a set of `make` targets in order, respecting declared dependencies between them. On failure, signals OpenCode to diagnose and push a fix, then retries automatically. Designed to run unattended on a remote machine (e.g. Arch laptop) while OpenCode monitors from a Mac.

## How it works

Two processes run concurrently and communicate via two JSON state files committed to git:

| File | Owner | Purpose |
|---|---|---|
| `loop-state.json` | Mac (OpenCode) | Targets, deps, max_attempts, fix signals |
| `loop-run-state.json` | Runner (laptop) | Runtime state: completed/failed/attempts |

```
[Arch laptop]                        [Mac]
homelab-loop.sh  ──── git push ────► opencode-loop.sh
   runs make targets                    watches for failures
   writes loop-run-state.json           invokes OpenCode to fix
   reads loop-state.json                pushes fix, sets fix_pushed=true
   retries on failure ◄──── git pull ──
```

## Scripts

| Script | Runs on | Purpose |
|---|---|---|
| `bin/homelab-loop.sh` | Runner | Core dependency-aware loop |
| `bin/homelab-loop-resilient.sh` | Runner | Crash-resilient wrapper with AWS refresh |
| `bin/opencode-loop.sh` | Mac | Polls for failures, invokes OpenCode to fix |
| `bin/loop-ack.sh` | Mac | Acknowledges a human action, resumes loop |
| `bin/loop-reset.sh` | Mac | Resets all state so targets re-run from scratch |
| `bin/trim-loop-context.sh` | Mac | Caps `loop-context.md` at 500 lines |

## Usage as a submodule

```bash
# Add to your repo
git submodule add https://github.com/jmpa-io/loop loop

# Reference scripts from your Makefile
homelab-loop:
	@bash loop/bin/homelab-loop.sh

opencode-loop:
	@bash loop/bin/opencode-loop.sh

loop-ack:
	@bash loop/bin/loop-ack.sh

loop-reset:
	@bash loop/bin/loop-reset.sh
```

State files (`loop-state.json`, `loop-run-state.json`) live in your repo root, not inside the submodule.

## loop-state.json

Edit this file in your repo root to configure targets and dependencies:

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

## Requirements

- `bash`, `python3`, `git`
- `make` (targets are invoked via `make <target>`)
- `aws` CLI with SSO profile `jmpa` (for AWS credential checks in `homelab-loop.sh`)
- `opencode` CLI (for `opencode-loop.sh`)
- `tmux` (optional, for detached runs)
