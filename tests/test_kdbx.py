import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pykeepass import PyKeePass

from tests.fixtures import create_database

from kp_sync.kdbx import KdbxError, inspect_kdbx
from kp_sync.ui.fake import FakeUI
from kp_sync.verify import verify_candidate
from tests.fixtures import kdbx4_bytes


class KdbxTest(unittest.TestCase):
    def test_kdbx4_header_and_block_chain(self) -> None:
        data = kdbx4_bytes(b"database payload")
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "pass.kdbx"
            path.write_bytes(data)
            info = inspect_kdbx(path)
            self.assertEqual(info.major, 4)
            self.assertEqual(info.block_count, 2)
            self.assertEqual(info.size, len(data))

    def test_kdbx4_truncated_data_and_bad_header_are_rejected(self) -> None:
        data = kdbx4_bytes(b"database payload")
        for broken in (data[:-1], data[:30] + b"x" + data[31:], data + b"x"):
            with self.subTest(length=len(broken)):
                with tempfile.TemporaryDirectory() as temp_dir:
                    path = Path(temp_dir) / "broken.kdbx"
                    path.write_bytes(broken)
                    with self.assertRaises(KdbxError):
                        inspect_kdbx(path)

    def test_kdbx3_and_large_size_drop_require_password_decision(self) -> None:
        kdbx3 = (
            bytes.fromhex("03d9a29a67fb4bb5")
            + bytes.fromhex("01000300")
            + b"encrypted"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "legacy.kdbx"
            path.write_bytes(kdbx3)
            self.assertIn("KDBX 3", verify_candidate(path).pending_reasons[0])

            path.write_bytes(kdbx4_bytes(b"small"))
            result = verify_candidate(path, reference_size=1000)
            self.assertTrue(
                any(
                    "smaller than half" in reason
                    for reason in result.pending_reasons
                )
            )

    def test_entry_drop_requires_password_and_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reference = root / "reference.kdbx"
            candidate = root / "candidate.kdbx"
            create_database(str(reference), password="test-password")
            database = PyKeePass(str(reference), password="test-password")
            for index in range(10):
                database.add_entry(
                    database.root_group,
                    f"Entry {index}",
                    "user",
                    "password",
                )
            database.save()
            candidate.write_bytes(reference.read_bytes())
            smaller = PyKeePass(str(candidate), password="test-password")
            for entry in smaller.entries[4:]:
                entry._element.getparent().remove(entry._element)
            smaller.save()

            deferred = verify_candidate(
                candidate,
                reference_path=reference,
                password="test-password",
                ui=FakeUI(),
            )
            self.assertIn("entry-count drop", deferred.pending_reasons[0])
            accepted = verify_candidate(
                candidate,
                reference_path=reference,
                password="test-password",
                ui=FakeUI(confirmations=(True,)),
            )
            self.assertTrue(accepted.verified)

    def test_keepassxc_receives_password_only_on_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "database.kdbx"
            create_database(str(path), password="do-not-log-this")
            with patch(
                "kp_sync.verify.subprocess.run",
                return_value=SimpleNamespace(returncode=0),
            ) as run:
                verify_candidate(
                    path,
                    password="do-not-log-this",
                    require_keepassxc=True,
                )

            args, kwargs = run.call_args
            self.assertNotIn("do-not-log-this", repr(args))
            self.assertEqual(kwargs["input"], "do-not-log-this\n")
            self.assertNotIn("env", kwargs)

    def test_large_size_drop_needs_confirmation_even_after_password_check(
        self,
    ):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "candidate.kdbx"
            create_database(str(path), password="test-password")
            reference_size = path.stat().st_size * 2 + 1
            self.assertIn(
                "large size drop requires confirmation",
                verify_candidate(
                    path,
                    reference_size=reference_size,
                    password="test-password",
                    ui=FakeUI(),
                ).pending_reasons,
            )
            self.assertTrue(
                verify_candidate(
                    path,
                    reference_size=reference_size,
                    password="test-password",
                    ui=FakeUI(confirmations=(True,)),
                ).verified
            )


if __name__ == "__main__":
    unittest.main()
