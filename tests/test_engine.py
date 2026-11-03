import base64
import fcntl
import copy
import hashlib
import json
import os
import shutil
import struct
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from pykeepass import PyKeePass, create_database

from kp_sync.config import load_config
from kp_sync.credentials import CredentialProvider
from kp_sync.drives import DriveEndpoint
from kp_sync.endpoints import EndpointUnavailable, FileEndpoint
from kp_sync.endpoints.file import _RenameUnsupported
from kp_sync.engine import SyncEngine, SyncError
from kp_sync.history import CopyRecord, Version, sha256_file
from kp_sync.kdbx import inspect_kdbx
from kp_sync.state import HistoryState
from kp_sync.ui.none import NoneUI
from kp_sync.ui.fake import FakeUI
from tests.fixtures import kdbx4_bytes
from tests.test_credentials import FakeKeyring, MultiKeyring


class RacingFileEndpoint(FileEndpoint):
    def __init__(self, name, path, replacement, **kwargs):
        super().__init__(name, path, **kwargs)
        self.replacement = replacement

    def store_if_unchanged(
        self,
        source,
        expected_sha256,
        *,
        before_replace=None,
        atomic_exchange=False,
    ):
        self.path.write_bytes(self.replacement)
        return super().store_if_unchanged(
            source,
            expected_sha256,
            before_replace=before_replace,
            atomic_exchange=atomic_exchange,
        )


class PartialWriteEndpoint(FileEndpoint):
    def store_if_unchanged(
        self,
        source,
        expected_sha256,
        *,
        before_replace=None,
        atomic_exchange=False,
    ):
        self.path.write_bytes(kdbx4_bytes(b"partial concurrent content" * 5))


class FailOnceEndpoint(FileEndpoint):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail = True

    def store_if_unchanged(
        self,
        source,
        expected_sha256,
        *,
        before_replace=None,
        atomic_exchange=False,
    ):
        if self.fail:
            self.fail = False
            raise EndpointUnavailable("synthetic target failure")
        return super().store_if_unchanged(
            source,
            expected_sha256,
            before_replace=before_replace,
            atomic_exchange=atomic_exchange,
        )


class LateRaceEndpoint(FileEndpoint):
    def __init__(self, name, path, replacement, **kwargs):
        super().__init__(name, path, **kwargs)
        self.replacement = replacement
        self.probes = 0

    def probe(self):
        self.probes += 1
        if self.probes == 5:
            self.path.write_bytes(self.replacement)
        return super().probe()


class StaticCredentials:
    def __init__(self, key_file):
        self.path = key_file

    def cached_password(self):
        return "test-password"

    def password(self, ui):
        return "test-password"

    def key_file(self, ui):
        return self.path

    def cache_password(self, password):
        pass


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(
            patch.object(
                CredentialProvider, "_backend", return_value=FakeKeyring()
            )
        )
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.hub_path = self.root / "hub.kdbx"
        config_path = self.root / "config.toml"
        config_path.write_text(
            f"hub = {json.dumps(str(self.hub_path))}\n"
            f'key_file = {json.dumps(str(self.root / "test.keyx"))}\n'
            "[sync]\nlock_timeout = 1\nmax_restarts = 1\n"
            "[backups]\nkeep = 5\n",
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
        self.copy_a = self.root / "copy-a.kdbx"
        self.copy_b = self.root / "copy-b.kdbx"
        self.endpoint_a = FileEndpoint("a", self.copy_a, identity="file:a")
        self.endpoint_b = FileEndpoint("b", self.copy_b, identity="file:b")
        self.engine = SyncEngine(
            self.config,
            (self.endpoint_a, self.endpoint_b),
            run_directory=self.run_dir,
            ui=NoneUI(),
        )

    def _write_all(self, data):
        self.hub_path.write_bytes(data)
        self.copy_a.write_bytes(data)
        self.copy_b.write_bytes(data)

    def _seed(self, current_data, *, stale_a=None):
        self._write_all(current_data)
        if stale_a is not None:
            self.copy_a.write_bytes(stale_a)
        sha, size = sha256_file(str(self.hub_path))
        state = HistoryState()
        state.commit(
            sha,
            (),
            "test-host",
            (self.engine.hub_identity, "file:a", "file:b"),
            size=size,
        )
        state.save(self.run_dir / "state.json")
        return state, sha

    def test_retirement_requires_cleanup_evidence_and_commits_locally(self):
        from kp_sync.sessions.records import SessionStore

        identity = "session:remote:aabbccdd"
        state = HistoryState()
        state.commit("a" * 64, (), "remote", (identity,), size=100)
        state.save(self.run_dir / "state.json")
        engine = SyncEngine(
            self.config,
            (),
            run_directory=self.run_dir,
            baseline_stores=(),
            ui=NoneUI(),
        )
        original = (self.run_dir / "state.json").read_bytes()
        preview = engine.session_retirement_preview(identity)
        self.assertEqual(preview["candidates"][0]["identity"], identity)
        self.assertTrue(preview["candidates"][0]["eligible"])
        self.assertEqual((self.run_dir / "state.json").read_bytes(), original)
        self.assertFalse((self.run_dir / "tmp").exists())
        with self.assertRaisesRegex(SyncError, "successful-cleanup"):
            engine.retire_session(identity)
        store = SessionStore(self.run_dir)
        store.ensure()
        dangling = store.staging_path("aabbccdd")
        dangling.symlink_to(self.root / "missing-stage")
        blocked_preview = engine.session_retirement_preview(identity)
        self.assertFalse(blocked_preview["candidates"][0]["eligible"])
        with self.assertRaisesRegex(SyncError, "runtime files"):
            engine.retire_session(identity, owner_confirmation=True)
        dangling.unlink()
        store.staging_path("aabbccdd").mkdir(parents=True)
        with self.assertRaisesRegex(SyncError, "runtime files"):
            engine.retire_session(identity, owner_confirmation=True)
        store.staging_path("aabbccdd").rmdir()
        store.prepare_retirement(identity, "aabbccdd")
        result = engine.retire_session(identity)
        retired = HistoryState.load(self.run_dir / "state.json")
        self.assertIn(identity, retired.retirements)
        self.assertNotIn(identity, retired.copies)
        self.assertEqual(result["runtime_history"], "committed")
        self.assertIsNone(result["publication_error"])

    def test_retirement_keeps_local_marker_when_publication_fails(self):
        from kp_sync.baseline import BaselineManager
        from kp_sync.sessions.records import SessionStore

        identity = "session:remote:11223344"
        state = HistoryState()
        state.commit("b" * 64, (), "remote", (identity,), size=100)
        state.save(self.run_dir / "state.json")
        store = SessionStore(self.run_dir)
        store.prepare_retirement(identity, "11223344")
        engine = SyncEngine(
            self.config,
            (),
            run_directory=self.run_dir,
            baseline_stores=(),
            ui=NoneUI(),
        )

        with patch.object(
            BaselineManager, "save", side_effect=OSError("offline")
        ):
            result = engine.retire_session(identity)

        self.assertIn(
            identity,
            HistoryState.load(self.run_dir / "state.json").retirements,
        )
        self.assertEqual(result["runtime_history"], "committed")
        self.assertEqual(result["publication_error"], "OSError: offline")

    def test_retirement_preview_reports_unavailable_store_without_writes(self):
        from kp_sync.endpoints.base import EndpointUnavailable

        class OfflineStore:
            name = "cloud:offline"
            cloud = True
            directory = "kp-sync"
            cache_path = None

            def fetch(self, relative, destination, *, max_age=None):
                raise EndpointUnavailable("offline")

        identity = "session:remote:22334455"
        state = HistoryState()
        state.commit("c" * 64, (), "remote", (identity,), size=100)
        state.save(self.run_dir / "state.json")
        original = (self.run_dir / "state.json").read_bytes()
        engine = SyncEngine(
            self.config,
            (),
            run_directory=self.run_dir,
            baseline_stores=(OfflineStore(),),
            ui=NoneUI(),
        )

        report = engine.session_retirement_preview(identity)

        self.assertEqual(
            report["shared_stores"], {"cloud:offline": "unavailable"}
        )
        self.assertEqual(report["candidates"][0]["identity"], identity)
        self.assertEqual((self.run_dir / "state.json").read_bytes(), original)
        self.assertFalse((self.run_dir / "history-cache").exists())

    def test_retirement_reports_each_store_after_partial_publication(self):
        from tests.test_baseline import FakeStore
        from kp_sync.sessions.records import SessionStore

        class FailingDrive(FakeStore):
            def put(self, relative, source):
                raise OSError("synthetic drive publication failure")

        identity = "session:remote:33445566"
        cloud = FakeStore(self.root / "baseline-cloud")
        drive = FailingDrive(
            self.root / "baseline-drive", name="drive:synthetic", cloud=False
        )
        shared = HistoryState()
        shared.commit("d" * 64, (), "remote", (identity,), size=100)
        shared.save(drive.root / "state.json")
        SessionStore(self.run_dir).prepare_retirement(identity, "33445566")
        engine = SyncEngine(
            self.config,
            (),
            run_directory=self.run_dir,
            baseline_stores=(cloud, drive),
            ui=NoneUI(),
        )

        result = engine.retire_session(identity)

        self.assertIn(
            identity,
            HistoryState.load(self.run_dir / "state.json").retirements,
        )
        self.assertIn(
            identity,
            HistoryState.load(cloud.root / "state.json").retirements,
        )
        self.assertIn(
            identity,
            HistoryState.load(drive.root / "state.json").copies,
        )
        self.assertEqual(result["shared_stores"]["cloud"], "published")
        self.assertEqual(
            result["shared_stores"]["drive:synthetic"],
            "failed: OSError: synthetic drive publication failure",
        )
        # The cloud holds the marker; the drive replica is best-effort.
        self.assertIsNone(result["publication_error"])

    def _unwritable_drive_engine(self, ui):
        old = kdbx4_bytes(b"drive copy before" * 100)
        new = kdbx4_bytes(b"drive copy after" * 100)
        mount = self.root / "drive"
        mount.mkdir()
        drive = DriveEndpoint(
            "pendrive",
            mount,
            "pass.kdbx",
            "serial:uuid",
            identity_resolver=lambda _: "serial:uuid",
        )
        drive.path.write_bytes(old)
        self._write_all(old)
        sha, size = sha256_file(str(self.hub_path))
        state = HistoryState()
        state.commit(
            sha,
            (),
            "test-host",
            (self.engine.hub_identity, "file:a", "file:b", drive.identity),
            size=size,
        )
        state.save(self.run_dir / "state.json")
        self.copy_a.write_bytes(new)
        # Like a read-only remount: readable, but not replaceable.
        mount.chmod(0o555)
        self.addCleanup(mount.chmod, 0o755)
        engine = SyncEngine(
            self.config,
            (self.endpoint_a, self.endpoint_b, drive),
            run_directory=self.run_dir,
            # Only the database copy is under test, not drive history.
            baseline_stores=(),
            ui=ui,
        )
        return engine, drive, mount, sha, sha256_file(str(self.copy_a))[0]

    def test_unwritable_drive_copy_is_skipped_and_left_pending(self):
        engine, drive, _, old, new = self._unwritable_drive_engine(NoneUI())
        with patch("kp_sync.log.error") as logged:
            result = engine.sync()

        self.assertEqual(result.action, "committed")
        self.assertEqual(result.current, new)
        self.assertTrue(result.consistent)
        self.assertNotIn(drive.identity, result.missing)
        self.assertIn(drive.identity, logged.call_args.args[0])
        self.assertEqual(sha256_file(str(self.hub_path))[0], new)
        self.assertEqual(sha256_file(str(self.copy_b))[0], new)
        self.assertEqual(sha256_file(str(drive.path))[0], old)
        state = HistoryState.load(self.run_dir / "state.json")
        self.assertEqual(state.copies[drive.identity].version, old)
        self.assertEqual(state.copies["file:b"].version, new)
        # The in-flight candidate does not wait for the skipped drive.
        inflight = self.run_dir / "inflight.json"
        if inflight.exists():
            self.assertEqual(
                json.loads(inflight.read_text())["candidates"], []
            )
        decision = result.decisions[f"drive-copy:{drive.identity}"]
        self.assertIn("database on pendrive serial:uuid", decision["reason"])

    def test_accepted_unwritable_drive_copy_clears_once_written(self):
        ui = FakeUI(confirmations=[True])
        engine, drive, mount, _, new = self._unwritable_drive_engine(ui)
        acknowledgements = self.run_dir / "replica-acknowledgements.json"
        with patch("kp_sync.log.error"):
            result = engine.sync()
            self.assertEqual(result.decisions, {})
            self.assertIn(
                f"drive-copy:{drive.identity}",
                json.loads(acknowledgements.read_text())["accepted"],
            )
            # Still unwritable: accepted, so no new question.
            result = engine.sync()
        self.assertEqual(result.decisions, {})
        self.assertEqual(sum(event[0] == "confirm" for event in ui.events), 1)
        mount.chmod(0o755)
        result = engine.sync()
        self.assertEqual(sha256_file(str(drive.path))[0], new)
        self.assertEqual(
            HistoryState.load(self.run_dir / "state.json")
            .copies[drive.identity]
            .version,
            new,
        )
        self.assertFalse(acknowledgements.exists())

    def test_drive_copy_changed_by_failed_write_still_fails(self):
        engine, drive, mount, old, _ = self._unwritable_drive_engine(NoneUI())
        mount.chmod(0o755)

        def partial(*args, **kwargs):
            drive.path.write_bytes(kdbx4_bytes(b"torn write" * 100))
            raise EndpointUnavailable("synthetic I/O error")

        with patch.object(drive, "store_if_unchanged", side_effect=partial):
            with self.assertRaisesRegex(SyncError, "synthetic I/O error"):
                engine.sync()
        state = HistoryState.load(self.run_dir / "state.json")
        self.assertEqual(state.copies[drive.identity].version, old)
        self.assertFalse((self.run_dir / "pending.json").exists())

    def test_unwritable_plain_file_endpoint_still_fails(self):
        engine, drive, mount, _, _ = self._unwritable_drive_engine(NoneUI())
        mount.chmod(0o755)
        with patch.object(
            self.endpoint_b,
            "store_if_unchanged",
            side_effect=EndpointUnavailable("synthetic read-only file"),
        ):
            with self.assertRaisesRegex(SyncError, "synthetic read-only"):
                engine.sync()

    def _replica_failure_engine(self, ui):
        from tests.test_baseline import FakeStore

        class FailingDrive(FakeStore):
            broken = True

            def put(self, relative, source):
                if self.broken:
                    raise OSError("synthetic full drive")
                super().put(relative, source)

        data = kdbx4_bytes(b"replica failure" * 100)
        state, sha = self._seed(data)
        cloud = FakeStore(self.root / "baseline-cloud")
        base = self.root / "seed.kdbx"
        base.write_bytes(data)
        cloud.put(f"bases/{sha}.kdbx", base)
        cloud.put("state.json", self.run_dir / "state.json")
        drive = FailingDrive(
            self.root / "baseline-drive", name="drive:synthetic", cloud=False
        )
        engine = SyncEngine(
            self.config,
            (self.endpoint_a,),
            run_directory=self.run_dir,
            baseline_stores=(cloud, drive),
            ui=ui,
        )
        return engine, cloud, drive, sha

    def test_failing_drive_replica_does_not_fail_cloud_published_sync(self):
        engine, cloud, drive, sha = self._replica_failure_engine(NoneUI())
        with patch("kp_sync.baseline.error") as logged:
            result = engine.sync()

        # Hub and cloud are done; only the drive warning is deferred.
        self.assertIsNone(result.pending)
        self.assertTrue(result.consistent)
        self.assertEqual(result.current, sha)
        self.assertIn("drive:synthetic", logged.call_args.args[0])
        self.assertEqual(
            HistoryState.load(cloud.root / "state.json").to_dict(),
            HistoryState.load(self.run_dir / "state.json").to_dict(),
        )
        self.assertFalse((drive.root / "state.json").exists())
        self.assertTrue((self.run_dir / "last-sync.json").exists())
        decision = result.decisions["drive-history:drive:synthetic"]
        self.assertIn("synthetic full drive", decision["reason"])
        self.assertFalse(
            (self.run_dir / "replica-acknowledgements.json").exists()
        )

    def test_declined_drive_replica_warning_stays_pending(self):
        ui = FakeUI(confirmations=[False])
        engine, _, _, _ = self._replica_failure_engine(ui)
        with patch("kp_sync.baseline.error"):
            result = engine.sync()
        prompts = [event for event in ui.events if event[0] == "confirm"]
        self.assertEqual(len(prompts), 1)
        self.assertIn("pendrive synthetic", prompts[0][1])
        self.assertIn("drive-history:drive:synthetic", result.decisions)
        self.assertTrue(result.consistent)

    def test_accepted_drive_replica_warning_is_kept_until_it_publishes(self):
        ui = FakeUI(confirmations=[True])
        engine, _, drive, _ = self._replica_failure_engine(ui)
        acknowledgements = self.run_dir / "replica-acknowledgements.json"
        with patch("kp_sync.baseline.error"):
            result = engine.sync()
            self.assertEqual(result.decisions, {})
            self.assertEqual(acknowledgements.stat().st_mode & 0o777, 0o600)
            # Still failing: accepted, so no new question or decision.
            result = engine.sync()
        self.assertEqual(result.decisions, {})
        self.assertEqual(sum(event[0] == "confirm" for event in ui.events), 1)
        drive.broken = False
        result = engine.sync()
        self.assertTrue((drive.root / "state.json").exists())
        self.assertFalse(acknowledgements.exists())
        drive.broken = True
        with patch("kp_sync.baseline.error"):
            # Changed history makes the drive outdated again.
            state = HistoryState.load(self.run_dir / "state.json")
            state.record_copies(result.current, ["hub:elsewhere"], "other")
            state.save(self.run_dir / "state.json")
            result = engine.sync()
        self.assertEqual(sum(event[0] == "confirm" for event in ui.events), 2)
        self.assertIn("drive-history:drive:synthetic", result.decisions)

    def test_online_sync_seeds_drive_history_for_cold_offline_boot(self):
        from kp_sync.baseline import DriveStore
        from kp_sync.daemon.server import Controller
        from tests.test_baseline import FakeStore

        data = kdbx4_bytes(b"offline startup" * 100)
        state, sha = self._seed(data)
        cloud = FakeStore(self.root / "baseline-cloud")
        base = self.root / "seed.kdbx"
        base.write_bytes(data)
        cloud.put(f"bases/{sha}.kdbx", base)
        cloud.put("state.json", self.run_dir / "state.json")
        mount = self.root / "drive"
        mount.mkdir()
        drive = DriveEndpoint(
            "pendrive",
            mount,
            "pass.kdbx",
            "serial:uuid",
            identity_resolver=lambda _: "serial:uuid",
        )
        drive.path.write_bytes(data)
        store = DriveStore(drive, "kp-sync")
        online = SyncEngine(
            self.config,
            (self.endpoint_a, drive),
            run_directory=self.run_dir,
            baseline_stores=(cloud, store),
            ui=NoneUI(),
        )
        self.assertFalse((store.root / "state.json").exists())
        self.assertIsNone(online.sync().pending)

        cold_root = self.root / "cold-runtime"
        cold_root.mkdir(mode=0o700)
        cold_runtime = cold_root / "kp-sync"
        cold_runtime.mkdir(mode=0o700)
        self.hub_path.unlink()
        cloud.online = False
        offline = SyncEngine(
            self.config,
            (
                FileEndpoint(
                    "cloud", self.root / "unreachable.kdbx", identity="file:a"
                ),
                drive,
            ),
            run_directory=cold_runtime,
            baseline_stores=(cloud, store),
            ui=NoneUI(),
        )
        with (
            patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(cold_root)}),
            patch("kp_sync.daemon.apps.notify_user"),
        ):
            controller = Controller(
                self.config, engine_factory=lambda ui: offline
            )
            self.addCleanup(controller.watch.close)
            self.addCleanup(lambda: controller.pool.shutdown(wait=True))
            self.addCleanup(lambda: controller.requests.shutdown(wait=True))
            self.assertEqual(controller.directory, cold_runtime)
            controller.apps = Mock()
            controller.apps.activate_existing.return_value = None
            controller.apps.refresh.return_value = ["app:keepassxc"]
            result = controller.handle({"command": "run", "ui": "none"})
        controller.apps.start.assert_called_once()
        self.assertFalse(result.get("pending"))
        self.assertEqual(self.hub_path.read_bytes(), data)
        self.assertEqual(result["current"], sha)
        self.assertIn("file:a", result["missing"])
        self.assertEqual(
            result["classifications"][drive.identity], "unchanged"
        )
        self.assertEqual(
            HistoryState.load(cold_runtime / "state.json")
            .copies[offline.hub_identity]
            .version,
            sha,
        )

    def _real_database(self):
        key_file = self.root / "test.keyx"
        key_file.write_bytes(b"synthetic-key-file")
        create_database(
            str(self.hub_path), password="test-password", keyfile=key_file
        )
        database = PyKeePass(
            str(self.hub_path), password="test-password", keyfile=key_file
        )
        database.add_entry(database.root_group, "Example", "user", "secret")
        database.save()
        self._seed(self.hub_path.read_bytes())
        sha, _ = sha256_file(str(self.hub_path))
        self.engine._save_base(self.hub_path, sha)
        return key_file, database

    def test_no_reachable_copy_is_unavailable_without_a_new_decision(self):
        from kp_sync.pending import PendingStore

        result = self.engine.sync()

        self.assertEqual(result.action, "unavailable")
        self.assertIsNone(result.pending)
        self.assertFalse(result.consistent)
        self.assertTrue(
            all(
                value == "missing" for value in result.classifications.values()
            )
        )
        pending = PendingStore(self.run_dir)
        self.assertEqual(pending.load()["decisions"], {})
        pending.save(
            "sync", "existing conflict", {"copies": {"file:a": "new"}}
        )
        original_pending = pending.pending_path.read_bytes()

        result = self.engine.sync()

        self.assertEqual(result.action, "unavailable")
        self.assertEqual(pending.pending_path.read_bytes(), original_pending)

    def test_changed_source_and_unknown_copy_both_contribute(self):
        key_file, database = self._real_database()
        state = HistoryState.load(self.run_dir / "state.json")
        del state.copies["file:b"]
        state.save(self.run_dir / "state.json")
        changed = PyKeePass(
            str(self.copy_a), password="test-password", keyfile=key_file
        )
        changed.entries[0].password = "changed-password"
        changed.save()
        unknown = PyKeePass(
            str(self.copy_b), password="test-password", keyfile=key_file
        )
        added = unknown.add_entry(
            unknown.root_group, "New", "new-user", "new-secret"
        )
        unknown.save()
        engine = SyncEngine(
            replace(self.config, key_file=key_file),
            (self.endpoint_a, self.endpoint_b),
            run_directory=self.run_dir,
            credentials=StaticCredentials(key_file),
            ui=FakeUI(
                confirmations=(True,),
                reviews=({f"unknown-entry:{added.uuid.hex}": "other"},),
            ),
        )

        self.assertEqual(engine.sync().action, "committed")
        merged = PyKeePass(
            str(self.hub_path), password="test-password", keyfile=key_file
        )
        self.assertEqual(
            {entry.title for entry in merged.entries}, {"Example", "New"}
        )
        self.assertEqual(merged.entries[0].password, "changed-password")

    def test_review_releases_lock_and_restarts_if_a_source_changes(self):
        key_file, database = self._real_database()
        identifier = database.entries[0].uuid.hex
        for path, username in ((self.copy_a, "left"), (self.copy_b, "right")):
            copy_db = PyKeePass(
                str(path), password="test-password", keyfile=key_file
            )
            copy_db.entries[0].username = username
            copy_db.save()
        test = self

        class ConcurrentUI(FakeUI):
            def review(self, conflicts):
                descriptor = os.open(test.run_dir / "lock", os.O_RDWR)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(descriptor)
                if not any(event == "review" for event, _ in self.events):
                    hub = PyKeePass(
                        str(test.hub_path),
                        password="test-password",
                        keyfile=key_file,
                    )
                    hub.entries[0].url = "https://concurrent.example"
                    hub.save()
                self.events.append(("review", None))
                return {f"entry:{identifier}:String:UserName": "other"}

        ui = ConcurrentUI(confirmations=(True,))
        engine = SyncEngine(
            replace(self.config, key_file=key_file),
            (self.endpoint_a, self.endpoint_b),
            run_directory=self.run_dir,
            credentials=StaticCredentials(key_file),
            ui=ui,
        )
        stores = []
        original_store = FileEndpoint.store_if_unchanged

        def store_after_review(endpoint, *args, **kwargs):
            stores.append(sum(event == "review" for event, _ in ui.events))
            return original_store(endpoint, *args, **kwargs)

        with patch.object(
            FileEndpoint, "store_if_unchanged", store_after_review
        ):
            result = engine.sync()
        self.assertEqual(result.action, "committed")
        self.assertEqual(result.restarted, 1)
        self.assertTrue(stores)
        self.assertGreaterEqual(min(stores), 2)
        merged = PyKeePass(
            str(self.hub_path), password="test-password", keyfile=key_file
        )
        self.assertEqual(merged.entries[0].url, "https://concurrent.example")
        self.assertEqual(merged.entries[0].username, "right")

    def test_one_sided_master_key_change_retries_cached_credentials_or_defers(
        self,
    ):
        key_file, database = self._real_database()
        candidate = PyKeePass(
            str(self.copy_a), password="test-password", keyfile=key_file
        )
        candidate.password = "new-password"
        candidate.save()
        engine = SyncEngine(
            replace(self.config, key_file=key_file),
            (self.endpoint_a, self.endpoint_b),
            run_directory=self.run_dir,
            credentials=StaticCredentials(key_file),
            ui=NoneUI(),
        )
        before = self.hub_path.read_bytes()
        self.assertEqual(engine.sync().action, "pending")
        self.assertEqual(self.hub_path.read_bytes(), before)
        engine.ui = FakeUI(
            confirmations=(True,),
            passwords=("new-password",),
            choices=("configured key file",),
        )
        self.assertEqual(engine.sync().action, "committed")
        merged = PyKeePass(
            str(self.hub_path), password="new-password", keyfile=key_file
        )
        self.assertEqual(merged.entries[0].title, "Example")

    def test_ask_first_defer_leaves_copies_and_review_untouched(self):
        key_file, database = self._real_database()
        for path, username in ((self.copy_a, "left"), (self.copy_b, "right")):
            changed = PyKeePass(
                str(path), password="test-password", keyfile=key_file
            )
            changed.entries[0].username = username
            changed.save()
        originals = [
            path.read_bytes()
            for path in (self.hub_path, self.copy_a, self.copy_b)
        ]
        ui = FakeUI(confirmations=(False,))
        engine = SyncEngine(
            replace(self.config, key_file=key_file),
            (self.endpoint_a, self.endpoint_b),
            run_directory=self.run_dir,
            credentials=StaticCredentials(key_file),
            ui=ui,
        )
        self.assertEqual(engine.sync().action, "pending")
        self.assertEqual([event for event, _ in ui.events], ["confirm"])
        self.assertEqual(
            [
                path.read_bytes()
                for path in (self.hub_path, self.copy_a, self.copy_b)
            ],
            originals,
        )
        engine.ui = FakeUI(
            confirmations=(True,),
            reviews=(
                {
                    (
                        f"entry:{database.entries[0].uuid.hex}:String:UserName"
                    ): "other"
                },
            ),
        )
        self.assertEqual(engine.sync().action, "committed")
        self.assertFalse((self.run_dir / "pending.json").exists())

    def test_one_known_change_promotes_and_backs_up_every_target_first(self):
        original = kdbx4_bytes(b"original database" * 15)
        updated = kdbx4_bytes(b"updated database" * 20)
        self._seed(original)
        self.copy_b.write_bytes(updated)
        events = []
        original_backup = __import__(
            "kp_sync.engine", fromlist=["backup_file"]
        ).backup_file
        original_store = FileEndpoint.store_if_unchanged

        def tracked_backup(*args, **kwargs):
            path = original_backup(*args, **kwargs)
            events.append(("backup", kwargs["identity"]))
            return path

        def tracked_store(
            endpoint,
            source,
            expected_sha256,
            *,
            before_replace=None,
            atomic_exchange=False,
        ):
            if endpoint.identity in {self.engine.hub_identity, "file:a"}:
                self.assertEqual(events[-1], ("backup", endpoint.identity))
            events.append(("store", endpoint.identity))
            return original_store(
                endpoint,
                source,
                expected_sha256,
                before_replace=before_replace,
                atomic_exchange=atomic_exchange,
            )

        with (
            patch("kp_sync.engine.backup_file", side_effect=tracked_backup),
            patch.object(FileEndpoint, "store_if_unchanged", tracked_store),
        ):
            result = self.engine.sync()

        self.assertEqual(result.action, "committed")
        self.assertEqual(self.hub_path.read_bytes(), updated)
        self.assertEqual(self.copy_a.read_bytes(), updated)
        self.assertEqual(self.copy_b.read_bytes(), updated)
        backups = list((self.run_dir / "backups").glob("*.kdbx"))
        self.assertEqual(len(backups), 2)
        state = HistoryState.load(self.run_dir / "state.json")
        updated_sha, _ = sha256_file(str(self.copy_b))
        self.assertEqual(result.current, updated_sha)
        self.assertEqual(
            state.copies[self.engine.hub_identity].version, updated_sha
        )
        self.assertEqual(len(state.versions), 1)
        self.assertEqual(state.versions[updated_sha].parents, ())

    def test_engine_promotes_when_atomic_exchange_is_unsupported(self):
        original = kdbx4_bytes(b"original database" * 15)
        updated = kdbx4_bytes(b"updated database" * 20)
        self._seed(original)
        self.copy_b.write_bytes(updated)

        with patch(
            "kp_sync.endpoints.file._rename_exchange",
            side_effect=_RenameUnsupported("exchange unsupported"),
        ):
            result = self.engine.sync()

        self.assertEqual(result.action, "committed")
        self.assertEqual(self.hub_path.read_bytes(), updated)
        self.assertEqual(self.copy_a.read_bytes(), updated)
        self.assertEqual(self.copy_b.read_bytes(), updated)
        self.assertEqual(
            list(self.root.glob(".*.kp-sync-displaced-*")),
            [],
        )

    def test_displaced_known_duplicate_is_recovered_before_endpoint_probe(
        self,
    ):
        original = kdbx4_bytes(b"recorded database" * 15)
        self._seed(original)
        displaced = self.copy_a.with_name(
            f".{self.copy_a.name}.kp-sync-displaced-{'a' * 32}"
        )
        displaced.write_bytes(original)
        original_probe = FileEndpoint.probe
        saw_recovered = []

        def probe_after_recovery(endpoint):
            if endpoint.path == self.copy_a:
                saw_recovered.append(not displaced.exists())
            return original_probe(endpoint)

        with patch.object(FileEndpoint, "probe", probe_after_recovery):
            result = self.engine.sync()

        self.assertEqual(result.action, "unchanged")
        self.assertEqual(saw_recovered, [True])
        self.assertFalse(displaced.exists())

    def test_displaced_mismatch_is_backed_up_and_leaves_sync_pending(self):
        original = kdbx4_bytes(b"recorded database" * 15)
        self._seed(original)
        unknown = (
            b"preserve first interrupted write",
            b"preserve second interrupted write",
        )
        displaced = [
            self.copy_a.with_name(
                f".{self.copy_a.name}.kp-sync-displaced-{token * 32}"
            )
            for token in ("b", "c")
        ]
        for path, data in zip(displaced, unknown):
            path.write_bytes(data)

        def probe_if_called(endpoint):
            self.fail(
                "endpoint probing must wait until displaced recovery "
                "is resolved"
            )

        with patch.object(FileEndpoint, "probe", probe_if_called):
            result = self.engine.sync()

        self.assertEqual(result.action, "pending")
        self.assertIn("displaced content", result.pending)
        self.assertEqual(
            [path.read_bytes() for path in displaced], list(unknown)
        )
        marker = json.loads((self.run_dir / "write-race.json").read_text())
        self.assertEqual(marker["identity"], "file:a")
        self.assertEqual(len(marker["backups"]), 2)
        self.assertEqual(
            self.engine.unresolved_write_race(),
            ("file:a", self.run_dir / "backups" / marker["backup"]),
        )
        for item, expected in zip(marker["backups"], unknown):
            backup = self.run_dir / "backups" / item["backup"]
            self.assertEqual(backup.read_bytes(), expected)
            self.assertEqual(
                hashlib.sha256(backup.read_bytes()).hexdigest(),
                item["sha256"],
            )
        self.assertEqual(self.copy_a.read_bytes(), original)
        second_result = self.engine.sync()
        self.assertEqual(second_result.action, "pending")
        self.assertIn("remains pending", second_result.pending)

    def test_preserved_concurrent_save_is_merged_and_resolved(self):
        key_file, database = self._real_database()
        concurrent = self.root / "concurrent.kdbx"
        shutil.copyfile(self.copy_a, concurrent)
        saved = PyKeePass(
            str(concurrent), password="test-password", keyfile=key_file
        )
        added = saved.add_entry(
            saved.root_group, "Raced", "race-user", "race-secret"
        )
        saved.save()
        displaced = self.copy_a.with_name(
            f".{self.copy_a.name}.kp-sync-displaced-{'e' * 32}"
        )
        shutil.copyfile(concurrent, displaced)
        engine = SyncEngine(
            replace(self.config, key_file=key_file),
            (self.endpoint_a, self.endpoint_b),
            run_directory=self.run_dir,
            credentials=StaticCredentials(key_file),
            ui=NoneUI(),
        )
        self.assertEqual(engine.sync().action, "pending")
        self.assertTrue((self.run_dir / "write-race.json").exists())

        engine.ui = FakeUI(
            confirmations=(True,),
            reviews=({f"unknown-entry:{added.uuid.hex}": "other"},),
        )
        result = engine.sync()
        self.assertEqual(result.action, "committed", result.pending)
        self.assertFalse((self.run_dir / "write-race.json").exists())
        for path in (self.hub_path, self.copy_a, self.copy_b):
            merged = PyKeePass(
                str(path), password="test-password", keyfile=key_file
            )
            self.assertEqual(
                {entry.title for entry in merged.entries}, {"Example", "Raced"}
            )
        state = HistoryState.load(self.run_dir / "state.json")
        self.assertFalse(
            any(key.startswith("recovered:") for key in state.copies)
        )
        self.assertEqual(list((self.run_dir / "recovered").iterdir()), [])
        self.assertTrue(displaced.exists())

        engine.ui = NoneUI()
        self.assertEqual(engine.sync().action, "unchanged")
        self.assertFalse(displaced.exists())

    def test_drive_identity_is_rechecked_during_displaced_recovery(self):
        original = kdbx4_bytes(b"recorded database" * 15)
        self._seed(original)
        mountpoint = self.root / "drive"
        mountpoint.mkdir()
        target = mountpoint / "pass.kdbx"
        target.write_bytes(original)
        identity = "SERIAL:UUID"
        checks = []

        def resolve_identity(path):
            checks.append(Path(path))
            return identity

        drive = DriveEndpoint(
            "drive",
            mountpoint,
            "pass.kdbx",
            identity,
            identity_resolver=resolve_identity,
        )
        displaced = mountpoint / f".pass.kdbx.kp-sync-displaced-{'d' * 32}"
        displaced.write_bytes(original)
        state = HistoryState.load(self.run_dir / "state.json")
        sha = next(iter(state.versions))
        state.record_copies(sha, (drive.identity,), self.config.host)
        state.save(self.run_dir / "state.json")
        engine = SyncEngine(
            self.config,
            (drive,),
            run_directory=self.run_dir,
            ui=NoneUI(),
        )

        self.assertIsNone(engine._recover_displaced_endpoints(state))

        self.assertGreaterEqual(len(checks), 2)
        self.assertFalse(displaced.exists())

    def test_dry_run_does_not_recover_or_backup_displaced_content(self):
        original = kdbx4_bytes(b"recorded database" * 15)
        self._seed(original)
        displaced = self.copy_a.with_name(
            f".{self.copy_a.name}.kp-sync-displaced-{'c' * 32}"
        )
        displaced.write_bytes(b"unknown content")
        before = displaced.read_bytes()

        result = self.engine.sync(dry_run=True)

        self.assertEqual(result.action, "unchanged")
        self.assertEqual(displaced.read_bytes(), before)
        self.assertFalse((self.run_dir / "write-race.json").exists())
        self.assertEqual(list((self.run_dir / "backups").glob("*.kdbx")), [])

    def test_two_independent_changes_are_merged_and_promoted(self):
        class TestCredentials:
            def __init__(self, key_file):
                self.key_file_path = key_file

            def cached_password(self):
                return "test-password"

            def password(self, ui):
                return "test-password"

            def key_file(self, ui):
                return self.key_file_path

            def cache_password(self, password):
                return None

        key_file = self.root / "keyfile.keyx"
        key_file.write_bytes(b"test key file contents")
        original = self.root / "original.kdbx"
        create_database(
            str(original), password="test-password", keyfile=str(key_file)
        )
        database = PyKeePass(
            str(original), password="test-password", keyfile=str(key_file)
        )
        database.add_entry(
            database.root_group,
            "Example",
            "user",
            "initial-password",
            url="https://old.example.test",
        )
        database.save()
        original_data = original.read_bytes()
        self._write_all(original_data)
        current_sha, size = sha256_file(str(self.hub_path))
        state = HistoryState()
        state.commit(
            current_sha,
            (),
            "test-host",
            (self.engine.hub_identity, "file:a", "file:b"),
            size=size,
        )
        state.save(self.run_dir / "state.json")
        self.engine._save_base(self.hub_path, current_sha)

        left = PyKeePass(
            str(self.copy_a), password="test-password", keyfile=str(key_file)
        )
        left.entries[0].password = "left-password"
        left.save()
        right = PyKeePass(
            str(self.copy_b), password="test-password", keyfile=str(key_file)
        )
        right.entries[0].url = "https://right.example.test"
        right.save()

        self.config = replace(self.config, key_file=key_file)
        self.engine = SyncEngine(
            self.config,
            (self.endpoint_a, self.endpoint_b),
            run_directory=self.run_dir,
            ui=NoneUI(),
            credentials=TestCredentials(key_file),
        )

        result = self.engine.sync()

        self.assertEqual(result.action, "committed")
        for path in (self.hub_path, self.copy_a, self.copy_b):
            merged = PyKeePass(
                str(path), password="test-password", keyfile=str(key_file)
            )
            self.assertEqual(merged.entries[0].password, "left-password")
            self.assertEqual(
                merged.entries[0].url, "https://right.example.test"
            )

    def test_foreign_stale_cache_edits_preserve_new_entries(
        self,
    ):
        class TestCredentials:
            def __init__(self, key_file):
                self.key_file_path = key_file

            def cached_password(self):
                return "test-password"

            def password(self, ui):
                return "test-password"

            def key_file(self, ui):
                return self.key_file_path

            def cache_password(self, password):
                return None

        key_file = self.root / "foreign-keyfile.keyx"
        key_file.write_bytes(b"test key file contents")
        initial = self.root / "initial.kdbx"
        create_database(
            str(initial), password="test-password", keyfile=str(key_file)
        )
        database = PyKeePass(
            str(initial), password="test-password", keyfile=str(key_file)
        )
        database.add_entry(database.root_group, "A", "user", "old-password")
        database.save()
        stale_copy = self.root / "stale-copy.kdbx"
        stale_copy.write_bytes(initial.read_bytes())

        database.add_entry(database.root_group, "B", "user", "keep-current")
        database.save()
        current_data = initial.read_bytes()
        self.hub_path.write_bytes(current_data)
        self.copy_a.write_bytes(current_data)
        self.copy_b.write_bytes(stale_copy.read_bytes())
        current_sha, size = sha256_file(str(self.hub_path))
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        state = HistoryState()
        state.commit(
            current_sha,
            (),
            "test-host",
            (self.engine.hub_identity, "file:a", "file:b"),
            size=size,
            synced_at=yesterday.isoformat(),
        )
        state.save(self.run_dir / "state.json")
        self.engine._save_base(self.hub_path, current_sha)

        stale = PyKeePass(
            str(self.copy_b), password="test-password", keyfile=str(key_file)
        )
        stale.entries[0].password = "phone-password"
        modified_at = int(
            (
                datetime.now(timezone.utc)
                - datetime(1, 1, 1, tzinfo=timezone.utc)
            ).total_seconds()
        )
        stale.entries[0]._element.find("Times/LastModificationTime").text = (
            base64.b64encode(struct.pack("<Q", modified_at)).decode()
        )
        stale.add_entry(stale.root_group, "C", "user", "phone-created")
        stale.save()

        foreign = FileEndpoint(
            "phone-writable",
            self.copy_b,
            identity="file:b",
            foreign_writers=True,
        )
        config = replace(self.config, key_file=key_file)
        engine = SyncEngine(
            config,
            (self.endpoint_a, foreign),
            run_directory=self.run_dir,
            ui=NoneUI(),
            credentials=TestCredentials(key_file),
        )

        result = engine.sync()

        self.assertEqual(result.action, "committed")
        for path in (self.hub_path, self.copy_a, self.copy_b):
            merged = PyKeePass(
                str(path), password="test-password", keyfile=str(key_file)
            )
            self.assertEqual(
                {entry.title for entry in merged.entries}, {"A", "B", "C"}
            )
            entry_a = next(
                entry for entry in merged.entries if entry.title == "A"
            )
            self.assertEqual(entry_a.password, "phone-password")

    def test_mistyped_password_is_not_left_in_the_keyring(self):
        key_file = self.root / "typo.keyx"
        key_file.write_bytes(b"typo key file")
        create_database(
            str(self.hub_path), password="right", keyfile=str(key_file)
        )
        config = replace(self.config, key_file=key_file)
        credentials = CredentialProvider(config)
        engine = SyncEngine(
            config,
            (),
            run_directory=self.run_dir,
            ui=FakeUI(passwords=("wrong", "right")),
            credentials=credentials,
        )
        engine._credential_probes = (self.hub_path,)
        self.assertEqual(engine._request_credentials(), ("right", key_file))
        self.assertEqual(credentials.cached_password(), "right")

        engine = SyncEngine(
            config,
            (),
            run_directory=self.run_dir,
            ui=FakeUI(passwords=("wrong",) * 3),
            credentials=credentials,
        )
        credentials.forget()
        engine._credential_probes = (self.hub_path,)
        with self.assertRaisesRegex(SyncError, "rejected three times"):
            engine._request_credentials()
        self.assertIsNone(credentials.cached_password())

    def test_keepassxc_merge_credentials_are_not_cached_or_retained(self):
        key_file, _ = self._real_database()
        config = replace(self.config, key_file=key_file)
        config.credentials["keepassxc"] = {
            "url": "https://kp-sync-test.invalid"
        }
        config.ui["ask_first"] = False
        credentials = CredentialProvider(config)
        credentials.cache_password("older-manual-password")
        engine = SyncEngine(
            config,
            (self.endpoint_a, self.endpoint_b),
            run_directory=self.run_dir,
            credentials=credentials,
            ui=FakeUI(),
        )
        with patch(
            "kp_sync.keepassxc.retrieve", return_value="test-password"
        ) as retrieve:
            for index in range(2):
                for path, title in (
                    (self.copy_a, "from a"),
                    (self.copy_b, "from b"),
                ):
                    db = PyKeePass(
                        str(path),
                        password="test-password",
                        keyfile=str(key_file),
                    )
                    db.add_entry(
                        db.root_group, f"{title} {index}", "user", "secret"
                    )
                    db.save()
                self.assertEqual(engine.sync().action, "committed")
                self.assertEqual(
                    credentials.cached_password(), "older-manual-password"
                )
                self.assertIsNone(engine._browser_credentials)
                self.assertEqual(engine._known_credentials, [])
                self.assertEqual(engine._transient_passwords, set())
        self.assertEqual(retrieve.call_count, 2)

    def test_keepassxc_bad_password_falls_back_once_and_headless_skips(self):
        key_file, _ = self._real_database()
        config = replace(self.config, key_file=key_file)
        config.credentials["keepassxc"] = {
            "url": "https://kp-sync-test.invalid"
        }
        credentials = CredentialProvider(config)
        ui = FakeUI(passwords=("test-password",))
        engine = SyncEngine(
            config,
            (),
            run_directory=self.run_dir,
            credentials=credentials,
            ui=ui,
        )
        engine._credential_probes = (self.hub_path,)
        with (
            patch("kp_sync.keepassxc.retrieve", return_value="invalid") as get,
            patch.object(ui, "notify") as notify,
        ):
            for _ in range(2):
                self.assertEqual(
                    engine._request_credentials(), ("test-password", key_file)
                )
            get.assert_called_once()
            notify.assert_called_once()
        self.assertEqual(credentials.cached_password(), "test-password")
        engine.ui = NoneUI()
        engine._browser_attempted = False
        with patch("kp_sync.keepassxc.retrieve") as get:
            self.assertEqual(
                engine._request_credentials(), ("test-password", key_file)
            )
            get.assert_not_called()

    def test_optional_content_hashing_never_queries_keepassxc(self):
        self.config.credentials["keepassxc"] = {
            "url": "https://kp-sync-test.invalid"
        }
        with patch("kp_sync.keepassxc.retrieve") as get:
            self.assertIsNone(self.engine._cached_password())
            get.assert_not_called()

    def test_browser_password_missing_key_file_defers_without_caching(self):
        self.config.credentials["keepassxc"] = {
            "url": "https://kp-sync-test.invalid"
        }
        self.engine.ui = FakeUI()
        with (
            patch("kp_sync.keepassxc.retrieve", return_value="test-secret"),
            patch.object(
                self.engine.credentials, "key_file", return_value=None
            ) as select,
            patch.object(self.engine.credentials, "password") as ask,
            patch.object(self.engine.credentials, "cache_password") as cache,
        ):
            self.assertEqual(self.engine._request_credentials(), (None, None))
            select.assert_called_once()
            ask.assert_not_called()
            cache.assert_not_called()

    def test_browser_approval_revalidates_inputs_before_promotion(self):
        key_file, _ = self._real_database()
        config = replace(self.config, key_file=key_file)
        config.credentials["keepassxc"] = {
            "url": "https://kp-sync-test.invalid"
        }
        config.ui["ask_first"] = False
        for path, title in ((self.copy_a, "from a"), (self.copy_b, "from b")):
            db = PyKeePass(
                str(path), password="test-password", keyfile=str(key_file)
            )
            db.add_entry(db.root_group, title, "user", "secret")
            db.save()
        engine = SyncEngine(
            config,
            (self.endpoint_a, self.endpoint_b),
            run_directory=self.run_dir,
            ui=FakeUI(),
        )

        def changed_during_approval(*args):
            db = PyKeePass(
                str(self.copy_b),
                password="test-password",
                keyfile=str(key_file),
            )
            db.add_entry(db.root_group, "late save", "user", "secret")
            db.save()
            return "test-password"

        with patch(
            "kp_sync.keepassxc.retrieve", side_effect=changed_during_approval
        ) as get:
            result = engine.sync()
        self.assertEqual(result.action, "committed")
        self.assertEqual(result.restarted, 1)
        get.assert_called_once()
        merged = PyKeePass(
            str(self.hub_path), password="test-password", keyfile=str(key_file)
        )
        self.assertIn("late save", {entry.title for entry in merged.entries})

    def test_alternate_copy_password_uses_its_own_keyring_slot(self):
        key_file = self.root / "slot.keyx"
        key_file.write_bytes(b"slot key file")
        old_copy = self.root / "old-drive.kdbx"
        create_database(
            str(old_copy), password="copy-secret", keyfile=str(key_file)
        )
        config = replace(self.config, key_file=key_file)
        keyring = MultiKeyring()
        self.enterContext(
            patch.object(CredentialProvider, "_backend", return_value=keyring)
        )
        credentials = CredentialProvider(config, keyring)
        credentials.cache_password("main-secret")
        ui = FakeUI(
            passwords=("copy-secret",), choices=("configured key file",)
        )
        engine = SyncEngine(
            config,
            (),
            run_directory=self.run_dir,
            ui=ui,
            credentials=credentials,
        )
        engine._path_identities = {old_copy: "drive:OLD:UUID"}

        self.assertEqual(
            engine._resolve_alternate_credentials(old_copy),
            ("copy-secret", key_file),
        )
        self.assertEqual(credentials.cached_password(), "main-secret")

        again = SyncEngine(
            config,
            (),
            run_directory=self.run_dir,
            ui=FakeUI(),
            credentials=credentials,
        )
        again._path_identities = {old_copy: "drive:OLD:UUID"}
        self.assertEqual(
            again._resolve_alternate_credentials(old_copy),
            ("copy-secret", key_file),
        )
        again._forget_copy_passwords()
        self.assertEqual(set(keyring.live()), {b"kp-sync:default:password"})

    def test_engine_merges_and_promotes_a_different_master_key(self):
        class TestCredentials:
            def __init__(self, key_file):
                self.key_file_path = key_file
                self.current_password = "old-password"

            def cached_password(self):
                return self.current_password

            def password(self, ui):
                return self.current_password

            def key_file(self, ui):
                return self.key_file_path

            def cache_password(self, password):
                self.current_password = password

        key_file = self.root / "master-key.keyx"
        key_file.write_bytes(b"master key test file")
        original = self.root / "old-master.kdbx"
        create_database(
            str(original), password="old-password", keyfile=str(key_file)
        )
        database = PyKeePass(
            str(original), password="old-password", keyfile=str(key_file)
        )
        database.add_entry(
            database.root_group,
            "Example",
            "user",
            "initial-password",
            url="https://old.example.test",
        )
        database.save()
        original_data = original.read_bytes()
        self._write_all(original_data)
        current_sha, size = sha256_file(str(self.hub_path))
        state = HistoryState()
        state.commit(
            current_sha,
            (),
            "test-host",
            (self.engine.hub_identity, "file:a", "file:b"),
            size=size,
        )
        state.save(self.run_dir / "state.json")
        self.engine._save_base(self.hub_path, current_sha)

        old_key_copy = PyKeePass(
            str(self.copy_a), password="old-password", keyfile=str(key_file)
        )
        old_key_copy.entries[0].password = "old-key-edit"
        old_key_copy.save()

        new_key_path = self.copy_b
        create_database(
            str(new_key_path), password="new-password", keyfile=str(key_file)
        )
        new_key_db = PyKeePass(
            str(new_key_path), password="new-password", keyfile=str(key_file)
        )
        incoming_root = new_key_db.tree.find("./Root")
        old_root = database.tree.find("./Root/Group")
        current_root = incoming_root.find("Group")
        incoming_root.replace(current_root, copy.deepcopy(old_root))
        incoming_entry = new_key_db.entries[0]
        incoming_entry.url = "https://new-key.example.test"
        master_changed = int(
            (
                datetime.now(timezone.utc)
                + timedelta(days=1)
                - datetime(1, 1, 1, tzinfo=timezone.utc)
            ).total_seconds()
        )
        new_key_db.tree.find("./Meta/MasterKeyChanged").text = (
            base64.b64encode(struct.pack("<Q", master_changed)).decode()
        )
        new_key_db.save()

        credentials = TestCredentials(key_file)
        engine = SyncEngine(
            replace(self.config, key_file=key_file),
            (self.endpoint_a, self.endpoint_b),
            run_directory=self.run_dir,
            ui=FakeUI(
                confirmations=(True,),
                passwords=("new-password",),
                choices=("configured key file",),
            ),
            credentials=credentials,
        )

        result = engine.sync()

        self.assertEqual(result.action, "committed")
        self.assertEqual(credentials.current_password, "new-password")
        for path in (self.hub_path, self.copy_a, self.copy_b):
            merged = PyKeePass(
                str(path), password="new-password", keyfile=str(key_file)
            )
            self.assertEqual(merged.entries[0].password, "old-key-edit")
            self.assertEqual(
                merged.entries[0].url, "https://new-key.example.test"
            )

    def test_first_run_reviews_differing_unknown_copies(self):
        class TestCredentials:
            def __init__(self, key_file):
                self.key_file_path = key_file

            def cached_password(self):
                return "test-password"

            def password(self, ui):
                return "test-password"

            def key_file(self, ui):
                return self.key_file_path

            def cache_password(self, password):
                return None

        key_file = self.root / "initial-keyfile.keyx"
        key_file.write_bytes(b"initial key file")
        original = self.root / "initial-unknown.kdbx"
        create_database(
            str(original), password="test-password", keyfile=str(key_file)
        )
        database = PyKeePass(
            str(original), password="test-password", keyfile=str(key_file)
        )
        entry = database.add_entry(
            database.root_group, "Example", "hub-user", "secret"
        )
        database.save()
        self.hub_path.write_bytes(original.read_bytes())
        self.copy_a.write_bytes(original.read_bytes())
        self.copy_b.write_bytes(original.read_bytes())
        incoming = PyKeePass(
            str(self.copy_a), password="test-password", keyfile=str(key_file)
        )
        incoming.entries[0].username = "other-user"
        incoming.save()

        review = {f"unknown-entry:{entry.uuid.hex}": "other"}
        ui = FakeUI(confirmations=(True,), reviews=(review,))
        engine = SyncEngine(
            replace(self.config, key_file=key_file),
            (self.endpoint_a, self.endpoint_b),
            run_directory=self.run_dir,
            ui=ui,
            credentials=TestCredentials(key_file),
        )

        result = engine.sync()

        self.assertEqual(result.action, "committed")
        self.assertTrue(
            any(event == "review" for event, _payload in ui.events),
            f"expected unknown-copy review; UI events were {ui.events!r}",
        )
        for path in (self.hub_path, self.copy_a, self.copy_b):
            merged = PyKeePass(
                str(path), password="test-password", keyfile=str(key_file)
            )
            self.assertEqual(len(merged.entries), 1)
            self.assertEqual(merged.entries[0].username, "other-user")
        state = HistoryState.load(self.run_dir / "state.json")
        version = state.versions[state.copies[engine.hub_identity].version]
        self.assertEqual(version.parents, ())

    def test_stale_copy_is_updated_from_current_without_password(self):
        old = kdbx4_bytes(b"old database content" * 15)
        current = kdbx4_bytes(b"current database content" * 20)
        self._write_all(current)
        self.copy_a.write_bytes(old)
        state = HistoryState()
        old_path = self.root / "old.kdbx"
        old_path.write_bytes(old)
        old_sha, old_size = sha256_file(str(old_path))
        current_sha, current_size = sha256_file(str(self.hub_path))
        state.commit(old_sha, (), "host", (), size=old_size)
        state.commit(
            current_sha,
            (old_sha,),
            "host",
            (self.engine.hub_identity, "file:a", "file:b"),
            size=current_size,
        )
        state.save(self.run_dir / "state.json")

        result = self.engine.sync()

        self.assertEqual(result.action, "committed")
        self.assertEqual(self.copy_a.read_bytes(), current)
        self.assertEqual(
            len(HistoryState.load(self.run_dir / "state.json").versions), 1
        )
        self.assertEqual(
            len(list((self.run_dir / "backups").glob("*.kdbx"))), 1
        )

    def test_missing_runtime_hub_is_refilled_but_persistent_hub_is_not(self):
        database = kdbx4_bytes(b"known database" * 20)
        self.copy_a.write_bytes(database)
        self.copy_b.write_bytes(database)
        sha, size = sha256_file(str(self.copy_a))
        state = HistoryState()
        state.commit(
            sha,
            (),
            "host",
            ("file:a", "file:b", self.engine.hub_identity),
            size=size,
        )
        state.save(self.run_dir / "state.json")

        result = self.engine.sync()
        self.assertEqual(result.action, "committed")
        self.assertEqual(self.hub_path.read_bytes(), database)

        self.hub_path.unlink()
        self.config = self.config.__class__(
            **(
                self.config.__dict__
                | {"hub_persistent": True, "require_encrypted": False}
            )
        )
        persistent = SyncEngine(
            self.config,
            (self.endpoint_a, self.endpoint_b),
            run_directory=self.run_dir,
            ui=NoneUI(),
        )
        with self.assertRaisesRegex(RuntimeError, "persistent hub is missing"):
            persistent.sync()
        self.assertFalse(self.hub_path.exists())

    def test_recreated_hub_is_new_and_never_promoted(self):
        known = kdbx4_bytes(b"known database" * 20)
        unknown = kdbx4_bytes(b"new database content" * 20)
        self._write_all(known)
        self.hub_path.write_bytes(unknown)
        sha, size = sha256_file(str(self.copy_a))
        state = HistoryState()
        state.commit(sha, (), "host", ("file:a", "file:b"), size=size)
        state.save(self.run_dir / "state.json")

        result = self.engine.sync()

        self.assertEqual(result.action, "pending")
        self.assertEqual(
            result.classifications[self.engine.hub_identity], "new"
        )
        self.assertEqual(self.hub_path.read_bytes(), unknown)
        self.assertEqual(self.copy_a.read_bytes(), known)
        self.assertTrue((self.run_dir / "pending.json").is_file())

    def test_missing_hub_record_is_cleared_when_all_copies_unavailable(
        self,
    ):
        known = kdbx4_bytes(b"known database" * 20)
        self._write_all(known)
        sha, size = sha256_file(str(self.copy_a))
        state = HistoryState()
        state.commit(
            sha,
            (),
            "host",
            (self.engine.hub_identity, "file:a", "file:b"),
            size=size,
        )
        state.save(self.run_dir / "state.json")
        self.hub_path.unlink()
        self.copy_a.unlink()
        self.copy_b.unlink()

        result = self.engine.sync()

        self.assertEqual(result.action, "unavailable")
        self.assertIsNone(result.pending)
        self.assertFalse(result.consistent)
        self.assertFalse((self.run_dir / "pending.json").exists())
        self.assertNotIn(
            self.engine.hub_identity,
            HistoryState.load(self.run_dir / "state.json").copies,
        )
        unknown = kdbx4_bytes(b"database created after reboot" * 15)
        self.hub_path.write_bytes(unknown)
        self.copy_a.write_bytes(known)
        self.copy_b.write_bytes(known)
        result = self.engine.sync()
        self.assertEqual(
            result.classifications[self.engine.hub_identity], "new"
        )
        self.assertEqual(self.hub_path.read_bytes(), unknown)

    def test_truncated_candidate_is_rejected_before_any_write(self):
        original = kdbx4_bytes(b"valid database" * 20)
        self._seed(original)
        self.copy_b.write_bytes(original[:-1])
        with self.assertRaisesRegex(SyncError, "invalid KDBX"):
            self.engine.sync()
        self.assertEqual(self.hub_path.read_bytes(), original)
        self.assertEqual(self.copy_a.read_bytes(), original)
        self.assertFalse(
            (self.run_dir / "backups").exists()
            and list((self.run_dir / "backups").glob("*.kdbx"))
        )

    def test_missing_endpoint_is_skipped_and_its_record_is_kept(self):
        old = kdbx4_bytes(b"old database" * 20)
        current = kdbx4_bytes(b"current database" * 20)
        self._write_all(current)
        old_path = self.root / "old.kdbx"
        old_path.write_bytes(old)
        old_sha, old_size = sha256_file(str(old_path))
        current_sha, current_size = sha256_file(str(self.hub_path))
        missing = FileEndpoint(
            "missing",
            self.root / "not-mounted.kdbx",
            identity="file:missing",
            optional=False,
            on_unavailable="skip",
        )
        engine = SyncEngine(
            self.config,
            (self.endpoint_a, self.endpoint_b, missing),
            run_directory=self.run_dir,
            ui=NoneUI(),
        )
        state = HistoryState()
        state.commit(old_sha, (), "host", (), size=old_size)
        state.commit(
            current_sha,
            (old_sha,),
            "host",
            (engine.hub_identity, "file:a", "file:b"),
            size=current_size,
        )
        state.record_copies(old_sha, ("file:missing",), "host")
        state.save(self.run_dir / "state.json")

        result = engine.sync()

        self.assertIn("file:missing", result.missing)
        self.assertIn(
            "file:missing",
            HistoryState.load(self.run_dir / "state.json").copies,
        )
        missing_path = self.root / "not-mounted.kdbx"
        missing_path.write_bytes(old)

        caught_up = engine.sync()

        self.assertEqual(caught_up.action, "committed")
        self.assertEqual(caught_up.missing, ())
        self.assertEqual(missing_path.read_bytes(), current)
        self.assertEqual(
            HistoryState.load(self.run_dir / "state.json")
            .copies["file:missing"]
            .version,
            current_sha,
        )

    def test_optional_missing_endpoint_is_not_reported_as_unreachable(self):
        original = kdbx4_bytes(b"known database" * 20)
        self._write_all(original)
        optional = FileEndpoint(
            "optional",
            self.root / "optional.kdbx",
            identity="file:optional",
            optional=True,
        )
        engine = SyncEngine(
            self.config,
            (self.endpoint_a, self.endpoint_b, optional),
            run_directory=self.run_dir,
            ui=NoneUI(),
        )

        result = engine.sync(dry_run=True)

        self.assertNotIn("file:optional", result.missing)

    def test_foreign_writer_change_waits_for_password_check(self):
        original = kdbx4_bytes(b"old database" * 20)
        updated = kdbx4_bytes(b"phone update" * 20)
        self._seed(original)
        self.copy_b.write_bytes(updated)
        foreign_endpoint = FileEndpoint(
            "phone-writable",
            self.copy_b,
            identity="file:b",
            foreign_writers=True,
        )
        engine = SyncEngine(
            self.config,
            (self.endpoint_a, foreign_endpoint),
            run_directory=self.run_dir,
            ui=NoneUI(),
        )

        result = engine.sync()

        self.assertEqual(result.action, "pending")
        self.assertIn("foreign change", result.pending)
        self.assertEqual(self.hub_path.read_bytes(), original)
        self.assertEqual(self.copy_b.read_bytes(), updated)

    def test_known_unchanged_kdbx3_does_not_require_password(self):
        legacy = (
            bytes.fromhex("03d9a29a67fb4bb5")
            + bytes.fromhex("01000300")
            + b"encrypted"
        )
        self._write_all(legacy)
        sha, size = sha256_file(str(self.hub_path))
        state = HistoryState()
        state.commit(
            sha,
            (),
            "host",
            (self.engine.hub_identity, "file:a", "file:b"),
            size=size,
        )
        state.save(self.run_dir / "state.json")

        result = self.engine.sync()

        self.assertEqual(result.action, "unchanged")
        self.assertIsNone(result.pending)

    def test_changed_kdbx3_candidate_is_not_promoted_without_password(self):
        original = kdbx4_bytes(b"known database" * 20)
        self._seed(original)
        legacy = (
            bytes.fromhex("03d9a29a67fb4bb5")
            + bytes.fromhex("01000300")
            + b"encrypted"
        )
        self.copy_b.write_bytes(legacy)

        result = self.engine.sync()

        self.assertEqual(result.action, "pending")
        self.assertIn("password verification", result.pending)
        self.assertEqual(self.hub_path.read_bytes(), original)
        self.assertEqual(self.copy_a.read_bytes(), original)
        self.assertEqual(self.copy_b.read_bytes(), legacy)

    def test_target_change_is_rechecked_and_not_committed_over(self):
        original = kdbx4_bytes(b"old content" * 20)
        updated = kdbx4_bytes(b"new source content" * 20)
        concurrent = kdbx4_bytes(b"concurrent update" * 20)
        self._seed(original)
        self.copy_b.write_bytes(updated)
        racing = RacingFileEndpoint(
            "a", self.copy_a, concurrent, identity="file:a"
        )
        engine = SyncEngine(
            self.config,
            (racing, self.endpoint_b),
            run_directory=self.run_dir,
            ui=NoneUI(),
        )

        result = engine.sync()

        self.assertEqual(result.action, "pending")
        self.assertEqual(self.copy_a.read_bytes(), concurrent)
        state = HistoryState.load(self.run_dir / "state.json")
        self.assertEqual(len(state.versions), 1)
        self.assertEqual(
            state.copies["file:a"].version, next(iter(state.versions))
        )

    def test_hub_exchange_preserves_concurrent_save_and_leaves_sync_pending(
        self,
    ):
        original = kdbx4_bytes(b"old content" * 20)
        updated = kdbx4_bytes(b"new source content" * 20)
        concurrent = kdbx4_bytes(b"concurrent hub save" * 20)
        self._seed(original)
        self.copy_b.write_bytes(updated)
        from kp_sync.endpoints import file as file_endpoint_module

        original_exchange = file_endpoint_module._rename_exchange

        def racing_exchange(staged, target):
            concurrent_path = Path(target).with_name("concurrent-save")
            concurrent_path.write_bytes(concurrent)
            os.replace(concurrent_path, target)
            original_exchange(staged, target)

        with patch.object(
            file_endpoint_module,
            "_rename_exchange",
            side_effect=racing_exchange,
        ):
            result = self.engine.sync()

        self.assertEqual(result.action, "pending")
        self.assertEqual(self.hub_path.read_bytes(), updated)
        backups = list((self.run_dir / "backups").glob("*.kdbx"))
        self.assertEqual(len(backups), 2)
        self.assertIn(concurrent, [path.read_bytes() for path in backups])
        self.assertIn(original, [path.read_bytes() for path in backups])
        self.assertEqual(
            len(HistoryState.load(self.run_dir / "state.json").versions), 1
        )
        second_result = self.engine.sync()
        # The preserved save takes the unknown-copy path, needing a key.
        self.assertEqual(second_result.action, "pending")
        self.assertIn("unknown copy", second_result.pending)
        self.assertTrue((self.run_dir / "write-race.json").exists())
        self.assertEqual(
            len(HistoryState.load(self.run_dir / "state.json").versions), 1
        )

    def test_file_endpoint_exchange_preserves_concurrent_save(self):
        original = kdbx4_bytes(b"old content" * 20)
        updated = kdbx4_bytes(b"new source content" * 20)
        concurrent = kdbx4_bytes(b"concurrent endpoint save" * 20)
        self._seed(original)
        self.copy_b.write_bytes(updated)
        from kp_sync.endpoints import file as file_endpoint_module

        original_exchange = file_endpoint_module._rename_exchange

        def racing_exchange(staged, target):
            if Path(target) == self.copy_a:
                concurrent_path = Path(target).with_name("concurrent-save")
                concurrent_path.write_bytes(concurrent)
                os.replace(concurrent_path, target)
            original_exchange(staged, target)

        with patch.object(
            file_endpoint_module,
            "_rename_exchange",
            side_effect=racing_exchange,
        ):
            result = self.engine.sync()

        self.assertEqual(result.action, "pending")
        self.assertEqual(self.hub_path.read_bytes(), updated)
        self.assertEqual(self.copy_a.read_bytes(), updated)
        self.assertIn(
            concurrent,
            [
                path.read_bytes()
                for path in (self.run_dir / "backups").glob("*.kdbx")
            ],
        )
        self.assertEqual(
            len(HistoryState.load(self.run_dir / "state.json").versions), 1
        )

    def test_partial_write_does_not_commit_an_inconsistent_version(self):
        original = kdbx4_bytes(b"old content" * 20)
        updated = kdbx4_bytes(b"new source content" * 20)
        self._seed(original)
        self.copy_b.write_bytes(updated)
        partial = PartialWriteEndpoint("a", self.copy_a, identity="file:a")
        engine = SyncEngine(
            self.config,
            (partial, self.endpoint_b),
            run_directory=self.run_dir,
            ui=NoneUI(),
        )
        engine.config = engine.config.__class__(
            **(
                engine.config.__dict__
                | {"sync": engine.config.sync | {"max_restarts": 0}}
            )
        )

        with self.assertRaisesRegex(SyncError, "restart limit"):
            engine.sync()
        self.assertEqual(
            len(HistoryState.load(self.run_dir / "state.json").versions), 1
        )
        marker = json.loads((self.run_dir / "inflight.json").read_text())
        self.assertEqual(len(marker["candidates"]), 1)
        self.assertTrue(
            (
                self.run_dir
                / "bases"
                / f"{marker['candidates'][0]['sha256']}.kdbx"
            ).is_file()
        )

    def test_partial_promotion_retry_recognizes_candidate_without_password(
        self,
    ):
        original = kdbx4_bytes(b"old partial recovery content" * 20)
        updated = kdbx4_bytes(b"new partial recovery content" * 20)
        self._seed(original)
        self.copy_b.write_bytes(updated)
        intermittent = FailOnceEndpoint("a", self.copy_a, identity="file:a")
        engine = SyncEngine(
            self.config,
            (intermittent, self.endpoint_b),
            run_directory=self.run_dir,
            ui=NoneUI(),
        )

        with self.assertRaisesRegex(SyncError, "synthetic target failure"):
            engine.sync()
        self.assertEqual(self.hub_path.read_bytes(), updated)
        self.assertTrue((self.run_dir / "inflight.json").is_file())

        result = engine.sync()

        self.assertEqual(result.action, "committed")
        self.assertEqual(self.copy_a.read_bytes(), updated)
        self.assertEqual(self.copy_b.read_bytes(), updated)
        self.assertFalse((self.run_dir / "inflight.json").exists())

    def test_final_consistency_check_prevents_commit_after_late_change(self):
        original = kdbx4_bytes(b"old content" * 20)
        updated = kdbx4_bytes(b"new source content" * 20)
        concurrent = kdbx4_bytes(b"late concurrent update" * 20)
        self._seed(original)
        self.copy_b.write_bytes(updated)
        racing = LateRaceEndpoint(
            "a", self.copy_a, concurrent, identity="file:a"
        )
        engine = SyncEngine(
            self.config,
            (racing, self.endpoint_b),
            run_directory=self.run_dir,
            ui=NoneUI(),
        )

        result = engine.sync()

        self.assertEqual(result.action, "pending")
        self.assertEqual(self.copy_a.read_bytes(), concurrent)
        self.assertEqual(
            len(HistoryState.load(self.run_dir / "state.json").versions), 1
        )

    def test_restore_defers_when_a_copy_has_unsynchronized_edits(self):
        original = kdbx4_bytes(b"committed content")
        self._seed(original)
        source = self.root / "restore-source.kdbx"
        source.write_bytes(original)
        changed = kdbx4_bytes(b"unsynchronized edit")
        self.copy_a.write_bytes(changed)
        state_before = (self.run_dir / "state.json").read_bytes()
        for mode in ("merge", "replace"):
            with self.subTest(mode=mode):
                result = self.engine.restore("file", str(source), mode=mode)
                self.assertEqual(result.action, "pending")
                self.assertIn("synchronize", result.pending)
                self.assertEqual(self.copy_a.read_bytes(), changed)
                self.assertEqual(self.hub_path.read_bytes(), original)
                self.assertEqual(
                    (self.run_dir / "state.json").read_bytes(), state_before
                )

    def test_restore_commits_both_parents_and_resists_stale_copy(
        self,
    ):
        key_file = self.config.key_file
        key_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        key_file.write_bytes(b"synthetic-key-file")
        key_file.chmod(0o600)
        create_database(str(self.hub_path), "test-password", keyfile=key_file)
        database = PyKeePass(
            str(self.hub_path), "test-password", keyfile=key_file
        )
        database.add_entry(
            database.root_group, "Restored", "user", "old-secret"
        )
        database.save()
        self._seed(self.hub_path.read_bytes())
        self.engine.credentials = StaticCredentials(key_file)

        old_path = self.root / "old-version.kdbx"
        shutil.copyfile(self.hub_path, old_path)
        old_sha = sha256_file(str(old_path))[0]
        self.engine._save_base(old_path, old_sha)
        old_database = PyKeePass(
            str(old_path), "test-password", keyfile=key_file
        )
        restored_id = old_database.find_entries(
            title="Restored", first=True
        ).uuid.hex

        current = PyKeePass(
            str(self.hub_path), "test-password", keyfile=key_file
        )
        current.delete_entry(
            current.find_entries(title="Restored", first=True)
        )
        current.add_entry(current.root_group, "Newer", "user", "new-secret")
        current.save()
        changed = self.hub_path.read_bytes()
        self.copy_a.write_bytes(changed)
        self.copy_b.write_bytes(changed)
        self.assertEqual(self.engine.sync().action, "committed")
        before_merge_sha = sha256_file(str(self.hub_path))[0]
        state = HistoryState.load(self.run_dir / "state.json")
        old_info = inspect_kdbx(old_path)
        old_version = Version.create(
            old_sha,
            (),
            "test-host",
            size=old_info.size,
        )
        current_version = state.versions[before_merge_sha]
        state.versions[old_sha] = old_version
        state.versions[before_merge_sha] = replace(
            current_version,
            parents=(old_sha,),
            generation=old_version.generation + 1,
        )
        state.copies["file:offline"] = CopyRecord(
            old_sha,
            state.generation,
            datetime.now(timezone.utc).isoformat(),
            "offline",
            sha256=old_sha,
        )
        state.save(self.run_dir / "state.json")

        restore_ui = FakeUI(
            reviews=[{f"restore:entry:{restored_id}:existence": "other"}],
            confirmations=[True, True],
        )
        self.engine.ui = restore_ui
        merged = self.engine.restore("file", str(old_path), mode="merge")

        self.assertEqual(merged.action, "committed")
        merged_db = PyKeePass(
            str(self.hub_path), "test-password", keyfile=key_file
        )
        self.assertEqual(
            {entry.title for entry in merged_db.entries},
            {"Restored", "Newer"},
        )
        merged_state = HistoryState.load(self.run_dir / "state.json")
        self.assertEqual(
            set(merged_state.versions[merged.current].parents),
            {before_merge_sha, old_sha},
        )

        before_replace_sha = merged.current
        self.engine.ui = FakeUI(confirmations=[True, True])
        replaced = self.engine.restore("file", str(old_path), mode="replace")

        self.assertEqual(replaced.action, "committed")
        replaced_db = PyKeePass(
            str(self.hub_path), "test-password", keyfile=key_file
        )
        self.assertEqual(
            {entry.title for entry in replaced_db.entries}, {"Restored"}
        )
        replaced_state = HistoryState.load(self.run_dir / "state.json")
        self.assertEqual(
            set(replaced_state.versions[replaced.current].parents),
            {before_replace_sha, old_sha},
        )

        self.copy_a.write_bytes(changed)
        self.engine.ui = FakeUI(confirmations=[True])
        self.assertEqual(self.engine.sync().action, "committed")
        final_db = PyKeePass(
            str(self.hub_path), "test-password", keyfile=key_file
        )
        self.assertEqual(
            {entry.title for entry in final_db.entries}, {"Restored"}
        )


if __name__ == "__main__":
    unittest.main()
