import shutil
import tempfile
import unittest
from pathlib import Path

from kp_sync.endpoints.base import EndpointUnavailable
from kp_sync.endpoints.rclone import RcloneEndpoint
from kp_sync.rclone_credentials import (
    CredentialsUnavailable,
    RcloneCredentials,
)
from tests.test_rclone import FakeRclone


class RcloneCredentialsTest(unittest.TestCase):
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
