import base64
import copy
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pykeepass import PyKeePass

from tests.fixtures import create_database
from pykeepass.exceptions import CredentialsError
from lxml import etree

from kp_sync.merge import (
    check_foreign_descent,
    filter_foreign_stale_cache,
    merge_databases,
    merge_restore,
    merge_unknown_copy,
)
from kp_sync.ui.fake import FakeUI
from kp_sync.verify import verify_candidate


def _add_custom_data(entry, key: str, value: str) -> None:
    custom_data = entry._element.find("CustomData")
    if custom_data is None:
        custom_data = etree.SubElement(entry._element, "CustomData")
    item = etree.SubElement(custom_data, "Item")
    etree.SubElement(item, "Key").text = key
    etree.SubElement(item, "Value").text = value


class MergeTest(unittest.TestCase):
    def _databases(self, directory: Path) -> tuple[Path, Path, Path, str]:
        base_path = directory / "base.kdbx"
        hub_path = directory / "hub.kdbx"
        other_path = directory / "other.kdbx"
        create_database(str(base_path), password="test-password")
        base = PyKeePass(str(base_path), password="test-password")
        entry = base.add_entry(
            base.root_group,
            "Example",
            "user",
            "initial-password",
            url="https://old.example.test",
        )
        identifier = entry.uuid.hex
        base.save()
        shutil.copyfile(base_path, hub_path)
        shutil.copyfile(base_path, other_path)
        return base_path, hub_path, other_path, identifier

    def test_independent_field_changes_merge_without_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            base, hub_path, other_path, _ = self._databases(directory)
            hub = PyKeePass(str(hub_path), password="test-password")
            hub.entries[0].password = "changed-password"
            hub.save()
            other = PyKeePass(str(other_path), password="test-password")
            other.entries[0].url = "https://new.example.test"
            other.save()

            output = directory / "merged.kdbx"
            result = merge_databases(
                base,
                hub_path,
                other_path,
                output,
                password="test-password",
                ui=FakeUI(),
            )

            self.assertTrue(result.merged)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertFalse(result.conflicts)
            merged = PyKeePass(str(output), password="test-password")
            self.assertEqual(merged.entries[0].password, "changed-password")
            self.assertEqual(merged.entries[0].url, "https://new.example.test")
            self.assertTrue(merged.entries[0].history)

    def test_independent_custom_data_keys_merge_without_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            base, hub_path, other_path, _ = self._databases(directory)
            hub = PyKeePass(str(hub_path), password="test-password")
            _add_custom_data(hub.entries[0], "hub-key", "hub-value")
            hub.save()
            other = PyKeePass(str(other_path), password="test-password")
            _add_custom_data(other.entries[0], "other-key", "other-value")
            other.save()

            output = directory / "merged.kdbx"
            result = merge_databases(
                base,
                hub_path,
                other_path,
                output,
                password="test-password",
                ui=FakeUI(),
            )

            self.assertTrue(result.merged)
            self.assertFalse(result.conflicts)
            merged = PyKeePass(str(output), password="test-password")
            items = {
                item.findtext("Key"): item.findtext("Value")
                for item in merged.entries[0]._element.findall(
                    "CustomData/Item"
                )
            }
            self.assertEqual(
                items, {"hub-key": "hub-value", "other-key": "other-value"}
            )

    def test_same_field_conflict_defers_without_ui_choice(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            base, hub_path, other_path, _ = self._databases(directory)
            hub = PyKeePass(str(hub_path), password="test-password")
            hub.entries[0].username = "hub-user"
            hub.save()
            other = PyKeePass(str(other_path), password="test-password")
            other.entries[0].username = "other-user"
            other.save()
            output = directory / "must-not-exist.kdbx"

            result = merge_databases(
                base,
                hub_path,
                other_path,
                output,
                password="test-password",
                ui=FakeUI(),
            )

            self.assertFalse(result.merged)
            self.assertEqual(len(result.unresolved), 1)
            self.assertFalse(output.exists())
            self.assertNotIn("initial-password", repr(result.conflicts))

    def test_saved_choice_resolves_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            base, hub_path, other_path, identifier = self._databases(directory)
            hub = PyKeePass(str(hub_path), password="test-password")
            hub.entries[0].username = "hub-user"
            hub.save()
            other = PyKeePass(str(other_path), password="test-password")
            other.entries[0].username = "other-user"
            other.save()
            output = directory / "merged.kdbx"
            result = merge_databases(
                base,
                hub_path,
                other_path,
                output,
                password="test-password",
                choices={f"entry:{identifier}:String:UserName": "other"},
            )

            self.assertTrue(result.merged)
            merged = PyKeePass(str(output), password="test-password")
            self.assertEqual(merged.entries[0].username, "other-user")

    def test_one_sided_deletion_is_recorded_as_deleted_object(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            base, hub_path, other_path, identifier = self._databases(directory)
            hub = PyKeePass(str(hub_path), password="test-password")
            hub.delete_entry(hub.entries[0])
            hub.tree.find("./Meta/RecycleBinEnabled").text = "False"
            hub.save()

            output = directory / "merged.kdbx"
            result = merge_databases(
                base,
                hub_path,
                other_path,
                output,
                password="test-password",
            )

            self.assertTrue(result.merged)
            merged = PyKeePass(str(output), password="test-password")
            self.assertFalse(merged.entries)
            deleted = merged.tree.xpath(
                "./Root/DeletedObjects/DeletedObject/UUID"
            )
            self.assertIn(
                base64.b64encode(bytes.fromhex(identifier)).decode(),
                {element.text for element in deleted},
            )
            if shutil.which("keepassxc-cli"):
                self.assertTrue(
                    verify_candidate(
                        output,
                        password="test-password",
                        require_keepassxc=True,
                    ).verified
                )

    def test_unknown_copy_imports_new_entries_but_never_restores_tombstones(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            current_path = directory / "current.kdbx"
            incoming_path = directory / "incoming.kdbx"
            create_database(str(current_path), password="test-password")
            current = PyKeePass(str(current_path), password="test-password")
            current.add_entry(current.root_group, "Base", "u", "base")
            current.save()
            shutil.copyfile(current_path, incoming_path)
            incoming = PyKeePass(str(incoming_path), password="test-password")
            removed = incoming.add_entry(
                incoming.root_group, "Removed", "u", "secret"
            )
            removed_id = removed.uuid.hex
            incoming.add_entry(incoming.root_group, "New", "u", "new-secret")
            incoming.save()
            current = PyKeePass(str(current_path), password="test-password")
            root = current.tree.find("./Root")
            deleted_objects = current.tree.find("./Root/DeletedObjects")
            if deleted_objects is None:
                deleted_objects = etree.SubElement(root, "DeletedObjects")
            deleted_object = etree.SubElement(deleted_objects, "DeletedObject")
            uuid_element = etree.SubElement(deleted_object, "UUID")
            uuid_element.text = base64.b64encode(
                bytes.fromhex(removed_id)
            ).decode()
            etree.SubElement(deleted_object, "DeletionTime").text = (
                "2026-10-23T00:00:00Z"
            )
            current.save()
            incoming = PyKeePass(str(incoming_path), password="test-password")
            new_id = incoming.entries[2].uuid.hex
            output = directory / "unknown-merged.kdbx"

            result = merge_unknown_copy(
                current_path,
                incoming_path,
                output,
                password="test-password",
                choices={f"unknown-entry:{new_id}": "other"},
            )

            self.assertIsNotNone(result.candidate)
            merged = PyKeePass(str(output), password="test-password")
            self.assertEqual(
                {entry.title for entry in merged.entries}, {"Base", "New"}
            )

    def test_unknown_copy_does_not_recreate_a_deleted_parent_group(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            current_path = directory / "current.kdbx"
            incoming_path = directory / "incoming.kdbx"
            create_database(str(current_path), password="test-password")
            current = PyKeePass(str(current_path), password="test-password")
            group = current.add_group(current.root_group, "Retired")
            current.save()
            shutil.copyfile(current_path, incoming_path)
            incoming = PyKeePass(str(incoming_path), password="test-password")
            added = incoming.add_entry(
                incoming.find_groups(name="Retired", first=True),
                "Kept",
                "u",
                "s",
            )
            added_id = added.uuid.hex
            incoming.save()
            current = PyKeePass(str(current_path), password="test-password")
            group_id = group.uuid.hex
            current.delete_group(
                current.find_groups(name="Retired", first=True)
            )
            deleted_objects = current.tree.find("./Root/DeletedObjects")
            if deleted_objects is None:
                deleted_objects = etree.SubElement(
                    current.tree.find("./Root"), "DeletedObjects"
                )
            record = etree.SubElement(deleted_objects, "DeletedObject")
            etree.SubElement(record, "UUID").text = base64.b64encode(
                bytes.fromhex(group_id)
            ).decode()
            etree.SubElement(record, "DeletionTime").text = (
                "2026-10-23T00:00:00Z"
            )
            current.save()
            output = directory / "unknown-merged.kdbx"

            result = merge_unknown_copy(
                current_path,
                incoming_path,
                output,
                password="test-password",
                choices={f"unknown-entry:{added_id}": "other"},
            )

            self.assertIsNotNone(result.candidate)
            merged = PyKeePass(str(output), password="test-password")
            self.assertIsNone(merged.find_groups(name="Retired", first=True))
            self.assertEqual(
                merged.find_entries(title="Kept", first=True).group.uuid,
                merged.root_group.uuid,
            )

    def test_restore_merge_resurrects_tombstones_and_keeps_newer_entries(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            current_path = directory / "current.kdbx"
            incoming_path = directory / "incoming.kdbx"
            output = directory / "restored.kdbx"
            password = "synthetic-restore-password"
            create_database(str(current_path), password)
            old = PyKeePass(str(current_path), password)
            old.add_entry(old.root_group, "Restored", "user", "old")
            old.save()
            shutil.copyfile(current_path, incoming_path)
            old_entry = PyKeePass(str(incoming_path), password).find_entries(
                title="Restored",
                first=True,
            )
            restored_id = old_entry.uuid.hex
            current = PyKeePass(str(current_path), password)
            current.delete_entry(
                current.find_entries(title="Restored", first=True)
            )
            current.add_entry(current.root_group, "Newer", "user", "new")
            deleted = current.tree.find("./Root/DeletedObjects")
            if deleted is None:
                deleted = etree.SubElement(
                    current.tree.find("./Root"), "DeletedObjects"
                )
            record = etree.SubElement(deleted, "DeletedObject")
            etree.SubElement(record, "UUID").text = base64.b64encode(
                bytes.fromhex(restored_id),
            ).decode()
            etree.SubElement(record, "DeletionTime").text = (
                "2026-10-23T00:00:00Z"
            )
            current.save()

            result = merge_restore(
                current_path,
                incoming_path,
                output,
                password=password,
                choices={f"restore:entry:{restored_id}:existence": "other"},
            )

            self.assertIsNotNone(result.candidate)
            restored = PyKeePass(str(output), password)
            self.assertEqual(
                {entry.title for entry in restored.entries},
                {"Newer", "Restored"},
            )
            tombstones = {
                base64.b64decode(item.findtext("UUID")).hex()
                for item in restored.tree.xpath(
                    "./Root/DeletedObjects/DeletedObject"
                )
            }
            self.assertNotIn(restored_id, tombstones)

    def test_restore_merge_reviews_conflicting_fields_independently(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            current_path = directory / "current.kdbx"
            incoming_path = directory / "incoming.kdbx"
            output = directory / "restored.kdbx"
            password = "synthetic-restore-password"
            create_database(str(current_path), password)
            current = PyKeePass(str(current_path), password)
            entry = current.add_entry(
                current.root_group,
                "Example",
                "user",
                "old-password",
                url="https://old.example.test",
            )
            identifier = entry.uuid.hex
            current.save()
            shutil.copyfile(current_path, incoming_path)
            current = PyKeePass(str(current_path), password)
            current_entry = current.find_entries(title="Example", first=True)
            current_entry.password = "new-password"
            current_entry.url = "https://new.example.test"
            current_entry.set_custom_property("NewerOnly", "keep-me")
            current.save()
            ui = FakeUI(
                reviews=[
                    {
                        f"restore:entry:{identifier}:String:Password": "other",
                        f"restore:entry:{identifier}:String:URL": "hub",
                    }
                ]
            )

            result = merge_restore(
                current_path,
                incoming_path,
                output,
                password=password,
                ui=ui,
            )

            self.assertIsNotNone(result.candidate)
            restored = PyKeePass(str(output), password)
            entry = restored.find_entries(title="Example", first=True)
            self.assertEqual(entry.password, "old-password")
            self.assertEqual(entry.url, "https://new.example.test")
            self.assertEqual(entry.get_custom_property("NewerOnly"), "keep-me")
            password_conflict = next(
                item
                for item in result.conflicts
                if item["id"] == f"restore:entry:{identifier}:String:Password"
            )
            self.assertTrue(password_conflict["secret"])

    def test_attachments_expiry_and_history_survive_independent_edits(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            base, hub_path, other_path, _ = self._databases(directory)
            hub = PyKeePass(str(hub_path), password="test-password")
            hub.entries[0].add_attachment(
                hub.add_binary(b"hub attachment"), "hub.txt"
            )
            hub.entries[0].username = "new-user"
            hub.save()
            other = PyKeePass(str(other_path), password="test-password")
            other.entries[0].add_attachment(
                other.add_binary(b"other attachment"), "other.txt"
            )
            other.entries[0].expires = True
            other.entries[0].expiry_time = datetime(
                2031, 1, 1, tzinfo=timezone.utc
            )
            other.save()

            output = directory / "merged.kdbx"
            result = merge_databases(
                base,
                hub_path,
                other_path,
                output,
                password="test-password",
            )

            self.assertTrue(result.merged)
            merged = PyKeePass(str(output), password="test-password")
            entry = merged.entries[0]
            self.assertEqual(
                {
                    attachment.filename: attachment.data
                    for attachment in entry.attachments
                },
                {
                    "hub.txt": b"hub attachment",
                    "other.txt": b"other attachment",
                },
            )
            self.assertTrue(entry.expires)
            self.assertEqual(entry.expiry_time.year, 2031)
            self.assertIn(
                "new-user", {item.username for item in entry.history}
            )
            if shutil.which("keepassxc-cli"):
                self.assertTrue(
                    verify_candidate(
                        output,
                        password="test-password",
                        require_keepassxc=True,
                    ).verified
                )

    def test_deletion_recycles_when_enabled_and_keeps_a_tombstone(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            base, hub_path, other_path, identifier = self._databases(directory)
            hub = PyKeePass(str(hub_path), password="test-password")
            hub.tree.find("./Meta/RecycleBinEnabled").text = "True"
            hub.delete_entry(hub.entries[0])
            hub.save()
            output = directory / "merged.kdbx"

            result = merge_databases(
                base,
                hub_path,
                other_path,
                output,
                password="test-password",
            )

            self.assertTrue(result.merged)
            merged = PyKeePass(str(output), password="test-password")
            self.assertEqual(len(merged.entries), 1)
            self.assertEqual(merged.entries[0].group.name, "Recycle Bin")
            self.assertEqual(
                merged.tree.findtext("./Meta/RecycleBinUUID"),
                merged.entries[0].group._element.findtext("UUID"),
            )
            self.assertIn(
                base64.b64encode(bytes.fromhex(identifier)).decode(),
                {
                    element.text
                    for element in merged.tree.xpath(
                        "./Root/DeletedObjects/DeletedObject/UUID"
                    )
                },
            )

    def test_unknown_copy_reviews_group_changes_without_importing_deletions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            _, hub_path, other_path, identifier = self._databases(directory)
            hub = PyKeePass(str(hub_path), password="test-password")
            hub.add_entry(hub.root_group, "Keep", "user", "keep")
            hub.save()
            other = PyKeePass(str(other_path), password="test-password")
            other.root_group.name = "Renamed"
            other.entries[0].username = "other-user"
            group_id = other.root_group.uuid.hex
            new_group = other.add_group(other.root_group, "Empty new group")
            other.save()
            output = directory / "unknown.kdbx"

            result = merge_unknown_copy(
                hub_path,
                other_path,
                output,
                password="test-password",
                force_review=True,
                choices={
                    f"unknown-entry:{identifier}": "other",
                    f"unknown-group:{group_id}": "other",
                    f"unknown-group:{new_group.uuid.hex}": "other",
                },
            )

            self.assertIsNotNone(result.candidate)
            merged = PyKeePass(str(output), password="test-password")
            self.assertEqual(merged.root_group.name, "Renamed")
            self.assertIn(
                "Empty new group", {group.name for group in merged.groups}
            )
            self.assertEqual(len(merged.entries), 2)
            self.assertEqual(
                next(
                    entry
                    for entry in merged.entries
                    if entry.uuid.hex == identifier
                ).username,
                "other-user",
            )

    def test_permanent_deletion_from_recycle_bin_is_not_resurrected(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            base, hub_path, other_path, _ = self._databases(directory)
            database = PyKeePass(str(base), password="test-password")
            database.tree.find("./Meta/RecycleBinEnabled").text = "True"
            recycle = database.add_group(database.root_group, "Recycle Bin")
            database.tree.find("./Meta/RecycleBinUUID").text = (
                recycle._element.findtext("UUID")
            )
            database.move_entry(database.entries[0], recycle)
            database.save()
            shutil.copyfile(base, hub_path)
            shutil.copyfile(base, other_path)
            hub = PyKeePass(str(hub_path), password="test-password")
            hub.delete_entry(hub.entries[0])
            hub.save()
            output = directory / "merged.kdbx"
            result = merge_databases(
                base,
                hub_path,
                other_path,
                output,
                password="test-password",
            )
            self.assertTrue(result.merged)
            self.assertFalse(
                PyKeePass(str(output), password="test-password").entries
            )

    def test_delete_versus_edit_or_move_and_group_deletion_require_review(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            base, hub_path, other_path, identifier = self._databases(directory)
            other = PyKeePass(str(other_path), password="test-password")
            moved = other.add_group(other.root_group, "Destination")
            other.move_entry(other.entries[0], moved)
            other.save()
            hub = PyKeePass(str(hub_path), password="test-password")
            hub.delete_entry(hub.entries[0])
            hub.save()
            output = directory / "merged.kdbx"

            result = merge_databases(
                base,
                hub_path,
                other_path,
                output,
                password="test-password",
            )
            self.assertIn(f"entry:{identifier}:existence", result.unresolved)
            self.assertFalse(output.exists())

            result = merge_databases(
                base,
                hub_path,
                other_path,
                output,
                password="test-password",
                choices={f"entry:{identifier}:existence": "other"},
            )
            self.assertTrue(result.merged)
            merged = PyKeePass(str(output), password="test-password")
            self.assertEqual(merged.entries[0].group.name, "Destination")

    def test_moved_group_deleted_on_the_other_side_requires_review(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            base, hub_path, other_path, _ = self._databases(directory)
            database = PyKeePass(str(base), password="test-password")
            group_id = database.add_group(
                database.root_group, "Moved"
            ).uuid.hex
            database.add_group(database.root_group, "Destination")
            database.save()
            shutil.copyfile(base, hub_path)
            shutil.copyfile(base, other_path)
            hub = PyKeePass(str(hub_path), password="test-password")
            hub.move_group(
                hub.find_groups(name="Moved", first=True),
                hub.find_groups(name="Destination", first=True),
            )
            hub.save()
            other = PyKeePass(str(other_path), password="test-password")
            other.delete_group(other.find_groups(name="Moved", first=True))
            other.save()
            output = directory / "merged.kdbx"

            result = merge_databases(
                base,
                hub_path,
                other_path,
                output,
                password="test-password",
            )
            self.assertIn(f"group:{group_id}:existence", result.unresolved)
            self.assertNotIn(f"group:{group_id}:location", result.unresolved)

            result = merge_databases(
                base,
                hub_path,
                other_path,
                output,
                password="test-password",
                choices={f"group:{group_id}:existence": "hub"},
            )
            self.assertTrue(result.merged)
            merged = PyKeePass(str(output), password="test-password")
            self.assertEqual(
                merged.find_groups(name="Moved", first=True).parentgroup.name,
                "Destination",
            )

    def test_group_deletion_conflicts_preserve_children_when_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            base, hub_path, other_path, _ = self._databases(directory)
            database = PyKeePass(str(base), password="test-password")
            group = database.add_group(database.root_group, "Group")
            group_id = group.uuid.hex
            database.move_entry(database.entries[0], group)
            database.save()
            shutil.copyfile(base, hub_path)
            shutil.copyfile(base, other_path)
            hub = PyKeePass(str(hub_path), password="test-password")
            hub.delete_group(hub.groups[1])
            hub.save()
            other = PyKeePass(str(other_path), password="test-password")
            other.add_entry(other.groups[1], "New", "user", "new-secret")
            other.save()
            output = directory / "merged.kdbx"
            result = merge_databases(
                base,
                hub_path,
                other_path,
                output,
                password="test-password",
            )
            self.assertIn(f"group:{group_id}:existence", result.unresolved)
            self.assertFalse(output.exists())
            result = merge_databases(
                base,
                hub_path,
                other_path,
                output,
                password="test-password",
                choices={f"group:{group_id}:existence": "other"},
            )
            self.assertTrue(result.merged)
            merged = PyKeePass(str(output), password="test-password")
            self.assertEqual(
                {entry.title for entry in merged.entries}, {"Example", "New"}
            )
            self.assertEqual(
                {entry.group.name for entry in merged.entries}, {"Group"}
            )

    def test_same_values_protected_properties_tags_and_icons_merge(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            base, hub_path, other_path, _ = self._databases(directory)
            for path in (hub_path, other_path):
                database = PyKeePass(str(path), password="test-password")
                database.entries[0].username = "same-user"
                database.save()
            hub = PyKeePass(str(hub_path), password="test-password")
            hub.entries[0].set_custom_property(
                "private", "private-value", protect=True
            )
            hub.entries[0].tags = ["work", "ssh"]
            hub.save()
            other = PyKeePass(str(other_path), password="test-password")
            other.entries[0].icon = "5"
            other.save()
            output = directory / "merged.kdbx"
            result = merge_databases(
                base,
                hub_path,
                other_path,
                output,
                password="test-password",
            )
            self.assertTrue(result.merged)
            merged = PyKeePass(str(output), password="test-password")
            self.assertEqual(merged.entries[0].username, "same-user")
            self.assertEqual(merged.entries[0].icon, "5")
            self.assertEqual(set(merged.entries[0].tags), {"work", "ssh"})
            self.assertEqual(
                merged.entries[0].get_custom_property("private"),
                "private-value",
            )
            self.assertTrue(
                merged.entries[0].is_custom_property_protected("private")
            )

    def test_foreign_stale_cache_keeps_only_post_baseline_changes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            recorded_path = directory / "recorded.kdbx"
            incoming_path = directory / "stale-cache.kdbx"
            create_database(str(recorded_path), password="test-password")
            recorded = PyKeePass(str(recorded_path), password="test-password")
            recorded.add_entry(
                recorded.root_group, "A", "user", "old-password"
            )
            # Back-date A so the later edit is clearly newer: both
            # times are whole seconds and could fall in the same one.
            hour_ago = recorded._encode_time(
                datetime.now(timezone.utc) - timedelta(hours=1)
            )
            for name in ("LastModificationTime", "LocationChanged"):
                recorded.entries[0]._element.find(
                    f"Times/{name}"
                ).text = hour_ago
            recorded.save()
            shutil.copyfile(recorded_path, incoming_path)

            recorded.add_entry(recorded.root_group, "B", "user", "preserve-me")
            recorded.save()
            stale = PyKeePass(str(incoming_path), password="test-password")
            stale.entries[0].password = "new-password"
            modified_at = int(
                (
                    datetime.now(timezone.utc)
                    - datetime(1, 1, 1, tzinfo=timezone.utc)
                ).total_seconds()
            )
            stale.entries[0]._element.find(
                "Times/LastModificationTime"
            ).text = base64.b64encode(struct.pack("<Q", modified_at)).decode()
            stale.add_entry(stale.root_group, "C", "user", "new-entry")
            stale.save()
            recorded_at = (
                datetime.now(timezone.utc) - timedelta(days=1)
            ).isoformat()

            check = check_foreign_descent(
                recorded_path,
                incoming_path,
                password="test-password",
                key_file=None,
                recorded_at=recorded_at,
            )
            self.assertFalse(check.descends)
            self.assertFalse(check.suspicious)

            output = directory / "filtered.kdbx"
            filtered, suspicious = filter_foreign_stale_cache(
                recorded_path,
                incoming_path,
                output,
                password="test-password",
                key_file=None,
                recorded_at=recorded_at,
            )

            self.assertFalse(suspicious)
            merged = PyKeePass(str(filtered), password="test-password")
            self.assertEqual(
                {entry.title for entry in merged.entries}, {"A", "B", "C"}
            )
            self.assertEqual(
                next(
                    entry for entry in merged.entries if entry.title == "A"
                ).password,
                "new-password",
            )

    def test_foreign_deletion_descends_and_future_clock_is_reported(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            base, _, incoming_path, identifier = self._databases(directory)
            database = PyKeePass(str(base), password="test-password")
            before = datetime.now(timezone.utc) - timedelta(days=2)
            for element in database.tree.xpath(
                ".//Times/LastModificationTime | .//Times/LocationChanged"
            ):
                element.text = database._encode_time(before)
            database.save()
            shutil.copyfile(base, incoming_path)
            incoming = PyKeePass(str(incoming_path), password="test-password")
            incoming.delete_entry(incoming.entries[0])
            deleted = incoming.tree.find("./Root/DeletedObjects")
            record = etree.SubElement(deleted, "DeletedObject")
            etree.SubElement(record, "UUID").text = base64.b64encode(
                bytes.fromhex(identifier)
            ).decode()
            etree.SubElement(record, "DeletionTime").text = (
                incoming._encode_time(
                    datetime.now(timezone.utc) - timedelta(hours=1)
                )
            )
            incoming.save()
            recorded_at = (
                datetime.now(timezone.utc) - timedelta(days=1)
            ).isoformat()
            check = check_foreign_descent(
                base,
                incoming_path,
                password="test-password",
                key_file=None,
                recorded_at=recorded_at,
            )
            self.assertTrue(check.descends)
            self.assertFalse(check.suspicious)
            group = incoming.add_group(incoming.root_group, "Future group")
            group._element.find("Times/LastModificationTime").text = (
                incoming._encode_time(
                    datetime.now(timezone.utc) + timedelta(days=2)
                )
            )
            incoming.save()
            check = check_foreign_descent(
                base,
                incoming_path,
                password="test-password",
                key_file=None,
                recorded_at=recorded_at,
            )
            self.assertIn(group.uuid.hex, check.suspicious)

    def test_foreign_save_in_commit_second_is_not_clock_skew(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            base, _, incoming_path, _ = self._databases(directory)
            moment = (
                datetime.now(timezone.utc) - timedelta(minutes=1)
            ).replace(microsecond=0)
            baseline = PyKeePass(str(base), password="test-password")
            baseline.entries[0].mtime = moment - timedelta(seconds=1)
            baseline.save()
            incoming = PyKeePass(str(incoming_path), password="test-password")
            incoming.entries[0].username = "new-user"
            incoming.entries[0].mtime = moment
            incoming.save()
            check = check_foreign_descent(
                base,
                incoming_path,
                password="test-password",
                key_file=None,
                recorded_at=moment.replace(microsecond=900000).isoformat(),
            )
            self.assertTrue(check.descends)
            self.assertFalse(check.suspicious)

    @unittest.skipUnless(
        shutil.which("keepassxc-cli"), "keepassxc-cli unavailable"
    )
    def test_foreign_cli_save_does_not_flag_untouched_defaults(self):
        for command, stale_cache in (
            ("edit", False),
            ("add", False),
            ("edit", True),
            ("add", True),
        ):
            with (
                self.subTest(command=command, stale_cache=stale_cache),
                tempfile.TemporaryDirectory(
                    dir=os.environ.get("XDG_RUNTIME_DIR"),
                ) as name,
            ):
                directory = Path(name)
                base = directory / "base.kdbx"
                incoming_path = directory / "incoming.kdbx"
                create_database(str(base), password="test")
                baseline = PyKeePass(str(base), password="test")
                example = baseline.add_entry(
                    baseline.root_group, "Example", "user", "dummy"
                )
                group = baseline.add_group(
                    baseline.root_group, "Untouched group"
                )
                untouched = baseline.add_entry(
                    group, "Untouched", "user", "dummy"
                )
                for entry in (example, untouched):
                    auto_type = entry._element.find("AutoType")
                    for child in tuple(auto_type):
                        if child.tag in {"DefaultSequence", "Association"}:
                            auto_type.remove(child)
                for tag in ("EnableAutoType", "EnableSearching"):
                    etree.SubElement(group._element, tag).text = "Null"
                for tag in (
                    "Notes",
                    "IsExpanded",
                    "DefaultAutoTypeSequence",
                    "IconID",
                ):
                    for element in group._element.findall(tag):
                        group._element.remove(element)
                old = datetime.now(timezone.utc) - timedelta(days=2)
                for element in baseline.tree.xpath(
                    ".//Times/LastModificationTime | .//Times/LocationChanged"
                ):
                    element.text = baseline._encode_time(old)
                baseline.save()
                shutil.copyfile(base, incoming_path)
                if stale_cache:
                    baseline.add_entry(
                        baseline.root_group,
                        "Newer baseline entry",
                        "user",
                        "keep",
                    )
                    baseline.save()
                result = subprocess.run(
                    [
                        "keepassxc-cli",
                        command,
                        "-q",
                        "-u",
                        "cli-user",
                        str(incoming_path),
                        "Example" if command == "edit" else "New",
                    ],
                    input="test\n",
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                recorded_at = (
                    datetime.now(timezone.utc) - timedelta(minutes=1)
                ).isoformat()
                check = check_foreign_descent(
                    base,
                    incoming_path,
                    password="test",
                    key_file=None,
                    recorded_at=recorded_at,
                )
                self.assertEqual(check.descends, not stale_cache)
                self.assertFalse(check.suspicious)
                filtered, suspicious = filter_foreign_stale_cache(
                    base,
                    incoming_path,
                    directory / "filtered.kdbx",
                    password="test",
                    key_file=None,
                    recorded_at=recorded_at,
                )
                self.assertFalse(suspicious)
                saved = PyKeePass(str(filtered), password="test")
                changed = saved.find_entries(
                    title="Example" if command == "edit" else "New",
                    first=True,
                )
                self.assertEqual(changed.username, "cli-user")
                self.assertIsNotNone(
                    saved.find_entries(title="Untouched", first=True)
                )
                if stale_cache:
                    self.assertIsNotNone(
                        saved.find_entries(
                            title="Newer baseline entry", first=True
                        ),
                    )

    def test_foreign_default_comparison_preserves_timestamp_safeguards(self):
        with tempfile.TemporaryDirectory(
            dir=os.environ.get("XDG_RUNTIME_DIR")
        ) as name:
            directory = Path(name)
            base = directory / "base.kdbx"
            incoming_path = directory / "incoming.kdbx"
            create_database(str(base), password="test")
            baseline = PyKeePass(str(base), password="test")
            entry = baseline.add_entry(
                baseline.root_group, "Example", "user", "dummy"
            )
            group = baseline.add_group(baseline.root_group, "Group")
            before = datetime.now(timezone.utc) - timedelta(days=2)
            for element in baseline.tree.xpath(
                ".//Times/LastModificationTime | .//Times/LocationChanged"
            ):
                element.text = baseline._encode_time(before)
            baseline.save()
            recorded_at = (before + timedelta(days=1)).isoformat()
            for mutation in (
                "old-notes",
                "future-notes",
                "custom-empty",
                "protected-empty",
                "group-setting",
            ):
                with self.subTest(mutation=mutation):
                    shutil.copyfile(base, incoming_path)
                    incoming = PyKeePass(str(incoming_path), password="test")
                    changed = incoming.find_entries(
                        uuid=entry.uuid, first=True
                    )
                    if mutation == "group-setting":
                        changed = incoming.find_groups(
                            uuid=group.uuid, first=True
                        )
                        etree.SubElement(
                            changed._element, "EnableAutoType"
                        ).text = "False"
                    elif mutation == "custom-empty":
                        changed.set_custom_property("Custom blank field", "")
                    elif mutation == "protected-empty":
                        changed.notes = ""
                        changed._element.find("String[Key='Notes']/Value").set(
                            "Protected",
                            "True",
                        )
                    else:
                        changed.notes = "A real change"
                        if mutation == "future-notes":
                            changed.mtime = datetime.now(
                                timezone.utc
                            ) + timedelta(days=1)
                    incoming.save()
                    check = check_foreign_descent(
                        base,
                        incoming_path,
                        password="test",
                        key_file=None,
                        recorded_at=recorded_at,
                    )
                    self.assertIn(changed.uuid.hex, check.suspicious)
                    output = directory / "filtered.kdbx"
                    _, suspicious = filter_foreign_stale_cache(
                        base,
                        incoming_path,
                        output,
                        password="test",
                        key_file=None,
                        recorded_at=recorded_at,
                    )
                    self.assertIn(changed.uuid.hex, suspicious)
                    self.assertFalse(output.exists())

    def test_merge_selects_the_most_recent_master_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            base_path = directory / "base.kdbx"
            hub_path = directory / "hub.kdbx"
            other_path = directory / "new-key.kdbx"
            create_database(str(base_path), password="old-password")
            base = PyKeePass(str(base_path), password="old-password")
            base.add_entry(base.root_group, "Example", "user", "secret")
            base.save()
            shutil.copyfile(base_path, hub_path)
            create_database(str(other_path), password="new-password")
            other = PyKeePass(str(other_path), password="new-password")
            other.tree._setroot(copy.deepcopy(base.tree.getroot()))
            changed = int(
                (
                    datetime.now(timezone.utc)
                    + timedelta(days=1)
                    - datetime(1, 1, 1, tzinfo=timezone.utc)
                ).total_seconds()
            )
            other.tree.find("./Meta/MasterKeyChanged").text = base64.b64encode(
                struct.pack("<Q", changed)
            ).decode()
            other.save()

            output = directory / "merged.kdbx"
            result = merge_databases(
                base_path,
                hub_path,
                other_path,
                output,
                password="old-password",
                credential_resolver=lambda _path: ("new-password", None),
            )

            self.assertTrue(result.merged)
            self.assertEqual(result.password, "new-password")
            merged = PyKeePass(str(output), password="new-password")
            self.assertEqual(merged.entries[0].title, "Example")
            with self.assertRaises(CredentialsError):
                PyKeePass(str(output), password="old-password")


if __name__ == "__main__":
    unittest.main()
