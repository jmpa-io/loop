#!/usr/bin/env python3
"""
tests/test_loop.py -- unit and integration tests for the loop library.

Unit tests:   exercise lib.py functions directly (no I/O, no subprocesses).
Integration:  run script main() functions against a real temp git repo,
              verifying state files are written correctly.

Run with:
    python3 tests/test_loop.py
    make loop-test
"""

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

# Allow importing from bin/
sys.path.insert(0, str(Path(__file__).parent.parent / "bin"))
import lib

BIN = Path(__file__).parent.parent / "bin"
LOOP_REPO = Path(__file__).parent.parent


# ===========================================================================
# Helpers
# ===========================================================================


def _make_git_repo(path: Path) -> Path:
    """
    Create a minimal git repo at path with both state files committed.
    Returns the repo path.
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(path)], capture_output=True, check=True
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test.com"],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        capture_output=True,
        check=True,
    )

    receiver = {
        "fix_pushed": False,
        "last_fix": None,
        "targets": ["a", "b"],
        "deps": {"b": ["a"]},
        "max_attempts": 3,
        "blocker_patterns": [],
        "stop": False,
        "pause": False,
    }
    sender = {
        "status": "idle",
        "targets": ["a", "b"],
        "completed_targets": [],
        "failed_targets": [],
        "attempts": {},
        "max_attempts": 3,
        "last_result": None,
        "last_run_log": None,
        "human_action": None,
    }
    (path / "receiver-state.json").write_text(json.dumps(receiver, indent=2))
    (path / "sender-state.json").write_text(json.dumps(sender, indent=2))

    subprocess.run(["git", "-C", str(path), "add", "."], capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "init"],
        capture_output=True,
        check=True,
    )
    return path


# ===========================================================================
# Unit: dependency resolution
# ===========================================================================


class TestDepStatus(unittest.TestCase):
    def setUp(self):
        self.receiver = {
            "targets": ["a", "b", "c", "d"],
            "deps": {"b": ["a"], "c": ["a"], "d": ["b", "c"]},
        }

    def test_no_deps_is_ready(self):
        sender = {"completed_targets": [], "failed_targets": []}
        self.assertEqual("ready", lib.dep_status("a", sender, self.receiver))

    def test_dep_completed_is_ready(self):
        sender = {"completed_targets": ["a"], "failed_targets": []}
        self.assertEqual("ready", lib.dep_status("b", sender, self.receiver))
        self.assertEqual("ready", lib.dep_status("c", sender, self.receiver))

    def test_dep_not_completed_is_waiting(self):
        sender = {"completed_targets": [], "failed_targets": []}
        self.assertEqual("waiting", lib.dep_status("b", sender, self.receiver))

    def test_dep_failed_is_blocked(self):
        sender = {"completed_targets": [], "failed_targets": ["a"]}
        self.assertEqual("blocked", lib.dep_status("b", sender, self.receiver))
        self.assertEqual("blocked", lib.dep_status("c", sender, self.receiver))

    def test_multi_dep_one_failed_is_blocked(self):
        sender = {"completed_targets": ["b"], "failed_targets": ["c"]}
        self.assertEqual("blocked", lib.dep_status("d", sender, self.receiver))

    def test_multi_dep_both_completed_is_ready(self):
        sender = {"completed_targets": ["b", "c"], "failed_targets": []}
        self.assertEqual("ready", lib.dep_status("d", sender, self.receiver))

    def test_unknown_target_no_deps_is_ready(self):
        sender = {"completed_targets": [], "failed_targets": []}
        self.assertEqual("ready", lib.dep_status("unknown", sender, self.receiver))


# ===========================================================================
# Unit: snapshot / all-done
# ===========================================================================


class TestBuildSnapshot(unittest.TestCase):
    def setUp(self):
        self.receiver = {
            "targets": ["a", "b", "c", "d"],
            "deps": {"b": ["a"], "c": ["a"], "d": ["b", "c"]},
        }

    def test_nothing_done_a_is_ready(self):
        sender = {"completed_targets": [], "failed_targets": []}
        snap = lib.build_snapshot(sender, self.receiver)
        self.assertIn("a", snap["ready"])
        self.assertEqual([], snap["skipped"])

    def test_a_completed_b_and_c_ready(self):
        sender = {"completed_targets": ["a"], "failed_targets": []}
        snap = lib.build_snapshot(sender, self.receiver)
        self.assertIn("b", snap["ready"])
        self.assertIn("c", snap["ready"])

    def test_a_failed_b_c_d_skipped(self):
        sender = {"completed_targets": [], "failed_targets": ["a"]}
        snap = lib.build_snapshot(sender, self.receiver)
        skipped = [t for t, _ in snap["skipped"]]
        self.assertIn("b", skipped)
        self.assertIn("c", skipped)
        self.assertIn("d", skipped)

    def test_d_waiting_on_b_and_c(self):
        sender = {"completed_targets": ["a"], "failed_targets": []}
        snap = lib.build_snapshot(sender, self.receiver)
        waiting = [t for t, _ in snap["waiting"]]
        self.assertIn("d", waiting)


class TestAllTargetsDone(unittest.TestCase):
    def setUp(self):
        self.receiver = {"targets": ["a", "b", "c"], "deps": {"b": ["a"], "c": ["b"]}}

    def test_nothing_done_not_complete(self):
        sender = {"completed_targets": [], "failed_targets": []}
        self.assertFalse(lib.all_targets_done(sender, self.receiver))

    def test_all_completed_is_done(self):
        sender = {"completed_targets": ["a", "b", "c"], "failed_targets": []}
        self.assertTrue(lib.all_targets_done(sender, self.receiver))

    def test_failed_dep_cascades_to_done(self):
        sender = {"completed_targets": [], "failed_targets": ["a"]}
        self.assertTrue(lib.all_targets_done(sender, self.receiver))

    def test_partial_not_done(self):
        sender = {"completed_targets": ["a"], "failed_targets": []}
        self.assertFalse(lib.all_targets_done(sender, self.receiver))


# ===========================================================================
# Unit: idempotency guard
# ===========================================================================


class TestIsAlreadyCompleted(unittest.TestCase):
    def test_completed_returns_true(self):
        self.assertTrue(lib.is_already_completed("x", {"completed_targets": ["x"]}))

    def test_incomplete_returns_false(self):
        self.assertFalse(lib.is_already_completed("x", {"completed_targets": ["y"]}))

    def test_failed_returns_false(self):
        self.assertFalse(
            lib.is_already_completed(
                "x", {"completed_targets": [], "failed_targets": ["x"]}
            )
        )

    def test_empty_returns_false(self):
        self.assertFalse(lib.is_already_completed("x", {}))


# ===========================================================================
# Unit: sender_needs_fix
# ===========================================================================


class TestSenderNeedsFix(unittest.TestCase):
    def test_failed_running_needs_fix(self):
        sender = {"last_result": "failed", "status": "running"}
        self.assertTrue(lib.sender_needs_fix(sender))

    def test_failed_needs_human_no_fix(self):
        sender = {"last_result": "failed", "status": "needs_human"}
        self.assertFalse(lib.sender_needs_fix(sender))

    def test_failed_completed_no_fix(self):
        sender = {"last_result": "failed", "status": "completed"}
        self.assertFalse(lib.sender_needs_fix(sender))

    def test_success_no_fix(self):
        sender = {"last_result": "success", "status": "running"}
        self.assertFalse(lib.sender_needs_fix(sender))

    def test_empty_state_no_fix(self):
        self.assertFalse(lib.sender_needs_fix({}))


# ===========================================================================
# Unit: state transitions
# ===========================================================================


class TestApplyTargetSuccess(unittest.TestCase):
    def _base(self):
        return {
            "completed_targets": [],
            "failed_targets": [],
            "attempts": {},
            "last_result": None,
            "last_run_log": None,
        }

    def test_adds_to_completed(self):
        result = lib.apply_target_success(self._base(), "build")
        self.assertIn("build", result["completed_targets"])

    def test_removes_from_failed(self):
        run = {**self._base(), "failed_targets": ["build"]}
        result = lib.apply_target_success(run, "build")
        self.assertNotIn("build", result["failed_targets"])

    def test_resets_attempt_to_1(self):
        run = {**self._base(), "attempts": {"build": 5}}
        result = lib.apply_target_success(run, "build")
        self.assertEqual(1, result["attempts"]["build"])

    def test_sets_last_result_and_log(self):
        result = lib.apply_target_success(self._base(), "build")
        self.assertEqual("success", result["last_result"])
        self.assertEqual("build", result["last_run_log"])

    def test_idempotent_double_success(self):
        run = lib.apply_target_success(self._base(), "build")
        run2 = lib.apply_target_success(run, "build")
        self.assertEqual(1, run2["completed_targets"].count("build"))

    def test_does_not_mutate_input(self):
        base = self._base()
        lib.apply_target_success(base, "build")
        self.assertEqual([], base["completed_targets"])


class TestApplyTargetFailure(unittest.TestCase):
    def _base(self, attempts=None):
        return {
            "completed_targets": [],
            "failed_targets": [],
            "attempts": attempts or {},
            "last_result": None,
            "last_run_log": None,
        }

    def test_increments_attempt(self):
        run, perm = lib.apply_target_failure(self._base({"build": 1}), "build", 3)
        self.assertEqual(2, run["attempts"]["build"])
        self.assertFalse(perm)

    def test_first_attempt_starts_at_2(self):
        run, perm = lib.apply_target_failure(self._base(), "build", 3)
        self.assertEqual(2, run["attempts"]["build"])

    def test_sets_last_result_failed(self):
        run, _ = lib.apply_target_failure(self._base(), "build", 3)
        self.assertEqual("failed", run["last_result"])
        self.assertEqual("build", run["last_run_log"])

    def test_not_permanently_failed_below_max(self):
        run, perm = lib.apply_target_failure(self._base({"build": 2}), "build", 5)
        self.assertFalse(perm)
        self.assertNotIn("build", run["failed_targets"])

    def test_permanently_failed_at_max(self):
        run, perm = lib.apply_target_failure(self._base({"build": 3}), "build", 3)
        self.assertTrue(perm)
        self.assertIn("build", run["failed_targets"])

    def test_permanently_failed_resets_attempt_to_1(self):
        run, perm = lib.apply_target_failure(self._base({"build": 3}), "build", 3)
        self.assertTrue(perm)
        self.assertEqual(1, run["attempts"]["build"])

    def test_does_not_mutate_input(self):
        base = self._base({"build": 1})
        lib.apply_target_failure(base, "build", 3)
        self.assertEqual(1, base["attempts"]["build"])


class TestInitialiseSenderState(unittest.TestCase):
    def _receiver(self, targets=None):
        return {"targets": targets or ["a", "b"], "max_attempts": 5}

    def test_fresh_start_returns_running(self):
        sender = lib.initialise_sender_state(None, self._receiver())
        self.assertEqual("running", sender["status"])
        self.assertEqual([], sender["completed_targets"])
        self.assertEqual([], sender["failed_targets"])
        self.assertEqual({}, sender["attempts"])

    def test_fresh_start_inherits_targets(self):
        sender = lib.initialise_sender_state(None, self._receiver(["x", "y"]))
        self.assertEqual(["x", "y"], sender["targets"])

    def test_fresh_start_inherits_max_attempts(self):
        sender = lib.initialise_sender_state(None, self._receiver())
        self.assertEqual(5, sender["max_attempts"])

    def test_restart_preserves_completed(self):
        existing = {
            "status": "running",
            "completed_targets": ["a"],
            "failed_targets": [],
            "attempts": {},
        }
        sender = lib.initialise_sender_state(existing, self._receiver())
        self.assertIn("a", sender["completed_targets"])

    def test_restart_idle_becomes_running(self):
        existing = {
            "status": "idle",
            "completed_targets": [],
            "failed_targets": [],
            "attempts": {},
        }
        sender = lib.initialise_sender_state(existing, self._receiver())
        self.assertEqual("running", sender["status"])

    def test_restart_needs_human_preserved(self):
        existing = {
            "status": "needs_human",
            "human_action": "fix me",
            "completed_targets": [],
            "failed_targets": [],
            "attempts": {},
        }
        sender = lib.initialise_sender_state(existing, self._receiver())
        self.assertEqual("needs_human", sender["status"])
        self.assertEqual("fix me", sender["human_action"])

    def test_restart_removes_legacy_fields(self):
        existing = {
            "status": "running",
            "current_target": "x",
            "current_index": 1,
            "current_attempt": 2,
            "completed_targets": [],
            "failed_targets": [],
            "attempts": {},
        }
        sender = lib.initialise_sender_state(existing, self._receiver())
        self.assertNotIn("current_target", sender)
        self.assertNotIn("current_index", sender)
        self.assertNotIn("current_attempt", sender)

    def test_does_not_mutate_existing(self):
        existing = {
            "status": "idle",
            "completed_targets": ["a"],
            "failed_targets": [],
            "attempts": {},
        }
        lib.initialise_sender_state(existing, self._receiver())
        self.assertEqual("idle", existing["status"])


# ===========================================================================
# Unit: stop / pause signals
# ===========================================================================


class TestSignals(unittest.TestCase):
    def test_should_stop_true(self):
        self.assertTrue(lib.should_stop({"stop": True}))

    def test_should_stop_false(self):
        self.assertFalse(lib.should_stop({"stop": False}))
        self.assertFalse(lib.should_stop({}))

    def test_should_pause_true(self):
        self.assertTrue(lib.should_pause({"pause": True}))

    def test_should_pause_false(self):
        self.assertFalse(lib.should_pause({"pause": False}))
        self.assertFalse(lib.should_pause({}))

    def test_apply_stop_sets_stop_clears_pause(self):
        rc = lib.apply_stop_signal({"pause": True})
        self.assertTrue(rc["stop"])
        self.assertNotIn("pause", rc)

    def test_apply_pause_sets_pause_clears_stop(self):
        rc = lib.apply_pause_signal({"stop": True})
        self.assertTrue(rc["pause"])
        self.assertNotIn("stop", rc)

    def test_clear_signals_removes_both(self):
        rc = lib.clear_signals({"stop": True, "pause": True, "targets": []})
        self.assertNotIn("stop", rc)
        self.assertNotIn("pause", rc)
        self.assertIn("targets", rc)

    def test_signal_functions_do_not_mutate_input(self):
        orig = {"stop": False}
        lib.apply_stop_signal(orig)
        self.assertFalse(orig["stop"])


# ===========================================================================
# Unit: blocker detection
# ===========================================================================


class TestCheckHardwareBlocker(unittest.TestCase):
    def _patterns(self):
        return [
            {
                "pattern": r"No route to host.*10\.0\.1\.1|NAS.*unreachable",
                "message": "NAS is unreachable",
            },
            {"pattern": r"No space left on device", "message": "Disk full"},
            {
                "pattern": r"USB.*disconnect|usbcore.*disconnect",
                "message": "USB disconnected",
            },
            {
                "pattern": r"Hardware Error|Machine Check Exception",
                "message": "Hardware error",
            },
        ]

    def test_nas_unreachable(self):
        self.assertIn(
            "NAS",
            lib.check_hardware_blocker(
                "No route to host 10.0.1.1 port 22", self._patterns()
            ),
        )

    def test_disk_full(self):
        self.assertIn(
            "Disk",
            lib.check_hardware_blocker(
                "No space left on device: '/var'", self._patterns()
            ),
        )

    def test_usb_disconnect(self):
        self.assertIn(
            "USB",
            lib.check_hardware_blocker("usb 3-1: USB disconnect", self._patterns()),
        )

    def test_hardware_error(self):
        self.assertIn(
            "Hardware",
            lib.check_hardware_blocker(
                "Hardware Error: Machine Check Exception", self._patterns()
            ),
        )

    def test_normal_failure_no_match(self):
        self.assertEqual(
            "",
            lib.check_hardware_blocker(
                "FAILED! SSH timeout on 10.0.3.60", self._patterns()
            ),
        )

    def test_empty_log_no_match(self):
        self.assertEqual("", lib.check_hardware_blocker("", self._patterns()))

    def test_empty_patterns_no_match(self):
        self.assertEqual(
            "", lib.check_hardware_blocker("No route to host 10.0.1.1", [])
        )

    def test_first_match_wins(self):
        p = [
            {"pattern": r"AAA", "message": "first"},
            {"pattern": r"AAA", "message": "second"},
        ]
        self.assertEqual("first", lib.check_hardware_blocker("AAA", p))


# ===========================================================================
# Unit: OpenCode result parsing
# ===========================================================================


class TestParseLastWord(unittest.TestCase):
    def test_retry_at_end(self):
        self.assertEqual("RETRY", lib.parse_last_word("Fixed the bug.\nRETRY"))

    def test_success_at_end(self):
        self.assertEqual("SUCCESS", lib.parse_last_word("All good.\nSUCCESS"))

    def test_needs_human_at_end(self):
        self.assertEqual("NEEDS_HUMAN", lib.parse_last_word("Cannot fix.\nNEEDS_HUMAN"))

    def test_last_token_wins(self):
        self.assertEqual(
            "NEEDS_HUMAN", lib.parse_last_word("Tried RETRY but then\nNEEDS_HUMAN")
        )

    def test_empty_returns_empty(self):
        self.assertEqual("", lib.parse_last_word(""))

    def test_fuzzy_needs_human(self):
        self.assertEqual(
            "NEEDS_HUMAN", lib.parse_last_word("This requires human intervention")
        )

    def test_fuzzy_retry(self):
        self.assertEqual(
            "RETRY", lib.parse_last_word("fix has been pushed to the repo")
        )

    def test_fuzzy_success(self):
        self.assertEqual(
            "SUCCESS", lib.parse_last_word("the target was already success")
        )


# ===========================================================================
# Unit: state file I/O
# ===========================================================================


class TestStateIO(unittest.TestCase):
    def test_load_json_missing_returns_empty(self):
        self.assertEqual({}, lib.load_json(Path("/tmp/does-not-exist-ever.json")))

    def test_load_json_invalid_returns_empty(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not valid json {{{{")
            name = f.name
        self.assertEqual({}, lib.load_json(Path(name)))

    def test_save_and_load_roundtrip(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            name = f.name
        data = {"targets": ["a", "b"], "max_attempts": 5}
        lib.save_json(Path(name), data)
        self.assertEqual(data, lib.load_json(Path(name)))

    def test_save_json_pretty_printed(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            name = f.name
        lib.save_json(Path(name), {"k": "v"})
        content = Path(name).read_text()
        self.assertIn("\n", content)


# ===========================================================================
# Unit: opencode_loop -- should_invoke_opencode (pure function)
# ===========================================================================


class TestShouldInvokeOpencode(unittest.TestCase):
    def setUp(self):
        import opencode_loop

        self.fn = opencode_loop.should_invoke_opencode

    def _sender(
        self, status="running", last_result="failed", target="build", completed=None
    ):
        return {
            "status": status,
            "last_result": last_result,
            "last_run_log": target,
            "completed_targets": completed or [],
            "failed_targets": [],
        }

    def _receiver(self, fix_pushed=False):
        return {"fix_pushed": fix_pushed}

    def test_failed_target_should_invoke(self):
        ok, reason = self.fn(self._sender(), self._receiver(), "", "build-123.log")
        self.assertTrue(ok, reason)

    def test_completed_loop_should_not_invoke(self):
        ok, reason = self.fn(
            self._sender(status="completed"), self._receiver(), "", "build-123.log"
        )
        self.assertFalse(ok)
        self.assertIn("complete", reason)

    def test_needs_human_should_not_invoke(self):
        ok, reason = self.fn(
            self._sender(status="needs_human"), self._receiver(), "", "build-123.log"
        )
        self.assertFalse(ok)
        self.assertIn("human", reason)

    def test_non_failure_should_not_invoke(self):
        ok, reason = self.fn(
            self._sender(last_result="success"), self._receiver(), "", "build-123.log"
        )
        self.assertFalse(ok)

    def test_already_completed_target_should_not_invoke(self):
        ok, reason = self.fn(
            self._sender(completed=["build"]), self._receiver(), "", "build-123.log"
        )
        self.assertFalse(ok)
        self.assertIn("already completed", reason)

    def test_no_log_should_not_invoke(self):
        ok, reason = self.fn(self._sender(), self._receiver(), "", "")
        self.assertFalse(ok)
        self.assertIn("no log", reason)

    def test_same_log_fix_pushed_should_not_invoke(self):
        ok, reason = self.fn(
            self._sender(),
            self._receiver(fix_pushed=True),
            "build-123.log",
            "build-123.log",
        )
        self.assertFalse(ok)
        self.assertIn("fix already pushed", reason)

    def test_same_log_no_fix_pushed_should_invoke(self):
        ok, reason = self.fn(
            self._sender(),
            self._receiver(fix_pushed=False),
            "build-123.log",
            "build-123.log",
        )
        self.assertTrue(ok, reason)


# ===========================================================================
# Unit: opencode_loop -- build_opencode_prompt (pure function)
# ===========================================================================


class TestBuildOpencodePrompt(unittest.TestCase):
    def setUp(self):
        import opencode_loop

        self.fn = opencode_loop.build_opencode_prompt

    def test_contains_target_name(self):
        prompt = self.fn("my-target", "my-target-123.log", "log content", "")
        self.assertIn("my-target", prompt)

    def test_contains_log_filename(self):
        prompt = self.fn("build", "build-20240101.log", "content", "")
        self.assertIn("build-20240101.log", prompt)

    def test_contains_file_ownership_rules(self):
        prompt = self.fn("build", "build.log", "", "")
        self.assertIn("sender-state.json", prompt)
        self.assertIn("receiver-state.json", prompt)
        self.assertIn("NEVER commit sender-state.json", prompt)

    def test_contains_output_instructions(self):
        prompt = self.fn("build", "build.log", "", "")
        self.assertIn("RETRY", prompt)
        self.assertIn("SUCCESS", prompt)
        self.assertIn("NEEDS_HUMAN", prompt)

    def test_contains_prev_logs_content(self):
        prompt = self.fn(
            "build", "build.log", "", "--- previous run ---\nerror details"
        )
        self.assertIn("error details", prompt)

    def test_test_fail_target_instructs_retry(self):
        prompt = self.fn("loop-test-fail", "loop-test-fail.log", "", "")
        self.assertIn("RETRY", prompt)


# ===========================================================================
# Unit: loop.py -- run_target (via mocked subprocess + state)
# ===========================================================================


class TestRunTarget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = _make_git_repo(Path(self.tmp) / "repo")
        (self.repo / "runs").mkdir(exist_ok=True)

    def _patch_loop(self):
        """Return context manager patches for loop.py testing."""
        return patch.multiple(
            "loop",
            REPO=self.repo,
        )

    def _run_target(self, target, branch, make_returncode=0):
        import loop

        with patch.object(loop, "REPO", self.repo):
            with patch("subprocess.run") as mock_run:
                with patch("lib.git") as mock_git:
                    with patch("lib.push_sender_state"):
                        with patch("lib.git_pull"):
                            with patch("time.sleep"):
                                mock_run.return_value = MagicMock(
                                    returncode=make_returncode
                                )
                                mock_git.return_value = MagicMock(returncode=0)
                                return loop.run_target(target, branch)

    def test_successful_target_returns_true(self):
        result = self._run_target("a", "main", make_returncode=0)
        self.assertTrue(result)

    def test_successful_target_marks_completed(self):
        self._run_target("a", "main", make_returncode=0)
        sender = lib.load_sender_state(self.repo)
        self.assertIn("a", sender["completed_targets"])

    def test_failed_target_returns_false(self):
        result = self._run_target("a", "main", make_returncode=1)
        self.assertFalse(result)

    def test_failed_target_increments_attempts(self):
        self._run_target("a", "main", make_returncode=1)
        sender = lib.load_sender_state(self.repo)
        self.assertGreater(sender.get("attempts", {}).get("a", 0), 0)

    def test_already_completed_target_skips_make(self):
        sender = lib.load_sender_state(self.repo)
        sender["completed_targets"] = ["a"]
        lib.save_sender_state(self.repo, sender)
        import loop

        with patch.object(loop, "REPO", self.repo):
            with patch("subprocess.run") as mock_run:
                with patch("lib.push_sender_state"):
                    mock_run.return_value = MagicMock(returncode=0)
                    result = loop.run_target("a", "main")
                    mock_run.assert_not_called()
                    self.assertTrue(result)

    def test_permanently_failed_marks_failed_targets(self):
        # Set attempts at max so next failure is permanent
        sender = lib.load_sender_state(self.repo)
        sender["attempts"] = {"a": 3}
        lib.save_sender_state(self.repo, sender)
        self._run_target("a", "main", make_returncode=1)
        sender = lib.load_sender_state(self.repo)
        self.assertIn("a", sender["failed_targets"])

    def test_blocker_pattern_sets_needs_human(self):
        # Set a blocker pattern and create a matching log
        receiver = lib.load_receiver_state(self.repo)
        receiver["blocker_patterns"] = [
            {"pattern": "NAS.*unreachable", "message": "NAS is down"}
        ]
        lib.save_receiver_state(self.repo, receiver)
        log = self.repo / "runs" / "a-20240101.log"
        log.write_text("NAS unreachable: connection refused")

        self._run_target("a", "main", make_returncode=1)
        sender = lib.load_sender_state(self.repo)
        self.assertEqual("needs_human", sender.get("status"))


# ===========================================================================
# Unit: loop.py -- initialise
# ===========================================================================


class TestInitialise(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = _make_git_repo(Path(self.tmp) / "repo")

    def test_initialise_creates_running_sender_state(self):
        import loop

        with patch.object(loop, "REPO", self.repo):
            with patch("lib.push_sender_state"):
                loop.initialise("main")
        sender = lib.load_sender_state(self.repo)
        self.assertEqual("running", sender["status"])

    def test_initialise_preserves_completed_targets_on_restart(self):
        sender = lib.load_sender_state(self.repo)
        sender["completed_targets"] = ["a"]
        lib.save_sender_state(self.repo, sender)

        import loop

        with patch.object(loop, "REPO", self.repo):
            with patch.object(
                loop, "SENDER_STATE_PATH", self.repo / "sender-state.json"
            ):
                with patch("lib.push_sender_state"):
                    loop.initialise("main")

        sender = lib.load_sender_state(self.repo)
        self.assertIn("a", sender["completed_targets"])


# ===========================================================================
# Integration: loop_reset.py
# ===========================================================================


class TestLoopResetIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = _make_git_repo(Path(self.tmp) / "repo")

    def _run_reset(self):
        import loop_reset

        with patch.object(loop_reset, "REPO", self.repo):
            with patch("lib.current_branch", return_value="main"):
                with patch("lib.git") as mock_git:
                    mock_git.return_value = MagicMock(returncode=0)
                    try:
                        loop_reset.main()
                    except SystemExit:
                        pass

    def test_reset_clears_completed_targets(self):
        sender = lib.load_sender_state(self.repo)
        sender["completed_targets"] = ["a", "b"]
        lib.save_sender_state(self.repo, sender)
        self._run_reset()
        sender = lib.load_sender_state(self.repo)
        self.assertEqual([], sender["completed_targets"])

    def test_reset_clears_failed_targets(self):
        sender = lib.load_sender_state(self.repo)
        sender["failed_targets"] = ["a"]
        lib.save_sender_state(self.repo, sender)
        self._run_reset()
        sender = lib.load_sender_state(self.repo)
        self.assertEqual([], sender["failed_targets"])

    def test_reset_clears_attempts(self):
        sender = lib.load_sender_state(self.repo)
        sender["attempts"] = {"a": 5}
        lib.save_sender_state(self.repo, sender)
        self._run_reset()
        sender = lib.load_sender_state(self.repo)
        self.assertEqual({}, sender["attempts"])

    def test_reset_clears_fix_signals(self):
        rc = lib.load_receiver_state(self.repo)
        rc["fix_pushed"] = True
        lib.save_receiver_state(self.repo, rc)
        self._run_reset()
        rc = lib.load_receiver_state(self.repo)
        self.assertFalse(rc["fix_pushed"])

    def test_reset_sets_status_running(self):
        sender = lib.load_sender_state(self.repo)
        sender["status"] = "completed_with_failures"
        lib.save_sender_state(self.repo, sender)
        self._run_reset()
        sender = lib.load_sender_state(self.repo)
        self.assertEqual("running", sender["status"])

    def test_reset_clears_stop_and_pause_signals(self):
        rc = lib.load_receiver_state(self.repo)
        rc["stop"] = True
        rc["pause"] = True
        lib.save_receiver_state(self.repo, rc)
        self._run_reset()
        rc = lib.load_receiver_state(self.repo)
        self.assertFalse(rc["stop"])
        self.assertFalse(rc["pause"])


# ===========================================================================
# Integration: loop_ack.py
# ===========================================================================


class TestLoopAckIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = _make_git_repo(Path(self.tmp) / "repo")

    def _run_ack(self):
        import loop_ack

        with patch.object(loop_ack, "REPO", self.repo):
            with patch("lib.current_branch", return_value="main"):
                with patch("lib.git") as mock_git:
                    mock_git.return_value = MagicMock(returncode=0)
                    try:
                        loop_ack.main()
                    except SystemExit:
                        pass

    def test_ack_sets_fix_pushed_in_receiver(self):
        self._run_ack()
        rc = lib.load_receiver_state(self.repo)
        self.assertTrue(rc["fix_pushed"])

    def test_ack_clears_human_action_in_sender(self):
        sender = lib.load_sender_state(self.repo)
        sender["human_action"] = "Fix the thing"
        sender["status"] = "needs_human"
        lib.save_sender_state(self.repo, sender)
        self._run_ack()
        sender = lib.load_sender_state(self.repo)
        self.assertIsNone(sender["human_action"])

    def test_ack_sets_status_running_in_sender(self):
        sender = lib.load_sender_state(self.repo)
        sender["status"] = "needs_human"
        lib.save_sender_state(self.repo, sender)
        self._run_ack()
        sender = lib.load_sender_state(self.repo)
        self.assertEqual("running", sender["status"])


# ===========================================================================
# Integration: loop_status.py
# ===========================================================================


class TestLoopStatusIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = _make_git_repo(Path(self.tmp) / "repo")

    def _run_status(self) -> str:
        import loop_status

        buf = io.StringIO()
        with patch.object(loop_status, "REPO", self.repo):
            with patch("sys.stdout", buf):
                try:
                    loop_status.main()
                except SystemExit:
                    pass
        return buf.getvalue()

    def test_status_shows_status_field(self):
        output = self._run_status()
        self.assertIn("Status:", output)

    def test_status_shows_completed(self):
        sender = lib.load_sender_state(self.repo)
        sender["completed_targets"] = ["a"]
        lib.save_sender_state(self.repo, sender)
        output = self._run_status()
        self.assertIn("a", output)

    def test_status_shows_needs_human(self):
        sender = lib.load_sender_state(self.repo)
        sender["human_action"] = "Please fix the thing"
        lib.save_sender_state(self.repo, sender)
        output = self._run_status()
        self.assertIn("NEEDS HUMAN", output)
        self.assertIn("Please fix the thing", output)

    def test_status_missing_file_says_not_started(self):
        (self.repo / "sender-state.json").unlink()
        buf = io.StringIO()
        import loop_status

        with patch.object(loop_status, "REPO", self.repo):
            with patch("sys.stdout", buf):
                try:
                    loop_status.main()
                except SystemExit:
                    pass
        self.assertIn("not been started", buf.getvalue())


# ===========================================================================
# Integration: loop_stop.py
# ===========================================================================


class TestLoopStop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = _make_git_repo(Path(self.tmp) / "repo")

    def _run_stop(self):
        import loop_stop

        with patch.object(loop_stop, "REPO", self.repo):
            with patch("lib.current_branch", return_value="main"):
                with patch("lib.git") as mock_git:
                    mock_git.return_value = MagicMock(returncode=0)
                    with patch("subprocess.run", return_value=MagicMock(returncode=1)):
                        try:
                            loop_stop.main()
                        except SystemExit:
                            pass

    def test_stop_sets_stop_in_receiver(self):
        self._run_stop()
        rc = lib.load_receiver_state(self.repo)
        self.assertTrue(rc.get("stop"))

    def test_stop_clears_pause(self):
        rc = lib.load_receiver_state(self.repo)
        rc["pause"] = True
        lib.save_receiver_state(self.repo, rc)
        self._run_stop()
        rc = lib.load_receiver_state(self.repo)
        self.assertTrue(rc.get("stop"))
        self.assertFalse(rc.get("pause", False))

    def test_stop_does_not_set_pause(self):
        self._run_stop()
        rc = lib.load_receiver_state(self.repo)
        self.assertFalse(rc.get("pause", False))


# ===========================================================================
# Integration: loop_pause.py
# ===========================================================================


class TestLoopPause(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = _make_git_repo(Path(self.tmp) / "repo")

    def _run_pause(self):
        import loop_pause

        with patch.object(loop_pause, "REPO", self.repo):
            with patch("lib.current_branch", return_value="main"):
                with patch("lib.git") as mock_git:
                    mock_git.return_value = MagicMock(returncode=0)
                    with patch("subprocess.run", return_value=MagicMock(returncode=1)):
                        try:
                            loop_pause.main()
                        except SystemExit:
                            pass

    def test_pause_sets_pause_in_receiver(self):
        self._run_pause()
        rc = lib.load_receiver_state(self.repo)
        self.assertTrue(rc.get("pause"))

    def test_pause_clears_stop(self):
        rc = lib.load_receiver_state(self.repo)
        rc["stop"] = True
        lib.save_receiver_state(self.repo, rc)
        self._run_pause()
        rc = lib.load_receiver_state(self.repo)
        self.assertTrue(rc.get("pause"))
        self.assertFalse(rc.get("stop", False))

    def test_pause_does_not_set_stop(self):
        self._run_pause()
        rc = lib.load_receiver_state(self.repo)
        self.assertFalse(rc.get("stop", False))


# ===========================================================================
# Integration: trim_loop_context.py
# ===========================================================================


class TestTrimLoopContextIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = Path(self.tmp)
        (self.repo / "docs").mkdir()

    def _write_context(self, lines: int) -> Path:
        ctx = self.repo / "loop-context.md"
        ctx.write_text("\n".join(f"line {i}" for i in range(lines)))
        return ctx

    def _run_trim(self, dry_run: bool = False) -> str:
        import trim_loop_context

        buf = io.StringIO()
        argv = ["trim_loop_context.py"] + (["--dry-run"] if dry_run else [])
        with patch.object(trim_loop_context, "REPO", self.repo):
            with patch.object(
                trim_loop_context, "CONTEXT_FILE", self.repo / "loop-context.md"
            ):
                with patch.object(
                    trim_loop_context,
                    "ARCHIVE_FILE",
                    self.repo / "docs" / "loop-context-archive.md",
                ):
                    with patch("sys.argv", argv):
                        with patch("sys.stdout", buf):
                            try:
                                trim_loop_context.main()
                            except SystemExit:
                                pass
        return buf.getvalue()

    def test_under_limit_no_trim(self):
        self._write_context(10)
        output = self._run_trim()
        self.assertIn("under limit", output)
        self.assertEqual(
            10, len((self.repo / "loop-context.md").read_text().splitlines())
        )

    def test_over_limit_trims_to_500(self):
        self._write_context(600)
        self._run_trim()
        lines = (self.repo / "loop-context.md").read_text().splitlines()
        self.assertLessEqual(len(lines), 500)

    def test_over_limit_creates_archive(self):
        self._write_context(600)
        self._run_trim()
        self.assertTrue((self.repo / "docs" / "loop-context-archive.md").exists())

    def test_archive_contains_oldest_lines(self):
        self._write_context(600)
        self._run_trim()
        archive = (self.repo / "docs" / "loop-context-archive.md").read_text()
        self.assertIn("line 0", archive)
        self.assertNotIn("line 599", archive)

    def test_kept_content_contains_newest_lines(self):
        self._write_context(600)
        self._run_trim()
        ctx = (self.repo / "loop-context.md").read_text()
        self.assertIn("line 599", ctx)
        self.assertNotIn("line 0", ctx)

    def test_dry_run_does_not_modify_file(self):
        self._write_context(600)
        self._run_trim(dry_run=True)
        lines = (self.repo / "loop-context.md").read_text().splitlines()
        self.assertEqual(600, len(lines))

    def test_dry_run_shows_would_archive(self):
        self._write_context(600)
        output = self._run_trim(dry_run=True)
        self.assertIn("Would archive", output)

    def test_missing_file_exits_cleanly(self):
        output = self._run_trim()
        self.assertIn("not found", output)

    def test_existing_archive_prepended(self):
        self._write_context(600)
        archive = self.repo / "docs" / "loop-context-archive.md"
        archive.write_text("old archive content")
        self._run_trim()
        content = archive.read_text()
        self.assertIn("old archive content", content)
        self.assertIn("line 0", content)


# ===========================================================================
# Schema validation
# ===========================================================================


class TestStateSchema(unittest.TestCase):
    def test_receiver_state_has_required_fields(self):
        data = json.loads((LOOP_REPO / "receiver-state.json").read_text())
        for field in (
            "targets",
            "deps",
            "max_attempts",
            "blocker_patterns",
            "fix_pushed",
            "stop",
            "pause",
        ):
            self.assertIn(field, data, f"Missing field: {field}")
        self.assertIsInstance(data["targets"], list)
        self.assertIsInstance(data["deps"], dict)
        self.assertIsInstance(data["max_attempts"], int)
        self.assertGreater(data["max_attempts"], 0)
        self.assertIsInstance(data["blocker_patterns"], list)
        self.assertIsInstance(data["stop"], bool)
        self.assertIsInstance(data["pause"], bool)
        self.assertIsInstance(data["fix_pushed"], bool)

    def test_sender_state_has_required_fields(self):
        data = json.loads((LOOP_REPO / "sender-state.json").read_text())
        for field in (
            "status",
            "completed_targets",
            "failed_targets",
            "attempts",
            "max_attempts",
        ):
            self.assertIn(field, data, f"Missing field: {field}")

    def test_receiver_state_no_circular_deps(self):
        data = json.loads((LOOP_REPO / "receiver-state.json").read_text())
        deps = data.get("deps", {})

        def has_cycle(node, visited, stack):
            visited.add(node)
            stack.add(node)
            for dep in deps.get(node, []):
                if dep not in visited:
                    if has_cycle(dep, visited, stack):
                        return True
                elif dep in stack:
                    return True
            stack.discard(node)
            return False

        for t in data.get("targets", []):
            self.assertFalse(has_cycle(t, set(), set()), f"Cycle involving '{t}'")

    def test_sender_state_status_valid(self):
        data = json.loads((LOOP_REPO / "sender-state.json").read_text())
        valid = {
            "running",
            "needs_human",
            "completed",
            "completed_with_failures",
            "idle",
        }
        self.assertIn(data["status"], valid)

    def test_blocker_patterns_have_required_keys(self):
        data = json.loads((LOOP_REPO / "receiver-state.json").read_text())
        for i, entry in enumerate(data.get("blocker_patterns", [])):
            self.assertIn("pattern", entry, f"blocker_patterns[{i}] missing 'pattern'")
            self.assertIn("message", entry, f"blocker_patterns[{i}] missing 'message'")

    def test_receiver_state_does_not_have_waiting_for_fix(self):
        data = json.loads((LOOP_REPO / "receiver-state.json").read_text())
        self.assertNotIn(
            "waiting_for_fix",
            data,
            "waiting_for_fix should not exist — receiver infers from sender-state.json",
        )

    def test_receiver_state_does_not_have_opencode_last_fix(self):
        data = json.loads((LOOP_REPO / "receiver-state.json").read_text())
        self.assertNotIn("opencode_last_fix", data, "old field — replaced by last_fix")


# ===========================================================================
# Unit: opencode_loop -- set_fix_pushed (with mocked git)
# ===========================================================================


class TestSetFixPushed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = _make_git_repo(Path(self.tmp) / "repo")

    def _run(self, message="test fix"):
        import opencode_loop

        with patch.object(opencode_loop, "REPO", self.repo):
            with patch("lib.git") as mock_git:
                mock_git.return_value = MagicMock(returncode=0)
                opencode_loop.set_fix_pushed(self.repo, "main", message)

    def test_sets_fix_pushed_true(self):
        self._run()
        rc = lib.load_receiver_state(self.repo)
        self.assertTrue(rc["fix_pushed"])

    def test_sets_last_fix_message(self):
        self._run("deploy fixed")
        rc = lib.load_receiver_state(self.repo)
        self.assertEqual("deploy fixed", rc["last_fix"])

    def test_does_not_write_sender_state(self):
        sender_before = lib.load_sender_state(self.repo)
        self._run()
        sender_after = lib.load_sender_state(self.repo)
        self.assertEqual(sender_before, sender_after)


# ===========================================================================
# Unit: opencode_loop -- gather_previous_logs (pure function)
# ===========================================================================


class TestGatherPreviousLogs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.runs_dir = Path(self.tmp) / "runs"
        self.runs_dir.mkdir()

    def _make_log(self, name, content):
        p = self.runs_dir / name
        p.write_text(content)
        return p

    def test_no_prev_logs_returns_empty(self):
        import opencode_loop

        result = opencode_loop.gather_previous_logs(self.runs_dir, "build", [])
        self.assertEqual("", result)

    def test_includes_previous_log_content(self):
        import opencode_loop

        logs = [
            self._make_log("build-001.log", "first run"),
            self._make_log("build-002.log", "second run"),
        ]
        result = opencode_loop.gather_previous_logs(self.runs_dir, "build", logs)
        self.assertIn("second run", result)

    def test_includes_opencode_log_if_present(self):
        import opencode_loop

        self._make_log("opencode-loop-20240101.log", "opencode output here")
        logs = [self._make_log("build-001.log", "main log")]
        result = opencode_loop.gather_previous_logs(self.runs_dir, "build", logs)
        self.assertIn("opencode output here", result)

    def test_max_3_prev_logs(self):
        import opencode_loop

        logs = [self._make_log(f"build-00{i}.log", f"run {i}") for i in range(6)]
        result = opencode_loop.gather_previous_logs(self.runs_dir, "build", logs)
        # logs[1:4] = indices 1,2,3 — that's 3 logs
        self.assertIn("run 1", result)
        self.assertIn("run 3", result)
        self.assertNotIn("run 4", result)


# ===========================================================================
# Unit: loop.py -- main() loop signal handling
# ===========================================================================


class TestLoopMainSignals(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = _make_git_repo(Path(self.tmp) / "repo")

    def _run_main_one_tick(self, receiver_override=None, sender_override=None):
        """Run loop.main() with stop signal set so it exits after one iteration."""
        import loop

        if receiver_override:
            rc = lib.load_receiver_state(self.repo)
            rc.update(receiver_override)
            lib.save_receiver_state(self.repo, rc)

        if sender_override:
            s = lib.load_sender_state(self.repo)
            s.update(sender_override)
            lib.save_sender_state(self.repo, s)

        with patch.object(loop, "REPO", self.repo):
            with patch.object(
                loop, "SENDER_STATE_PATH", self.repo / "sender-state.json"
            ):
                with patch.object(
                    loop, "RECEIVER_STATE_PATH", self.repo / "receiver-state.json"
                ):
                    with patch("lib.git") as mock_git:
                        with patch("lib.git_pull"):
                            with patch("lib.push_sender_state"):
                                with patch("time.sleep"):
                                    mock_git.return_value = MagicMock(returncode=0)
                                    try:
                                        loop.main()
                                    except SystemExit:
                                        pass

    def test_stop_signal_exits(self):
        self._run_main_one_tick(receiver_override={"stop": True, "targets": ["a"]})
        # If we get here without hanging, stop signal worked

    def test_no_targets_exits(self):
        self._run_main_one_tick(receiver_override={"targets": []})
        # Should exit with sys.exit(1) cleanly

    def test_all_completed_exits(self):
        self._run_main_one_tick(
            receiver_override={"targets": ["a"], "deps": {}},
            sender_override={
                "status": "running",
                "completed_targets": ["a"],
                "failed_targets": [],
                "attempts": {},
            },
        )
        # Should detect all done and exit

    def test_pause_signal_sleeps(self):
        # Set pause=true and stop=true so it pauses then stops
        rc = lib.load_receiver_state(self.repo)
        rc["pause"] = True
        rc["targets"] = ["a"]
        lib.save_receiver_state(self.repo, rc)

        import loop

        call_count = 0

        def fake_pull(repo, branch):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                # After first pause poll, set stop signal
                rc2 = lib.load_receiver_state(repo)
                rc2["stop"] = True
                rc2["pause"] = False
                lib.save_receiver_state(repo, rc2)

        with patch.object(loop, "REPO", self.repo):
            with patch.object(
                loop, "SENDER_STATE_PATH", self.repo / "sender-state.json"
            ):
                with patch.object(
                    loop, "RECEIVER_STATE_PATH", self.repo / "receiver-state.json"
                ):
                    with patch("lib.git_pull", side_effect=fake_pull):
                        with patch("lib.push_sender_state"):
                            with patch("lib.git") as mock_git:
                                with patch("time.sleep"):
                                    mock_git.return_value = MagicMock(returncode=0)
                                    try:
                                        loop.main()
                                    except SystemExit:
                                        pass
        self.assertGreaterEqual(call_count, 1)


# ===========================================================================
# Unit: opencode_loop -- main() key paths
# ===========================================================================


class TestOpencodeLoopMain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = _make_git_repo(Path(self.tmp) / "repo")
        (self.repo / "runs").mkdir(exist_ok=True)

    def _run_receiver_one_tick(
        self, sender_override=None, receiver_override=None, opencode_output="RETRY"
    ):
        import opencode_loop

        if sender_override:
            s = lib.load_sender_state(self.repo)
            s.update(sender_override)
            lib.save_sender_state(self.repo, s)

        if receiver_override:
            rc = lib.load_receiver_state(self.repo)
            rc.update(receiver_override)
            lib.save_receiver_state(self.repo, rc)

        tick = 0

        def fake_pull(*args, **kwargs):
            nonlocal tick
            tick += 1
            if tick >= 2:
                # After first iteration, set completed so receiver exits
                s = lib.load_sender_state(self.repo)
                s["status"] = "completed"
                lib.save_sender_state(self.repo, s)
            return MagicMock(returncode=0)

        mock_proc = MagicMock()
        mock_proc.stdout = opencode_output
        mock_proc.stderr = ""
        mock_proc.returncode = 0

        with patch.object(opencode_loop, "REPO", self.repo):
            with patch("lib.git", return_value=MagicMock(returncode=0)):
                with patch("lib.git_pull", side_effect=fake_pull):
                    with patch("subprocess.run", return_value=mock_proc):
                        with patch("time.sleep"):
                            try:
                                opencode_loop.main()
                            except SystemExit:
                                pass

    def test_receiver_exits_on_completed(self):
        # Set completed status immediately — receiver should exit cleanly
        s = lib.load_sender_state(self.repo)
        s["status"] = "completed"
        lib.save_sender_state(self.repo, s)
        self._run_receiver_one_tick()
        # No assertion needed — just verify it doesn't hang

    def test_receiver_stands_by_when_no_failure(self):
        # Verify via pure function — last_result=success means nothing to fix
        import opencode_loop

        sender = {
            "last_result": "success",
            "status": "running",
            "last_run_log": "a",
            "completed_targets": [],
            "failed_targets": [],
        }
        receiver = {"fix_pushed": False}
        ok, reason = opencode_loop.should_invoke_opencode(
            sender, receiver, "", "a-001.log"
        )
        self.assertFalse(ok)

    def test_receiver_waits_when_no_sender_state(self):
        # Verify via pure function — empty sender state means nothing to fix
        import opencode_loop

        ok, reason = opencode_loop.should_invoke_opencode(
            {}, {"fix_pushed": False}, "", ""
        )
        self.assertFalse(ok)

    def test_should_invoke_opencode_true_for_failed_target_with_log(self):
        # Verify the decision function returns True for a real failure with a log
        import opencode_loop

        log = self.repo / "runs" / "a-20240101-120000.log"
        log.write_text("Error: something failed")

        sender = {
            "last_result": "failed",
            "status": "running",
            "last_run_log": "a",
            "completed_targets": [],
            "failed_targets": [],
        }
        receiver = {"fix_pushed": False}
        ok, reason = opencode_loop.should_invoke_opencode(
            sender, receiver, "", "a-20240101-120000.log"
        )
        self.assertTrue(ok, f"Expected should_invoke=True, got reason: {reason}")


# ===========================================================================
# Unit: loop_resilient -- loop-context.md auto-creation
# ===========================================================================


class TestLoopResilientContextCreation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = _make_git_repo(Path(self.tmp) / "repo")

    def test_creates_loop_context_if_missing(self):
        import loop_resilient

        context_file = self.repo / "loop-context.md"
        self.assertFalse(context_file.exists())

        with patch.object(loop_resilient, "REPO", self.repo):
            with patch.object(
                loop_resilient, "LOG_FILE", self.repo / "runs" / "resilient.log"
            ):
                with patch.object(loop_resilient, "LOOP_SCRIPT", Path("/dev/null")):
                    with patch("lib.register_ours_driver"):
                        with patch("lib.current_branch", return_value="main"):
                            with patch("lib.git"):
                                with patch("subprocess.run") as mock_run:
                                    # Make loop_resilient exit after first loop
                                    mock_run.return_value = MagicMock(returncode=0)
                                    (self.repo / "runs").mkdir(exist_ok=True)
                                    try:
                                        loop_resilient.main()
                                    except SystemExit:
                                        pass

        self.assertTrue(context_file.exists())
        content = context_file.read_text()
        self.assertIn("Loop Context", content)

    def test_does_not_overwrite_existing_context(self):
        import loop_resilient

        context_file = self.repo / "loop-context.md"
        context_file.write_text("# My existing context\n\nDo not overwrite me.")

        with patch.object(loop_resilient, "REPO", self.repo):
            with patch.object(
                loop_resilient, "LOG_FILE", self.repo / "runs" / "resilient.log"
            ):
                with patch.object(loop_resilient, "LOOP_SCRIPT", Path("/dev/null")):
                    with patch("lib.register_ours_driver"):
                        with patch("lib.current_branch", return_value="main"):
                            with patch("lib.git"):
                                with patch("subprocess.run") as mock_run:
                                    mock_run.return_value = MagicMock(returncode=0)
                                    (self.repo / "runs").mkdir(exist_ok=True)
                                    try:
                                        loop_resilient.main()
                                    except SystemExit:
                                        pass

        self.assertIn("Do not overwrite me", context_file.read_text())


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
