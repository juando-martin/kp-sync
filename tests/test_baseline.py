import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kp_sync.baseline import (
    BaselineChanged,
    BaselineManager,
    CloudStore,
    DriveStore,
    MissingBases,
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

    def base_sizes(self):
        if not self.online:
            raise EndpointUnavailable("offline")
        directory = self.root / "bases"
        return (
            {path.name: path.stat().st_size for path in directory.iterdir()}
            if directory.exists()
            else {}
        )

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
        # The cloud already holds this state; the drive is best-effort.
        with patch("kp_sync.baseline.error") as logged:
            manager.save(merged, self.work)
        logged.assert_called_once()
        self.assertEqual(
            manager.publication_outcomes["drive:one"],
            "failed: OSError: synthetic transfer failure",
        )
        self.assertEqual(
            manager.publication_outcomes["cloud"], "already current"
        )
        self.assertEqual(
            manager.replica_failures,
            {"drive:one": "OSError: synthetic transfer failure"},
        )
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

    def _newer_cloud_history(self):
        state, base = self.seed(self.cloud)
        self.seed(
            self.cloud,
            state=state,
            parent=base,
            payload=b"newer",
            copy="hub:other",
        )

    def test_required_cloud_failure_still_fails_publication(self):
        self._newer_cloud_history()
        manager = BaselineManager(self.runtime, (self.cloud, self.drive))
        merged = manager.load(self.work)
        merged.record_copies(next(iter(merged.versions)), ["hub:this"], "t")

        def fail_cloud_state(relative):
            if relative == "state.json":
                raise OSError("synthetic cloud failure")

        self.cloud.before_put = fail_cloud_state
        with self.assertRaisesRegex(OSError, "synthetic cloud failure"):
            manager.save(merged, self.work)
        self.assertNotIn("state.json", self.drive.events)

    def test_drive_failure_continues_to_later_drives_after_cloud(self):
        self._newer_cloud_history()
        second = FakeStore(self.root / "drive2", name="drive:two", cloud=False)
        manager = BaselineManager(
            self.runtime, (self.cloud, self.drive, second)
        )
        merged = manager.load(self.work)
        merged.record_copies(next(iter(merged.versions)), ["hub:this"], "t")

        broken = []

        def full(relative):
            broken.append(relative)
            raise OSError("synthetic full drive")

        self.drive.before_put = full
        original_fetch = self.drive.fetch

        def unreadable(relative, destination, *, max_age=None):
            # After its write failure the drive also stops reading.
            if broken:
                raise OSError("synthetic read failure")
            return original_fetch(relative, destination, max_age=max_age)

        self.drive.fetch = unreadable
        with patch("kp_sync.baseline.error"):
            manager.save(merged, self.work)
        self.assertEqual(manager.publication_outcomes["cloud"], "published")
        self.assertEqual(
            manager.publication_outcomes["drive:two"], "published"
        )
        self.assertTrue(
            manager.publication_outcomes["drive:one"].startswith("failed:")
        )
        self.assertEqual(
            HistoryState.load(second.root / "state.json").to_dict(),
            merged.to_dict(),
        )

    def test_failing_drive_checks_run_until_it_fails(self):
        # Later drives still detect changes; a failed drive is skipped.
        self._newer_cloud_history()
        second = FakeStore(self.root / "drive2", name="drive:two", cloud=False)
        manager = BaselineManager(
            self.runtime, (self.cloud, self.drive, second)
        )
        merged = manager.load(self.work)
        merged.record_copies(next(iter(merged.versions)), ["hub:this"], "t")
        competing = HistoryState.from_dict(merged.to_dict())
        competing.record_copies(next(iter(merged.versions)), ["hub:race"], "r")

        def race(relative):
            if relative.startswith("bases/"):
                competing.save(self.cloud.root / "state.json")

        second.before_put = race
        with self.assertRaises(BaselineChanged):
            manager.save(merged, self.work)
        self.assertNotIn("state.json", second.events)
        self.assertEqual(
            manager.publication_outcomes["drive:two"],
            "failed: shared history changed",
        )

    def test_only_reachable_drive_failure_fails_publication(self):
        self.cloud.online = False
        second = FakeStore(self.root / "drive2", name="drive:two", cloud=False)
        manager = BaselineManager(
            self.runtime, (self.cloud, self.drive, second)
        )
        merged = manager.load(self.work)
        merged.commit("a" * 64, (), "test", ("hub:this",), size=1)
        (self.runtime / "bases").mkdir(mode=0o700)
        (self.runtime / "bases" / f"{'a' * 64}.kdbx").write_bytes(b"x")
        with patch("kp_sync.baseline.inspect_kdbx") as inspected:
            inspected.return_value.sha256 = "a" * 64

            def full(relative):
                raise OSError("synthetic full drive")

            self.drive.before_put = full
            with self.assertRaisesRegex(OSError, "synthetic full drive"):
                manager.save(merged, self.work)
            self.assertEqual(second.events, [])
            # The first drive is current; later drives are best-effort.
            self.drive.before_put = None
            second.before_put = full
            with patch("kp_sync.baseline.error"):
                manager.save(merged, self.work)
        self.assertEqual(
            manager.publication_outcomes["drive:one"], "published"
        )
        self.assertTrue(
            manager.publication_outcomes["drive:two"].startswith("failed:")
        )

    def test_drive_pruning_failure_after_state_is_reported_as_published(self):
        initial, base = self.seed(self.drive)
        state, tip = self.seed(
            self.drive, parent=base, state=initial, payload=b"newer"
        )
        manager = BaselineManager(self.runtime, (self.cloud, self.drive))
        merged = manager.load(self.work)
        merged.prune()
        merged.record_copies(tip, ["hub:this"], "t")
        self.assertNotIn(base, merged.versions)

        def fail_delete(name):
            raise OSError("synthetic delete failure")

        self.drive.delete_base = fail_delete
        with patch("kp_sync.baseline.error"):
            manager.save(merged, self.work)
        self.assertEqual(
            manager.publication_outcomes["drive:one"],
            "published; pruning failed: OSError: synthetic delete failure",
        )
        # Its history is current, so no outdated-replica warning.
        self.assertEqual(manager.replica_failures, {})
        self.assertEqual(
            HistoryState.load(self.drive.root / "state.json").to_dict(),
            merged.to_dict(),
        )

    def _lose_cloud_base(self):
        state, sha = self.seed(self.cloud)
        base = self.cloud.root / "bases" / f"{sha}.kdbx"
        data = base.read_bytes()
        base.unlink()
        return state, sha, data

    def test_base_missing_from_current_cloud_history_is_restored(self):
        _, sha, data = self._lose_cloud_base()
        (self.runtime / "bases").mkdir(mode=0o700)
        (self.runtime / "bases" / f"{sha}.kdbx").write_bytes(data)
        manager = BaselineManager(self.runtime, (self.cloud,))
        merged = manager.load(self.work)
        self.cloud.events.clear()
        manager.save(merged, self.work)
        # History was already current: only the base is uploaded.
        self.assertEqual(self.cloud.events, [f"bases/{sha}.kdbx"])
        self.assertEqual(
            (self.cloud.root / "bases" / f"{sha}.kdbx").read_bytes(), data
        )

    def test_missing_cloud_base_is_restored_from_a_drive_store(self):
        state, sha, data = self._lose_cloud_base()
        (self.drive.root / "bases").mkdir()
        (self.drive.root / "bases" / f"{sha}.kdbx").write_bytes(data)
        state.save(self.drive.root / "state.json")
        manager = BaselineManager(self.runtime, (self.cloud, self.drive))
        manager.save(manager.load(self.work), self.work)
        self.assertEqual(
            (self.cloud.root / "bases" / f"{sha}.kdbx").read_bytes(), data
        )

    def test_missing_base_is_restored_from_a_verified_observed_copy(self):
        _, sha, data = self._lose_cloud_base()
        copy = self.work / "observed.kdbx"
        copy.write_bytes(data)
        wrong = self.work / "wrong.kdbx"
        wrong.write_bytes(kdbx4_bytes(b"other bytes" * 20))
        manager = BaselineManager(self.runtime, (self.cloud,))
        merged = manager.load(self.work)
        with patch("kp_sync.baseline.error"):
            # A source with other bytes is never trusted by name.
            manager.save(merged, self.work, sources={sha: wrong})
        self.assertFalse((self.cloud.root / "bases" / f"{sha}.kdbx").exists())
        manager.save(merged, self.work, sources={sha: copy})
        self.assertEqual(
            (self.cloud.root / "bases" / f"{sha}.kdbx").read_bytes(), data
        )

    def test_base_missing_everywhere_blocks_new_history_by_name(self):
        state, sha, _ = self._lose_cloud_base()
        manager = BaselineManager(self.runtime, (self.cloud,))
        merged = manager.load(self.work)
        # Current history is only reported, not failed.
        with patch("kp_sync.baseline.error") as logged:
            manager.save(merged, self.work)
        self.assertIn(sha[:12], logged.call_args.args[0])
        self.assertIn(
            "missing everywhere", manager.publication_outcomes["cloud"]
        )
        merged.record_copies(sha, ["hub:this"], "this")
        with self.assertRaises(MissingBases) as raised:
            manager.save(merged, self.work)
        self.assertEqual(raised.exception.missing, (sha,))
        self.assertIn("repair-history", str(raised.exception))
        self.assertNotIn(
            "hub:this",
            HistoryState.load(self.cloud.root / "state.json").copies,
        )

    def test_wrongly_sized_store_base_is_replaced(self):
        _, sha, data = self._lose_cloud_base()
        (self.cloud.root / "bases" / f"{sha}.kdbx").write_bytes(b"torn")
        (self.runtime / "bases").mkdir(mode=0o700)
        (self.runtime / "bases" / f"{sha}.kdbx").write_bytes(data)
        manager = BaselineManager(self.runtime, (self.cloud,))
        manager.save(manager.load(self.work), self.work)
        self.assertEqual(
            (self.cloud.root / "bases" / f"{sha}.kdbx").read_bytes(), data
        )

    def test_fresh_runtime_does_not_download_bases_stores_already_have(self):
        state, base = self.seed(self.cloud)
        self.seed(self.cloud, state=state, parent=base, payload=b"newer")
        fetched = []
        original = self.cloud.fetch

        def fetch(relative, destination, *, max_age=None):
            fetched.append(relative)
            return original(relative, destination, max_age=max_age)

        self.cloud.fetch = fetch
        manager = BaselineManager(self.runtime, (self.cloud,))
        merged = manager.load(self.work)
        merged.record_copies(base, ["hub:this"], "this")
        manager.save(merged, self.work)
        self.assertEqual(
            [name for name in fetched if name.startswith("bases/")], []
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
