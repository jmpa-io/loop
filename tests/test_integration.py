#!/usr/bin/env python3
"""
tests/test_integration.py -- end-to-end integration tests for the loop.

Runs sender.py and receiver.py as real subprocesses communicating through
a real git repo (bare repo acting as remote). No mocking — real git, real
file I/O, real process communication.

Fake binaries on PATH replace real infrastructure:
  fake make   — fails on first call per target, succeeds on second
  fake opencode — outputs RETRY immediately (simulates AI pushing a fix)

LOOP_REPO env var tells lib.repo_root() which directory to use as the
consuming repo root (bypasses the .loop/bin/... submodule path assumption).
FAKE_MAKE_DIR env var tells fake make where to store per-target call counters.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

BIN = Path(__file__).parent.parent / "bin"


# ===========================================================================
# Helpers
# ===========================================================================


def _git(repo: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )


def _setup_repos(tmp: Path) -> tuple:
    """
    Create a bare repo (remote) and two working clones (sender, receiver).
    Returns (bare, sender_repo, receiver_repo).
    """
    bare = tmp / "bare"
    sender_repo = tmp / "sender"
    receiver_repo = tmp / "receiver"

    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)],
        check=True,
        capture_output=True,
    )

    for clone in [sender_repo, receiver_repo]:
        subprocess.run(
            ["git", "clone", str(bare), str(clone)], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(clone), "config", "user.email", "test@test.com"],
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(clone), "config", "user.name", "Test"],
            capture_output=True,
        )

    return bare, sender_repo, receiver_repo


def _seed_state(repo: Path, targets: list, deps: dict = None, max_attempts: int = 3):
    """Commit initial state files to repo and push to origin."""
    receiver = {
        "fix_pushed": False,
        "last_fix": None,
        "targets": targets,
        "deps": deps or {},
        "max_attempts": max_attempts,
        "blocker_patterns": [],
        "stop": False,
        "pause": False,
    }
    sender = {
        "status": "idle",
        "targets": targets,
        "completed_targets": [],
        "failed_targets": [],
        "attempts": {},
        "max_attempts": max_attempts,
        "last_result": None,
        "last_run_log": None,
        "human_action": None,
    }
    (repo / "receiver-state.json").write_text(json.dumps(receiver, indent=2))
    (repo / "sender-state.json").write_text(json.dumps(sender, indent=2))
    (repo / "runs").mkdir(exist_ok=True)
    (repo / "loop-context.md").write_text(
        "# Loop Context\n\n## History\n\n_No failures yet._\n"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init state")
    _git(repo, "push", "origin", "main")


def _make_fake_bin(tmp: Path) -> Path:
    """
    Create a bin/ directory with fake make and opencode scripts.
    fake make uses FAKE_MAKE_DIR to store per-target call counters.
    """
    fake_bin = tmp / "fake_bin"
    fake_bin.mkdir()

    fake_make = fake_bin / "make"
    fake_make.write_text("""\
#!/bin/bash
TARGET="${1:-unknown}"
DIR="${FAKE_MAKE_DIR:-/tmp/fake_make_default}"
mkdir -p "$DIR"
COUNTER="$DIR/count_${TARGET}"
if [ -f "$COUNTER" ]; then
    echo "[fake_make] $TARGET: second call — SUCCESS"
    exit 0
else
    touch "$COUNTER"
    echo "[fake_make] $TARGET: first call — FAIL" >&2
    exit 1
fi
""")
    fake_make.chmod(0o755)

    fake_opencode = fake_bin / "opencode"
    fake_opencode.write_text("""\
#!/bin/bash
echo "Fake OpenCode: simulating a fix..."
echo "RETRY"
""")
    fake_opencode.chmod(0o755)

    return fake_bin


def _make_env(repo: Path, fake_bin: Path, fake_make_dir: Path = None) -> dict:
    """Build subprocess env with LOOP_REPO, fake PATH, and optional FAKE_MAKE_DIR."""
    env = os.environ.copy()
    env["PATH"] = str(fake_bin) + ":" + env.get("PATH", "")
    env["PYTHONPATH"] = str(BIN)
    env["LOOP_REPO"] = str(repo)
    env["LOOP_POLL_INTERVAL"] = "1"  # fast polling for tests
    if fake_make_dir:
        env["FAKE_MAKE_DIR"] = str(fake_make_dir)
    return env


def _run(script: Path, env: dict, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a Python script as a subprocess."""
    return subprocess.run(
        [sys.executable, str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ===========================================================================
# Sender alone
# ===========================================================================


class TestSenderAlone(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _, self.sender_repo, _ = _setup_repos(self.tmp)
        self.fake_bin = _make_fake_bin(self.tmp)
        self.make_dir = self.tmp / "make_counters"
        self.make_dir.mkdir()
        _seed_state(self.sender_repo, targets=["build"])

    def test_completes_target_that_always_succeeds(self):
        """make succeeds immediately — sender exits 0, build in completed_targets."""
        # Pre-seed counter so fake_make succeeds on first call
        (self.make_dir / "count_build").touch()

        env = _make_env(self.sender_repo, self.fake_bin, self.make_dir)
        result = _run(BIN / "sender.py", env, timeout=30)

        self.assertEqual(
            0, result.returncode, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        sender = json.loads((self.sender_repo / "sender-state.json").read_text())
        self.assertIn("build", sender["completed_targets"])
        self.assertEqual("completed", sender["status"])

    def test_exits_1_with_no_targets(self):
        """sender.py exits 1 immediately if receiver-state.json has no targets."""
        rc = json.loads((self.sender_repo / "receiver-state.json").read_text())
        rc["targets"] = []
        (self.sender_repo / "receiver-state.json").write_text(json.dumps(rc, indent=2))
        _git(self.sender_repo, "add", ".")
        _git(self.sender_repo, "commit", "-m", "clear targets")
        _git(self.sender_repo, "push", "origin", "main")

        env = _make_env(self.sender_repo, self.fake_bin, self.make_dir)
        result = _run(BIN / "sender.py", env, timeout=10)

        self.assertEqual(1, result.returncode)
        self.assertIn("no targets", result.stdout + result.stderr)

    def test_fails_permanently_at_max_attempts(self):
        """With max_attempts=1, build fails permanently on first try."""
        rc = json.loads((self.sender_repo / "receiver-state.json").read_text())
        rc["max_attempts"] = 1
        (self.sender_repo / "receiver-state.json").write_text(json.dumps(rc, indent=2))
        _git(self.sender_repo, "add", ".")
        _git(self.sender_repo, "commit", "-m", "max_attempts=1")
        _git(self.sender_repo, "push", "origin", "main")

        # No counter — fake_make will fail
        env = _make_env(self.sender_repo, self.fake_bin, self.make_dir)
        result = _run(BIN / "sender.py", env, timeout=30)

        self.assertEqual(
            0, result.returncode, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        sender = json.loads((self.sender_repo / "sender-state.json").read_text())
        self.assertIn("build", sender["failed_targets"])
        self.assertEqual("completed_with_failures", sender["status"])


# ===========================================================================
# Sender with dependencies
# ===========================================================================


class TestSenderDependencies(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _, self.sender_repo, _ = _setup_repos(self.tmp)
        self.fake_bin = _make_fake_bin(self.tmp)
        self.make_dir = self.tmp / "make_counters"
        self.make_dir.mkdir()

    def test_targets_complete_in_dependency_order(self):
        """build -> test -> deploy all complete in order."""
        _seed_state(
            self.sender_repo,
            targets=["build", "test", "deploy"],
            deps={"test": ["build"], "deploy": ["test"]},
        )
        # Pre-seed all counters — every make call succeeds immediately
        for t in ["build", "test", "deploy"]:
            (self.make_dir / f"count_{t}").touch()

        env = _make_env(self.sender_repo, self.fake_bin, self.make_dir)
        result = _run(BIN / "sender.py", env, timeout=30)

        self.assertEqual(
            0, result.returncode, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        sender = json.loads((self.sender_repo / "sender-state.json").read_text())
        self.assertEqual({"build", "test", "deploy"}, set(sender["completed_targets"]))
        self.assertEqual("completed", sender["status"])

    def test_failed_dep_skips_dependents(self):
        """If build fails permanently, test and deploy are skipped."""
        _seed_state(
            self.sender_repo,
            targets=["build", "test", "deploy"],
            deps={"test": ["build"], "deploy": ["test"]},
            max_attempts=1,
        )
        # No counter — build will fail

        env = _make_env(self.sender_repo, self.fake_bin, self.make_dir)
        result = _run(BIN / "sender.py", env, timeout=30)

        self.assertEqual(
            0, result.returncode, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        sender = json.loads((self.sender_repo / "sender-state.json").read_text())
        self.assertIn("build", sender["failed_targets"])
        self.assertNotIn("test", sender["completed_targets"])
        self.assertNotIn("deploy", sender["completed_targets"])
        self.assertEqual("completed_with_failures", sender["status"])

    def test_independent_targets_both_complete(self):
        """Two independent targets (no deps) both complete."""
        _seed_state(
            self.sender_repo,
            targets=["lint", "test"],
            deps={},
        )
        for t in ["lint", "test"]:
            (self.make_dir / f"count_{t}").touch()

        env = _make_env(self.sender_repo, self.fake_bin, self.make_dir)
        result = _run(BIN / "sender.py", env, timeout=30)

        self.assertEqual(
            0, result.returncode, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        sender = json.loads((self.sender_repo / "sender-state.json").read_text())
        self.assertIn("lint", sender["completed_targets"])
        self.assertIn("test", sender["completed_targets"])


# ===========================================================================
# Sender stop signal
# ===========================================================================


class TestSenderStopSignal(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _, self.sender_repo, _ = _setup_repos(self.tmp)
        self.fake_bin = _make_fake_bin(self.tmp)
        self.make_dir = self.tmp / "make_counters"
        self.make_dir.mkdir()
        # Target that always fails so sender keeps looping
        _seed_state(self.sender_repo, targets=["deploy"], max_attempts=99)

    def test_stop_signal_causes_clean_exit(self):
        """Sender exits 0 when stop=true is written to receiver-state.json."""

        def write_stop_after_delay():
            time.sleep(3)
            rc = json.loads((self.sender_repo / "receiver-state.json").read_text())
            rc["stop"] = True
            (self.sender_repo / "receiver-state.json").write_text(
                json.dumps(rc, indent=2)
            )
            _git(self.sender_repo, "add", ".")
            _git(self.sender_repo, "commit", "-m", "stop signal")

        t = threading.Thread(target=write_stop_after_delay, daemon=True)
        t.start()

        env = _make_env(self.sender_repo, self.fake_bin, self.make_dir)
        result = _run(BIN / "sender.py", env, timeout=30)

        t.join(timeout=5)

        self.assertEqual(
            0, result.returncode, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        self.assertIn("Stop signal", result.stdout + result.stderr)


# ===========================================================================
# Full end-to-end: sender + receiver together
# ===========================================================================


class TestSenderReceiverEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bare, self.sender_repo, self.receiver_repo = _setup_repos(self.tmp)
        self.fake_bin = _make_fake_bin(self.tmp)
        self.make_dir = self.tmp / "make_counters"
        self.make_dir.mkdir()

        _seed_state(self.sender_repo, targets=["build"], max_attempts=3)
        _git(self.receiver_repo, "pull", "origin", "main")

    def test_sender_retries_after_receiver_fix(self):
        """
        Full end-to-end flow:
        1. Sender runs build — fake_make fails on first call
        2. Receiver sees failure, invokes fake opencode (outputs RETRY)
        3. Receiver sets fix_pushed=true in receiver-state.json
        4. Sender polls, sees fix_pushed, retries
        5. fake_make succeeds on second call (counter file exists)
        6. Sender exits 0, build in completed_targets
        """
        # Both sender and receiver share the same FAKE_MAKE_DIR so the
        # counter created by the first sender make call persists for retry
        sender_env = _make_env(self.sender_repo, self.fake_bin, self.make_dir)
        receiver_env = _make_env(self.receiver_repo, self.fake_bin, self.make_dir)

        sender_result = [None]
        receiver_result = [None]
        errors = []

        def run_sender():
            try:
                sender_result[0] = _run(BIN / "sender.py", sender_env, timeout=90)
            except Exception as e:
                errors.append(f"sender: {e}")

        def run_receiver():
            try:
                receiver_result[0] = _run(BIN / "receiver.py", receiver_env, timeout=90)
            except Exception as e:
                errors.append(f"receiver: {e}")

        sender_thread = threading.Thread(target=run_sender, daemon=True)
        receiver_thread = threading.Thread(target=run_receiver, daemon=True)

        sender_thread.start()
        receiver_thread.start()

        sender_thread.join(timeout=95)
        receiver_thread.join(timeout=95)

        self.assertEqual([], errors, f"Errors: {errors}")
        self.assertIsNotNone(sender_result[0], "Sender did not finish")

        s = sender_result[0]
        self.assertEqual(
            0, s.returncode, f"Sender failed:\nstdout:\n{s.stdout}\nstderr:\n{s.stderr}"
        )

        sender = json.loads((self.sender_repo / "sender-state.json").read_text())
        self.assertIn(
            "build",
            sender["completed_targets"],
            f"build not in completed. state: {sender}",
        )
        self.assertEqual("completed", sender["status"])

    def test_receiver_exits_when_sender_completes(self):
        """Receiver exits cleanly (code 0) once sender reaches completed status."""
        # Pre-seed counter so sender completes immediately without needing receiver
        (self.make_dir / "count_build").touch()

        sender_env = _make_env(self.sender_repo, self.fake_bin, self.make_dir)
        receiver_env = _make_env(self.receiver_repo, self.fake_bin, self.make_dir)

        sender_result = [None]
        receiver_result = [None]

        def run_sender():
            sender_result[0] = _run(BIN / "sender.py", sender_env, timeout=30)

        def run_receiver():
            receiver_result[0] = _run(BIN / "receiver.py", receiver_env, timeout=30)

        sender_thread = threading.Thread(target=run_sender, daemon=True)
        receiver_thread = threading.Thread(target=run_receiver, daemon=True)

        sender_thread.start()
        receiver_thread.start()

        sender_thread.join(timeout=35)
        receiver_thread.join(timeout=35)

        self.assertIsNotNone(receiver_result[0], "Receiver did not finish")
        r = receiver_result[0]
        self.assertEqual(
            0,
            r.returncode,
            f"Receiver failed:\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}",
        )
        self.assertIn("Loop complete", r.stdout + r.stderr)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(__import__(__name__))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
