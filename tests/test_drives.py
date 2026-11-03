import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kp_sync.config import load_config
from kp_sync.drives import (
    DriveEndpoint,
    DriveError,
    identify_drive,
    list_drives,
    register_drive,
)
from kp_sync.endpoints import EndpointChanged, EndpointUnavailable
from kp_sync.engine import endpoints_from_config
from kp_sync.baseline import DriveStore


class DriveTest(unittest.TestCase):
    def test_runtime_directory_preserves_drive_identity_and_store_root(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            mount = root / "mount"
            mount.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (outside / "pass.kdbx").write_bytes(b"outside")
            config_path = root / "config.toml"
            drive = register_drive(root / "runtime", mount, "SERIAL:1234-abcd")
            for directory in (None, ".", "kp", "vault/kp"):
                with self.subTest(directory=directory):
                    config_path.write_text(
                        '[[endpoints]]\nname = "pendrive"\ntype = "file"\n'
                        'source = "runtime"\n'
                        + (
                            f'[drives]\ndirectory = "{directory}"\n'
                            if directory
                            else ""
                        )
                    )
                    config = load_config(
                        config_path,
                        environ={
                            "HOME": temp_dir,
                            "XDG_RUNTIME_DIR": temp_dir,
                        },
                    )
                    target = mount / (directory or ".") / "pass.kdbx"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(b"database")
                    with patch(
                        "kp_sync.engine.list_drives", return_value=[drive]
                    ):
                        (endpoint,) = endpoints_from_config(
                            config, root / "runtime"
                        )
                    endpoint.identity_resolver = lambda _: drive.identity
                    self.assertEqual(endpoint.path, target)
                    self.assertEqual(
                        endpoint.identity, f"drive:{drive.identity}"
                    )
                    self.assertEqual(
                        endpoint.probe().sha256,
                        hashlib.sha256(b"database").hexdigest(),
                    )
                    store = DriveStore(endpoint, config.baseline["dir"])
                    self.assertEqual(store.root, mount / "kp-sync")
                    if directory not in (None, "."):
                        target.parent.rename(
                            target.parent.with_name(
                                target.parent.name + "-saved"
                            )
                        )
                        target.parent.symlink_to(
                            outside, target_is_directory=True
                        )
                        with self.assertRaisesRegex(DriveError, "escapes"):
                            endpoint.probe()
                        target.parent.unlink()

    def test_drive_registration_and_stale_removal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            mount = root / "mount"
            mount.mkdir()
            run_dir = root / "runtime"
            drive = register_drive(run_dir, mount, "SERIAL:1234-abcd")
            self.assertEqual(drive.identity, "SERIAL:1234-abcd")

            def resolve_identity(_mount):
                return "SERIAL:1234-abcd"

            self.assertEqual(
                list_drives(run_dir, identity_resolver=resolve_identity),
                [drive],
            )

            mount.rmdir()
            self.assertEqual(
                list_drives(run_dir, identity_resolver=resolve_identity), []
            )
            self.assertEqual(list((run_dir / "drives").glob("*.json")), [])

    def test_identity_from_lsblk_and_invalid_record(self) -> None:
        def runner(argv, **kwargs):
            payload = {
                "blockdevices": [
                    {
                        "serial": "SERIAL",
                        "uuid": "1234-abcd",
                        "mountpoints": ["/mnt/key"],
                    }
                ]
            }
            return subprocess.CompletedProcess(
                argv, 0, json.dumps(payload), ""
            )

        self.assertEqual(
            identify_drive("/mnt/key", runner=runner),
            "SERIAL:1234-abcd",
        )

        with self.assertRaises(DriveError):
            identify_drive("/mnt/other", runner=runner)

    def test_partition_identity_inherits_disk_serial_without_crossing_disks(
        self,
    ):
        def runner(argv, **kwargs):
            self.assertIn("--tree", argv)
            payload = {
                "blockdevices": [
                    {
                        "serial": "FIRST",
                        "children": [
                            {
                                "serial": None,
                                "uuid": "1234-abcd",
                                "mountpoints": ["/mnt/key"],
                            },
                        ],
                    },
                    {
                        "serial": "SECOND",
                        "children": [
                            {
                                "serial": None,
                                "children": [
                                    {
                                        "serial": None,
                                        "uuid": "5678-efgh",
                                        "mountpoints": ["/mnt/other"],
                                    },
                                ],
                            },
                        ],
                    },
                    {
                        "serial": None,
                        "uuid": "9012-abcd",
                        "mountpoints": ["/mnt/no-serial"],
                    },
                ]
            }
            return subprocess.CompletedProcess(
                argv, 0, json.dumps(payload), ""
            )

        self.assertEqual(
            identify_drive("/mnt/key", runner=runner), "FIRST:1234-abcd"
        )
        self.assertEqual(
            identify_drive("/mnt/other", runner=runner), "SECOND:5678-efgh"
        )
        with self.assertRaises(DriveError):
            identify_drive("/mnt/no-serial", runner=runner)

    def test_stale_mount_record_with_a_different_device_is_removed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            mount = root / "mount"
            mount.mkdir()
            run_dir = root / "runtime"
            register_drive(run_dir, mount, "SERIAL:1234-abcd")

            drives = list_drives(
                run_dir,
                identity_resolver=lambda _mount: "OTHER:5678-efgh",
            )

            self.assertEqual(drives, [])
            self.assertEqual(list((run_dir / "drives").glob("*.json")), [])

    def test_drive_endpoint_rechecks_device_identity_before_access(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            mount = Path(temp_dir) / "mount"
            mount.mkdir()
            endpoint = DriveEndpoint(
                "pendrive",
                mount,
                "pass.kdbx",
                "SERIAL:1234-abcd",
                identity_resolver=lambda _mount: "OTHER:5678-efgh",
            )
            with self.assertRaises(EndpointUnavailable):
                endpoint.probe()

    def test_drive_endpoint_rechecks_identity_before_replacement(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            mount = root / "mount"
            mount.mkdir()
            target = mount / "pass.kdbx"
            target.write_bytes(b"original")
            candidate = root / "candidate.kdbx"
            candidate.write_bytes(b"candidate")
            identities = iter(("SERIAL:1234-abcd", "OTHER:5678-efgh"))
            endpoint = DriveEndpoint(
                "pendrive",
                mount,
                "pass.kdbx",
                "SERIAL:1234-abcd",
                identity_resolver=lambda _mount: next(identities),
            )

            with self.assertRaises(EndpointChanged):
                endpoint.store_if_unchanged(
                    candidate,
                    hashlib.sha256(b"original").hexdigest(),
                )

            self.assertEqual(target.read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()
