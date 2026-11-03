import tempfile
import unittest
from pathlib import Path

from kp_sync.baseline import (
    BaselineChanged,
    BaselineManager,
    CloudStore,
    DriveStore,
)
from kp_sync.drives import DriveEndpoint
from kp_sync.endpoints.base import EndpointUnavailable
from kp_sync.endpoints.file import FileEndpoint
from kp_sync.kdbx import inspect_kdbx
from kp_sync.state import HistoryState, StateError
from tests.fixtures import kdbx4_bytes


class FakeStore:
    def __init__(self, root, *, name="cloud", cloud=True):
        self.root = Path(root)
        self.root.mkdir()
        self.name = name
        self.cloud = cloud
        self.online = True
        self.events = []
        self.before_put = None
        self.directory = "kp-sync"
        self.cache_path = None

    def fetch(self, relative, destination, *, max_age=None):
        if not self.online:
            raise EndpointUnavailable("offline")
        path = self.root / relative
        if not path.exists():
            return False
        FileEndpoint("fake-store", path).fetch(destination)
        return True

    def put(self, relative, source):
        if not self.online:
            raise EndpointUnavailable("offline")
        if self.before_put is not None:
            self.before_put(relative)
        self.events.append(relative)
        FileEndpoint("fake-store", self.root / relative).store(source)

    def bases(self, cached=False):
        if not self.online:
            raise EndpointUnavailable("offline")
        directory = self.root / "bases"
        return (
            [path.name for path in directory.iterdir()]
            if directory.exists()
            else []
        )

    def delete_base(self, name):
        (self.root / "bases" / name).unlink()

    def remove(self):
        for name in self.bases():
            (self.root / "bases" / name).unlink()
        if (self.root / "bases").exists():
            (self.root / "bases").rmdir()
        (self.root / "state.json").unlink()
        self.root.rmdir()


class BaselineTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.work = self.runtime / "tmp"
        self.work.mkdir(mode=0o700)
        self.cloud = FakeStore(self.root / "cloud")
        self.drive = FakeStore(
            self.root / "drive", name="drive:one", cloud=False
        )

    def seed(
        self,
        store,
        *,
        payload=b"original",
        parent=None,
        state=None,
        copy="cloud",
    ):
        data = kdbx4_bytes(payload * 20)
        path = self.work / "input.kdbx"
        path.write_bytes(data)
        sha = inspect_kdbx(path).sha256
        state = state or HistoryState()
        state.commit(
            sha, (parent,) if parent else (), "test", (copy,), size=len(data)
        )
        store.put(f"bases/{sha}.kdbx", path)
        state_path = self.work / "state.json"
        state.save(state_path)
        store.put("state.json", state_path)
        return state, sha

    def test_load_unions_per_copy_records_and_verifies_downloaded_bases(self):
        cloud, base = self.seed(self.cloud)
        drive, changed = self.seed(
            self.drive,
            payload=b"changed",
            parent=base,
            state=HistoryState.from_dict(cloud.to_dict()),
            copy="drive:one",
        )
        manager = BaselineManager(self.runtime, (self.cloud, self.drive))
        merged = manager.load(self.work)
        self.assertEqual(merged.copies["cloud"].version, base)
        self.assertEqual(merged.copies["drive:one"].version, changed)
        path = manager.ensure_base(changed, self.work)
        self.assertEqual(inspect_kdbx(path).sha256, changed)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        (self.cloud.root / "bases" / f"{base}.kdbx").write_bytes(
            kdbx4_bytes(b"wrong")
        )
        with self.assertRaises(StateError):
            manager.ensure_base(base, self.work)

    def test_read_only_cloud_fetch_does_not_change_history_cache(self):
        class Endpoint:
            identity = "cloud:test"

            def track_directory(self, directory):
                pass

            def remote_path(self, relative):
                return relative

            def download_file(self, remote, destination, **kwargs):
                self.kwargs = kwargs
                Path(destination).write_bytes(b"remote state")
                return True

        endpoint = Endpoint()
        store = CloudStore(endpoint, "kp-sync")
        store.cache_path = self.runtime / "history-cache.json"
        store.cache_path.write_bytes(b"cached state")
        destination = self.work / "preview.json"
        self.assertTrue(store.fetch_read_only("state.json", destination))
        self.assertEqual(destination.read_bytes(), b"remote state")
        self.assertEqual(store.cache_path.read_bytes(), b"cached state")
        self.assertIsNone(endpoint.kwargs["cached"])

    def test_offline_history_is_retained_on_all_drives_after_cloud_returns(
        self,
    ):
        state, sha = self.seed(self.drive)
        another = FakeStore(
            self.root / "another", name="drive:two", cloud=False
        )
        self.cloud.online = False
        manager = BaselineManager(
            self.runtime, (self.cloud, self.drive, another)
        )
        merged = manager.load(self.work)
        manager.save(merged, self.work)
        self.assertEqual(
            HistoryState.load(another.root / "state.json").to_dict(),
            state.to_dict(),
        )
        self.cloud.online = True
        manager = BaselineManager(
            self.runtime, (self.cloud, self.drive, another)
        )
        merged = manager.load(self.work)
        self.cloud.events.clear()
        manager.save(merged, self.work)
        self.assertEqual(
            self.cloud.events, [f"bases/{sha}.kdbx", "state.json"]
        )
        for store in (self.drive, another):
            self.assertEqual(
                HistoryState.load(store.root / "state.json").to_dict(),
                state.to_dict(),
            )
            self.assertTrue((store.root / "bases" / f"{sha}.kdbx").is_file())
        self.assertEqual(
            HistoryState.load(self.cloud.root / "state.json").to_dict(),
            state.to_dict(),
        )

    def test_generation_or_same_generation_record_race_stops_publish(self):
        state, sha = self.seed(self.cloud)
        manager = BaselineManager(self.runtime, (self.cloud,))
        merged = manager.load(self.work)
        merged.record_copies(sha, ["hub:this"], "this")
        state.record_copies(sha, ["hub:other"], "other")
        state.save(self.cloud.root / "state.json")
        with self.assertRaises(BaselineChanged):
            manager.save(merged, self.work)
        self.assertEqual(
            HistoryState.load(self.cloud.root / "state.json").generation,
            state.generation,
        )

    def test_retirement_marker_publishes_without_a_database_version(self):
        identity = "session:remote:aabbccdd"
        self.seed(self.cloud, copy=identity)
        manager = BaselineManager(self.runtime, (self.cloud,))
        state = manager.load(self.work)
        state.retire(identity, "test-host")
        state.prune()
        self.cloud.events.clear()
        manager.save(state, self.work)
        published = HistoryState.load(self.cloud.root / "state.json")
        self.assertIn(identity, published.retirements)
        self.assertNotIn(identity, published.copies)
        self.assertEqual(published.versions, {})
        self.assertEqual(self.cloud.events, ["state.json"])
        self.assertEqual(manager.publication_outcomes["cloud"], "published")

    def test_unknown_schema_is_not_treated_as_unavailable(self):
        (self.cloud.root / "state.json").write_text('{"schema_version":99}')
        (self.cloud.root / "state.json").chmod(0o600)
        with self.assertRaises(StateError):
            BaselineManager(self.runtime, (self.cloud,)).load(self.work)

    def test_unchanged_history_skips_upload_prune_and_snapshot_check(self):
        self.seed(self.cloud)
        manager = BaselineManager(self.runtime, (self.cloud,))
        merged = manager.load(self.work)
        self.cloud.events.clear()
        from unittest.mock import patch

        with patch.object(
            self.cloud,
            "bases",
            side_effect=AssertionError("unexpected prune scan"),
        ):
            manager.save(merged, self.work)
        self.assertEqual(self.cloud.events, [])

    def test_unchanged_cloud_and_drive_history_are_retained_without_writes(
        self,
    ):
        state, _ = self.seed(self.cloud)
        self.seed(self.drive, state=HistoryState.from_dict(state.to_dict()))
        # The cloud already contains the merged drive records.
        HistoryState.load(self.drive.root / "state.json").save(
            self.cloud.root / "state.json"
        )
        manager = BaselineManager(self.runtime, (self.cloud, self.drive))
        merged = manager.load(self.work)
        self.cloud.events.clear()
        self.drive.events.clear()
        manager.save(merged, self.work)
        self.assertEqual(self.cloud.events, [])
        self.assertEqual(self.drive.events, [])
        self.assertEqual(
            HistoryState.load(self.drive.root / "state.json").to_dict(),
            merged.to_dict(),
        )

    def test_online_cloud_seeds_empty_drive_without_reuploading_cloud(self):
        state, sha = self.seed(self.cloud)
        manager = BaselineManager(self.runtime, (self.cloud, self.drive))
        merged = manager.load(self.work)
        self.cloud.events.clear()
        manager.save(merged, self.work)
        self.assertEqual(self.cloud.events, [])
        self.assertEqual(
            self.drive.events, [f"bases/{sha}.kdbx", "state.json"]
        )
        self.assertEqual(
            HistoryState.load(self.drive.root / "state.json").to_dict(),
            state.to_dict(),
        )

    def test_drive_history_change_blocks_mirror_after_base_transfer(self):
        state, sha = self.seed(self.cloud)
        manager = BaselineManager(self.runtime, (self.cloud, self.drive))
        merged = manager.load(self.work)
        competing = HistoryState.from_dict(state.to_dict())
        competing.record_copies(sha, ["hub:other"], "other")

        def race(relative):
            if relative.startswith("bases/"):
                competing.save(self.drive.root / "state.json")

        self.drive.before_put = race
        with self.assertRaises(BaselineChanged):
            manager.save(merged, self.work)
        self.assertNotIn("state.json", self.drive.events)
        self.assertEqual(
            HistoryState.load(self.drive.root / "state.json").to_dict(),
            competing.to_dict(),
        )

    def test_drive_mirror_base_failure_does_not_publish_partial_history(self):
        state, base = self.seed(self.cloud)
        self.seed(
            self.cloud,
            state=state,
            parent=base,
            payload=b"newer",
            copy="hub:other",
        )
        manager = BaselineManager(self.runtime, (self.cloud, self.drive))
        merged = manager.load(self.work)

        def fail_second_base(relative):
            if relative.startswith("bases/") and self.drive.events:
                raise OSError("synthetic transfer failure")

        self.drive.before_put = fail_second_base
        with self.assertRaisesRegex(OSError, "synthetic transfer failure"):
            manager.save(merged, self.work)
        self.assertFalse((self.drive.root / "state.json").exists())
        self.assertEqual(len(self.drive.events), 1)
        self.drive.before_put = None
        manager.save(merged, self.work)
        self.assertEqual(
            HistoryState.load(self.drive.root / "state.json").to_dict(),
            merged.to_dict(),
        )
        self.assertEqual(
            set(self.drive.bases()),
            {f"{sha}.kdbx" for sha in merged.versions},
        )

    def test_prune_removes_only_unretained_hash_named_bases(self):
        initial, base = self.seed(self.cloud)
        state, tip = self.seed(
            self.cloud, parent=base, state=initial, payload=b"newer"
        )
        (self.cloud.root / "bases" / "notes.txt").write_text("unrelated")
        manager = BaselineManager(self.runtime, (self.cloud,))
        merged = manager.load(self.work)
        merged.prune()
        manager.save(merged, self.work)
        # A single stale cloud base waits for a deletion batch.
        self.assertTrue((self.cloud.root / "bases" / f"{base}.kdbx").exists())
        merged.record_copies(tip, ["hub:this"], "this")
        manager.prune_batch = 1
        manager.save(merged, self.work)
        self.assertFalse((self.cloud.root / "bases" / f"{base}.kdbx").exists())
        self.assertTrue((self.cloud.root / "bases" / f"{tip}.kdbx").exists())
        self.assertTrue((self.cloud.root / "bases" / "notes.txt").exists())

    def test_drive_store_refuses_replacement_mount_and_symlink(self):
        mount = self.root / "mount"
        mount.mkdir()
        identity = ["serial:uuid"]
        endpoint = DriveEndpoint(
            "pendrive",
            mount,
            "pass.kdbx",
            identity[0],
            identity_resolver=lambda mount: identity[0],
        )
        store = DriveStore(endpoint, "kp-sync")
        source = self.work / "input"
        source.write_bytes(b"metadata")
        identity[0] = "other:uuid"
        with self.assertRaises(EndpointUnavailable):
            store.put("state.json", source)
        self.assertFalse(store.root.exists())
        identity[0] = "serial:uuid"
        store.root.symlink_to(self.cloud.root, target_is_directory=True)
        with self.assertRaises(StateError):
            store.put("state.json", source)
