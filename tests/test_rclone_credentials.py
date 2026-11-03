import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from kp_sync.endpoints.base import EndpointUnavailable
from kp_sync.endpoints.rclone import RcloneEndpoint
from kp_sync.lock import LockTimeout, file_lock
from kp_sync.rclone_credentials import (
    CredentialsUnavailable,
    RcloneCredentials,
)
from tests.test_rclone import FakeRclone


class CredentialsFixture:
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.external = self.root / "ext" / "rclone.conf"
        self.external.parent.mkdir()
        self.run_dir = self.root / "run"
        self.run_dir.mkdir(mode=0o700)
        self.creds = RcloneCredentials("cloud", self.external, self.run_dir)
        self.runner = FakeRclone()
        self.endpoint = RcloneEndpoint(
            "cloud",
            "GoogleDrive",
            runner=self.runner,
            credentials=self.creds,
        )

    def publish(self, text=b"[GoogleDrive]\ntoken=1\n"):
        self.external.write_bytes(text)


class RcloneCredentialsTest(CredentialsFixture, unittest.TestCase):

    def test_unavailable_without_copy_makes_no_rclone_call(self):
        with self.assertRaises(CredentialsUnavailable):
            self.endpoint.probe()
        self.assertIsInstance(CredentialsUnavailable(), EndpointUnavailable)
        self.assertEqual(self.runner.calls, [])

    def test_uses_private_copy_and_never_touches_external(self):
        self.publish()
        before = self.external.stat()
        self.endpoint.begin_run()
        self.endpoint.probe()
        argv = self.runner.calls[0][0]
        self.assertEqual(
            argv[argv.index("--config") + 1], str(self.creds.path)
        )
        self.assertNotIn(str(self.external), argv)
        self.assertEqual(self.creds.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.creds.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.external.stat().st_mtime_ns, before.st_mtime_ns)

    def test_refreshed_copy_kept_until_external_changes(self):
        self.publish()
        self.creds.refresh()
        self.creds.path.write_bytes(b"[GoogleDrive]\ntoken=refreshed\n")
        self.creds.refresh()
        self.assertIn(b"refreshed", self.creds.path.read_bytes())
        self.publish(b"[GoogleDrive]\ntoken=2\n")
        self.creds.refresh()
        self.assertIn(b"token=2", self.creds.path.read_bytes())

    def test_drain_deletes_copy_only_after_external_disappears(self):
        self.publish()
        self.endpoint.begin_run()
        self.endpoint.end_run()
        self.assertTrue(self.creds.copy_exists())
        self.external.unlink()
        self.endpoint.begin_run()
        self.endpoint.probe()  # draining run still usable
        self.endpoint.end_run()
        self.assertFalse(self.creds.copy_exists())
        with self.assertRaises(CredentialsUnavailable):
            self.endpoint.probe()

    def test_reappearing_file_cancels_drain(self):
        self.publish()
        self.creds.refresh()
        self.external.unlink()
        self.publish()
        self.assertFalse(self.creds.finish())

    def test_symlink_external_is_ignored(self):
        target = self.root / "other"
        target.write_bytes(b"x")
        self.external.symlink_to(target)
        self.creds.refresh()
        self.assertFalse(self.creds.copy_exists())

    def test_profiles_have_separate_copies(self):
        other = RcloneCredentials("cloud", self.external, self.root / "run2")
        self.publish()
        self.creds.refresh()
        other.refresh()
        self.assertNotEqual(self.creds.path, other.path)
        other.discard()
        self.assertTrue(self.creds.copy_exists())


class LockHolder:
    """Hold the profile's credentials lock from a separate process."""

    def __init__(self, path, *, shared):
        code = (
            "import sys\n"
            "from kp_sync.lock import file_lock\n"
            "with file_lock(sys.argv[1], 5, shared=sys.argv[2] == 'sh'):\n"
            "    print('held', flush=True)\n"
            "    sys.stdin.readline()\n"
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(
            Path(__file__).resolve().parents[1] / "src"
        )
        self.process = subprocess.Popen(
            [sys.executable, "-c", code, str(path), "sh" if shared else "ex"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env=environment,
        )
        assert self.process.stdout.readline().strip() == "held"

    def release(self):
        if self.process.poll() is None:
            self.process.stdin.write("\n")
            self.process.stdin.flush()
            self.process.wait(timeout=10)
        self.process.stdin.close()
        self.process.stdout.close()


class RcloneCredentialsLockTest(CredentialsFixture, unittest.TestCase):
    """Import and drain wait for rclone runs in any profile process."""

    def setUp(self):
        super().setUp()
        self.creds.lock_timeout = 0.2
        # Another process of the same profile (history, doctor).
        self.other = RcloneCredentials(
            "cloud", self.external, self.run_dir, lock_timeout=0.2
        )

    def hold(self, *, shared):
        holder = LockHolder(self.creds.lock_path, shared=shared)
        self.addCleanup(holder.release)
        return holder

    def test_lock_lives_outside_the_discarded_directory(self):
        self.publish()
        self.creds.refresh()
        self.assertEqual(
            self.creds.lock_path, self.run_dir / "rclone-credentials.lock"
        )
        self.assertEqual(self.creds.lock_path.stat().st_mode & 0o777, 0o600)
        self.creds.discard()
        self.assertFalse(self.creds.directory.exists())
        self.assertTrue(self.creds.lock_path.exists())

    def test_rclone_run_holds_the_lock_shared(self):
        self.publish()
        self.creds.refresh()
        seen = []

        def runner(argv, **kwargs):
            try:
                with file_lock(self.creds.lock_path, 0, shared=True):
                    seen.append("shared ok")
            except LockTimeout:
                seen.append("shared blocked")
            try:
                with file_lock(self.creds.lock_path, 0):
                    seen.append("exclusive ok")
            except LockTimeout:
                seen.append("exclusive blocked")
            return self.runner(argv, **kwargs)

        self.endpoint._runner = runner
        self.endpoint.probe()
        self.assertEqual(seen, ["shared ok", "exclusive blocked"])

    def test_import_waits_for_a_running_rclone_writing_back(self):
        self.publish(b"[GoogleDrive]\ntoken=old\n")
        self.creds.refresh()
        results = []

        def runner(argv, **kwargs):
            # The provider re-authorizes while rclone still runs.
            self.publish(b"[GoogleDrive]\ntoken=new\n")
            results.append(self.other.refresh())
            # rclone saves a refreshed token from its old config.
            self.creds.path.write_bytes(b"[GoogleDrive]\ntoken=old-2\n")
            return self.runner(argv, **kwargs)

        self.endpoint._runner = runner
        self.endpoint.probe()
        self.assertEqual(results, [False])
        self.assertIn(b"old-2", self.creds.path.read_bytes())
        # The next run imports, because the import was not recorded.
        self.assertTrue(self.creds.refresh())
        self.assertIn(b"token=new", self.creds.path.read_bytes())

    def test_drain_waits_for_a_running_rclone_writing_back(self):
        self.publish()
        self.creds.refresh()
        results = []

        def runner(argv, **kwargs):
            # The key drive is pulled while rclone runs.
            self.external.unlink()
            results.append(self.other.finish())
            results.append(self.other.copy_exists())
            self.creds.path.write_bytes(b"[GoogleDrive]\ntoken=saved\n")
            return self.runner(argv, **kwargs)

        self.endpoint._runner = runner
        self.endpoint.probe()
        self.assertEqual(results, [False, True])
        self.assertTrue(self.creds.finish())
        self.assertFalse(self.creds.copy_exists())

    def test_drain_and_discard_wait_for_another_process(self):
        self.publish()
        self.creds.refresh()
        self.external.unlink()
        holder = self.hold(shared=True)
        with patch("kp_sync.rclone_credentials.error") as logged:
            self.assertFalse(self.creds.discard())
        logged.assert_called_once()
        self.assertFalse(self.creds.finish())
        self.assertFalse(self.creds.finish(wait=False))
        self.assertTrue(self.creds.copy_exists())
        holder.release()
        self.assertTrue(self.creds.finish(wait=False))
        self.assertFalse(self.creds.copy_exists())

    def test_rclone_does_not_start_during_another_process_import(self):
        self.publish()
        self.creds.refresh()
        holder = self.hold(shared=False)
        with self.assertRaisesRegex(CredentialsUnavailable, "being updated"):
            self.endpoint.probe()
        self.assertEqual(self.runner.calls, [])
        self.assertFalse(self.creds.refresh())
        holder.release()
        self.endpoint.probe()
        self.assertEqual(len(self.runner.calls), 1)

    def test_parallel_rclone_runs_share_the_lock(self):
        self.publish()
        self.creds.refresh()
        self.hold(shared=True)
        self.endpoint.probe()
        self.assertEqual(len(self.runner.calls), 1)
