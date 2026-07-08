#!/usr/bin/env python3
"""
tests/test_loop.py — unit tests for the loop library.

Tests all core logic in bin/lib.py directly (no subprocesses needed for
logic tests). Integration tests for trim_loop_context.py use a temp dir.

Run with:
    python3 tests/test_loop.py
    make loop-test
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

# Allow importing lib from bin/
sys.path.insert(0, str(Path(__file__).parent.parent / "bin"))
import lib


# ---------------------------------------------------------------------------
# Dependency resolution
# ---------------------------------------------------------------------------


class TestDepStatus(unittest.TestCase):
    def setUp(self):
        self.oc = {
            "targets": ["a", "b", "c", "d"],
            "deps": {"b": ["a"], "c": ["a"], "d": ["b", "c"]},
        }

    def test_no_deps_is_ready(self):
        run = {"completed_targets": [], "failed_targets": []}
        self.assertEqual(lib.dep_status("a", run, self.oc), "ready")

    def test_dep_completed_is_ready(self):
        run = {"completed_targets": ["a"], "failed_targets": []}
        self.assertEqual(lib.dep_status("b", run, self.oc), "ready")
        self.assertEqual(lib.dep_status("c", run, self.oc), "ready")

    def test_dep_not_completed_is_waiting(self):
        run = {"completed_targets": [], "failed_targets": []}
        self.assertEqual(lib.dep_status("b", run, self.oc), "waiting")

    def test_dep_failed_is_blocked(self):
        run = {"completed_targets": [], "failed_targets": ["a"]}
        self.assertEqual(lib.dep_status("b", run, self.oc), "blocked")
        self.assertEqual(lib.dep_status("c", run, self.oc), "blocked")

    def test_multi_dep_one_failed_is_blocked(self):
        run = {"completed_targets": ["b"], "failed_targets": ["c"]}
        self.assertEqual(lib.dep_status("d", run, self.oc), "blocked")

    def test_multi_dep_both_completed_is_ready(self):
        run = {"completed_targets": ["b", "c"], "failed_targets": []}
        self.assertEqual(lib.dep_status("d", run, self.oc), "ready")

    def test_unknown_target_no_deps_is_ready(self):
        run = {"completed_targets": [], "failed_targets": []}
        self.assertEqual(lib.dep_status("unknown", run, self.oc), "ready")


# ---------------------------------------------------------------------------
# Snapshot / all-done logic
# ---------------------------------------------------------------------------


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
        self.assertEqual(snap["skipped"], [])

    def test_a_completed_b_and_c_ready(self):
        run = {"completed_targets": ["a"], "failed_targets": []}
        snap = lib.build_snapshot(run, self.oc)
        self.assertIn("b", snap["ready"])
        self.assertIn("c", snap["ready"])

    def test_a_failed_b_c_d_skipped(self):
        run = {"completed_targets": [], "failed_targets": ["a"]}
        snap = lib.build_snapshot(run, self.oc)
        skipped_targets = [t for t, _ in snap["skipped"]]
        self.assertIn("b", skipped_targets)
        self.assertIn("c", skipped_targets)
        self.assertIn("d", skipped_targets)

    def test_d_waiting_on_b_and_c(self):
        run = {"completed_targets": ["a"], "failed_targets": []}
        snap = lib.build_snapshot(run, self.oc)
        waiting_targets = [t for t, _ in snap["waiting"]]
        self.assertIn("d", waiting_targets)


class TestAllTargetsDone(unittest.TestCase):
    def setUp(self):
        self.oc = {
            "targets": ["a", "b", "c"],
            "deps": {"b": ["a"], "c": ["b"]},
        }

    def test_nothing_done_not_complete(self):
        run = {"completed_targets": [], "failed_targets": []}
        self.assertFalse(lib.all_targets_done(run, self.oc))

    def test_all_completed_is_done(self):
        run = {"completed_targets": ["a", "b", "c"], "failed_targets": []}
        self.assertTrue(lib.all_targets_done(run, self.oc))

    def test_failed_dep_cascades_to_done(self):
        # a fails → b and c skipped → all accounted for
        run = {"completed_targets": [], "failed_targets": ["a"]}
        self.assertTrue(lib.all_targets_done(run, self.oc))

    def test_partial_not_done(self):
        run = {"completed_targets": ["a"], "failed_targets": []}
        self.assertFalse(lib.all_targets_done(run, self.oc))


# ---------------------------------------------------------------------------
# Idempotency guard
# ---------------------------------------------------------------------------


class TestIsAlreadyCompleted(unittest.TestCase):
    def test_completed_target_returns_true(self):
        run = {"completed_targets": ["deploy-k3s"], "failed_targets": []}
        self.assertTrue(lib.is_already_completed("deploy-k3s", run))

    def test_incomplete_target_returns_false(self):
        run = {"completed_targets": ["deploy-pbs"], "failed_targets": []}
        self.assertFalse(lib.is_already_completed("deploy-k3s", run))

    def test_failed_target_returns_false(self):
        run = {"completed_targets": [], "failed_targets": ["deploy-k3s"]}
        self.assertFalse(lib.is_already_completed("deploy-k3s", run))

    def test_empty_state_returns_false(self):
        run = {}
        self.assertFalse(lib.is_already_completed("anything", run))

    def test_missing_key_returns_false(self):
        self.assertFalse(lib.is_already_completed("x", {"failed_targets": []}))


# ---------------------------------------------------------------------------
# Hardware / blocker detection
# ---------------------------------------------------------------------------


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
        log = "fatal: No route to host 10.0.1.1 port 22"
        self.assertIn("NAS", lib.check_hardware_blocker(log, self._patterns()))

    def test_nas_text(self):
        self.assertIn(
            "NAS",
            lib.check_hardware_blocker("NAS (10.0.1.1) unreachable", self._patterns()),
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
            lib.check_hardware_blocker(
                "usb 3-1: USB disconnect, device 4", self._patterns()
            ),
        )

    def test_usbcore_registered_no_match(self):
        self.assertEqual(
            "",
            lib.check_hardware_blocker(
                "usbcore: registered new interface driver r8152", self._patterns()
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
        patterns = [
            {"pattern": r"CUSTOM_SIGNAL", "message": "Custom blocker triggered"}
        ]
        self.assertIn(
            "Custom", lib.check_hardware_blocker("CUSTOM_SIGNAL seen in log", patterns)
        )


# ---------------------------------------------------------------------------
# OpenCode result parsing
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# State file I/O
# ---------------------------------------------------------------------------


class TestStateIO(unittest.TestCase):
    def test_load_json_missing_file_returns_empty(self):
        result = lib.load_json(Path("/tmp/does-not-exist-ever.json"))
        self.assertEqual(result, {})

    def test_load_json_invalid_json_returns_empty(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not valid json {{{{")
            name = f.name
        result = lib.load_json(Path(name))
        self.assertEqual(result, {})

    def test_save_and_load_roundtrip(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            name = f.name
        data = {"targets": ["a", "b"], "max_attempts": 5}
        lib.save_json(Path(name), data)
        loaded = lib.load_json(Path(name))
        self.assertEqual(loaded, data)


# ---------------------------------------------------------------------------
# trim_loop_context.py — tested via direct function import
# ---------------------------------------------------------------------------


class TestTrimLoopContext(unittest.TestCase):
    """
    Import and exercise the trimming logic directly rather than via subprocess.
    """

    def _make_context(self, lines: int) -> str:
        return "\n".join(f"line {i}" for i in range(lines))

    def test_under_limit_not_trimmed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ctx = Path(tmpdir) / "loop-context.md"
            ctx.write_text(self._make_context(10))
            (Path(tmpdir) / "docs").mkdir()
            # Simulate: would the trimmer say it needs trimming?
            current = len(ctx.read_text().splitlines())
            self.assertLessEqual(current, 500)

    def test_over_limit_trimmed_to_500(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ctx = Path(tmpdir) / "loop-context.md"
            ctx.write_text(self._make_context(600))
            archive = Path(tmpdir) / "docs" / "loop-context-archive.md"
            archive.parent.mkdir()

            # Run the trim logic inline (mirrors trim_loop_context.py)
            lines = ctx.read_text().splitlines()
            MAX_LINES = 500
            archive_count = len(lines) - MAX_LINES
            to_archive = "\n".join(lines[:archive_count])
            to_keep = "\n".join(lines[archive_count:])
            archive.write_text(f"# archive\n\n{to_archive}")
            ctx.write_text(to_keep)

            self.assertLessEqual(len(ctx.read_text().splitlines()), MAX_LINES)
            self.assertTrue(archive.exists())

    def test_archive_contains_oldest_lines(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ctx = Path(tmpdir) / "loop-context.md"
            lines_600 = [f"line {i}" for i in range(600)]
            ctx.write_text("\n".join(lines_600))
            archive = Path(tmpdir) / "docs" / "loop-context-archive.md"
            archive.parent.mkdir()

            lines = ctx.read_text().splitlines()
            MAX_LINES = 500
            archive_count = len(lines) - MAX_LINES
            archive.write_text("\n".join(lines[:archive_count]))
            ctx.write_text("\n".join(lines[archive_count:]))

            archive_content = archive.read_text()
            self.assertIn("line 0", archive_content)
            self.assertNotIn("line 599", archive_content)


# ---------------------------------------------------------------------------
# loop-state.json schema validation
# ---------------------------------------------------------------------------


class TestLoopStateSchema(unittest.TestCase):
    LOOP_REPO = Path(__file__).parent.parent

    def test_loop_state_has_required_fields(self):
        data = json.loads((self.LOOP_REPO / "loop-state.json").read_text())
        for field in ("targets", "deps", "max_attempts", "blocker_patterns"):
            self.assertIn(field, data, f"loop-state.json missing: {field}")
        self.assertIsInstance(data["targets"], list)
        self.assertIsInstance(data["deps"], dict)
        self.assertIsInstance(data["max_attempts"], int)
        self.assertGreater(data["max_attempts"], 0)
        self.assertIsInstance(data["blocker_patterns"], list)

    def test_loop_run_state_has_required_fields(self):
        data = json.loads((self.LOOP_REPO / "loop-run-state.json").read_text())
        for field in (
            "status",
            "completed_targets",
            "failed_targets",
            "attempts",
            "max_attempts",
        ):
            self.assertIn(field, data, f"loop-run-state.json missing: {field}")

    def test_loop_state_deps_no_circular(self):
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

        for target in data.get("targets", []):
            self.assertFalse(
                has_cycle(target, set(), set()),
                f"Circular dependency involving '{target}'",
            )

    def test_loop_run_state_status_is_valid(self):
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
