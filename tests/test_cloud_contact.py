# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Tests for recording and reporting the last cloud contact."""

import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from kp_sync import cloud_contact
from kp_sync.cli import main
from kp_sync.config import EndpointConfig, load_config
from kp_sync.engine import SyncEngine, SyncResult
from kp_sync.state import HistoryState
from kp_sync.ui.none import NoneUI


class CloudContactTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name) / "run"
        self.run.mkdir(mode=0o700)

    def test_records_only_reached_clouds_and_keeps_older_ones(self):
        result = SyncResult(
            current=None,
            action="unchanged",
            classifications={"cloud:a": "unchanged", "cloud:b": "missing"},
            missing=("cloud:b",),
        )
        reached = cloud_contact.reached_in(
            result, ["cloud:a", "cloud:b", "cloud:c"]
        )
        self.assertEqual(reached, ["cloud:a"])
        then = datetime(2026, 11, 1, tzinfo=timezone.utc)
        cloud_contact.record(self.run, ["cloud:b"], now=then)
        cloud_contact.record(self.run, reached)
        cloud_contact.record(self.run, [])
        stored = cloud_contact.load(self.run)
        self.assertEqual(stored["cloud:b"], then.isoformat())
        self.assertIn("cloud:a", stored)

    def test_describes_contact_falling_back_to_the_copy_record(self):
        self.assertIn(
            "last reached 2d ago",
            cloud_contact.describe(
                {
                    "reached_at": (
                        datetime.now(timezone.utc) - timedelta(days=2)
                    ).isoformat()
                }
            ),
        )
        self.assertIn(
            "last synchronized 3h ago",
            cloud_contact.describe(
                {
                    "reached_at": None,
                    "synced_at": (
                        datetime.now(timezone.utc) - timedelta(hours=3)
                    ).isoformat(),
                }
            ),
        )
        self.assertEqual(
            cloud_contact.describe({}),
            "not reached since this machine started",
        )

    def test_unusable_files_read_as_unknown(self):
        (self.run / cloud_contact.FILENAME).write_text("[1, 2]")
        self.assertEqual(cloud_contact.load(self.run), {})
        (self.run / cloud_contact.FILENAME).write_text("not json")
        self.assertEqual(cloud_contact.load(self.run), {})
        self.assertTrue(cloud_contact.warning_due(self.run, 60))
        cloud_contact.record_warning(self.run)
        self.assertFalse(cloud_contact.warning_due(self.run, 60))
        later = datetime.now(timezone.utc) + timedelta(minutes=2)
        self.assertTrue(cloud_contact.warning_due(self.run, 60, now=later))

    def test_status_shows_when_the_cloud_was_last_reached(self):
        home = Path(self.temp.name)
        config = replace(
            load_config(
                environ={"HOME": str(home), "XDG_RUNTIME_DIR": str(home)}
            ),
            endpoints=(EndpointConfig("cloud", "rclone", remote="gd"),),
        )
        state = HistoryState()
        state.commit("a" * 64, (), "alpha", ("cloud:gd",), size=1)
        state.save(self.run / "state.json")
        (self.run / "last-sync.json").write_text(
            json.dumps({"action": "committed", "classifications": {}})
        )
        (self.run / "last-sync.json").chmod(0o600)
        engine = SyncEngine(config, run_directory=self.run, ui=NoneUI())
        contact = engine.status().cloud["cloud:gd"]
        self.assertIsNone(contact["reached_at"])
        self.assertIn("last synchronized", contact["description"])
        cloud_contact.record(self.run, ["cloud:gd"])
        with (
            patch("kp_sync.cli.load_config", return_value=config),
            patch("kp_sync.cli.ensure_run_dir", return_value=self.run),
            patch(
                "kp_sync.cli.endpoints_from_config",
                side_effect=AssertionError("live probe"),
            ),
        ):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                main(["status"])
        self.assertIn("Cloud cloud:gd: last reached", output.getvalue())


if __name__ == "__main__":
    unittest.main()
