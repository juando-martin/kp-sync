# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Tests for decommissioning and re-enrolling key drives."""

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kp_sync.config import load_config
from kp_sync.credentials import CredentialProvider
from kp_sync.drive_retire import DriveRetireError, resolve, retire
from kp_sync.drive_setup import setup
from kp_sync.drives import DriveEndpoint
from kp_sync.endpoints import FileEndpoint
from kp_sync.engine import SyncEngine, SyncError
from kp_sync.history import CopyRecord, sha256_file
from kp_sync.state import HistoryState, StateError, merge_states
from kp_sync.ui.base import UI
from kp_sync.ui.fake import FakeUI
from kp_sync.ui.none import NoneUI
from tests.fixtures import kdbx4_bytes
from tests.test_credentials import FakeKeyring

DRIVE = "drive:SERIAL1:UUID-1"


class NotifyingUI(FakeUI):
    def notify(self, message):
        self.events.append(("notify", message))
        UI.notify(self, message)  # the real one logs the message


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


class DriveMarkerStateTest(unittest.TestCase):
    def history(self):
        state = HistoryState()
        state.commit(SHA_A, (), "h", ("hub:h", DRIVE, "drive:S2:U2"), size=1)
        return state

    def test_decommission_suppresses_records_from_any_store(self):
        stale = self.history()
        marked = self.history()
        self.assertTrue(marked.mark_drive(DRIVE, "decommissioned", "h"))
        self.assertNotIn(DRIVE, marked.copies)
        # A newer record (another machine attached it meanwhile).
        newer = self.history()
        newer.generation = 50
        newer.copies[DRIVE] = CopyRecord(
            SHA_A, 50, "2027-01-01T00:00:00+00:00", "alpha"
        )
        for ordering in ((marked, stale, newer), (newer, stale, marked)):
            merged = merge_states(ordering)
            self.assertNotIn(DRIVE, merged.copies)
            self.assertIn("drive:S2:U2", merged.copies)
            self.assertEqual(merged.decommissioned(), {DRIVE})
            with self.assertRaisesRegex(StateError, "decommissioned drive"):
                merged.record_copies(SHA_A, [DRIVE], "h")
        self.assertFalse(marked.mark_drive(DRIVE, "decommissioned", "h"))

    def test_schema_three_only_while_markers_exist(self):
        plain = self.history()
        self.assertEqual(plain.to_dict()["schema_version"], 2)
        self.assertNotIn("drive_markers", plain.to_dict())
        plain.mark_drive(DRIVE, "decommissioned", "h", name="key-extra")
        payload = plain.to_dict()
        self.assertEqual(payload["schema_version"], 3)
        loaded = HistoryState.from_dict(json.loads(json.dumps(payload)))
        self.assertEqual(loaded.drive_markers, plain.drive_markers)
        self.assertEqual(loaded.drive_markers[DRIVE].name, "key-extra")
        with self.assertRaises(StateError):
            HistoryState.from_dict(payload | {"schema_version": 4})

    def test_enrolment_lifts_marker_but_keeps_old_records_hidden(self):
        stale = self.history()
        state = self.history()
        state.mark_drive(DRIVE, "decommissioned", "h")
        self.assertFalse(
            self.history().mark_drive(DRIVE, "enrolled", "h"),
            "enrolment without a marker records nothing",
        )
        self.assertTrue(state.mark_drive(DRIVE, "enrolled", "h"))
        merged = merge_states((stale, state))
        self.assertNotIn(DRIVE, merged.copies)
        self.assertEqual(merged.decommissioned(), set())
        merged.record_copies(SHA_A, [DRIVE], "h")
        again = merge_states((stale, merged))
        self.assertIn(DRIVE, again.copies)
        # A later decommission wins over the enrolment.
        self.assertTrue(again.mark_drive(DRIVE, "decommissioned", "h"))
        self.assertEqual(
            merge_states((state, again)).decommissioned(), {DRIVE}
        )

    def test_invalid_identity_is_rejected(self):
        state = HistoryState()
        for identity in ("session:x", "drive:", "drive:a/b:U", "drive:S"):
            with self.assertRaises(StateError):
                state.mark_drive(identity, "decommissioned", "h")


class DriveDecommissionEngineTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(
            patch.object(
                CredentialProvider, "_backend", return_value=FakeKeyring()
            )
        )
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.hub_path = self.root / "hub.kdbx"
        config_path = self.root / "config.toml"
        config_path.write_text(
            f"hub = {json.dumps(str(self.hub_path))}\n"
            f'key_file = {json.dumps(str(self.root / "test.keyx"))}\n'
            "[sync]\nlock_timeout = 1\nmax_restarts = 1\n",
            encoding="utf-8",
        )
        self.config = load_config(
            config_path,
            environ={
                "HOME": str(self.root),
                "XDG_RUNTIME_DIR": str(self.runtime),
            },
            hostname="test-host",
        )
        self.run_dir = self.runtime / "kp-sync"
        self.run_dir.mkdir(mode=0o700)
        self.enterContext(
            patch.dict("os.environ", {"XDG_RUNTIME_DIR": str(self.runtime)})
        )

    def engine(self, endpoints=(), ui=None):
        return SyncEngine(
            self.config,
            endpoints,
            run_directory=self.run_dir,
            baseline_stores=(),
            ui=ui or NoneUI(),
        )

    def seed(self, copies, *, branch=False):
        state = HistoryState()
        state.commit(SHA_A, (), "test-host", copies, size=1)
        if branch:
            state.commit(SHA_B, (SHA_A,), "test-host", ("hub:x",), size=1)
            state.commit(SHA_C, (SHA_A,), "test-host", (DRIVE,), size=1)
        state.save(self.run_dir / "state.json")

    def test_preview_changes_nothing_and_apply_commits(self):
        self.seed((DRIVE, "drive:S2:U2", "hub:test-host"))
        original = (self.run_dir / "state.json").read_bytes()
        engine = self.engine()
        preview = engine.drive_decommission_preview("SERIAL1:UUID-1")
        self.assertEqual(preview["blockers"], [])
        self.assertTrue(preview["first_drive_marker"])
        self.assertEqual(preview["record"]["version"], SHA_A)
        self.assertEqual((self.run_dir / "state.json").read_bytes(), original)
        result = engine.decommission_drive("SERIAL1:UUID-1", name="key")
        self.assertTrue(result["changed"])
        self.assertEqual(result["runtime_history"], "committed")
        state = HistoryState.load(self.run_dir / "state.json")
        self.assertNotIn(DRIVE, state.copies)
        self.assertEqual(state.drive_markers[DRIVE].name, "key")
        again = engine.decommission_drive(DRIVE)
        self.assertFalse(again["changed"])

    def test_blockers(self):
        self.seed((DRIVE, "hub:test-host"))
        engine = self.engine()
        with self.assertRaisesRegex(SyncError, "last key drive"):
            engine.decommission_drive(DRIVE)
        drives = self.run_dir / "drives"
        drives.mkdir()
        (drives / "SERIAL1:UUID-1.json").write_text("{}")
        (self.run_dir / "pending.json").write_text(
            '{"decisions": {"drive-history:SERIAL1:UUID-1": {}}}'
        )
        with self.assertRaisesRegex(SyncError, "attached") as caught:
            engine.decommission_drive(DRIVE, last_history_drive=True)
        self.assertIn("pending decision", str(caught.exception))
        (drives / "SERIAL1:UUID-1.json").unlink()
        (self.run_dir / "pending.json").unlink()
        engine.decommission_drive(DRIVE, last_history_drive=True)
        self.assertEqual(
            HistoryState.load(self.run_dir / "state.json").decommissioned(),
            {DRIVE},
        )

    def test_unmerged_branch_blocks(self):
        self.seed(("hub:test-host", "drive:S2:U2"), branch=True)
        with self.assertRaisesRegex(SyncError, "not held or merged"):
            self.engine().decommission_drive(DRIVE)

    def test_attached_decommissioned_drive_is_left_untouched(self):
        data = kdbx4_bytes(b"current" * 100)
        self.hub_path.write_bytes(data)
        sha, size = sha256_file(str(self.hub_path))
        other = self.root / "other.kdbx"
        other.write_bytes(data)
        state = HistoryState()
        state.commit(
            sha, (), "test-host", ("hub:test-host", "file:o"), size=size
        )
        state.mark_drive(DRIVE, "decommissioned", "beta")
        state.save(self.run_dir / "state.json")
        mount = self.root / "drive"
        mount.mkdir()

        def drive():
            return DriveEndpoint(
                "pendrive",
                mount,
                "pass.kdbx",
                "SERIAL1:UUID-1",
                identity_resolver=lambda _: "SERIAL1:UUID-1",
            )

        from kp_sync import log

        saved = log.settings()
        self.addCleanup(log.restore_settings, saved)
        log.configure(environ={})
        ui = NotifyingUI()
        endpoints = (FileEndpoint("o", other, identity="file:o"),)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = self.engine((*endpoints, drive()), ui).sync()
        log.restore_settings(saved)
        self.assertEqual(stderr.getvalue().count("is decommissioned"), 1)
        self.assertEqual(result.action, "unchanged")
        self.assertEqual(list(mount.iterdir()), [])
        notices = [event for event in ui.events if event[0] == "notify"]
        self.assertEqual(len(notices), 1)
        self.assertIn("decommissioned", notices[0][1])
        second = NotifyingUI()
        self.engine((*endpoints, drive()), second).sync()
        self.assertFalse([e for e in second.events if e[0] == "notify"])
        self.assertNotIn(
            DRIVE, HistoryState.load(self.run_dir / "state.json").copies
        )

        self.assertTrue(self.engine().enrol_drive("SERIAL1:UUID-1")["changed"])
        self.engine((*endpoints, drive())).sync()
        self.assertEqual(sha256_file(str(mount / "pass.kdbx"))[0], sha)
        self.assertIn(
            DRIVE, HistoryState.load(self.run_dir / "state.json").copies
        )

    def test_marker_learned_during_the_run_still_excludes_the_drive(self):
        data = kdbx4_bytes(b"current" * 100)
        self.hub_path.write_bytes(data)
        sha, size = sha256_file(str(self.hub_path))
        state = HistoryState()
        state.commit(sha, (), "test-host", ("hub:test-host",), size=size)
        state.save(self.run_dir / "state.json")
        mount = self.root / "drive"
        mount.mkdir()
        stale = mount / "pass.kdbx"
        stale.write_bytes(kdbx4_bytes(b"old" * 100))
        before = stale.read_bytes()
        engine = self.engine(
            (
                DriveEndpoint(
                    "pendrive",
                    mount,
                    "pass.kdbx",
                    "SERIAL1:UUID-1",
                    identity_resolver=lambda _: "SERIAL1:UUID-1",
                ),
            )
        )
        # Another machine's marker arrives after the engine was built.
        state.mark_drive(DRIVE, "decommissioned", "alpha")
        state.save(self.run_dir / "state.json")
        result = engine.sync()
        self.assertNotIn(DRIVE, result.classifications)
        self.assertEqual(stale.read_bytes(), before)


class DriveRetireCommandTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.environ = {
            "HOME": str(self.home),
            "XDG_RUNTIME_DIR": str(self.home / "run"),
        }
        self.udevmon_dir = self.home / ".config" / "udevmon"
        (self.udevmon_dir / "config.d").mkdir(parents=True)
        profiles = self.home / ".config" / "kp-sync"
        profiles.mkdir(parents=True)
        (profiles / "beta.toml").write_text(
            f'hub = "{self.home}/hub/pass.kdbx"\n'
            "hub_persistent = true\nrequire_encrypted = false\n"
            '[[endpoints]]\nname = "pendrive"\ntype = "file"\n'
            'source = "runtime"\n'
            '[drives]\ndirectory = "kp"\n'
            "[drives.aliases]\n"
            '"SERIAL1:UUID-1" = "Extra"\n'
            '"SERIAL2:UUID-2" = "Twin"\n'
            '"SERIAL3:UUID-3" = "Twin"\n'
        )
        self.config = load_config(profile="beta", environ=self.environ)
        self.calls = []

    def drop_in(self, text, name="config.d/50-kp-sync.toml"):
        (self.udevmon_dir / name).write_text(text)

    def runner(self, argv, **kwargs):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def test_resolution(self):
        self.drop_in(
            "[devices.key-extra]\n"
            'match = { fs_uuid = "UUID-1", drive_serial = "SERIAL1" }\n'
            'tags = ["keepass", "rclone"]\n'
            "[devices.loose]\n"
            'match = { drive_serial = "SERIAL9" }\n'
        )
        for name in ("key-extra", "Extra", "SERIAL1:UUID-1", DRIVE):
            found = resolve(self.config, name, environ=self.environ)
            self.assertEqual(found.identity, "SERIAL1:UUID-1")
            self.assertEqual(found.device, "key-extra")
        with self.assertRaisesRegex(DriveRetireError, "both"):
            resolve(self.config, "loose", environ=self.environ)
        with self.assertRaisesRegex(DriveRetireError, "several"):
            resolve(self.config, "Twin", environ=self.environ)
        with self.assertRaisesRegex(DriveRetireError, "not a udevmon"):
            resolve(self.config, "nothing", environ=self.environ)
        self.assertIsNone(
            resolve(self.config, "SERIAL7:UUID-7", environ=self.environ).device
        )

    class FakeEngine:
        def __init__(self, blockers=()):
            self.blockers = list(blockers)
            self.applied = []

        def report(self):
            return {
                "identity": DRIVE,
                "record": None,
                "marker": None,
                "first_drive_marker": True,
                "blockers": self.blockers,
                "shared_stores": {"cloud": "awaiting publication"},
            }

        def drive_decommission_preview(self, identity):
            return self.report()

        def decommission_drive(self, identity, **kwargs):
            self.applied.append((identity, kwargs))
            return self.report() | {
                "changed": True,
                "runtime_history": "committed",
                "shared_stores": {"cloud": "published"},
                "publication_error": None,
            }

    def run_retire(self, engine, drive="key-extra", **kwargs):
        lines = []
        status = retire(
            self.config,
            engine,
            drive,
            runner=self.runner,
            environ=self.environ,
            out=lines.append,
            **kwargs,
        )
        return status, "\n".join(lines)

    def test_preview_apply_and_device_handling(self):
        self.drop_in(
            "[devices.key-extra]\n"
            'match = { fs_uuid = "UUID-1", drive_serial = "SERIAL1" }\n'
            'tags = ["keepass-kp-test", "rclone", "backup"]\n'
        )
        engine = self.FakeEngine()
        status, text = self.run_retire(engine)
        self.assertEqual(status, 0)
        self.assertIn("--confirm SERIAL1:UUID-1", text)
        self.assertIn("schema 3", text)
        self.assertIn("rclone tag stops too", text)
        self.assertIn("other tags stop too: backup.", text)
        self.assertEqual(self.calls, [])
        self.assertEqual(engine.applied, [])
        with self.assertRaisesRegex(DriveRetireError, "exactly"):
            self.run_retire(engine, confirm="SERIAL1:UUID-2")
        self.assertEqual(engine.applied, [])

        status, text = self.run_retire(engine, confirm="SERIAL1:UUID-1")
        self.assertEqual(status, 0)
        self.assertEqual(engine.applied[0][1]["name"], "key-extra")
        self.assertEqual(
            self.calls, [["udevmon", "devices", "remove", "key-extra"]]
        )
        self.assertIn("remove kp/pass.kdbx, the kp-sync/ history", text)
        self.calls.clear()
        self.run_retire(engine, confirm="SERIAL1:UUID-1", keep_device=True)
        self.assertEqual(
            self.calls, [["udevmon", "devices", "ignore", "key-extra"]]
        )

    def test_blocked_preview_and_foreign_device_file(self):
        self.drop_in(
            "[devices.key-extra]\n"
            'match = { fs_uuid = "UUID-1", drive_serial = "SERIAL1" }\n',
            name="config.toml",
        )
        status, text = self.run_retire(self.FakeEngine(["it is attached"]))
        self.assertEqual(status, 1)
        self.assertIn("Blocked: it is attached.", text)
        status, text = self.run_retire(
            self.FakeEngine(), confirm="SERIAL1:UUID-1"
        )
        self.assertEqual(status, 0)
        self.assertEqual(self.calls, [])
        self.assertIn("not kp-sync's drop-in", text)

    def test_drive_setup_enables_ignored_device_and_enrols_first(self):
        self.drop_in(
            "[devices.key-extra]\n"
            'match = { fs_uuid = "UUID-1", drive_serial = "SERIAL1" }\n'
            'tags = ["keepass"]\nstatus = "ignored"\n'
            '[[actions]]\nname = "a"\ntag = "keepass"\n'
            'run = ["kp-sync", "--profile", "beta", "drive-sync", "up"]\n'
            '[[actions]]\nname = "b"\ntag = "keepass"\n'
            'run = ["kp-sync", "--profile", "beta", "drive-sync", "down"]\n'
        )
        mount = self.home / "media"
        mount.mkdir()
        order = []

        def runner(argv, **kwargs):
            if argv[:2] == ["udevmon", "--version"]:
                return subprocess.CompletedProcess(
                    argv, 0, "udevmon 0.7.0", ""
                )
            order.append(argv[2] if argv[1] == "devices" else argv[1])
            return subprocess.CompletedProcess(argv, 0, "", "")

        def enrol(identity):
            order.append(("enrol", identity))
            return {"changed": True}

        lines = []
        setup(
            self.config,
            mount,
            "key-extra",
            runner=runner,
            environ=self.environ,
            identify=lambda _: "SERIAL1:UUID-1",
            out=lines.append,
            enrol=enrol,
        )
        self.assertEqual(
            order, [("enrol", "SERIAL1:UUID-1"), "enable", "trigger"]
        )
        self.assertIn("enrolled again", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
