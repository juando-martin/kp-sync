import os
import hashlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kp_sync.endpoints import (
    CommandEndpoint,
    EndpointChanged,
    EndpointUnavailable,
    FileEndpoint,
)
from kp_sync.endpoints.file import _RenameUnsupported


class EndpointTest(unittest.TestCase):
    def test_file_endpoint_atomic_store_and_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "candidate.kdbx"
            source.write_bytes(b"candidate")
            target = root / "endpoint" / "pass.kdbx"
            endpoint = FileEndpoint("backup", target)
            endpoint.store(source)
            fingerprint = endpoint.probe()
            fetched = root / "download" / "copy.kdbx"
            endpoint.fetch(fetched)
            self.assertEqual(fetched.read_bytes(), b"candidate")
            self.assertEqual(fingerprint.size, len(b"candidate"))
            self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)

    def test_command_endpoint_uses_argument_lists_for_fetch_store_and_probe(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote_file = root / "remote"
            source = root / "source"
            source.write_text("payload", encoding="utf-8")
            fetch = (
                sys.executable,
                "-c",
                "import pathlib,sys; "
                "pathlib.Path(sys.argv[1]).write_text('fetched')",
                "{file}",
            )
            store = (
                sys.executable,
                "-c",
                "import pathlib,shutil,sys; "
                "shutil.copyfile(sys.argv[1],sys.argv[2])",
                "{file}",
                str(remote_file),
            )
            probe = (
                sys.executable,
                "-c",
                "import pathlib,sys; "
                "print(pathlib.Path(sys.argv[1]).stat().st_size)",
                str(remote_file),
            )
            endpoint = CommandEndpoint("cmd", fetch, store, probe=probe)
            downloaded = root / "downloaded"
            endpoint.fetch(downloaded)
            endpoint.store(source)
            self.assertEqual(downloaded.read_text(encoding="utf-8"), "fetched")
            self.assertEqual(
                remote_file.read_text(encoding="utf-8"), "payload"
            )
            self.assertEqual(endpoint.probe().token, str(len("payload")))

    def test_command_failures_are_reported(self) -> None:
        endpoint = CommandEndpoint(
            "broken",
            (sys.executable, "-c", "raise SystemExit(7)", "{file}"),
            (sys.executable, "-c", "raise SystemExit(7)", "{file}"),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(EndpointUnavailable):
                endpoint.fetch(Path(temp_dir) / "copy")

    def test_file_store_detects_change_during_candidate_staging(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "endpoint.kdbx"
            target.write_bytes(b"original")
            candidate = root / "candidate.kdbx"
            candidate.write_bytes(b"candidate")
            concurrent = b"concurrent save"
            endpoint = FileEndpoint("file", target)
            expected = hashlib.sha256(b"original").hexdigest()
            original_copy = shutil.copyfileobj

            def racing_copy(source, destination, *args, **kwargs):
                original_copy(source, destination, *args, **kwargs)
                target.write_bytes(concurrent)

            with unittest.mock.patch(
                "kp_sync.endpoints.file.shutil.copyfileobj", racing_copy
            ):
                with self.assertRaises(EndpointChanged):
                    endpoint.store_if_unchanged(candidate, expected)

            self.assertEqual(target.read_bytes(), concurrent)
            self.assertEqual(list(root.glob(".endpoint.kdbx.*")), [])

    def test_file_store_rechecks_after_pre_replace_callback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "endpoint.kdbx"
            target.write_bytes(b"original")
            candidate = root / "candidate.kdbx"
            candidate.write_bytes(b"candidate")
            concurrent = b"concurrent save"
            endpoint = FileEndpoint("file", target)
            expected = hashlib.sha256(b"original").hexdigest()

            def concurrent_save():
                target.write_bytes(concurrent)

            with self.assertRaises(EndpointChanged):
                endpoint.store_if_unchanged(
                    candidate,
                    expected,
                    before_replace=concurrent_save,
                )

            self.assertEqual(target.read_bytes(), concurrent)

    def test_atomic_exchange_replaces_expected_file_and_removes_displaced_copy(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "endpoint.kdbx"
            target.write_bytes(b"original")
            candidate = root / "candidate.kdbx"
            candidate.write_bytes(b"candidate")
            endpoint = FileEndpoint("file", target)

            endpoint.store_if_unchanged(
                candidate,
                hashlib.sha256(b"original").hexdigest(),
                atomic_exchange=True,
            )

            self.assertEqual(target.read_bytes(), b"candidate")
            self.assertEqual(list(root.glob(".endpoint.kdbx.*")), [])

    def test_atomic_exchange_retains_unexpected_displaced_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "endpoint.kdbx"
            target.write_bytes(b"original")
            candidate = root / "candidate.kdbx"
            candidate.write_bytes(b"candidate")
            concurrent = b"concurrent save"
            endpoint = FileEndpoint("file", target)
            original_exchange = __import__(
                "kp_sync.endpoints.file", fromlist=["_rename_exchange"]
            )._rename_exchange

            def racing_exchange(staged, path):
                concurrent_path = Path(path).with_name("concurrent-save")
                concurrent_path.write_bytes(concurrent)
                os.replace(concurrent_path, path)
                original_exchange(staged, path)

            with unittest.mock.patch(
                "kp_sync.endpoints.file._rename_exchange",
                side_effect=racing_exchange,
            ):
                with self.assertRaises(EndpointChanged) as raised:
                    endpoint.store_if_unchanged(
                        candidate,
                        hashlib.sha256(b"original").hexdigest(),
                        atomic_exchange=True,
                    )

            self.assertEqual(target.read_bytes(), b"candidate")
            self.assertEqual(
                raised.exception.preserved_path.read_bytes(), concurrent
            )
            self.assertEqual(
                raised.exception.observed_sha256,
                hashlib.sha256(concurrent).hexdigest(),
            )

    def test_missing_target_install_does_not_overwrite_concurrent_creation(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "endpoint.kdbx"
            candidate = root / "candidate.kdbx"
            candidate.write_bytes(b"candidate")
            concurrent = b"concurrent creation"
            endpoint = FileEndpoint("file", target)
            original_install = __import__(
                "kp_sync.endpoints.file", fromlist=["_rename_noreplace"]
            )._rename_noreplace

            def racing_install(source, destination):
                concurrent_path = root / "concurrent-save"
                concurrent_path.write_bytes(concurrent)
                os.replace(concurrent_path, destination)
                return original_install(source, destination)

            with unittest.mock.patch(
                "kp_sync.endpoints.file._rename_noreplace",
                side_effect=racing_install,
            ):
                with self.assertRaises(EndpointChanged):
                    endpoint.store_if_unchanged(
                        candidate,
                        None,
                        atomic_exchange=True,
                    )

            self.assertEqual(target.read_bytes(), concurrent)

    def test_missing_target_noreplace_unsupported_preserves_candidate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "endpoint.kdbx"
            candidate = root / "candidate.kdbx"
            candidate.write_bytes(b"candidate")
            endpoint = FileEndpoint("file", target)

            with (
                patch(
                    "kp_sync.endpoints.file._rename_noreplace",
                    side_effect=_RenameUnsupported("no-replace unsupported"),
                ),
                self.assertRaises(EndpointUnavailable),
            ):
                endpoint.store_if_unchanged(
                    candidate,
                    None,
                    atomic_exchange=True,
                )

            self.assertFalse(target.exists())
            self.assertEqual(candidate.read_bytes(), b"candidate")
            self.assertEqual(list(root.glob(".endpoint.kdbx.*")), [])

    def test_exchange_unsupported_uses_displace_then_install(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "pass.kdbx"
            target.write_bytes(b"original")
            candidate = root / "candidate.kdbx"
            candidate.write_bytes(b"candidate")
            endpoint = FileEndpoint("file", target)

            with patch(
                "kp_sync.endpoints.file._rename_exchange",
                side_effect=_RenameUnsupported("exchange unsupported"),
            ):
                endpoint.store_if_unchanged(
                    candidate,
                    hashlib.sha256(b"original").hexdigest(),
                    atomic_exchange=True,
                )

            self.assertEqual(target.read_bytes(), b"candidate")
            self.assertEqual(
                list(root.glob(".pass.kdbx.kp-sync-displaced-*")), []
            )
            self.assertEqual(list(root.glob(".kp-sync-noreplace-*")), [])

    def test_non_unsupported_exchange_error_does_not_fall_back(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "pass.kdbx"
            target.write_bytes(b"original")
            candidate = root / "candidate.kdbx"
            candidate.write_bytes(b"candidate")
            endpoint = FileEndpoint("file", target)

            with (
                patch(
                    "kp_sync.endpoints.file._rename_exchange",
                    side_effect=OSError("exchange failed"),
                ),
                self.assertRaises(EndpointUnavailable),
            ):
                endpoint.store_if_unchanged(
                    candidate,
                    hashlib.sha256(b"original").hexdigest(),
                    atomic_exchange=True,
                )

            self.assertEqual(target.read_bytes(), b"original")
            self.assertEqual(
                list(root.glob(".pass.kdbx.kp-sync-displaced-*")), []
            )

    def test_unsupported_noreplace_fails_before_displacing_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "pass.kdbx"
            target.write_bytes(b"original")
            candidate = root / "candidate.kdbx"
            candidate.write_bytes(b"candidate")
            endpoint = FileEndpoint("file", target)

            with (
                patch(
                    "kp_sync.endpoints.file._rename_exchange",
                    side_effect=_RenameUnsupported("exchange unsupported"),
                ),
                patch(
                    "kp_sync.endpoints.file._rename_noreplace",
                    side_effect=_RenameUnsupported("no-replace unsupported"),
                ),
                self.assertRaises(EndpointUnavailable),
            ):
                endpoint.store_if_unchanged(
                    candidate,
                    hashlib.sha256(b"original").hexdigest(),
                    atomic_exchange=True,
                )

            self.assertEqual(target.read_bytes(), b"original")
            self.assertEqual(candidate.read_bytes(), b"candidate")
            self.assertEqual(
                list(root.glob(".pass.kdbx.kp-sync-displaced-*")), []
            )
            self.assertEqual(list(root.glob(".kp-sync-noreplace-*")), [])

    def test_displace_install_keeps_concurrent_target_and_displaced_original(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "pass.kdbx"
            target.write_bytes(b"original")
            candidate = root / "candidate.kdbx"
            candidate.write_bytes(b"candidate")
            endpoint = FileEndpoint("file", target)
            concurrent = b"concurrent writer"
            original_install = __import__(
                "kp_sync.endpoints.file", fromlist=["_rename_noreplace"]
            )._rename_noreplace
            target_install_called = False

            def unsupported_exchange(*args):
                raise _RenameUnsupported("exchange unsupported")

            def racing_install(source, destination):
                nonlocal target_install_called
                if Path(destination) == target and not target_install_called:
                    target_install_called = True
                    target.write_bytes(concurrent)
                return original_install(source, destination)

            with (
                patch(
                    "kp_sync.endpoints.file._rename_exchange",
                    side_effect=unsupported_exchange,
                ),
                patch(
                    "kp_sync.endpoints.file._rename_noreplace",
                    side_effect=racing_install,
                ),
                self.assertRaises(EndpointChanged),
            ):
                endpoint.store_if_unchanged(
                    candidate,
                    hashlib.sha256(b"original").hexdigest(),
                    atomic_exchange=True,
                )

            self.assertEqual(target.read_bytes(), concurrent)
            displaced = list(root.glob(".pass.kdbx.kp-sync-displaced-*"))
            self.assertEqual(len(displaced), 1)
            self.assertEqual(displaced[0].read_bytes(), b"original")
            self.assertEqual(list(root.glob(".pass.kdbx.*")), displaced)

    def test_displaced_recovery_restores_or_cleans_known_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "pass.kdbx"
            endpoint = FileEndpoint("file", target)
            original = b"recorded version"
            expected = hashlib.sha256(original).hexdigest()
            displaced = root / f".pass.kdbx.kp-sync-displaced-{'a' * 32}"
            displaced.write_bytes(original)

            self.assertEqual(endpoint.recover_displaced(expected), [])
            self.assertEqual(target.read_bytes(), original)
            self.assertFalse(displaced.exists())

            target.write_bytes(b"new target")
            displaced.write_bytes(original)
            self.assertEqual(endpoint.recover_displaced(expected), [])
            self.assertFalse(displaced.exists())

    def test_displaced_recovery_preserves_mismatch_and_multiple_absent_files(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "pass.kdbx"
            endpoint = FileEndpoint("file", target)
            expected = hashlib.sha256(b"recorded").hexdigest()
            first = root / f".pass.kdbx.kp-sync-displaced-{'a' * 32}"
            second = root / f".pass.kdbx.kp-sync-displaced-{'b' * 32}"
            unrelated = root / ".pass.kdbx.kp-sync-displaced-not-a-token"
            symlink = root / f".pass.kdbx.kp-sync-displaced-{'c' * 32}"
            first.write_bytes(b"unknown one")
            second.write_bytes(b"unknown two")
            unrelated.write_bytes(b"unrelated")
            symlink.symlink_to(first.name)

            recovered = endpoint.recover_displaced(expected)

            self.assertEqual(recovered, [first, second])
            self.assertFalse(target.exists())
            self.assertEqual(first.read_bytes(), b"unknown one")
            self.assertEqual(second.read_bytes(), b"unknown two")
            self.assertEqual(unrelated.read_bytes(), b"unrelated")
            self.assertTrue(symlink.is_symlink())

    def test_displaced_recovery_inspection_does_not_mutate_mismatch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "pass.kdbx"
            target.write_bytes(b"target")
            endpoint = FileEndpoint("file", target)
            displaced = root / f".pass.kdbx.kp-sync-displaced-{'d' * 32}"
            displaced.write_bytes(b"unrecognized")
            before = (target.read_bytes(), displaced.read_bytes())

            self.assertEqual(
                endpoint.recover_displaced(
                    hashlib.sha256(b"expected").hexdigest(),
                ),
                [displaced],
            )
            self.assertEqual(
                (target.read_bytes(), displaced.read_bytes()), before
            )


if __name__ == "__main__":
    unittest.main()
