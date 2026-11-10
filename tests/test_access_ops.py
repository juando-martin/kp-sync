# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
import json
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kp_sync.access_ops import (
    ACCESS_IDENTITY,
    ACCESS_LEASE,
    AccessOperationError,
    access_identity,
    access_paths,
    apply_file_operation,
    clear_transit,
    create_upload,
    discard_upload,
    in_transit,
    mark_transit,
    validate_operation,
)
from kp_sync.access_op import run_access_op
from kp_sync.config import ConfigError, load_config
from kp_sync.daemon.leases import LeaseStore
from kp_sync.daemon.protocol import ProtocolError, validate_request
from kp_sync.daemon.server import Controller
from kp_sync.engine import SyncResult
from kp_sync.paths import ensure_run_dir
from kp_sync.state import HistoryState
from tests.fixtures import kdbx4_bytes


class AccessOpsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        config_path = self.root / "config.toml"
        config_path.write_text(
            f'hub = {json.dumps(str(self.root / "hub.kdbx"))}\n'
            '[access]\ndb_path = "/vault/pass.kdbx"\n',
            encoding="utf-8",
        )
        self.config = load_config(
            config_path,
            environ={
                "HOME": str(self.root),
                "XDG_RUNTIME_DIR": str(self.runtime),
            },
        )
        self.run_dir = ensure_run_dir(
            self.config.profile,
            xdg_runtime_dir=self.runtime,
        )
        self.staging, self.database, _ = access_paths(
            self.config, self.run_dir
        )
        self.database.write_bytes(kdbx4_bytes(b"initial"))

    def test_virtual_paths_are_restricted_to_database_and_temp_siblings(self):
        self.assertEqual(
            validate_operation(
                self.config,
                self.run_dir,
                "PUT",
                "/vault/pass.kdbx.tmp",
            )[0],
            self.staging / "pass.kdbx.tmp",
        )
        for path in (
            "/etc/passwd",
            "/vault/../outside",
            "/vault/pass.kdbx/child",
            "/vault/unrelated.txt",
            "vault/pass.kdbx",
        ):
            with self.subTest(path=path):
                with self.assertRaises(AccessOperationError):
                    validate_operation(
                        self.config, self.run_dir, "DELETE", path
                    )

    def test_access_identity_changes_between_lifecycles(self):
        first = access_identity(self.run_dir)
        self.assertRegex(first, r"^session:kp-access:[0-9a-f]{32}$")
        self.assertEqual(access_identity(self.run_dir), first)
        (self.run_dir / "access-lifecycle.json").unlink()
        second = access_identity(self.run_dir)
        self.assertNotEqual(second, first)

    def test_root_level_database_path_is_supported_and_stays_file_scoped(self):
        config_path = self.root / "root-path.toml"
        config_path.write_text(
            f'hub = {json.dumps(str(self.root / "hub.kdbx"))}\n'
            '[access]\ndb_path = "/pass.kdbx"\n',
            encoding="utf-8",
        )
        config = load_config(
            config_path,
            environ={
                "HOME": str(self.root),
                "XDG_RUNTIME_DIR": str(self.runtime),
            },
        )
        staging, database, _ = access_paths(config, self.run_dir)
        self.assertEqual(database, staging / "pass.kdbx")
        self.assertEqual(
            validate_operation(config, self.run_dir, "PUT", "/pass.kdbx.tmp")[
                0
            ],
            staging / "pass.kdbx.tmp",
        )
        for path in ("/etc/passwd", "/anything/else", "/pass.kdbx/child"):
            with self.subTest(path=path):
                with self.assertRaises(AccessOperationError):
                    validate_operation(config, self.run_dir, "DELETE", path)

    def test_configured_database_path_must_be_normalized_and_absolute(self):
        config_path = self.root / "bad.toml"
        for path in ("/vault/../pass.kdbx", "vault/pass.kdbx", "/"):
            with self.subTest(path=path):
                config_path.write_text(
                    f'hub = "{self.root / "hub.kdbx"}"\n'
                    f'[access]\ndb_path = "{path}"\n',
                    encoding="utf-8",
                )
                with self.assertRaises(ConfigError):
                    load_config(
                        config_path,
                        environ={
                            "HOME": str(self.root),
                            "XDG_RUNTIME_DIR": str(self.runtime),
                        },
                    )

    def test_put_move_and_delete_obey_kdbx_and_backup_rules(self):
        incoming = kdbx4_bytes(b"new database")
        upload_id = create_upload(self.config, self.run_dir, incoming)
        sync, _ = apply_file_operation(
            self.config,
            self.run_dir,
            operation="PUT",
            file_path="/vault/pass.kdbx.tmp",
            upload_id=upload_id,
        )
        self.assertFalse(sync)
        self.assertEqual(
            (self.staging / "pass.kdbx.tmp").read_bytes(), incoming
        )
        sync, _ = apply_file_operation(
            self.config,
            self.run_dir,
            operation="MOVE",
            file_path="/vault/pass.kdbx.tmp",
            destination="/vault/pass.kdbx",
        )
        self.assertTrue(sync)
        self.assertEqual(self.database.read_bytes(), incoming)
        backups = list((self.run_dir / "backups").glob("*.kdbx"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), kdbx4_bytes(b"initial"))

        invalid_id = create_upload(self.config, self.run_dir, b"not a KDBX")
        with self.assertRaisesRegex(AccessOperationError, "KDBX verification"):
            apply_file_operation(
                self.config,
                self.run_dir,
                operation="PUT",
                file_path="/vault/pass.kdbx",
                upload_id=invalid_id,
            )
        self.assertEqual(self.database.read_bytes(), incoming)

        apply_file_operation(
            self.config,
            self.run_dir,
            operation="DELETE",
            file_path="/vault/pass.kdbx",
        )
        self.assertFalse(self.database.exists())
        self.assertTrue(list((self.run_dir / "backups").glob("*.kdbx")))

    def test_symbolic_links_and_in_transit_timeout_are_rejected_safely(self):
        link = self.staging / "pass.kdbx.tmp"
        outside = self.root / "outside"
        outside.write_bytes(b"outside")
        os.symlink(outside, link)
        with self.assertRaisesRegex(
            AccessOperationError, "regular user-owned"
        ):
            validate_operation(
                self.config,
                self.run_dir,
                "DELETE",
                "/vault/pass.kdbx.tmp",
            )
        link.unlink()

        mark_transit(self.run_dir, now=100)
        self.assertTrue(in_transit(self.run_dir, now=159))
        self.assertFalse(in_transit(self.run_dir, now=160))
        self.assertFalse((self.run_dir / "access-in-transit.json").exists())
        clear_transit(self.run_dir)

    def test_daemon_protocol_rejects_unscoped_or_incomplete_access_requests(
        self,
    ):
        base = {
            "command": "access-op",
            "operation": "BEGIN",
            "db_path": "/vault/pass.kdbx",
        }
        self.assertEqual(validate_request(base)["operation"], "BEGIN")
        with self.assertRaises(ProtocolError):
            validate_request(base | {"upload_id": "a" * 32})
        with self.assertRaises(ProtocolError):
            validate_request({"command": "access-op", "operation": "PUT"})
        restore = {
            "command": "restore",
            "source_kind": "version",
            "source_value": "a" * 64,
            "restore_mode": "merge",
        }
        self.assertEqual(validate_request(restore)["command"], "restore")
        with self.assertRaises(ProtocolError):
            validate_request(restore | {"restore_mode": "unsafe"})

    def test_access_cli_validates_paths_and_stages_put_bytes_out_of_band(self):
        class Client:
            directory = self.run_dir

            def __init__(self):
                self.requests = []

            def request(self, command, **fields):
                self.requests.append((command, fields))
                return {"action": "access"}

        client = Client()
        payload = kdbx4_bytes(b"synthetic upload")
        self.assertEqual(
            run_access_op(
                self.config,
                environ={
                    "OPERATION": "PUT",
                    "DB_PATH": str(self.database),
                    "FILE_PATH": "/vault/pass.kdbx",
                    "IDENTIFIED_USER": "synthetic-user",
                },
                stdin=io.BytesIO(payload),
                client=client,
            ),
            0,
        )
        command, fields = client.requests[0]
        self.assertEqual(command, "access-op")
        self.assertEqual(fields["db_path"], "/vault/pass.kdbx")
        upload = self.staging / f".kp-sync-upload-{fields['upload_id']}"
        self.assertEqual(upload.read_bytes(), payload)
        discard_upload(self.config, self.run_dir, fields["upload_id"])

        output = io.BytesIO()
        self.assertEqual(
            run_access_op(
                self.config,
                environ={
                    "OPERATION": "GET",
                    "DB_PATH": str(self.database),
                    "FILE_PATH": "/vault/pass.kdbx",
                },
                stdout=output,
                client=client,
            ),
            0,
        )
        self.assertEqual(output.getvalue(), self.database.read_bytes())
        self.assertEqual(len(client.requests), 2)
        with self.assertRaises(AccessOperationError):
            run_access_op(
                self.config,
                environ={
                    "OPERATION": "GET",
                    "DB_PATH": str(self.database),
                    "FILE_PATH": "/vault/other.kdbx",
                },
                client=client,
            )
        self.assertEqual(len(client.requests), 2)

    def test_access_cli_rejects_owner_database_path_outside_profile_staging(
        self,
    ):
        class Client:
            directory = self.run_dir

            def __init__(self):
                self.requests = []

            def request(self, command, **fields):
                self.requests.append((command, fields))

        client = Client()
        with self.assertRaisesRegex(
            AccessOperationError, "access staging database"
        ):
            run_access_op(
                self.config,
                environ={
                    "OPERATION": "BEGIN",
                    "DB_PATH": str(self.root / "other.kdbx"),
                },
                client=client,
            )
        self.assertEqual(client.requests, [])

    def test_access_lease_expiry_does_not_remove_staging(self):
        leases = LeaseStore(self.run_dir, clock=lambda: 100, access_ttl=10)
        leases.take(ACCESS_LEASE, "access")
        self.assertFalse(leases.expire())
        self.assertEqual(ACCESS_IDENTITY, "session:kp-access")
        self.assertTrue(self.database.exists())
        leases.clock = lambda: 111
        self.assertTrue(leases.expire())
        self.assertTrue(self.database.exists())

    def test_begin_end_stage_and_remove_only_after_clean_post_sync(self):
        hub = self.root / "hub.kdbx"
        hub.write_bytes(kdbx4_bytes(b"hub database"))
        controller, calls = self._controller()
        begin = self._request("BEGIN")
        self.assertEqual(controller.handle(begin)["action"], "access-begin")
        staged_database = controller.directory / "staging" / "pass.kdbx"
        self.assertEqual(staged_database.read_bytes(), kdbx4_bytes(b"initial"))
        self.assertIn(ACCESS_LEASE, controller.leases.leases)
        self.assertEqual(len(calls), 1)

        end = self._request("END")
        self.assertEqual(controller.handle(end)["action"], "access-end")
        self.assertNotIn(ACCESS_LEASE, controller.leases.leases)
        self.assertFalse(staged_database.parent.exists())

    def test_unavailable_post_sync_retains_access_staging(self):
        from concurrent.futures import Future

        controller, _ = self._controller()
        before = self.database.read_bytes()
        future = Future()
        future.set_result({"action": "unavailable"})
        with patch.object(controller, "queue_sync", return_value=future):
            with self.assertRaisesRegex(
                AccessOperationError, "staged files were retained"
            ):
                controller.handle(self._request("END"))
        self.assertEqual(self.database.read_bytes(), before)

    def test_database_delete_blocks_get_until_move_or_timeout(self):
        hub = self.root / "hub.kdbx"
        hub.write_bytes(kdbx4_bytes(b"hub database"))
        controller, _ = self._controller()
        controller.handle(self._request("BEGIN"))
        delete = self._request(
            "DELETE", file_path="/vault/pass.kdbx", identified_user="alice"
        )
        controller.handle(delete)
        with self.assertRaisesRegex(AccessOperationError, "still in transit"):
            controller.handle(
                self._request("GET", file_path="/vault/pass.kdbx")
            )

        upload = create_upload(
            self.config,
            controller.directory,
            kdbx4_bytes(b"complete upload"),
        )
        controller.handle(
            self._request(
                "PUT",
                file_path="/vault/pass.kdbx.tmp",
                identified_user="alice",
                upload_id=upload,
            )
        )
        self.assertFalse(
            (controller.directory / "staging" / "pass.kdbx").exists(),
        )
        controller.handle(
            self._request(
                "MOVE",
                file_path="/vault/pass.kdbx.tmp",
                destination="/vault/pass.kdbx",
                identified_user="alice",
            )
        )
        self.assertFalse((self.run_dir / "access-in-transit.json").exists())
        self.assertEqual(
            (controller.directory / "staging" / "pass.kdbx").read_bytes(),
            kdbx4_bytes(b"complete upload"),
        )

    def test_end_after_lease_expiry_syncs_and_retains_pending_upload(self):
        from concurrent.futures import Future

        controller, _ = self._controller()
        controller.leases.take(ACCESS_LEASE, "access")
        controller.leases.release(ACCESS_LEASE)
        before = self.database.read_bytes()
        calls = []

        def pending_sync(*args):
            calls.append(ACCESS_LEASE in controller.leases.leases)
            future = Future()
            future.set_result({"pending": "needs review", "missing": []})
            return future

        with patch.object(controller, "queue_sync", side_effect=pending_sync):
            with self.assertRaisesRegex(
                AccessOperationError, "staged files were retained"
            ):
                controller.handle(self._request("END"))
        self.assertEqual(calls, [True])
        self.assertEqual(self.database.read_bytes(), before)

    def test_end_does_not_discard_unfinished_atomic_upload(self):
        controller, _ = self._controller()
        self.database.unlink()
        temporary = self.staging / "pass.kdbx.tmp"
        payload = kdbx4_bytes(b"unfinished user save")
        temporary.write_bytes(payload)
        with self.assertRaisesRegex(AccessOperationError, "unfinished"):
            controller.handle(self._request("END"))
        self.assertEqual(temporary.read_bytes(), payload)

    def test_upload_creation_waits_for_end_cleanup(self):
        from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
        from threading import Event

        controller, _ = self._controller()
        starting = Event()
        payload = kdbx4_bytes(b"concurrent upload")
        with ThreadPoolExecutor(max_workers=1) as pool:

            def upload():
                starting.set()
                return create_upload(self.config, self.run_dir, payload)

            def sync_during_end(*args):
                self.upload_future = pool.submit(upload)
                self.assertTrue(starting.wait(timeout=2))
                with self.assertRaises(TimeoutError):
                    self.upload_future.result(timeout=0.1)
                result = Future()
                result.set_result({"missing": [], "pending": None})
                return result

            with patch.object(
                controller, "queue_sync", side_effect=sync_during_end
            ):
                controller.handle(self._request("END"))
            identifier = self.upload_future.result(timeout=2)
        self.assertEqual(
            (self.staging / f".kp-sync-upload-{identifier}").read_bytes(),
            payload,
        )

    def test_expired_transit_get_does_not_refill_missing_database(self):
        hub = self.root / "hub.kdbx"
        hub_data = kdbx4_bytes(b"hub database")
        hub.write_bytes(hub_data)
        controller, _ = self._controller()
        self.database.unlink()
        mark_transit(
            controller.directory,
            now=controller.leases.clock() - 61,
        )

        with self.assertRaisesRegex(
            AccessOperationError, "staged database is missing"
        ):
            controller.handle(
                self._request("GET", file_path="/vault/pass.kdbx")
            )

        self.assertFalse(self.database.exists())
        self.assertEqual(hub.read_bytes(), hub_data)

    def test_get_does_not_refresh_a_changed_staged_copy(self):
        hub = self.root / "hub.kdbx"
        hub.write_bytes(kdbx4_bytes(b"new hub database"))
        controller, calls = self._controller()
        old_sha = _write_version_state(
            controller.directory,
            self.database.read_bytes(),
            hub.read_bytes(),
        )
        controller.leases.take(ACCESS_LEASE, "access")
        database = controller.directory / "staging" / "pass.kdbx"
        database.write_bytes(kdbx4_bytes(b"user upload not synced"))
        before = database.read_bytes()
        controller.handle(self._request("GET", file_path="/vault/pass.kdbx"))
        self.assertEqual(database.read_bytes(), before)
        self.assertEqual(calls, [])
        self.assertNotEqual(old_sha, "")

    def _controller(self):
        calls = []

        class Engine:
            def sync(self, *, dry_run=False):
                calls.append(dry_run)
                return SyncResult(
                    current=None, action="unchanged", classifications={}
                )

        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.runtime)}):
            controller = Controller(
                self.config, engine_factory=lambda _ui: Engine()
            )
            self.assertEqual(controller.directory, self.run_dir)
        self.addCleanup(controller.watch.close)
        self.addCleanup(lambda: controller.pool.shutdown(wait=True))
        self.addCleanup(lambda: controller.requests.shutdown(wait=True))
        return controller, calls

    def _request(self, operation, **fields):
        return {
            "command": "access-op",
            "operation": operation,
            "db_path": "/vault/pass.kdbx",
            "ui": "none",
            **fields,
        }


def _write_version_state(run_dir, old_data, current_data):
    from kp_sync.history import sha256_file

    state = HistoryState()
    old_path = run_dir / "old.kdbx"
    new_path = run_dir / "new.kdbx"
    old_path.write_bytes(old_data)
    new_path.write_bytes(current_data)
    old_sha, old_size = sha256_file(str(old_path))
    current_sha, current_size = sha256_file(str(new_path))
    state.commit(old_sha, (), "test", (ACCESS_IDENTITY,), size=old_size)
    state.commit(
        current_sha, (old_sha,), "test", ("hub:test",), size=current_size
    )
    state.save(run_dir / "state.json")
    return old_sha


if __name__ == "__main__":
    unittest.main()
