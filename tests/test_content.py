import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pykeepass import PyKeePass

from tests.fixtures import create_database

from kp_sync.content import content_hash


class ContentHashTest(unittest.TestCase):
    def test_ignores_usage_and_metadata_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            original = Path(temp_dir) / "original.kdbx"
            resaved = Path(temp_dir) / "resaved.kdbx"
            create_database(str(original), password="test-password")
            database = PyKeePass(str(original), password="test-password")
            database.add_entry(
                database.root_group,
                "Example",
                "user",
                "secret",
                url="https://example.test",
            )
            database.save()
            resaved.write_bytes(original.read_bytes())

            edited = PyKeePass(str(resaved), password="test-password")
            times = edited.tree.find(".//Times")
            assert times is not None
            last_access = times.find("LastAccessTime")
            assert last_access is not None
            last_access.text = "2030-01-01T00:00:00Z"
            edited.tree.find(".//UsageCount").text = "42"
            metadata_change = edited.tree.find(".//Meta/DatabaseNameChanged")
            if metadata_change is not None:
                metadata_change.text = "2030-01-01T00:00:00Z"
            edited.save()

            self.assertEqual(
                content_hash(original, password="test-password"),
                content_hash(resaved, password="test-password"),
            )

    def test_entry_content_changes_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "database.kdbx"
            create_database(str(path), password="test-password")
            database = PyKeePass(str(path), password="test-password")
            entry = database.add_entry(
                database.root_group, "Example", "user", "first"
            )
            database.save()
            original = content_hash(path, password="test-password")

            entry.password = "second"
            database.save()

            self.assertNotEqual(
                original, content_hash(path, password="test-password")
            )

    def test_binary_content_and_master_key_change_are_not_plain_resaves(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "database.kdbx"
            create_database(str(path), password="test-password")
            database = PyKeePass(str(path), password="test-password")
            entry = database.add_entry(
                database.root_group, "Example", "user", "secret"
            )
            entry.add_attachment(database.add_binary(b"first"), "note")
            database.save()
            original = content_hash(path, password="test-password")
            database.payload.inner_header.binary[0].data = b"\x01second"
            database.save()
            self.assertNotEqual(
                original, content_hash(path, password="test-password")
            )
            # MasterKeyChanged has one-second resolution: back-date it
            # so the change below shows however fast the test runs.
            database.tree.find("Meta/MasterKeyChanged").text = (
                database._encode_time(
                    datetime.now(timezone.utc) - timedelta(hours=1)
                )
            )
            database.save()
            original = content_hash(path, password="test-password")
            database.password = "replacement-password"
            database.save()
            self.assertNotEqual(
                original, content_hash(path, password="replacement-password")
            )


if __name__ == "__main__":
    unittest.main()
