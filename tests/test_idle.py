# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from kp_sync import idle, reach
from kp_sync.cli import main
from kp_sync.config import EndpointConfig, load_config
from kp_sync.integration import render_units

HOUR = 3600


def probes(connect=None):
    return reach.Probes(
        connectivity=lambda timeout: "unknown",
        connect=lambda host, port, timeout: connect,
    )


OFFLINE = probes("connect to h:1 failed (Network is unreachable)")


class SkipTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.run = self.root / "run"
        self.run.mkdir()
        base = load_config(
            environ={"HOME": temp.name, "XDG_RUNTIME_DIR": temp.name}
        )
        self.config = replace(
            base,
            endpoints=(
                EndpointConfig(
                    "cloud", "rclone", remote="Drive:kp", reach_check="h:1"
                ),
            ),
        )
        self.now = 1_000_000.0

    def skip(self, config=None, *, now=None, offline=True):
        with patch.object(reach, "PROBES", OFFLINE if offline else probes()):
            return idle.skip_reason(
                config or self.config,
                self.run,
                now=self.now if now is None else now,
            )

    def test_offline_skips_only_after_a_recent_idle_run(self):
        self.assertIsNone(self.skip())  # no idle sync ran yet
        idle.record_run(self.run, now=self.now - HOUR)
        reason = self.skip()
        self.assertIn("idle sync skipped: the cloud is unreachable", reason)
        self.assertIn("cloud: connect to h:1 failed", reason)
        self.assertIsNone(self.skip(offline=False))
        # One idle sync still runs every OFFLINE_RUN_EVERY intervals.
        every = idle.OFFLINE_RUN_EVERY * HOUR
        self.assertIsNotNone(self.skip(now=self.now - HOUR + every - 1))
        self.assertIsNone(self.skip(now=self.now - HOUR + every))
        # A record from the future (clock change) does not block runs.
        self.assertIsNone(self.skip(now=self.now - 2 * HOUR))

    def test_runs_when_a_cloud_cannot_be_judged_or_none_exists(self):
        idle.record_run(self.run, now=self.now - HOUR)
        unjudged = EndpointConfig(
            "other", "rclone", remote="Nowhere:", reach_check=False
        )
        both = replace(
            self.config, endpoints=self.config.endpoints + (unjudged,)
        )
        self.assertIsNone(self.skip(both))
        no_type = replace(
            self.config,
            endpoints=(EndpointConfig("c", "rclone", remote="Unknown:"),),
        )
        self.assertIsNone(self.skip(no_type))
        self.assertIsNone(self.skip(replace(self.config, endpoints=())))
        disabled = replace(
            self.config,
            endpoints=(replace(self.config.endpoints[0], enabled=False),),
        )
        self.assertIsNone(self.skip(disabled))

    def test_unpublished_credentials_count_as_unreachable(self):
        idle.record_run(self.run, now=self.now - HOUR)
        provider = self.root / "provider.conf"
        config = replace(
            self.config,
            endpoints=(
                replace(self.config.endpoints[0], rclone_config=provider),
            ),
        )
        reason = self.skip(config, offline=False)
        self.assertIn("cloud: rclone credentials not published", reason)
        provider.write_text("[Drive]\ntype = drive\n")
        self.assertIsNone(self.skip(config, offline=False))

    def test_a_damaged_record_runs_the_sync(self):
        (self.run / idle.RECORD).write_text("not json")
        self.assertIsNone(self.skip())
        (self.run / idle.RECORD).write_text(json.dumps({"ran_at": "x"}))
        self.assertIsNone(self.skip())


class CommandTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.run = self.root / "kp-sync"
        self.run.mkdir(mode=0o700)
        base = load_config(
            environ={"HOME": temp.name, "XDG_RUNTIME_DIR": temp.name}
        )
        self.config = replace(
            base,
            endpoints=(
                EndpointConfig(
                    "cloud", "rclone", remote="Drive:kp", reach_check="h:1"
                ),
            ),
        )
        self.reply = {
            "action": "unchanged",
            "current": None,
            "classifications": {"cloud:Drive:kp": "missing"},
            "missing": ["cloud:Drive:kp"],
        }

    def main(self, argv, *, reply=None, offline=False):
        quiet = io.StringIO()
        with (
            patch("kp_sync.cli.load_config", return_value=self.config),
            patch("kp_sync.cli.ensure_run_dir", return_value=self.run),
            patch.object(reach, "PROBES", OFFLINE if offline else probes()),
            patch(
                "kp_sync.daemon.client.Client.request",
                return_value=reply or self.reply,
            ) as request,
            contextlib.redirect_stdout(quiet),
            contextlib.redirect_stderr(quiet),
        ):
            status = main(argv)
        return status, request, quiet.getvalue()

    def test_idle_sync_runs_records_and_tolerates_unreachable_copies(self):
        status, request, _ = self.main(["sync", "--idle"])
        self.assertEqual(status, 0)
        request.assert_called_once()
        self.assertTrue((self.run / idle.RECORD).is_file())
        self.assertEqual(self.main(["sync"])[0], 4)
        pending = self.reply | {"pending": "decision"}
        self.assertEqual(self.main(["sync", "--idle"], reply=pending)[0], 3)

    def test_offline_idle_sync_is_skipped_without_the_daemon(self):
        idle.record_run(self.run)
        status, request, output = self.main(["sync", "--idle"], offline=True)
        self.assertEqual(status, 0)
        request.assert_not_called()
        self.assertIn("idle sync skipped", output)
        # A plain sync is never skipped.
        status, request, _ = self.main(["sync"], offline=True)
        request.assert_called_once()

    def test_idle_needs_the_daemon(self):
        status, request, output = self.main(["sync", "--idle", "--no-daemon"])
        self.assertEqual(status, 2)
        request.assert_not_called()
        self.assertIn("--idle runs through the daemon", output)

    def test_the_timer_runs_idle_syncs(self):
        for name, text in render_units(self.config).items():
            if name.startswith("kp-sync-sync") and name.endswith(".service"):
                with self.subTest(unit=name):
                    self.assertRegex(text, r"ExecStart=.* sync --idle\n")


if __name__ == "__main__":
    unittest.main()
