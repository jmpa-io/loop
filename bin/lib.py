"""
bin/lib.py — shared library for the loop scripts.

All state I/O, git operations, dependency resolution, and hardware-blocker
detection live here so every other script imports rather than duplicates them,
and tests can exercise the logic without subprocesses.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Repo root resolution
# ---------------------------------------------------------------------------


def repo_root() -> Path:
    """
    Return the consuming repo root.

    Scripts live at  <repo>/.loop/bin/<script>.py
    So:  Path(__file__).resolve().parent  → .loop/bin
         .parent                           → .loop
         .parent                           → <repo>
    """
    return Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"{_ts()} [loop] {msg}", flush=True)


def log_ok(msg: str) -> None:
    print(f"{_ts()} [loop] ✓ {msg}", flush=True)


def log_fail(msg: str) -> None:
    print(f"{_ts()} [loop] ✗ {msg}", flush=True)


def log_skip(msg: str) -> None:
    print(f"{_ts()} [loop] ⊘ {msg}", flush=True)


def log_wait(msg: str) -> None:
    print(f"{_ts()} [loop] ⏳ {msg}", flush=True)


# ---------------------------------------------------------------------------
# State file I/O
# ---------------------------------------------------------------------------


def load_json(path: Path) -> dict:
    """Load a JSON file, returning {} on any error."""
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_json(path: Path, data: dict) -> None:
    """Write data as indented JSON."""
    path.write_text(json.dumps(data, indent=2))


def load_run_state(repo: Path) -> dict:
    return load_json(repo / "loop-run-state.json")


def load_oc_state(repo: Path) -> dict:
    return load_json(repo / "loop-state.json")


def save_run_state(repo: Path, data: dict) -> None:
    save_json(repo / "loop-run-state.json", data)


def save_oc_state(repo: Path, data: dict) -> None:
    save_json(repo / "loop-state.json", data)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def git(repo: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    """Run a git command in repo, suppress output, never raise by default."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def current_branch(repo: Path) -> str:
    r = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return r.stdout.strip() or "main"


def register_ours_driver(repo: Path) -> None:
    """Register the 'ours' merge driver so .gitattributes merge=ours works."""
    git(repo, "config", "merge.ours.driver", "true")


def git_pull(repo: Path, branch: str) -> None:
    """
    Pull with rebase, preserving completed_targets across the pull.

    Problem: --autostash stashes loop-run-state.json, then the stash-pop
    merges it back. During stash-pop 'our side' is the just-pulled remote
    version, so the stash is discarded and completed_targets can shrink.

    Fix: snapshot the file first, then merge by taking the UNION of
    completed_targets after the pull.
    """
    run_state_path = repo / "loop-run-state.json"
    backup = load_run_state(repo)

    git(
        repo,
        "pull",
        "origin",
        branch,
        "--rebase",
        "--autostash",
        "-X",
        "theirs",
        "--quiet",
    )

    if backup:
        current = load_run_state(repo)
        current_completed = set(current.get("completed_targets", []))
        backup_completed = set(backup.get("completed_targets", []))
        merged_completed = list(backup_completed | current_completed)
        if merged_completed != current.get("completed_targets", []):
            current["completed_targets"] = merged_completed

        all_completed = backup_completed | current_completed
        current_failed = set(current.get("failed_targets", []))
        backup_failed = set(backup.get("failed_targets", []))
        merged_failed = list((current_failed & backup_failed) - all_completed)
        if merged_failed != current.get("failed_targets", []):
            current["failed_targets"] = merged_failed

        save_run_state(repo, current)


def push_run_state(repo: Path, branch: str, message: str) -> None:
    """Stage loop-run-state.json + runs/, commit, pull-rebase, push."""
    git(repo, "add", "loop-run-state.json", "runs/")
    r = git(repo, "diff", "--staged", "--quiet")
    if r.returncode == 0:
        return  # nothing to commit
    git(repo, "commit", "-m", f"loop: {message}")
    git(repo, "pull", "origin", branch, "--rebase", "-X", "theirs", "--quiet")
    r = git(repo, "push", "origin", branch)
    if r.returncode != 0:
        git(repo, "pull", "origin", branch, "--rebase", "-X", "theirs", "--quiet")
        git(repo, "push", "origin", branch)


# ---------------------------------------------------------------------------
# Dependency resolution
# ---------------------------------------------------------------------------


def dep_status(target: str, run_state: dict, oc_state: dict) -> str:
    """
    Return 'ready', 'waiting', or 'blocked' for a target.

    ready   — all declared deps have completed successfully
    blocked — at least one dep has permanently failed (max retries hit)
    waiting — deps exist but haven't completed or failed yet
    """
    deps = oc_state.get("deps", {}).get(target, [])
    completed = set(run_state.get("completed_targets", []))
    failed = set(run_state.get("failed_targets", []))
    if any(d in failed for d in deps):
        return "blocked"
    if all(d in completed for d in deps):
        return "ready"
    return "waiting"


def build_snapshot(run_state: dict, oc_state: dict) -> dict:
    """
    Compute ready / waiting / skipped sets for all targets.

    Cascades blocked status: if a target is skipped because its dep failed,
    its dependents are also skipped.

    Returns:
        {
            "ready":   [targets ready to run],
            "waiting": [(target, reason), ...],
            "skipped": [(target, reason), ...],
        }
    """
    targets = oc_state.get("targets", [])
    deps_map = oc_state.get("deps", {})
    completed = set(run_state.get("completed_targets", []))
    failed = set(run_state.get("failed_targets", []))
    done = completed | failed
    effective_failed = set(failed)

    ready = []
    waiting = []
    skipped = []

    for t in targets:
        if t in done:
            continue
        deps = deps_map.get(t, [])
        blocked_by = [d for d in deps if d in effective_failed]
        pending = [d for d in deps if d not in completed and d not in effective_failed]
        if blocked_by:
            skipped.append((t, "dep failed: " + ",".join(blocked_by)))
            effective_failed.add(t)
        elif pending:
            waiting.append((t, "waiting: " + ",".join(pending)))
        else:
            ready.append(t)

    return {"ready": ready, "waiting": waiting, "skipped": skipped}


def all_targets_done(run_state: dict, oc_state: dict) -> bool:
    """Return True when every target is either completed, failed, or skipped."""
    snapshot = build_snapshot(run_state, oc_state)
    targets = oc_state.get("targets", [])
    completed = set(run_state.get("completed_targets", []))
    failed = set(run_state.get("failed_targets", []))
    skipped = {t for t, _ in snapshot["skipped"]}
    done = completed | failed | skipped
    return all(t in done for t in targets) and not snapshot["ready"]


# ---------------------------------------------------------------------------
# Hardware / blocker detection
# ---------------------------------------------------------------------------


def check_hardware_blocker(log_content: str, patterns: list[dict]) -> str:
    """
    Scan log_content against caller-supplied blocker patterns.

    Each pattern dict has:
        { "pattern": "<regex>", "message": "<human-readable description>" }

    Returns the message of the first match, or "" if none matched.
    These patterns come from loop-state.json["blocker_patterns"] so the loop
    repo itself contains no hardcoded infrastructure assumptions.
    """
    for entry in patterns:
        regex = entry.get("pattern", "")
        message = entry.get("message", "")
        if regex and re.search(regex, log_content):
            return message
    return ""


# ---------------------------------------------------------------------------
# OpenCode result parsing
# ---------------------------------------------------------------------------


def parse_last_word(output: str) -> str:
    """
    Extract the last occurrence of SUCCESS / RETRY / NEEDS_HUMAN from output.
    Falls back to fuzzy keyword scan if no exact token found.
    """
    tokens = [
        w
        for w in re.split(r"[\s\n]+", output)
        if w in ("SUCCESS", "RETRY", "NEEDS_HUMAN")
    ]
    if tokens:
        return tokens[-1]
    lower = output.lower()
    if re.search(r"needs.human|cannot fix|human intervention", lower):
        return "NEEDS_HUMAN"
    if re.search(r"retry|fix.*push|push.*fix", lower):
        return "RETRY"
    if "success" in lower:
        return "SUCCESS"
    return ""


# ---------------------------------------------------------------------------
# Idempotency guard
# ---------------------------------------------------------------------------


def is_already_completed(target: str, run_state: dict) -> bool:
    """True if target is already in completed_targets (prevents re-running)."""
    return target in run_state.get("completed_targets", [])


# ---------------------------------------------------------------------------
# Pure state-transition functions (no I/O — fully testable)
# ---------------------------------------------------------------------------


def apply_target_success(run_state: dict, target: str) -> dict:
    """
    Return an updated run_state reflecting a successful target run.
    Does not write to disk — caller is responsible for saving.
    """
    run = dict(run_state)
    run.setdefault("completed_targets", [])
    if target not in run["completed_targets"]:
        run["completed_targets"] = run["completed_targets"] + [target]
    run.setdefault("failed_targets", [])
    if target in run["failed_targets"]:
        run["failed_targets"] = [t for t in run["failed_targets"] if t != target]
    run.setdefault("attempts", {})
    run["attempts"] = {**run["attempts"], target: 1}
    run["last_result"] = "success"
    run["last_run_log"] = target
    return run


def apply_target_failure(
    run_state: dict, target: str, max_attempts: int
) -> tuple[dict, bool]:
    """
    Return (updated_run_state, permanently_failed).

    permanently_failed is True when attempt count has reached max_attempts,
    in which case the target is added to failed_targets.
    Does not write to disk — caller is responsible for saving.
    """
    run = dict(run_state)
    run.setdefault("attempts", {})
    attempt = run["attempts"].get(target, 1)
    run["attempts"] = {**run["attempts"], target: attempt + 1}
    run["last_result"] = "failed"
    run["last_run_log"] = target

    permanently_failed = attempt >= max_attempts
    if permanently_failed:
        run.setdefault("failed_targets", [])
        if target not in run["failed_targets"]:
            run["failed_targets"] = run["failed_targets"] + [target]
        run["attempts"] = {**run["attempts"], target: 1}

    return run, permanently_failed


def initialise_run_state(existing_run: dict | None, oc_state: dict) -> dict:
    """
    Return a fresh or updated run_state based on oc_state configuration.

    If existing_run is None (first start), returns a blank running state.
    If existing_run is provided (restart), preserves completed/failed targets
    and updates targets + max_attempts from oc_state.
    Does not write to disk — caller is responsible for saving.
    """
    targets = oc_state.get("targets", [])
    max_att = oc_state.get("max_attempts", 10)

    if existing_run is None:
        return {
            "status": "running",
            "targets": targets,
            "completed_targets": [],
            "failed_targets": [],
            "attempts": {},
            "max_attempts": max_att,
            "last_result": None,
            "last_run_log": None,
            "human_action": None,
        }

    run = dict(existing_run)
    run.setdefault("failed_targets", [])
    run.setdefault("attempts", {})
    run["targets"] = targets
    run["max_attempts"] = max_att
    # Only clear needs_human if status is idle/None (genuine fresh start)
    if run.get("status") in ("idle", None):
        run["status"] = "running"
        run["human_action"] = None
    # Remove legacy sequential fields
    for k in ("current_target", "current_index", "current_attempt"):
        run.pop(k, None)
    return run


def apply_stop_signal(oc_state: dict) -> dict:
    """Return updated oc_state with stop signal set."""
    oc = dict(oc_state)
    oc["stop"] = True
    oc.pop("pause", None)
    return oc


def apply_pause_signal(oc_state: dict) -> dict:
    """Return updated oc_state with pause signal set."""
    oc = dict(oc_state)
    oc["pause"] = True
    oc.pop("stop", None)
    return oc


def clear_signals(oc_state: dict) -> dict:
    """Return updated oc_state with stop/pause signals cleared."""
    oc = dict(oc_state)
    oc.pop("stop", None)
    oc.pop("pause", None)
    return oc


def should_stop(oc_state: dict) -> bool:
    """True if a stop signal is set."""
    return bool(oc_state.get("stop"))


def should_pause(oc_state: dict) -> bool:
    """True if a pause signal is set."""
    return bool(oc_state.get("pause"))
