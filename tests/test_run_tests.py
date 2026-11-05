"""Tests for tools/run_tests.py (with fake test processes)."""

import io
import subprocess
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from tools import run_tests


class RunTestsTest(unittest.TestCase):
    def test_every_module_is_scheduled_once_slowest_first(self):
        names = run_tests.modules()
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("test_run_tests", names)
        self.assertEqual(names[0], run_tests.SLOW_FIRST[0])

    def test_split_modules_run_each_test_exactly_once(self):
        targets = run_tests.jobs(
            ["test_compatibility", "test_skeleton"], {"test_compatibility": 2}
        )
        split = [t for job in targets[:-1] for t in job]
        self.assertEqual(
            sorted(split), sorted(run_tests._test_ids("test_compatibility"))
        )
        self.assertEqual(len(targets) - 1, 2)
        self.assertEqual(targets[-1], ["tests.test_skeleton"])

    def test_failures_and_empty_modules_fail_the_run(self):
        outputs = {
            "tests.test_a": (0, "Ran 3 tests in 0.1s\n\nOK\n"),
            "tests.test_b": (1, "FAIL: x\nRan 2 tests in 0.1s\n\nFAILED\n"),
            "tests.test_c": (0, "Ran 0 tests in 0.0s\n\nNO TESTS RAN\n"),
        }

        def fake(argv, **kwargs):
            status, text = outputs[argv[-1]]
            return subprocess.CompletedProcess(argv, status, text, "")

        with patch("tools.run_tests.subprocess.run", side_effect=fake):
            results = [
                run_tests.run_module([target], "python") for target in outputs
            ]
        self.assertEqual([r["ok"] for r in results], [True, False, False])
        self.assertEqual([r["tests"] for r in results], [3, 2, 0])
        with (
            patch("tools.run_tests.subprocess.run", side_effect=fake),
            patch("tools.run_tests.modules", return_value=["test_a"]),
            redirect_stdout(io.StringIO()) as out,
        ):
            self.assertEqual(run_tests.main(["-j", "1"]), 0)
        self.assertIn("Ran 3 tests in 1 processes", out.getvalue())
        with (
            patch("tools.run_tests.subprocess.run", side_effect=fake),
            redirect_stdout(io.StringIO()) as out,
        ):
            self.assertEqual(run_tests.main(["test_a", "test_b"]), 1)
        self.assertIn("===== test_b FAILED =====", out.getvalue())
        self.assertIn("FAILED: test_b", out.getvalue())


if __name__ == "__main__":
    unittest.main()
