"""Tests for kp-sync drive-setup (udevmon and lsblk are faked)."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from kp_sync.config import load_config
from kp_sync.drive_setup import DriveSetupError, setup


class DriveSetupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.environ = {
            "HOME": str(self.home),
            "XDG_RUNTIME_DIR": str(self.home / "run"),
        }
        self.mount = self.home / "media" / "KEY"
        self.mount.mkdir(parents=True)
        self.udevmon_dir = self.home / ".config" / "udevmon"
        (self.udevmon_dir / "config.d").mkdir(parents=True)
        self.calls = []
        self.version = "udevmon 0.7.0"
        self.fail = None

    def config(self, *, persistent=True, pendrive=True, name="beta"):
        profiles = self.home / ".config" / "kp-sync"
        profiles.mkdir(parents=True, exist_ok=True)
        text = (
            f'hub = "{self.home}/hub/pass.kdbx"\n'
            f"hub_persistent = {'true' if persistent else 'false'}\n"
            "require_encrypted = false\n"
            '[[endpoints]]\nname = "cloud"\ntype = "rclone"\nremote = "gd"\n'
        )
        if pendrive:
            text += (
                '[[endpoints]]\nname = "pendrive"\ntype = "file"\n'
                'source = "runtime"\n'
            )
        text += '[drives]\ndirectory = "kp"\n'
        (profiles / f"{name}.toml").write_text(text)
        return load_config(profile=name, environ=self.environ)

    def runner(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[:2] == ["udevmon", "--version"]:
            if self.version is None:
                raise FileNotFoundError("udevmon")
            return subprocess.CompletedProcess(argv, 0, self.version, "")
        if self.fail and argv[1] == self.fail:
            return subprocess.CompletedProcess(argv, 1, "", "refused")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def run_setup(self, config, name="key-daily", **kwargs):
        lines = []
        status = setup(
            config,
            self.mount,
            name,
            runner=self.runner,
            environ=self.environ,
            identify=lambda mount: "SERIAL1:UUID-1",
            out=lines.append,
            **kwargs,
        )
        return status, lines, [c for c in self.calls if c[1] != "--version"]

    def write_drop_in(self, text):
        path = self.udevmon_dir / "config.d" / "50-kp-sync.toml"
        path.write_text(text)

    def test_new_drive_on_a_persistent_hub_gets_device_actions_trigger(self):
        status, lines, calls = self.run_setup(self.config())
        self.assertEqual(status, 0)
        self.assertEqual(
            calls[0],
            [
                "udevmon",
                "devices",
                "add",
                "key-daily",
                "--from-mountpoint",
                str(self.mount.resolve()),
                "--tag",
                "keepass",
                "--file",
                "50-kp-sync",
            ],
        )
        attach, detach = calls[1], calls[2]
        self.assertEqual(
            attach[:4], ["udevmon", "actions", "add", "startup,mounted"]
        )
        self.assertIn("keepass-attach", attach)
        self.assertIn("--notify-on-failure", attach)
        tail = attach[attach.index("--") + 1 :]
        self.assertEqual(
            tail,
            [
                "kp-sync",
                "--profile",
                "beta",
                "drive-sync",
                "up",
                "--mode",
                "secondary",
                "--drive-id",
                "{drive_serial}:{fs_uuid}",
                "{mountpoint}",
            ],
        )
        self.assertIn("keepass-detach", detach)
        self.assertEqual(detach[3], "unmounted")
        self.assertEqual(
            calls[3], ["udevmon", "trigger", "key-daily", "--wait"]
        )
        self.assertTrue(
            any("writes the current database" in line for line in lines)
        )

    def test_dry_run_only_reports(self):
        status, lines, calls = self.run_setup(self.config(), dry_run=True)
        self.assertEqual((status, calls), (0, []))
        self.assertIn("Dry run: nothing was changed.", lines)

    def test_known_drive_with_actions_is_only_triggered(self):
        (self.mount / "kp").mkdir()
        (self.mount / "kp" / "pass.kdbx").write_bytes(b"existing")
        self.write_drop_in(
            '[devices.old-key]\nmatch = { fs_uuid = "UUID-1", '
            'drive_serial = "SERIAL1" }\ntags = ["keepass"]\n'
            '[[actions]]\nname = "keepass-attach"\ntag = "keepass"\n'
            'on = ["startup"]\nrun = ["kp-sync", "--profile", "beta", '
            '"drive-sync", "up", "--mode", "secondary"]\n'
            '[[actions]]\nname = "keepass-detach"\ntag = "keepass"\n'
            'on = ["unmounted"]\nrun = ["kp-sync", "--profile", "beta", '
            '"drive-sync", "down", "--mode", "secondary"]\n'
        )
        status, lines, calls = self.run_setup(self.config())
        self.assertEqual(calls, [["udevmon", "trigger", "old-key", "--wait"]])
        self.assertTrue(any("never overwritten" in line for line in lines))

    def test_known_drive_gets_missing_tags(self):
        (self.udevmon_dir / "config.toml").write_text(
            '[devices.old-key]\nmatch = { fs_uuid = "UUID-1", '
            'drive_serial = "SERIAL1" }\n'
        )
        _, _, calls = self.run_setup(self.config(), rclone=True)
        self.assertEqual(
            calls[0],
            ["udevmon", "devices", "tag", "old-key", "keepass", "rclone"],
        )

    def test_ram_hub_uses_main_mode_and_avoids_taken_action_names(self):
        self.write_drop_in(
            '[[actions]]\nname = "keepass-up"\ntag = "keepass"\n'
            'on = ["startup"]\nrun = ["kp-sync", "--profile", "other", '
            '"drive-sync", "up", "--mode", "main"]\n'
        )
        config = self.config(persistent=False, name="alpha")
        _, lines, calls = self.run_setup(config)
        up = calls[1]
        self.assertIn("keepass-up-alpha", up)
        self.assertEqual(up[up.index("--mode") + 1], "main")
        self.assertIn("keepass-down", calls[2])
        self.assertTrue(
            any("starts the application" in line for line in lines)
        )

    def test_refusals_change_nothing(self):
        cases = [
            (dict(pendrive=False), "no pendrive endpoint"),
        ]
        for options, message in cases:
            with self.assertRaisesRegex(DriveSetupError, message):
                self.run_setup(self.config(**options))
        for version, message in (
            ("udevmon 0.6.0", "found 0.6.0"),
            (None, "0.7.0 or later is needed"),
        ):
            self.version = version
            with self.assertRaisesRegex(DriveSetupError, message):
                self.run_setup(self.config())
        self.version = "udevmon 0.7.0"
        self.write_drop_in(
            '[devices.key-daily]\nmatch = { fs_uuid = "OTHER", '
            'drive_serial = "SERIAL9" }\n'
        )
        with self.assertRaisesRegex(DriveSetupError, "another drive"):
            self.run_setup(self.config())
        self.assertEqual([c for c in self.calls if c[1] != "--version"], [])

    def test_a_failing_udevmon_command_stops_with_its_message(self):
        self.fail = "actions"
        with self.assertRaisesRegex(DriveSetupError, "refused"):
            self.run_setup(self.config())
        self.assertEqual(
            [c[1] for c in self.calls[1:]], ["devices", "actions"]
        )


if __name__ == "__main__":
    unittest.main()
