import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from kp_sync.cleanup import cleanup
from kp_sync.cli import main
from kp_sync.config import load_config
from kp_sync.paths import atomic_write
from kp_sync.pending import PendingStore
from kp_sync.sessions.records import SessionStore
from kp_sync.state import HistoryState
from tests.test_baseline import FakeStore


class CleanupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.config = load_config(
            home=self.root,
            environ={
                "HOME": str(self.root),
                "XDG_RUNTIME_DIR": str(self.root),
            },
        )
        self.cloud = FakeStore(self.root / "cloud")
        self.cloud.endpoint = SimpleNamespace(name="cloud")
        self.cloud.base_sizes = lambda: {
            path.name: path.stat().st_size
            for path in (self.cloud.root / "bases").iterdir()
        }
        self.engine = SimpleNamespace(
            run_directory=self.runtime, baseline_stores=(self.cloud,)
        )
        self.state = HistoryState()
        self.old = "a" * 64
        self.current = "b" * 64
        self.orphan = "c" * 64 + ".kdbx"
        self.state.commit(self.old, (), "host", ("drive:offline",), size=3)
        self.state.commit(
            self.current, (self.old,), "host", ("cloud",), size=3
        )
        self.state.save(self.runtime / "state.json")
        self.state.save(self.cloud.root / "state.json")
        for root in (self.runtime, self.cloud.root):
            (root / "bases").mkdir()
            for name in (
                self.old + ".kdbx",
                self.current + ".kdbx",
                self.orphan,
                "unrelated.txt",
            ):
                (root / "bases" / name).write_bytes(b"abc")
        self.original_fetch = self.cloud.fetch

        def metadata_only(relative, destination, **kwargs):
            self.assertEqual(relative, "state.json")
            return self.original_fetch(relative, destination, **kwargs)

        self.cloud.fetch = metadata_only

    def run_cleanup(self, **kwargs):
        return cleanup(self.config, engine=self.engine, **kwargs)

    def test_preview_then_apply_below_threshold_and_repeat(self):
        before = (self.runtime / "state.json").read_bytes()
        preview = self.run_cleanup()
        self.assertEqual(preview["errors"], [])
        for entry in preview["stores"].values():
            self.assertEqual(entry["candidates"], [self.orphan])
            self.assertEqual(entry["candidate_bytes"], 3)
            self.assertEqual(len(entry["protected"]), 2)
            self.assertEqual(entry["deleted"], [])
        self.assertTrue((self.cloud.root / "bases" / self.orphan).exists())
        result = self.run_cleanup(
            store_name="cloud", apply=True, writers_paused=True
        )
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["stores"]["cloud"]["deleted"], [self.orphan])
        self.assertTrue((self.cloud.root / "bases" / "unrelated.txt").exists())
        again = self.run_cleanup(
            store_name="cloud", apply=True, writers_paused=True
        )
        self.assertEqual(again["stores"]["cloud"]["candidates"], [])
        self.assertEqual((self.runtime / "state.json").read_bytes(), before)

    def test_runtime_scope_and_symlinks_are_not_followed(self):
        target = self.root / "keep"
        target.write_bytes(b"keep")
        link = self.runtime / "bases" / ("d" * 64 + ".kdbx")
        link.symlink_to(target)
        report = self.run_cleanup(runtime_only=True, apply=True)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["stores"]["runtime"]["deleted"], [self.orphan])
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_bytes(), b"keep")
        self.assertTrue((self.cloud.root / "bases" / self.orphan).exists())

    def test_pending_recovery_and_unavailable_history_block_deletion(self):
        for marker in (
            "inflight.json",
            "write-race.json",
            "resolved-displaced.json",
        ):
            with self.subTest(marker=marker):
                atomic_write(self.runtime / marker, "{}")
                report = self.run_cleanup(runtime_only=True, apply=True)
                self.assertTrue(report["errors"])
                self.assertTrue(
                    (self.runtime / "bases" / self.orphan).exists()
                )
                (self.runtime / marker).unlink()
        PendingStore(self.runtime).save("merge", "needs review")
        report = self.run_cleanup(
            store_name="cloud", apply=True, writers_paused=True
        )
        self.assertTrue(report["errors"])
        self.assertTrue((self.cloud.root / "bases" / self.orphan).exists())
        PendingStore(self.runtime).resolve("merge")
        self.cloud.online = False
        report = self.run_cleanup(runtime_only=True, apply=True)
        self.assertTrue(report["errors"])
        self.assertTrue((self.runtime / "bases" / self.orphan).exists())

    def test_shared_change_aborts_and_partial_failure_is_reported(self):
        fetches = 0

        def changing(relative, destination, **kwargs):
            nonlocal fetches
            fetches += 1
            if fetches == 2:
                self.state.retire("session:old:12345678", "other")
                self.state.save(self.cloud.root / "state.json")
            return self.original_fetch(relative, destination, **kwargs)

        self.cloud.fetch = changing
        report = self.run_cleanup(
            store_name="cloud", apply=True, writers_paused=True
        )
        self.assertTrue(report["errors"])
        self.assertTrue((self.cloud.root / "bases" / self.orphan).exists())
        self.cloud.fetch = self.original_fetch
        second = "d" * 64 + ".kdbx"
        (self.cloud.root / "bases" / second).write_bytes(b"abc")
        original_delete = self.cloud.delete_base

        def fail_second(filename):
            if filename == second:
                raise OSError("synthetic delete failure")
            original_delete(filename)

        self.cloud.delete_base = fail_second
        report = self.run_cleanup(
            store_name="cloud", apply=True, writers_paused=True
        )
        self.assertIn("synthetic delete failure", report["errors"][0])
        self.assertEqual(report["stores"]["cloud"]["deleted"], [self.orphan])
        self.assertTrue((self.cloud.root / "bases" / second).exists())

    def test_cli_requires_explicit_scope_and_paused_writer_confirmation(self):
        for args in (
            ["cleanup", "--apply"],
            ["cleanup", "--store", "cloud", "--apply"],
        ):
            with (
                self.subTest(args=args),
                patch("kp_sync.cli.load_config") as load,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(main(args), 2)
                load.assert_not_called()
        with self.assertRaisesRegex(ValueError, "paused"):
            self.run_cleanup(store_name="cloud", apply=True)
        with self.assertRaisesRegex(ValueError, "explicit"):
            self.run_cleanup(apply=True)
        with self.assertRaisesRegex(ValueError, "no attached"):
            self.run_cleanup(store_name="nonexistent")

    def test_retirement_progress_and_missing_history_block_apply(self):
        SessionStore(self.runtime).prepare_retirement(
            "session:remote:12345678", "12345678"
        )
        report = self.run_cleanup(runtime_only=True, apply=True)
        self.assertIn("unfinished session retirement", report["errors"][0])
        self.assertTrue((self.runtime / "bases" / self.orphan).exists())
        (self.cloud.root / "state.json").unlink()
        report = self.run_cleanup(
            store_name="cloud", apply=True, writers_paused=True
        )
        self.assertIn("store history is absent", report["errors"][0])
        self.assertTrue((self.cloud.root / "bases" / self.orphan).exists())

    def test_local_recovery_appearing_during_check_aborts_deletion(self):
        from kp_sync.baseline import BaselineManager

        original = BaselineManager.check

        def check(manager, temporary, **kwargs):
            original(manager, temporary, **kwargs)
            atomic_write(self.runtime / "write-race.json", "{}")

        with patch.object(BaselineManager, "check", check):
            report = self.run_cleanup(runtime_only=True, apply=True)
        self.assertIn("runtime history/recovery changed", report["errors"][0])
        self.assertTrue((self.runtime / "bases" / self.orphan).exists())

    def test_cleanup_cli_routes_scope_and_reports_partial_failure(self):
        report = {"stores": {}, "errors": ["synthetic failure"]}
        output = io.StringIO()
        with (
            patch("kp_sync.cli.load_config", return_value=self.config),
            patch("kp_sync.cleanup.cleanup", return_value=report) as action,
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(main(["cleanup", "--runtime", "--apply"]), 1)
        self.assertIn("synthetic failure", output.getvalue())
        self.assertTrue(action.call_args.kwargs["runtime_only"])
        self.assertTrue(action.call_args.kwargs["apply"])
