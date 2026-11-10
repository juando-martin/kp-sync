# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""The suite never sees the real home or runtime directory.

Importing ``tests`` (here, at discovery time) applies the isolation in
``tests/__init__.py`` before any test runs.
"""

import os
import sys
import unittest
from pathlib import Path

try:
    import tests
except ModuleNotFoundError:  # run from inside tests/
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import tests


class IsolationTest(unittest.TestCase):
    def test_home_and_runtime_directory_are_private_temporary_ones(self):
        self.assertEqual(Path.home(), tests.HOME)
        self.assertEqual(Path(os.environ["HOME"]), tests.HOME)
        self.assertEqual(
            Path(os.environ["XDG_RUNTIME_DIR"]), tests.RUNTIME_DIR
        )
        for name in (
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "XDG_CACHE_HOME",
        ):
            self.assertTrue(
                Path(os.environ[name]).is_relative_to(tests.HOME), name
            )
        if tests.REAL_HOME:
            self.assertNotEqual(Path.home(), Path(tests.REAL_HOME))

    def test_real_profiles_are_out_of_reach(self):
        from kp_sync.config import load_config

        config = load_config()
        self.assertIsNone(config.source)
        self.assertTrue(config.hub.is_relative_to(tests.RUNTIME_DIR))


if __name__ == "__main__":
    unittest.main()
