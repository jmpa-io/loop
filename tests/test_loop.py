#!/usr/bin/env python3
"""
tests/test_loop.py — unit and integration tests for the loop library.

Unit tests:   exercise lib.py functions directly (no I/O, no subprocesses).
Integration:  run script main() functions against a real temp git repo,
              verifying state files are written correctly.

Run with:
    python3 tests/test_loop.py
    make loop-test
"""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Allow importing from bin/
sys.path.insert(0, str(Path(__file__).parent.parent / "bin"))
import lib

BIN = Path(__file__).parent.parent / "bin"


# ===========================================================================
# Helpers
# ===========================================================================


def _load_script(name: str):
    """Import a bin/ script as a module by filename."""
    spec = importlib.util.spec_from_file_location(name, BIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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

    oc = {
        "fix_pushed": False,
        "waiting_for_fix": False,
        "opencode_last_fix": None,
        "targets": ["a", "b"],
        "deps": {"b": ["a"]},
        "max_attempts": 3,
        "human_action": None,
        "blocker_patterns": [],
    }
    run = {
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
    (path / "loop-state.json").write_text(json.dumps(oc, indent=2))
    (path / "loop-run-state.json").write_text(json.dumps(run, indent=2))

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
        self.oc = {
            "targets": ["a", "b", "c", "d"],
            "deps": {"b": ["a"], "c": ["a"], "d": ["b", "c"]},
        }

    def test_no_deps_is_ready(self):
        run = {"completed_targets": [], "failed_targets": []}
        self.assertEqual("ready", lib.dep_status("a", run, self.oc))

    def test_dep_completed_is_ready(self):
        run = {"completed_targets": ["a"], "failed_targets": []}
        self.assertEqual("ready", lib.dep_status("b", run, self.oc))
        self.assertEqual("ready", lib.dep_status("c", run, self.oc))

    def test_dep_not_completed_is_waiting(self):
        run = {"completed_targets": [], "failed_targets": []}
        self.assertEqual("waiting", lib.dep_status("b", run, self.oc))

    def test_dep_failed_is_blocked(self):
        run = {"completed_targets": [], "failed_targets": ["a"]}
        self.assertEqual("blocked", lib.dep_status("b", run, self.oc))
        self.assertEqual("blocked", lib.dep_status("c", run, self.oc))

    def test_multi_dep_one_failed_is_blocked(self):
        run = {"completed_targets": ["b"], "failed_targets": ["c"]}
        self.assertEqual("blocked", lib.dep_status("d", run, self.oc))

    def test_multi_dep_both_completed_is_ready(self):
        run = {"completed_targets": ["b", "c"], "failed_targets": []}
        self.assertEqual("ready", lib.dep_status("d", run, self.oc))

    def test_unknown_target_no_deps_is_ready(self):
        run = {"completed_targets": [], "failed_targets": []}
        self.assertEqual("ready", lib.dep_status("unknown", run, self.oc))


# ===========================================================================
# Unit: snapshot / all-done
# ===========================================================================


class TestBuildSnapshot(unittest.TestCase):
    def setUp(self):
        self.oc = {
            "targets": ["a", "b", "c", "d"],
            "deps": {"b": ["a"], "c": ["a"], "d": ["b", "c"]},
        }

    def test_nothing_done_a_is_ready(self):
        run = {"completed_targets": [], "failed_targets": []}
        snap = lib.build_snapshot(run, self.oc)
        self.assertIn("a", snap["ready"])
        self.assertEqual([], snap["skipped"])

    def test_a_completed_b_and_c_ready(self):
        run = {"completed_targets": ["a"], "failed_targets": []}
        snap = lib.build_snapshot(run, self.oc)
        self.assertIn("b", snap["ready"])
        self.assertIn("c", snap["ready"])

    def test_a_failed_b_c_d_skipped(self):
        run = {"completed_targets": [], "failed_targets": ["a"]}
        snap = lib.build_snapshot(run, self.oc)
        skipped = [t for t, _ in snap["skipped"]]
        self.assertIn("b", skipped)
        self.assertIn("c", skipped)
        self.assertIn("d", skipped)

    def test_d_waiting_on_b_and_c(self):
        run = {"completed_targets": ["a"], "failed_targets": []}
        snap = lib.build_snapshot(run, self.oc)
        waiting = [t for t, _ in snap["waiting"]]
        self.assertIn("d", waiting)


class TestAllTargetsDone(unittest.TestCase):
    def setUp(self):
        self.oc = {"targets": ["a", "b", "c"], "deps": {"b": ["a"], "c": ["b"]}}

    def test_nothing_done_not_complete(self):
        run = {"completed_targets": [], "failed_targets": []}
        self.assertFalse(lib.all_targets_done(run, self.oc))

    def test_all_completed_is_done(self):
        run = {"completed_targets": ["a", "b", "c"], "failed_targets": []}
        self.assertTrue(lib.all_targets_done(run, self.oc))

    def test_failed_dep_cascades_to_done(self):
        run = {"completed_targets": [], "failed_targets": ["a"]}
        self.assertTrue(lib.all_targets_done(run, self.oc))

    def test_partial_not_done(self):
        run = {"completed_targets": ["a"], "failed_targets": []}
        self.assertFalse(lib.all_targets_done(run, self.oc))


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


class TestInitialiseRunState(unittest.TestCase):
    def _oc(self, targets=None):
        return {"targets": targets or ["a", "b"], "max_attempts": 5}

    def test_fresh_start_returns_running(self):
        run = lib.initialise_run_state(None, self._oc())
        self.assertEqual("running", run["status"])
        self.assertEqual([], run["completed_targets"])
        self.assertEqual([], run["failed_targets"])
        self.assertEqual({}, run["attempts"])

    def test_fresh_start_inherits_targets(self):
        run = lib.initialise_run_state(None, self._oc(["x", "y"]))
        self.assertEqual(["x", "y"], run["targets"])

    def test_fresh_start_inherits_max_attempts(self):
        run = lib.initialise_run_state(None, self._oc())
        self.assertEqual(5, run["max_attempts"])

    def test_restart_preserves_completed(self):
        existing = {
            "status": "running",
            "completed_targets": ["a"],
            "failed_targets": [],
            "attempts": {},
        }
        run = lib.initialise_run_state(existing, self._oc())
        self.assertIn("a", run["completed_targets"])

    def test_restart_idle_becomes_running(self):
        existing = {
            "status": "idle",
            "completed_targets": [],
            "failed_targets": [],
            "attempts": {},
        }
        run = lib.initialise_run_state(existing, self._oc())
        self.assertEqual("running", run["status"])

    def test_restart_needs_human_preserved(self):
        existing = {
            "status": "needs_human",
            "human_action": "fix me",
            "completed_targets": [],
            "failed_targets": [],
            "attempts": {},
        }
        run = lib.initialise_run_state(existing, self._oc())
        self.assertEqual("needs_human", run["status"])
        self.assertEqual("fix me", run["human_action"])

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
        run = lib.initialise_run_state(existing, self._oc())
        self.assertNotIn("current_target", run)
        self.assertNotIn("current_index", run)
        self.assertNotIn("current_attempt", run)

    def test_does_not_mutate_existing(self):
        existing = {
            "status": "idle",
            "completed_targets": ["a"],
            "failed_targets": [],
            "attempts": {},
        }
        lib.initialise_run_state(existing, self._oc())
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
        oc = lib.apply_stop_signal({"pause": True})
        self.assertTrue(oc["stop"])
        self.assertNotIn("pause", oc)

    def test_apply_pause_sets_pause_clears_stop(self):
        oc = lib.apply_pause_signal({"stop": True})
        self.assertTrue(oc["pause"])
        self.assertNotIn("stop", oc)

    def test_clear_signals_removes_both(self):
        oc = lib.clear_signals({"stop": True, "pause": True, "targets": []})
        self.assertNotIn("stop", oc)
        self.assertNotIn("pause", oc)
        self.assertIn("targets", oc)

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

    def test_usbcore_registered_no_match(self):
        self.assertEqual(
            "",
            lib.check_hardware_blocker(
                "usbcore: registered new interface driver", self._patterns()
            ),
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

    def test_custom_pattern(self):
        p = [{"pattern": r"CUSTOM_SIGNAL", "message": "Custom blocker triggered"}]
        self.assertIn("Custom", lib.check_hardware_blocker("CUSTOM_SIGNAL seen", p))

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

    def test_unclear_returns_empty(self):
        self.assertEqual("", lib.parse_last_word("Something happened but unclear"))

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
        self.assertIn("\n", content)  # indented, not one-liner


# ===========================================================================
# Integration: loop_reset.py
# ===========================================================================


class TestLoopResetIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = _make_git_repo(Path(self.tmp) / "repo")

    def _run_reset(self):
        """Run loop_reset.main() with REPO pointing at our temp repo."""
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
        # Pre-populate state with completed targets
        run = lib.load_run_state(self.repo)
        run["completed_targets"] = ["a", "b"]
        lib.save_run_state(self.repo, run)

        self._run_reset()

        run = lib.load_run_state(self.repo)
        self.assertEqual([], run["completed_targets"])

    def test_reset_clears_failed_targets(self):
        run = lib.load_run_state(self.repo)
        run["failed_targets"] = ["a"]
        lib.save_run_state(self.repo, run)

        self._run_reset()

        run = lib.load_run_state(self.repo)
        self.assertEqual([], run["failed_targets"])

    def test_reset_clears_attempts(self):
        run = lib.load_run_state(self.repo)
        run["attempts"] = {"a": 5}
        lib.save_run_state(self.repo, run)

        self._run_reset()

        run = lib.load_run_state(self.repo)
        self.assertEqual({}, run["attempts"])

    def test_reset_clears_fix_signals(self):
        oc = lib.load_oc_state(self.repo)
        oc["fix_pushed"] = True
        oc["waiting_for_fix"] = True
        lib.save_oc_state(self.repo, oc)

        self._run_reset()

        oc = lib.load_oc_state(self.repo)
        self.assertFalse(oc["fix_pushed"])
        self.assertFalse(oc["waiting_for_fix"])

    def test_reset_sets_status_running(self):
        run = lib.load_run_state(self.repo)
        run["status"] = "completed_with_failures"
        lib.save_run_state(self.repo, run)

        self._run_reset()

        run = lib.load_run_state(self.repo)
        self.assertEqual("running", run["status"])


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

    def test_ack_sets_fix_pushed(self):
        self._run_ack()
        oc = lib.load_oc_state(self.repo)
        self.assertTrue(oc["fix_pushed"])

    def test_ack_clears_waiting_for_fix(self):
        oc = lib.load_oc_state(self.repo)
        oc["waiting_for_fix"] = True
        lib.save_oc_state(self.repo, oc)

        self._run_ack()

        oc = lib.load_oc_state(self.repo)
        self.assertFalse(oc["waiting_for_fix"])

    def test_ack_clears_human_action(self):
        run = lib.load_run_state(self.repo)
        run["human_action"] = "Fix the thing"
        run["status"] = "needs_human"
        lib.save_run_state(self.repo, run)

        self._run_ack()

        run = lib.load_run_state(self.repo)
        self.assertIsNone(run["human_action"])

    def test_ack_sets_status_running(self):
        run = lib.load_run_state(self.repo)
        run["status"] = "needs_human"
        lib.save_run_state(self.repo, run)

        self._run_ack()

        run = lib.load_run_state(self.repo)
        self.assertEqual("running", run["status"])


# ===========================================================================
# Integration: loop_status.py
# ===========================================================================


class TestLoopStatusIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = _make_git_repo(Path(self.tmp) / "repo")

    def _run_status(self) -> str:
        import io
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
        run = lib.load_run_state(self.repo)
        run["completed_targets"] = ["a"]
        lib.save_run_state(self.repo, run)
        output = self._run_status()
        self.assertIn("a", output)

    def test_status_shows_needs_human(self):
        run = lib.load_run_state(self.repo)
        run["human_action"] = "Please fix the thing"
        lib.save_run_state(self.repo, run)
        output = self._run_status()
        self.assertIn("NEEDS HUMAN", output)
        self.assertIn("Please fix the thing", output)

    def test_status_missing_file_says_not_started(self):
        import io, loop_status

        (self.repo / "loop-run-state.json").unlink()
        buf = io.StringIO()
        with patch.object(loop_status, "REPO", self.repo):
            with patch("sys.stdout", buf):
                try:
                    loop_status.main()
                except SystemExit:
                    pass
        self.assertIn("not been started", buf.getvalue())


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
        import io, trim_loop_context

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

    def test_stop_sets_stop_true_in_oc_state(self):
        self._run_stop()
        oc = lib.load_oc_state(self.repo)
        self.assertTrue(oc.get("stop"))

    def test_stop_clears_pause_field(self):
        oc = lib.load_oc_state(self.repo)
        oc["pause"] = True
        lib.save_oc_state(self.repo, oc)
        self._run_stop()
        oc = lib.load_oc_state(self.repo)
        self.assertTrue(oc.get("stop"))
        self.assertFalse(oc.get("pause", False))

    def test_stop_does_not_set_pause(self):
        self._run_stop()
        oc = lib.load_oc_state(self.repo)
        self.assertFalse(oc.get("pause", False))


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

    def test_pause_sets_pause_true_in_oc_state(self):
        self._run_pause()
        oc = lib.load_oc_state(self.repo)
        self.assertTrue(oc.get("pause"))

    def test_pause_clears_stop_field(self):
        oc = lib.load_oc_state(self.repo)
        oc["stop"] = True
        lib.save_oc_state(self.repo, oc)
        self._run_pause()
        oc = lib.load_oc_state(self.repo)
        self.assertTrue(oc.get("pause"))
        self.assertFalse(oc.get("stop", False))

    def test_pause_does_not_set_stop(self):
        self._run_pause()
        oc = lib.load_oc_state(self.repo)
        self.assertFalse(oc.get("stop", False))


# ===========================================================================
# Schema validation
# ===========================================================================


class TestLoopStateSchema(unittest.TestCase):
    LOOP_REPO = Path(__file__).parent.parent

    def test_loop_state_has_required_fields(self):
        data = json.loads((self.LOOP_REPO / "loop-state.json").read_text())
        for field in (
            "targets",
            "deps",
            "max_attempts",
            "blocker_patterns",
            "stop",
            "pause",
        ):
            self.assertIn(field, data)
        self.assertIsInstance(data["targets"], list)
        self.assertIsInstance(data["deps"], dict)
        self.assertIsInstance(data["max_attempts"], int)
        self.assertGreater(data["max_attempts"], 0)
        self.assertIsInstance(data["blocker_patterns"], list)
        self.assertIsInstance(data["stop"], bool)
        self.assertIsInstance(data["pause"], bool)

    def test_loop_run_state_has_required_fields(self):
        data = json.loads((self.LOOP_REPO / "loop-run-state.json").read_text())
        for field in (
            "status",
            "completed_targets",
            "failed_targets",
            "attempts",
            "max_attempts",
        ):
            self.assertIn(field, data)

    def test_loop_state_no_circular_deps(self):
        data = json.loads((self.LOOP_REPO / "loop-state.json").read_text())
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

    def test_loop_run_state_status_valid(self):
        data = json.loads((self.LOOP_REPO / "loop-run-state.json").read_text())
        valid = {
            "running",
            "needs_human",
            "completed",
            "completed_with_failures",
            "idle",
        }
        self.assertIn(data["status"], valid)

    def test_blocker_patterns_have_required_keys(self):
        data = json.loads((self.LOOP_REPO / "loop-state.json").read_text())
        for i, entry in enumerate(data.get("blocker_patterns", [])):
            self.assertIn("pattern", entry, f"blocker_patterns[{i}] missing 'pattern'")
            self.assertIn("message", entry, f"blocker_patterns[{i}] missing 'message'")


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
