# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
import contextlib
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from kp_sync.config import load_config
from kp_sync.daemon.client import Client, ClientError, LeaseConnection
from kp_sync.daemon.leases import LeaseStore
from kp_sync.daemon.protocol import (
    Connection,
    MAX_MESSAGE,
    ProtocolError,
    validate_request,
)
from kp_sync.daemon.server import Controller, RemoteUI, Server, UnlockedUI
from kp_sync.daemon.triggers import HubWatch, Triggers
from kp_sync.engine import SyncResult
from kp_sync.ui.none import NoneUI
from kp_sync.ui.fake import FakeUI


class DaemonTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.notifications = patch("kp_sync.daemon.apps.notify_user")
        self.notifications.start()
        self.addCleanup(self.notifications.stop)
        self.config = load_config(
            environ={
                "XDG_RUNTIME_DIR": str(self.root),
                "HOME": str(self.root),
            },
            home=self.root,
            hostname="test",
        )
        self.calls = []
        self.restore_calls = []

    def controller(self, action=None):
        test = self

        class Engine:
            def sync(self, *, dry_run=False):
                test.calls.append(dry_run)
                if action is not None:
                    return action()
                return SyncResult(
                    current=None, action="unchanged", classifications={}
                )

            def restore(
                self, source_kind, source_value, *, mode="merge", dry_run=False
            ):
                test.restore_calls.append(
                    (source_kind, source_value, mode, dry_run)
                )
                return SyncResult(
                    current=None, action="committed", classifications={}
                )

        controller = Controller(
            self.config, engine_factory=lambda ui: Engine()
        )
        self.addCleanup(controller.watch.close)
        self.addCleanup(lambda: controller.pool.shutdown(wait=True))
        self.addCleanup(lambda: controller.requests.shutdown(wait=True))
        return controller

    def server(self, controller):
        server = Server(self.config, controller=controller)
        ready = threading.Event()
        original_listen = server.listen

        def listen():
            original_listen()
            ready.set()

        server.listen = listen
        thread = threading.Thread(
            target=lambda: server.serve(startup_sync=False)
        )
        thread.start()
        self.assertTrue(ready.wait(3))

        def stop():
            server.stop_event.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())

        self.addCleanup(stop)
        return server

    def test_completed_session_cleanup_is_retired_from_runtime_progress(self):
        from kp_sync.sessions.records import SessionRecord, SessionStore
        from kp_sync.state import HistoryState

        controller = self.controller()
        identity = "session:remote:aabbccdd"
        store = SessionStore(controller.directory)
        record = SessionRecord(
            id="aabbccdd",
            host="remote",
            user=None,
            mode="rw",
            owner_pid=os.getpid(),
            owner_start=1,
            ssh_pid=None,
            ssh_start=None,
            confined=True,
            created=time.time(),
            expires=time.time() + 60,
        )
        store.save(record)
        store.staging_path(record.id).mkdir()
        store.key_path(record.id).write_text("private")
        store.key_path(record.id).chmod(0o600)
        store.prepare_retirement(identity, "aabbccdd")

        class RetirementEngine:
            def retire_session(self, requested):
                state = HistoryState()
                state.retire(requested, "test")
                state.save(controller.directory / "state.json")
                return {
                    "shared_stores": {},
                    "publication_error": None,
                }

        controller._engine = lambda _ui: RetirementEngine()
        controller._process_session_retirements()
        self.assertIn(
            identity,
            HistoryState.load(controller.directory / "state.json").retirements,
        )
        self.assertEqual(store.pending_retirements(), [])
        self.assertFalse(store.record_path(record.id).exists())
        self.assertFalse(store.staging_path(record.id).exists())
        self.assertFalse(store.key_path(record.id).exists())

    def test_retirement_commit_failure_retries_without_losing_progress(self):
        from kp_sync.sessions.records import SessionRecord, SessionStore
        from kp_sync.state import HistoryState

        controller = self.controller()
        identity = "session:remote:bbccdd11"
        store = SessionStore(controller.directory)
        record = SessionRecord(
            id="bbccdd11",
            host="remote",
            user=None,
            mode="rw",
            owner_pid=os.getpid(),
            owner_start=1,
            ssh_pid=None,
            ssh_start=None,
            confined=True,
            created=time.time(),
            expires=time.time() + 60,
        )
        store.save(record)
        store.staging_path(record.id).mkdir()
        store.prepare_retirement(identity, record.id)
        attempts = []

        class RetirementEngine:
            def retire_session(self, requested):
                attempts.append(requested)
                if len(attempts) == 1:
                    raise OSError("synthetic retirement commit failure")
                state = HistoryState()
                state.retire(requested, "test")
                state.save(controller.directory / "state.json")
                return {"shared_stores": {}, "publication_error": None}

        controller._engine = lambda _ui: RetirementEngine()

        controller._process_session_retirements()

        self.assertEqual(attempts, [identity])
        self.assertEqual(len(store.pending_retirements()), 1)
        self.assertFalse(store.record_path(record.id).exists())
        self.assertFalse((controller.directory / "state.json").exists())

        controller._process_session_retirements()

        self.assertEqual(attempts, [identity, identity])
        self.assertEqual(store.pending_retirements(), [])
        self.assertIn(
            identity,
            HistoryState.load(controller.directory / "state.json").retirements,
        )

    def test_access_cleanup_progress_retires_without_repeating_sync(self):
        from kp_sync.access_ops import access_identity, access_paths
        from kp_sync.sessions.records import SessionStore
        from kp_sync.state import HistoryState

        controller = self.controller()
        controller.config.access["db_path"] = "/kp/pass.kdbx"
        staging, database, _ = access_paths(
            controller.config, controller.directory
        )
        database.write_bytes(b"synthetic staged database")
        identity = access_identity(controller.directory)
        store = SessionStore(controller.directory)
        store.prepare_retirement(identity, "access")

        class RetirementEngine:
            def retire_session(self, requested):
                state = HistoryState.load(controller.directory / "state.json")
                state.retire(requested, "test")
                state.save(controller.directory / "state.json")
                return {
                    "shared_stores": {},
                    "publication_error": None,
                }

        controller._engine = lambda _ui: RetirementEngine()
        controller._process_session_retirements()

        self.assertFalse(staging.exists())
        self.assertFalse(
            (controller.directory / "access-lifecycle.json").exists()
        )
        self.assertEqual(store.pending_retirements(), [])
        self.assertIn(
            identity,
            HistoryState.load(controller.directory / "state.json").retirements,
        )

    def test_socket_protocol_and_sync_share_engine(self):
        controller = self.controller()
        self.server(controller)
        result = Client(self.config).request(
            "sync", ui="none", dry_run=True, activate=False
        )
        self.assertEqual(result["action"], "unchanged")
        self.assertEqual(self.calls, [True])
        self.assertEqual(
            (controller.directory / "control.sock").stat().st_mode & 0o777,
            0o600,
        )

    def test_restore_request_runs_through_the_daemon_sync_slot(self):
        controller = self.controller()
        result = controller.handle(
            {
                "command": "restore",
                "source_kind": "version",
                "source_value": "a" * 64,
                "restore_mode": "replace",
                "dry_run": True,
                "ui": "none",
            }
        )

        self.assertEqual(result["action"], "committed")
        self.assertEqual(
            self.restore_calls,
            [("version", "a" * 64, "replace", True)],
        )
        self.assertEqual(self.calls, [])

    def test_unexpected_request_error_is_reported_to_client(self):
        def broken():
            raise KeyError("synthetic")

        self.server(self.controller(broken))
        with self.assertRaisesRegex(
            ClientError, "internal daemon error: KeyError"
        ):
            Client(self.config).request("sync", ui="none", activate=False)

    def test_missing_endpoint_notification_repeats_only_after_recovery(self):
        outcome = SyncResult(
            current=None,
            action="unchanged",
            classifications={"hub:test": "unchanged"},
            missing=("cloud:test",),
        )
        controller = self.controller(lambda: outcome)
        self.config.ui["notify"] = True
        with patch("kp_sync.daemon.apps.notify_user") as notify:
            controller.sync({"dry_run": True})
            notify.assert_not_called()
            controller.sync()
            controller.sync()
            notify.assert_called_once_with(
                "KeePass endpoints unavailable: cloud:test"
            )
            outcome = SyncResult(
                current=None,
                action="unchanged",
                classifications={"hub:test": "unchanged"},
            )
            controller.sync()
            outcome = SyncResult(
                current=None,
                action="unchanged",
                classifications={"hub:test": "unchanged"},
                missing=("cloud:test",),
            )
            controller.sync()
            self.assertEqual(notify.call_count, 2)

    def test_engine_notices_reach_the_desktop_logged_once(self):
        message = "Key drive S:U is decommissioned in the shared history"

        class Engine:
            def __init__(self, ui):
                self.ui = ui

            def sync(self, *, dry_run=False):
                self.ui.notify(message)
                return SyncResult(
                    current=None, action="unchanged", classifications={}
                )

        controller = self.controller()
        controller.engine_factory = Engine
        for enabled in (True, False):
            with self.subTest(notify=enabled):
                self.config.ui["notify"] = enabled
                with (
                    patch("kp_sync.daemon.apps.notify_user") as notify,
                    patch("kp_sync.daemon.server.info") as log_info,
                ):
                    controller.sync({"ui": "none"})
                if enabled:
                    notify.assert_called_once_with(message)
                else:
                    notify.assert_not_called()
                self.assertEqual(
                    [c.args for c in log_info.call_args_list].count(
                        (message,)
                    ),
                    1,
                )

    def test_notifications_respect_disabled_setting_and_log_delivery_failure(
        self,
    ):
        from kp_sync.daemon.apps import NotificationError

        controller = self.controller()
        self.config.ui["notify"] = False
        with patch("kp_sync.daemon.apps.notify_user") as notify:
            controller._notify("Pending KeePass decisions")
            notify.assert_not_called()
            self.config.ui["notify"] = True
            notify.side_effect = NotificationError(
                "synthetic notification failure"
            )
            with patch("kp_sync.daemon.server.error") as log_error:
                controller._notify("Pending KeePass decisions")
            log_error.assert_called_once_with(
                "desktop notification failed: synthetic notification failure"
            )

    def test_browser_lookup_releases_both_locks_without_socket_secrets(self):
        import fcntl
        from kp_sync.engine import _UnlockedUI
        from kp_sync.lock import file_lock

        controller = self.controller()
        progress = Mock()
        progress.paused.side_effect = contextlib.nullcontext
        for remote in (False, True):
            with self.subTest(remote=remote):
                connection = Mock()
                backend = (
                    RemoteUI(connection, self.config, controller.sync_gate)
                    if remote
                    else UnlockedUI(FakeUI(), controller.sync_gate, progress)
                )
                with (
                    controller.sync_gate,
                    file_lock(controller.directory / "lock") as fd,
                ):
                    ui = _UnlockedUI(backend, fd, 1)

                    def lookup():
                        self.assertFalse(controller.sync_gate.locked())
                        other = os.open(
                            controller.directory / "lock", os.O_RDWR
                        )
                        try:
                            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        finally:
                            os.close(other)
                        return "never-send-this-secret"

                    self.assertEqual(
                        ui.credential_lookup(lookup), "never-send-this-secret"
                    )
                    self.assertTrue(controller.sync_gate.locked())
                connection.send.assert_not_called()

    def test_no_copy_suppresses_notifications_until_a_copy_is_reachable(self):
        from kp_sync.pending import PendingStore

        outcome = SyncResult(
            current=None,
            action="unavailable",
            classifications={"hub:test": "missing", "cloud:test": "missing"},
            missing=("cloud:test",),
        )
        controller = self.controller(lambda: outcome)
        controller.config.ui["notify"] = True
        pending = PendingStore(controller.directory)
        pending.save("sync", "existing decision", {"copies": {}})
        original_pending = pending.pending_path.read_bytes()
        with (
            patch.dict(os.environ, {"DISPLAY": ":synthetic"}),
            patch("kp_sync.daemon.apps.notify_user") as notify,
            patch.object(controller, "queue_sync"),
        ):
            controller.tick()
            result = controller.sync()
            controller.tick()
            notify.assert_not_called()
            self.assertEqual(result["missing"], ("cloud:test",))
            self.assertEqual(
                pending.pending_path.read_bytes(), original_pending
            )

            outcome = SyncResult(
                current=None,
                action="pending",
                classifications={"hub:test": "new", "cloud:test": "missing"},
                missing=("cloud:test",),
                pending="existing decision",
            )
            controller.sync()
            controller.tick()
            self.assertEqual(
                [call.args[0] for call in notify.call_args_list],
                [
                    "KeePass endpoints unavailable: cloud:test",
                    "Pending KeePass decisions; use kp-sync resolve",
                ],
            )

            outcome = SyncResult(
                current=None,
                action="unavailable",
                classifications={
                    "hub:test": "missing",
                    "cloud:test": "missing",
                },
                missing=("cloud:test",),
            )
            controller.sync()
            controller.last_notification = -float("inf")
            controller.tick()
            self.assertEqual(notify.call_count, 2)

    def test_tick_contains_drive_and_inhibitor_failures(self):
        controller = self.controller()
        controller.config.daemon["poll_interval"] = 0
        controller.leases.take("drive:SERIAL:filesystem", "drive")
        controller.inhibitor = Mock()
        controller.inhibitor.update.side_effect = OSError(
            "synthetic inhibitor failure"
        )
        controller.next_drive_scan = 0
        controller.tick_retry_at["drives"] = 0
        with patch(
            "kp_sync.daemon.server.list_drives",
            side_effect=OSError("synthetic lsblk failure"),
        ):
            controller.tick()
        self.assertIn("drive:SERIAL:filesystem", controller.leases.leases)
        self.assertEqual(controller.tick_failures["drives"], 1)
        self.assertEqual(controller.tick_failures["inhibitor"], 1)

    def test_tick_step_circuit_breaker_stops_after_five_failures(self):
        controller = self.controller()

        def fail():
            raise OSError("synthetic repeated failure")

        with contextlib.redirect_stderr(io.StringIO()):
            for attempt in range(1, 5):
                succeeded, _ = controller._tick_step("synthetic", fail)
                self.assertFalse(succeeded)
                self.assertEqual(
                    controller.tick_failures["synthetic"], attempt
                )
                controller.tick_retry_at["synthetic"] = 0
            with self.assertRaisesRegex(RuntimeError, "5 consecutive times"):
                controller._tick_step("synthetic", fail)

    def test_corrupt_pending_state_blocks_sync_without_stopping_daemon(self):
        controller = self.controller()
        pending_path = controller.directory / "pending.json"
        pending_path.write_text("{invalid", encoding="utf-8")
        pending_path.chmod(0o600)
        controller.tick()
        self.assertIsNotNone(controller.pending_error)
        self.assertIn("pending", controller.leases.leases)
        with self.assertRaisesRegex(RuntimeError, "sync is disabled"):
            controller.sync()
        self.assertEqual(self.calls, [])

    def test_protocol_rejects_wrong_version_unknown_fields_and_password(self):
        for request in (
            {"command": "sync", "password": "synthetic"},
            {"command": "shell"},
            {"command": "sync", "environment": {"PATH": "/tmp"}},
            {"command": "sync", "wait": 1},
            {"command": "sync", "ui": []},
            {"command": [], "dry_run": True},
        ):
            with (
                self.subTest(request=request),
                self.assertRaises(ProtocolError),
            ):
                validate_request(request)
        a, b = socket.socketpair()
        connection = Connection(a)
        self.addCleanup(connection.close)
        self.addCleanup(b.close)
        b.sendall(b'{"version":2,"command":"sync"}\n')
        with self.assertRaisesRegex(ProtocolError, "incompatible"):
            connection.receive()

    def test_protocol_rejects_oversized_unterminated_and_nonfinite_json(self):
        for payload in (
            b"x" * (MAX_MESSAGE + 1),
            b'{"version":1,"command":"sync"}',
            b'{"version":1,"command":"sync","wait":NaN}\n',
            b'{"version":true,"command":"sync"}\n',
        ):
            with self.subTest(length=len(payload)):
                sock = Mock()
                sock.makefile.return_value = io.BytesIO(payload)
                connection = Connection(sock)
                with self.assertRaises(ProtocolError):
                    connection.receive()
                connection.close()
        sock = Mock()
        sock.makefile.return_value = io.BytesIO()
        connection = Connection(sock)
        with self.assertRaisesRegex(ProtocolError, "too large"):
            connection.send({"command": "sync", "padding": "x" * MAX_MESSAGE})
        sock.sendall.assert_not_called()
        connection.close()

    def test_foreign_peer_is_rejected_before_request(self):
        controller = self.controller()
        with patch(
            "kp_sync.daemon.server.peer_uid", return_value=os.getuid() + 1
        ):
            self.server(controller)
            client = socket.socket(socket.AF_UNIX)
            self.addCleanup(client.close)
            client.connect(str(controller.directory / "control.sock"))
            self.assertEqual(client.recv(1), b"")
        self.assertEqual(self.calls, [])

    def test_no_wait_returns_before_slow_sync(self):
        started, finish = threading.Event(), threading.Event()

        def slow():
            started.set()
            self.assertTrue(finish.wait(5))
            return SyncResult(
                current=None, action="unchanged", classifications={}
            )

        controller = self.controller(slow)
        self.server(controller)
        try:
            result = Client(self.config).request(
                "sync", wait=False, ui="none", activate=False
            )
            self.assertEqual(result["action"], "accepted")
            self.assertTrue(started.wait(2))
        finally:
            finish.set()

    def test_named_leases_restart_ttl_and_connection_disconnect(self):
        clock = [10.0]
        directory = self.root / "leases"
        directory.mkdir(mode=0o700)
        store = LeaseStore(directory, clock=lambda: clock[0], access_ttl=20)
        store.take("drive:serial:uuid", "drive")
        store.take("access:test", "access")
        store.take("session:test", "session", owner="client-1")
        self.assertTrue(store.disconnect("client-1"))
        store.take("session:test", "session", owner="client-2")
        restarted = LeaseStore(
            directory, clock=lambda: clock[0], access_ttl=20
        )
        self.assertEqual(
            set(restarted.leases), {"drive:serial:uuid", "access:test"}
        )
        clock[0] = 25.0
        restarted.take("access:test", "access")
        clock[0] = 31.0
        self.assertFalse(restarted.expire())
        clock[0] = 46.0
        self.assertTrue(restarted.expire())
        self.assertEqual(set(restarted.leases), {"drive:serial:uuid"})
        self.assertEqual(
            (directory / "leases.json").stat().st_mode & 0o777, 0o600
        )

    def test_session_connection_lease_disappears_without_duplicate_postsync(
        self,
    ):
        controller = self.controller()
        self.server(controller)
        connection = Client(self.config).connect(activate=False)
        connection.send(
            {"command": "lease", "name": "session:test", "kind": "session"}
        )
        self.assertEqual(connection.receive()["type"], "result")
        self.assertEqual(self.calls, [])
        self.assertIn("session:test", controller.leases.leases)
        connection.close()
        deadline = time.monotonic() + 3
        while (
            "session:test" in controller.leases.leases
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        self.assertNotIn("session:test", controller.leases.leases)
        self.assertEqual(self.calls, [])

    def test_debounce_poll_coalescing_and_idle_timer(self):
        clock = [0]
        triggers = Triggers(
            debounce=2, poll_interval=10, idle_exit=5, clock=lambda: clock[0]
        )
        triggers.trigger()
        clock[0] = 1
        triggers.trigger()
        clock[0] = 2
        self.assertFalse(triggers.ready())
        clock[0] = 3
        self.assertTrue(triggers.ready())
        triggers.start()
        triggers.trigger()
        triggers.trigger()
        triggers.finish()
        self.assertTrue(triggers.ready())
        triggers.start()
        triggers.finish()
        triggers.leases_changed(True)
        clock[0] = 10
        self.assertTrue(triggers.ready())
        self.assertTrue(triggers.start())
        triggers.finish()
        triggers.leases_changed(False)
        clock[0] = 14
        self.assertFalse(triggers.should_exit())
        clock[0] = 15
        self.assertTrue(triggers.should_exit())

    def test_poll_trigger_is_silent_but_explicit_trigger_is_not(self):
        clock = [0]
        triggers = Triggers(
            debounce=2, poll_interval=10, idle_exit=5, clock=lambda: clock[0]
        )
        triggers.leases_changed(True)
        clock[0] = 10
        self.assertTrue(triggers.ready())
        self.assertTrue(triggers.start())
        triggers.finish()

        triggers.trigger()
        clock[0] = 12
        self.assertTrue(triggers.ready())
        self.assertFalse(triggers.start())

    def test_visible_trigger_overrides_coalesced_poll(self):
        clock = [0]
        triggers = Triggers(
            debounce=2, poll_interval=10, idle_exit=5, clock=lambda: clock[0]
        )
        triggers.leases_changed(True)
        clock[0] = 10
        self.assertTrue(triggers.ready())
        triggers.trigger()
        clock[0] = 12
        self.assertTrue(triggers.ready())
        self.assertFalse(triggers.start())

    def test_inotify_catches_rename_save_and_open(self):
        hub = self.root / "db.kdbx"
        hub.write_bytes(b"synthetic original")
        watch = HubWatch(hub)
        self.addCleanup(watch.close)
        watch.read()
        staged = self.root / "db.new"
        staged.write_bytes(b"synthetic replacement")
        staged.replace(hub)
        with hub.open("rb") as source:
            source.read()
        saved, opened = watch.read()
        self.assertTrue(saved)
        self.assertTrue(opened)

    def test_terminal_secret_values_are_masked_and_slot_is_released(self):
        a, b = socket.socketpair()
        server_connection, client_connection = Connection(a), Connection(b)
        self.addCleanup(server_connection.close)
        self.addCleanup(client_connection.close)
        gate = threading.Lock()
        gate.acquire()
        ui = RemoteUI(server_connection, self.config, gate)
        results = []

        def ask():
            results.append(
                ui.review(
                    [
                        {
                            "id": "test",
                            "label": "synthetic",
                            "secret": True,
                            "base": "synthetic-old",
                            "hub": "synthetic-hub",
                            "other": "synthetic-other",
                        }
                    ]
                )
            )

        thread = threading.Thread(target=ask)
        thread.start()
        question = client_connection.receive()
        self.assertEqual(question["args"][0][0]["hub"], "[hidden]")
        self.assertTrue(gate.acquire(timeout=1))
        gate.release()
        client_connection.send(
            {
                "type": "ui-reply",
                "id": question["id"],
                "answer": {"test": "hub"},
            }
        )
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [{"test": "hub"}])
        self.assertTrue(gate.locked())
        gate.release()

    def test_run_never_launches_missing_hub_after_presync(self):
        controller = self.controller()

        class Apps:
            def activate_existing(self, *args, **kwargs):
                return None

            def start(self, *args, **kwargs):
                raise AssertionError("must not launch on missing hub")

        controller.apps = Apps()
        with self.assertRaisesRegex(RuntimeError, "no database copy"):
            controller.handle({"command": "run"})

    def test_run_activates_existing_instance_without_hub_or_presync(self):
        from kp_sync.daemon.apps import AppStart

        for args in ([], ["explicit.kdbx"]):
            with self.subTest(args=args):
                controller = self.controller()
                controller.apps = Mock()
                controller.apps.activate_existing.return_value = AppStart(
                    "keepassxc",
                    "kp-sync-app-keepassxc.service",
                    False,
                )
                controller.apps.refresh.return_value = ["app:keepassxc"]
                with patch.object(controller, "queue_sync") as queued:
                    outcome = controller.handle(
                        {
                            "command": "run",
                            "app": "keepassxc",
                            "args": args,
                            "interactive": True,
                            "environment": {"DISPLAY": ":synthetic"},
                        }
                    )
                self.assertEqual(
                    outcome["action"], "activated existing application"
                )
                queued.assert_not_called()
                controller.apps.start.assert_not_called()
                controller.apps.activate_existing.assert_called_once_with(
                    "keepassxc",
                    args,
                    interactive=True,
                    environment={"DISPLAY": ":synthetic"},
                )
                self.assertIn("app:keepassxc", controller.leases.leases)

    def _cloud_config(self, **launch):
        from dataclasses import replace

        from kp_sync.config import EndpointConfig

        return replace(
            self.config,
            endpoints=(EndpointConfig("cloud", "rclone", remote="gd"),),
            launch={**self.config.launch, **launch},
        )

    def _offline_controller(self, config):
        controller = self.controller(
            lambda: SyncResult(
                current=None,
                action="unchanged",
                classifications={"cloud:gd": "unchanged"},
                missing=("cloud:gd",),
            )
        )
        controller.config = config
        config.hub.parent.mkdir(parents=True, exist_ok=True)
        config.hub.write_bytes(b"synthetic hub")
        controller.apps = Mock()
        controller.apps.refresh.return_value = []
        return controller

    def test_launch_without_cloud_warns_with_a_dialog_beside_the_app(self):
        from kp_sync import cloud_contact

        controller = self._offline_controller(self._cloud_config())
        cloud_contact.record(
            controller.directory,
            ["cloud:gd"],
            now=datetime.now(timezone.utc) - timedelta(days=2),
        )
        shown = threading.Event()
        dialogs = []

        class UI:
            def warn(self, message):
                dialogs.append(message)
                shown.set()

            def confirm(self, prompt, *, default=False):
                raise AssertionError("no question without the setting")

        with patch.object(controller, "_desktop_ui", return_value=UI()):
            outcome = controller.handle({"command": "up"})
            self.assertTrue(shown.wait(5))
        controller.apps.start.assert_called_once()
        self.assertEqual(outcome["action"], "unchanged")
        self.assertIn("cloud:gd: last reached 2d ago", dialogs[0])

    def test_launch_without_cloud_asks_with_confirm_without_cloud(self):
        controller = self._offline_controller(
            self._cloud_config(confirm_without_cloud=True)
        )
        prompts = []

        class UI:
            def confirm(self, prompt, *, default=False):
                prompts.append(prompt)
                return False

        with patch.object(controller, "_desktop_ui", return_value=UI()):
            outcome = controller.handle({"command": "up"})
        self.assertEqual(outcome["action"], "launch cancelled")
        controller.apps.start.assert_not_called()
        self.assertIn("not reached since this machine started", prompts[0])
        self.assertIn("Launch the existing database?", prompts[0])

    def _slow_controller(self, missing):
        from dataclasses import replace

        config = self._cloud_config()
        config = replace(
            config, daemon={**config.daemon, "prestart_timeout": 0.2}
        )
        controller = self._offline_controller(config)
        release = threading.Event()
        self.addCleanup(release.set)

        class Engine:
            def sync(self, dry_run=False):
                release.wait(10)
                return SyncResult(
                    current=None,
                    action="unchanged",
                    classifications={"cloud:gd": "unchanged"},
                    missing=missing,
                )

        controller.engine_factory = lambda ui: Engine()
        return controller, release

    def test_pre_sync_outliving_the_launch_warns_late_without_cloud(self):
        controller, release = self._slow_controller(("cloud:gd",))
        shown = threading.Event()
        dialogs = []

        class UI:
            def warn(self, message):
                dialogs.append(message)
                shown.set()

        with patch.object(controller, "_desktop_ui", return_value=UI()):
            outcome = controller.handle({"command": "up"})
            self.assertEqual(outcome["action"], "pre-sync still running")
            controller.apps.start.assert_called_once()
            self.assertEqual(dialogs, [])
            release.set()
            self.assertTrue(shown.wait(5))
        self.assertIn("The cloud could not be reached", dialogs[0])
        self.assertIn("KeePassXC is open on this machine's copy", dialogs[0])

    def test_pre_sync_outliving_the_launch_and_reaching_cloud_is_silent(self):
        controller, release = self._slow_controller(())
        with patch.object(
            controller, "_desktop_ui", side_effect=AssertionError("dialog")
        ):
            future_done = threading.Event()
            original = controller._late_cloud_warning

            def watched(future):
                original(future)
                future_done.set()

            with patch.object(controller, "_late_cloud_warning", watched):
                controller.handle({"command": "up"})
                release.set()
                self.assertTrue(future_done.wait(5))
        controller.apps.start.assert_called_once()

    def test_launch_with_cloud_reached_shows_no_dialog(self):
        controller = self._offline_controller(self._cloud_config())
        controller.engine_factory = lambda ui: type(
            "Engine",
            (),
            {
                "sync": lambda self, dry_run=False: SyncResult(
                    current=None,
                    action="unchanged",
                    classifications={"cloud:gd": "unchanged"},
                )
            },
        )()
        with patch.object(
            controller, "_desktop_ui", side_effect=AssertionError("dialog")
        ):
            controller.handle({"command": "up"})
        controller.apps.start.assert_called_once()

    def test_stale_cloud_notifies_once_per_period_when_hub_changed(self):
        import hashlib

        from kp_sync import cloud_contact
        from kp_sync.state import HistoryState

        config = self._cloud_config()
        controller = self._offline_controller(config)
        synced = b"synced hub"
        version = hashlib.sha256(synced).hexdigest()
        state = HistoryState()
        state.commit(
            version, (), "test", (f"hub:{config.host}", "cloud:gd"), size=10
        )
        state.save(controller.directory / "state.json")
        offline = SyncResult(
            current=version,
            action="unchanged",
            classifications={"cloud:gd": "unchanged"},
            missing=("cloud:gd",),
        )
        now = datetime.now(timezone.utc)
        notices = []
        with patch.object(controller, "_notify", side_effect=notices.append):
            config.hub.write_bytes(synced)
            cloud_contact.record(
                controller.directory, ["cloud:gd"], now=now - timedelta(days=2)
            )
            controller._warn_stale_cloud(offline)
            self.assertEqual(notices, [], "hub unchanged: nothing waits")
            config.hub.write_bytes(b"edited offline")
            cloud_contact.record(
                controller.directory,
                ["cloud:gd"],
                now=now - timedelta(hours=1),
            )
            controller._warn_stale_cloud(offline)
            self.assertEqual(notices, [], "cloud reached an hour ago")
            cloud_contact.record(
                controller.directory, ["cloud:gd"], now=now - timedelta(days=2)
            )
            controller._warn_stale_cloud(offline)
            self.assertEqual(len(notices), 1)
            self.assertIn("have not reached the cloud", notices[0])
            controller._warn_stale_cloud(offline)
            self.assertEqual(len(notices), 1, "once per period")
            controller.config = replace(
                config, daemon={**config.daemon, "cloud_warning_hours": 0}
            )
            (controller.directory / cloud_contact.WARNED).unlink()
            controller._warn_stale_cloud(offline)
            self.assertEqual(len(notices), 1, "0 disables")

    def test_up_still_presyncs_before_reusing_running_application(self):
        controller = self.controller()
        self.config.hub.write_bytes(b"synthetic hub")
        controller.apps = Mock()
        controller.apps.refresh.return_value = ["app:keepassxc"]
        outcome = controller.handle({"command": "up"})
        self.assertEqual(outcome["action"], "unchanged")
        self.assertEqual(self.calls, [False])
        controller.apps.activate_existing.assert_not_called()
        controller.apps.start.assert_called_once()

    def test_main_mode_events_warn_on_a_persistent_hub(self):
        from dataclasses import replace

        controller = self.controller()
        self.config.hub.write_bytes(b"synthetic hub")
        controller.apps = Mock()
        controller.apps.refresh.return_value = []
        for persistent, expected in ((False, 0), (True, 1)):
            with self.subTest(persistent=persistent):
                controller.config = replace(
                    self.config, hub_persistent=persistent
                )
                with patch("kp_sync.daemon.server.info") as log:
                    controller.handle({"command": "up"})
                warnings = [
                    call.args[0]
                    for call in log.call_args_list
                    if "--mode secondary" in call.args[0]
                ]
                self.assertEqual(len(warnings), expected)

    def test_queued_triggers_coalesce_and_never_overlap_syncs(self):
        first_started, finish = threading.Event(), threading.Event()
        active = [0]

        def slow():
            active[0] += 1
            self.assertEqual(active[0], 1)
            if len(self.calls) == 1:
                first_started.set()
                self.assertTrue(finish.wait(3))
            active[0] -= 1
            return SyncResult(
                current=None, action="unchanged", classifications={}
            )

        controller = self.controller(slow)
        first = controller.queue_sync()
        self.assertTrue(first_started.wait(2))
        second = controller.queue_sync()
        self.assertIs(controller.queue_sync(), second)
        self.assertIsNot(first, second)
        finish.set()
        first.result(timeout=3)
        second.result(timeout=3)
        self.assertEqual(len(self.calls), 2)

    def test_coalesced_desktop_request_upgrades_background_ui(self):
        first_started, finish = threading.Event(), threading.Event()

        def slow():
            if len(self.calls) == 1:
                first_started.set()
                self.assertTrue(finish.wait(3))
            return SyncResult(
                current=None, action="unchanged", classifications={}
            )

        controller = self.controller(slow)
        modes = []
        original = controller._ui
        controller._ui = lambda request, *args: (
            modes.append(request.get("ui")),
            original(request, *args),
        )[1]
        first = controller.queue_sync()
        self.assertTrue(first_started.wait(2))
        second = controller.queue_sync({"ui": "none"})
        self.assertIs(controller.queue_sync({"ui": "gui"}), second)
        finish.set()
        first.result(timeout=3)
        second.result(timeout=3)
        self.assertEqual(modes, [None, "gui"])

    def test_restore_is_not_coalesced_with_a_pending_sync(self):
        first_started, finish = threading.Event(), threading.Event()

        def slow():
            if len(self.calls) == 1:
                first_started.set()
                self.assertTrue(finish.wait(3))
            return SyncResult(
                current=None, action="unchanged", classifications={}
            )

        controller = self.controller(slow)
        first = controller.queue_sync()
        self.assertTrue(first_started.wait(2))
        queued = controller.queue_sync()
        restore = controller.queue_sync(
            {
                "command": "restore",
                "source_kind": "version",
                "source_value": "a" * 64,
                "restore_mode": "merge",
            }
        )

        self.assertIsNot(restore, queued)
        finish.set()
        first.result(timeout=3)
        queued.result(timeout=3)
        restore.result(timeout=3)
        self.assertEqual(
            self.restore_calls,
            [("version", "a" * 64, "merge", False)],
        )

    def test_attach_checks_physical_identity_before_registering(self):
        controller = self.controller()
        controller.drive_resolver = lambda _: "real-serial:real-uuid"
        mount = self.root / "mount"
        mount.mkdir()
        with self.assertRaisesRegex(ValueError, "does not match"):
            controller.handle(
                {
                    "command": "attach",
                    "mountpoint": str(mount),
                    "drive_id": "wrong:uuid",
                }
            )
        self.assertFalse((controller.directory / "drives").exists())
        controller.handle(
            {
                "command": "attach",
                "mountpoint": str(mount),
                "drive_id": "real-serial:real-uuid",
            }
        )
        self.assertIn("drive:real-serial:real-uuid", controller.leases.leases)
        controller.handle(
            {"command": "detach", "drive_id": "real-serial:real-uuid"}
        )
        self.assertFalse(controller.leases.leases)
        self.assertGreaterEqual(len(self.calls), 2)

    def test_client_password_is_cached_locally_and_never_sent(self):
        from kp_sync.ui.fake import FakeUI

        with (
            patch(
                "kp_sync.daemon.client.create_ui",
                return_value=FakeUI(passwords=("synthetic-only",)),
            ),
            patch("kp_sync.daemon.client.CredentialProvider") as provider,
        ):
            answer = Client(self.config)._answer(
                {
                    "method": "password",
                    "args": ["Password for synthetic test"],
                }
            )
        self.assertEqual(answer, {"cached": True})
        provider.return_value.cache_password.assert_called_once_with(
            "synthetic-only"
        )
        self.assertNotIn("synthetic-only", json.dumps(answer))

    def test_client_caches_a_copy_password_in_its_own_slot(self):
        from kp_sync.ui.fake import FakeUI

        with (
            patch(
                "kp_sync.daemon.client.create_ui",
                return_value=FakeUI(passwords=("synthetic-copy",)),
            ),
            patch("kp_sync.daemon.client.CredentialProvider") as provider,
        ):
            answer = Client(self.config)._answer(
                {
                    "method": "password",
                    "args": ["Password for copy", "0123456789abcdef"],
                }
            )
            with self.assertRaises(ProtocolError):
                Client(self.config)._answer(
                    {
                        "method": "password",
                        "args": ["Password for copy", "../main"],
                    }
                )
        self.assertEqual(answer, {"cached": True})
        provider.return_value.cache_copy_password.assert_called_once_with(
            "0123456789abcdef",
            "synthetic-copy",
        )
        provider.return_value.cache_password.assert_not_called()

    def test_remote_copy_password_question_reads_its_slot(self):
        a, b = socket.socketpair()
        server_connection, client_connection = Connection(a), Connection(b)
        self.addCleanup(server_connection.close)
        self.addCleanup(client_connection.close)
        gate = threading.Lock()
        gate.acquire()
        ui = RemoteUI(server_connection, self.config, gate)
        results = []
        with patch("kp_sync.daemon.server.CredentialProvider") as provider:
            provider.return_value.cached_copy_password.return_value = (
                "synthetic-copy"
            )
            provider.return_value.cached_password.return_value = (
                "synthetic-main"
            )
            thread = threading.Thread(
                target=lambda: results.append(
                    ui.password("Copy", slot="0123456789abcdef")
                ),
            )
            thread.start()
            question = client_connection.receive()
            self.assertEqual(question["args"], ["Copy", "0123456789abcdef"])
            client_connection.send(
                {
                    "type": "ui-reply",
                    "id": question["id"],
                    "answer": {"cached": True},
                }
            )
            thread.join(2)
        gate.release()
        self.assertEqual(results, ["synthetic-copy"])
        provider.return_value.cached_copy_password.assert_called_once_with(
            "0123456789abcdef"
        )

    def test_protocol_rejects_client_sending_password_in_ui_reply(self):
        a, b = socket.socketpair()
        server_connection, client_connection = Connection(a), Connection(b)
        self.addCleanup(server_connection.close)
        self.addCleanup(client_connection.close)
        gate = threading.Lock()
        gate.acquire()
        ui = RemoteUI(server_connection, self.config, gate)
        errors = []

        def ask():
            try:
                ui.password("Synthetic password")
            except ProtocolError as exc:
                errors.append(str(exc))

        thread = threading.Thread(target=ask)
        thread.start()
        question = client_connection.receive()
        client_connection.send(
            {
                "type": "ui-reply",
                "id": question["id"],
                "answer": "synthetic-forbidden-value",
            }
        )
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIn("keyring", errors[0])
        gate.release()

    def test_client_reconnects_idempotent_request_after_daemon_restart(self):
        client = Client(self.config, sleep=lambda _: None)
        with patch.object(
            client,
            "_request_once",
            side_effect=[
                EOFError("restart"),
                {"action": "unchanged"},
            ],
        ) as request:
            self.assertEqual(client.request("sync"), {"action": "unchanged"})
        self.assertEqual(request.call_count, 2)

    def test_connection_lease_is_reestablished_after_restart(self):
        previous, restarted = Mock(), Mock()
        previous.receive.side_effect = EOFError("restart")
        restarted.receive.return_value = {
            "type": "result",
            "result": {"action": "unchanged"},
        }
        client = Mock()
        client.connect.side_effect = [previous, restarted]
        lease = LeaseConnection(client, "session:test")
        self.addCleanup(lease.close)
        self.assertEqual(lease.renew(), {"action": "unchanged"})
        self.assertEqual(client.connect.call_count, 2)
        previous.close.assert_called_once()
        restarted.send.assert_called_once_with(
            {
                "command": "lease",
                "name": "session:test",
                "kind": "session",
                "wait": True,
                "ui": "none",
            }
        )

    def test_down_displacement_preserves_save_between_check_and_removal(self):
        import hashlib

        controller = self.controller()
        hub = self.config.hub
        hub.write_bytes(b"known synchronized bytes")
        known = hashlib.sha256(hub.read_bytes()).hexdigest()
        hub.write_bytes(b"new unsynchronized save")
        self.assertFalse(controller._remove_hub(known))
        self.assertEqual(hub.read_bytes(), b"new unsynchronized save")

    def test_down_preserves_both_versions_when_writer_recreates_hub(self):
        import hashlib
        from kp_sync.endpoints.file import _rename_noreplace

        controller = self.controller()
        hub = self.config.hub
        hub.write_bytes(b"known synchronized bytes")
        known = hashlib.sha256(hub.read_bytes()).hexdigest()
        hub.write_bytes(b"first unsynchronized save")
        real_rename = _rename_noreplace

        def concurrent_writer(source, target):
            real_rename(source, target)
            if source == hub:
                hub.write_bytes(b"second unsynchronized save")

        with patch(
            "kp_sync.daemon.server._rename_noreplace",
            side_effect=concurrent_writer,
        ):
            self.assertFalse(controller._remove_hub(known))
        self.assertEqual(hub.read_bytes(), b"second unsynchronized save")
        backups = list(
            (controller.directory / "backups").glob("hub-down-*.kdbx")
        )
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"first unsynchronized save")
        self.assertTrue((controller.directory / "pending.json").exists())

    def test_launcher_presync_timeout_uses_existing_hub_without_waiting(self):
        from dataclasses import replace

        controller = self.controller()
        controller.config = replace(
            self.config,
            daemon={**self.config.daemon, "prestart_timeout": 0.01},
        )
        self.config.hub.write_bytes(b"existing synthetic hub")
        controller.apps = Mock()
        controller.apps.activate_existing.return_value = None
        controller.apps.refresh.return_value = ["app:test"]
        blocked = __import__(
            "concurrent.futures", fromlist=["Future"]
        ).Future()
        with patch.object(controller, "queue_sync", return_value=blocked):
            result = controller.handle(
                {
                    "command": "run",
                    "app": "keepassxc",
                    "environment": {"DISPLAY": ":synthetic"},
                }
            )
        self.assertEqual(result["action"], "pre-sync still running")
        controller.apps.start.assert_called_once_with(
            "keepassxc",
            [],
            interactive=False,
            environment={"DISPLAY": ":synthetic"},
        )
        self.assertIn("app:test", controller.leases.leases)

    def test_launcher_missing_hub_waits_for_timed_out_presync_then_launches(
        self,
    ):
        import concurrent.futures

        controller = self.controller()
        controller.apps = Mock()
        controller.apps.activate_existing.return_value = None
        controller.apps.refresh.return_value = ["app:keepassxc"]
        future = Mock()

        def result(**kwargs):
            if "timeout" in kwargs:
                raise concurrent.futures.TimeoutError
            controller.apps.start.assert_not_called()
            self.config.hub.write_bytes(b"synchronized synthetic hub")
            return {"action": "committed"}

        future.result.side_effect = result
        with patch.object(
            controller, "queue_sync", return_value=future
        ) as queued:
            outcome = controller.handle({"command": "run", "ui": "none"})
        self.assertEqual(outcome["action"], "committed")
        self.assertEqual(
            future.result.call_args_list,
            [
                unittest.mock.call(
                    timeout=self.config.daemon["prestart_timeout"]
                ),
                unittest.mock.call(),
            ],
        )
        queued.assert_called_once()
        controller.apps.start.assert_called_once()
        self.assertIn("app:keepassxc", controller.leases.leases)

    def test_launcher_timed_out_presync_without_hub_fails_safely(self):
        import concurrent.futures

        for completion in (
            {"action": "pending"},
            RuntimeError("synthetic sync failed"),
        ):
            with self.subTest(completion=completion):
                controller = self.controller()
                controller.apps = Mock()
                controller.apps.activate_existing.return_value = None
                future = Mock()
                future.result.side_effect = [
                    concurrent.futures.TimeoutError(),
                    completion,
                ]
                with patch.object(
                    controller, "queue_sync", return_value=future
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, "no database copy|synthetic sync failed"
                    ):
                        controller.handle({"command": "run", "ui": "none"})
                controller.apps.start.assert_not_called()

    def test_down_persistent_hub_is_never_removed(self):
        from dataclasses import replace

        controller = self.controller()
        controller.config = replace(self.config, hub_persistent=True)
        self.config.hub.write_bytes(b"persistent synthetic database")
        result = controller._down({"ui": "none"}, None)
        self.assertEqual(result["action"], "unchanged")
        self.assertEqual(
            self.config.hub.read_bytes(), b"persistent synthetic database"
        )

    def test_down_pending_decisions_prevent_runtime_cleanup(self):
        from kp_sync.pending import PendingStore

        controller = self.controller()
        self.config.hub.write_bytes(b"synthetic database still needed")
        PendingStore(controller.directory).save(
            "merge", "unresolved synthetic conflict"
        )
        with patch(
            "kp_sync.daemon.rescue.hub_is_safe_to_remove", return_value=True
        ):
            controller._down({"ui": "none"}, None)
        self.assertTrue(self.config.hub.exists())
        self.assertTrue((controller.directory / "pending.json").exists())

    def test_down_keeps_hub_while_another_sync_waits_at_a_question(self):
        import concurrent.futures

        controller = self.controller()
        self.config.hub.write_bytes(b"synthetic database under review")
        done = concurrent.futures.Future()
        done.set_result({"action": "unchanged"})
        controller.inflight = 1
        with (
            patch.object(controller, "queue_sync", return_value=done),
            patch(
                "kp_sync.daemon.rescue.hub_is_safe_to_remove",
                return_value=True,
            ),
        ):
            controller._down({"ui": "none"}, None)
        self.assertTrue(self.config.hub.exists())

    def test_reinsert_during_down_preserves_prepared_credentials_and_hub(self):
        from kp_sync.drives import pending_attach_path
        from kp_sync.paths import atomic_write

        marker = pending_attach_path(None, "SERIAL:1234-abcd")

        def reinsert():
            atomic_write(marker, "attachment pending\n")
            return SyncResult(
                current=None, action="unchanged", classifications={}
            )

        controller = self.controller(reinsert)
        self.config.hub.write_bytes(b"saved database")
        with patch(
            "kp_sync.daemon.rescue.hub_is_safe_to_remove", return_value=True
        ):
            controller._down({"ui": "none"}, None)
        self.assertEqual(self.config.hub.read_bytes(), b"saved database")
        self.assertTrue(marker.exists())

    def test_sync_progress_respects_ui_mode_display_and_config(self):
        controller = self.controller()
        with (
            patch.dict(os.environ, {"DISPLAY": ":fake"}),
            patch(
                "kp_sync.daemon.server.SyncProgress",
            ) as progress,
        ):
            controller.sync({"ui": "auto"})
            self.assertTrue(progress.call_args.kwargs["enabled"])
            progress.return_value.__exit__.assert_called_once()
            for mode in ("none", "tty"):
                controller.sync({"ui": mode})
                self.assertFalse(progress.call_args.kwargs["enabled"])
            controller.config.ui["progress"] = False
            controller.sync({"ui": "gui"})
            self.assertFalse(progress.call_args.kwargs["enabled"])
            controller.config.ui["progress"] = True
            with patch.dict(os.environ, {}, clear=True):
                controller.sync({"ui": "auto"})
                self.assertFalse(progress.call_args.kwargs["enabled"])

    def test_progress_is_hidden_while_question_releases_sync_slot(self):
        gate = threading.Lock()
        gate.acquire()
        backend = Mock()
        progress = Mock()
        progress.paused.return_value = contextlib.nullcontext()

        def answer(_, *, default=False):
            self.assertFalse(gate.locked())
            progress.paused.assert_called_once()
            return True

        backend.confirm.side_effect = answer
        ui = UnlockedUI(backend, gate, progress)
        self.assertTrue(ui.confirm("Continue?"))
        self.assertTrue(gate.locked())
        gate.release()

    def test_attach_clears_only_its_own_pending_marker_after_registration(
        self,
    ):
        from kp_sync.drives import pending_attach_path
        from kp_sync.paths import atomic_write

        controller = self.controller()
        controller.drive_resolver = lambda _: "SERIAL:1234-abcd"
        marker = pending_attach_path(None, "SERIAL:1234-abcd")
        other = pending_attach_path("work", "OTHER:5678-efgh")
        atomic_write(marker, "pending\n")
        atomic_write(other, "pending\n")
        controller.handle(
            {
                "command": "attach",
                "mountpoint": str(self.root),
                "drive_id": "SERIAL:1234-abcd",
                "ui": "none",
            }
        )
        self.assertFalse(marker.exists())
        self.assertTrue(other.exists())
        self.assertIn("drive:SERIAL:1234-abcd", controller.leases.leases)

    def test_socket_sync_drives_real_engine_and_own_write_does_not_retrigger(
        self,
    ):
        from kp_sync.engine import SyncEngine, endpoints_from_config
        from tests.fixtures import kdbx4_bytes

        copy = self.root / "endpoint.kdbx"
        config_path = self.root / "profile.toml"
        config_path.write_text(
            f"hub = {json.dumps(str(self.config.hub))}\n"
            f'key_file = {json.dumps(str(self.root / "missing.key"))}\n'
            '[[endpoints]]\nname = "file"\ntype = "file"\n'
            f"path = {json.dumps(str(copy))}\n",
        )
        self.config = load_config(config_path, hostname="test")
        original = kdbx4_bytes(b"synthetic original database" * 20)
        updated = kdbx4_bytes(b"synthetic updated database" * 20)
        self.config.hub.write_bytes(original)
        copy.write_bytes(original)

        def actual_engine(ui):
            from kp_sync.paths import ensure_run_dir

            directory = ensure_run_dir()
            return SyncEngine(
                self.config,
                endpoints_from_config(self.config, directory),
                run_directory=directory,
                ui=ui,
                credentials=Mock(
                    cached_password=Mock(return_value=None),
                ),
            )

        self.assertEqual(actual_engine(NoneUI()).sync().action, "committed")
        controller = Controller(self.config, engine_factory=actual_engine)
        self.server(controller)
        self.config.hub.write_bytes(updated)
        result = Client(self.config).request("sync", ui="none", activate=False)
        self.assertEqual(result["action"], "committed")
        self.assertEqual(copy.read_bytes(), updated)
        self.assertEqual(self.config.hub.read_bytes(), updated)
        self.assertEqual(
            list((controller.directory / "backups").glob("*"))[0].read_bytes(),
            original,
        )
        controller.tick()
        self.assertIsNone(controller.triggers.due)

    def test_waiting_terminal_review_does_not_block_another_sync(self):
        controller = self.controller()
        count = [0]

        class Engine:
            def __init__(self, ui):
                self.ui = ui

            def sync(self, *, dry_run=False):
                count[0] += 1
                if count[0] == 1:
                    self.ui.confirm("Review synthetic conflict?")
                return SyncResult(
                    current=None, action="unchanged", classifications={}
                )

        controller.engine_factory = Engine
        self.server(controller)
        terminal = Client(self.config).connect(activate=False)
        self.addCleanup(terminal.close)
        terminal.send({"command": "sync", "wait": True, "ui": "tty"})
        question = terminal.receive()
        self.assertEqual(question["type"], "question")
        reply = Client(self.config).request("sync", ui="none", activate=False)
        self.assertEqual(reply["action"], "unchanged")
        terminal.send(
            {"type": "ui-reply", "id": question["id"], "answer": None}
        )
        self.assertEqual(terminal.receive()["type"], "result")

    def test_shutdown_final_sync_releases_inhibitor_and_bounds_rescue_wait(
        self,
    ):
        controller = self.controller()
        controller.inhibitor = Mock()
        controller.rescue = Mock()
        controller.rescue.update.return_value = False
        controller.leases.take("access:test", "access")
        controller.tick()
        controller.inhibitor.update.assert_called_with(True)
        controller.close()
        controller.inhibitor.close.assert_called_once()
        controller.rescue.stop.assert_called_once()
        timeout = controller.rescue.stop.call_args.kwargs["timeout"]
        self.assertGreaterEqual(timeout, 0)
        self.assertLessEqual(timeout, self.config.daemon["rescue_wait"])
        self.assertGreaterEqual(len(self.calls), 1)

    def test_failed_run_does_not_retrigger_for_the_same_hub(self):
        import hashlib

        def fail():
            # The run wrote the hub (atomically, by rename), then failed
            # before committing.
            staged = self.config.hub.with_name(".staged-synthetic")
            staged.write_bytes(b"uncommitted synthetic change")
            os.replace(staged, self.config.hub)
            raise RuntimeError("synthetic persistent failure")

        controller = self.controller(fail)
        self.config.hub.write_bytes(b"committed synthetic data")
        controller.last_written = hashlib.sha256(
            b"committed synthetic data"
        ).hexdigest()
        controller.watch.read()  # The daemon's ticks start the watch.
        with self.assertRaisesRegex(RuntimeError, "synthetic persistent"):
            controller.sync()
        controller.triggers.due = None
        controller.tick()
        controller.tick()
        # No visible re-run (and progress dialog) on every tick.
        self.assertIsNone(controller.triggers.due)
        self.assertEqual(len(self.calls), 1)
        self.config.hub.write_bytes(b"a later synthetic save")
        controller.tick()
        self.assertIsNotNone(controller.triggers.due)

    def test_save_after_engine_commit_is_not_mistaken_for_own_write(self):
        import hashlib

        committed_hash = hashlib.sha256(
            b"committed synthetic data"
        ).hexdigest()

        def last_moment_save():
            self.config.hub.write_bytes(b"new unsynchronized synthetic save")
            return SyncResult(
                current=committed_hash, action="committed", classifications={}
            )

        controller = self.controller(last_moment_save)
        self.config.hub.write_bytes(b"committed synthetic data")
        controller.sync()
        self.assertEqual(controller.last_written, committed_hash)
        self.assertIsNotNone(controller.triggers.due)
        self.assertNotEqual(
            hashlib.sha256(self.config.hub.read_bytes()).hexdigest(),
            controller.last_written,
        )

    def test_hub_and_session_self_writes_do_not_queue_visible_rerun(
        self,
    ):
        import hashlib
        from tests.test_sessions import record

        rec = record(mode="rw")
        committed = hashlib.sha256(b"synchronized").hexdigest()

        def promote():
            self.config.hub.write_bytes(b"synchronized")
            staged.write_bytes(b"synchronized")
            controller.tick()
            self.assertFalse(controller.triggers.again)
            return SyncResult(
                current=committed,
                action="committed",
                classifications={rec.identity: "changed"},
            )

        self.config.daemon["poll_interval"] = 0
        controller = self.controller(promote)
        controller.sessions.store.save(rec)
        staged = (
            controller.sessions.store.staging_path(rec.id)
            / self.config.db_name
        )
        staged.parent.mkdir()
        staged.write_bytes(b"original")
        controller.session_signatures[rec.id] = controller._session_signature(
            rec
        )
        controller.triggers.trigger(immediate=True)
        controller.triggers.start()
        controller.sync()
        controller.triggers.finish()
        controller.next_session_scan = 0
        controller.tick()
        self.assertIsNone(controller.triggers.due)
        self.assertFalse(controller.triggers.again)
        self.assertEqual(len(self.calls), 1)

    def test_session_save_after_engine_commit_still_triggers_followup(self):
        import hashlib
        from tests.test_sessions import record

        rec = record(mode="rw")
        committed = hashlib.sha256(b"committed").hexdigest()

        def last_moment_save():
            staged.write_bytes(b"new remote edit")
            return SyncResult(
                current=committed,
                action="committed",
                classifications={rec.identity: "changed"},
            )

        self.config.hub.write_bytes(b"committed")
        controller = self.controller(last_moment_save)
        controller.sessions.store.save(rec)
        staged = (
            controller.sessions.store.staging_path(rec.id)
            / self.config.db_name
        )
        staged.parent.mkdir()
        staged.write_bytes(b"committed")
        controller.sync()
        self.assertIsNone(controller.triggers.due)
        controller._refresh_sessions(force=True)
        self.assertIsNotNone(controller.triggers.due)

    def test_real_trigger_during_sync_is_preserved(self):
        for scheduled in (False, True):
            with self.subTest(scheduled=scheduled):

                def external_event():
                    controller.triggers.trigger(immediate=True)
                    return SyncResult(
                        current=None, action="unchanged", classifications={}
                    )

                controller = self.controller(external_event)
                if scheduled:
                    controller.triggers.start()
                controller.sync()
                if scheduled:
                    controller.triggers.finish()
                self.assertTrue(controller.triggers.ready())
                self.assertFalse(controller.triggers.start())

    def test_session_disconnect_and_removal_do_not_duplicate_post_sync(
        self,
    ):
        controller = self.controller()
        controller.leases.take("session:test", "session", owner="connection")
        controller.leases.take("session-proc:test", "session")
        with patch.object(controller, "queue_sync") as sync:
            controller.disconnect("connection")
            controller._refresh_sessions(force=True)
            sync.assert_not_called()
            self.assertFalse(controller.leases.leases)
            controller.leases.take("client:test", "client", owner="connection")
            controller.disconnect("connection")
            sync.assert_called_once_with({"ui": "none"})

    def test_dead_session_syncs_after_revocation_and_retains_failed_copy(
        self,
    ):
        from concurrent.futures import Future
        from tests.test_sessions import record

        controller = self.controller()
        for action in ("pending", "unavailable", "unchanged"):
            with self.subTest(action=action):
                clean = action == "unchanged"
                rec = record(mode="rw", alive=False)
                controller.sessions.store.save(rec)
                staged = controller.sessions.store.staging_path(rec.id)
                staged.mkdir(exist_ok=True)
                (staged / self.config.db_name).write_bytes(
                    b"unsynchronized remote edit"
                )
                controller.closing_sessions.clear()
                events = []
                future = Future()
                future.set_result({"action": action})

                def post_sync(request):
                    events.append("sync")
                    self.assertTrue(staged.exists())
                    return future

                discard = controller.sessions.discard

                def remove(record):
                    events.append("discard")
                    discard(record)

                with (
                    patch.object(
                        controller.sessions,
                        "revoke",
                        side_effect=lambda _: events.append("revoke"),
                    ),
                    patch.object(
                        controller.sessions, "discard", side_effect=remove
                    ),
                    patch.object(
                        controller, "queue_sync", side_effect=post_sync
                    ),
                    patch.object(
                        controller.requests,
                        "submit",
                        side_effect=lambda fn, *args: fn(*args),
                    ),
                ):
                    controller._refresh_sessions(force=True)
                self.assertEqual(
                    events, ["revoke", "sync"] + (["discard"] if clean else [])
                )
                self.assertEqual(staged.exists(), not clean)
                self.assertEqual(
                    controller.sessions.store.record_path(rec.id).exists(),
                    not clean,
                )
                if not clean:
                    self.assertGreater(
                        controller.closing_sessions[rec.id], controller.clock()
                    )

    def test_cleanup_drops_private_copy_but_never_external_config(self):
        from kp_sync.rclone_credentials import RcloneCredentials

        controller = self.controller()
        external = self.root / "provider" / "rclone.conf"
        external.parent.mkdir()
        external.write_text("synthetic config only")
        handle = RcloneCredentials("cloud", external, controller.directory)
        controller.credentials = [handle]
        handle.refresh()
        events = []
        controller.rclone_daemons = {
            "cloud": Mock(stop=lambda: events.append("stop"))
        }
        discard = handle.discard
        handle.discard = lambda **kw: events.append("discard") or discard(**kw)
        controller._cleanup_runtime()
        self.assertEqual(events, ["stop", "discard"])
        self.assertFalse(handle.copy_exists())
        self.assertEqual(external.read_text(), "synthetic config only")

    def test_credentials_watch_drains_or_drops_copy(self):
        from kp_sync.lock import file_lock
        from kp_sync.rclone_credentials import RcloneCredentials

        controller = self.controller()
        external = self.root / "provider" / "rclone.conf"
        external.parent.mkdir()
        external.write_text("v1")
        handle = RcloneCredentials("cloud", external, controller.directory)
        controller.credentials = [handle]
        handle.refresh()
        controller._watch_credentials()
        trigger = Mock()
        controller.triggers.trigger = trigger
        external.write_text("v2")
        controller.leases.take("drive:x:y", "drive")
        controller._watch_credentials()
        trigger.assert_called_once_with(immediate=True)
        external.unlink()
        trigger.reset_mock()
        controller._watch_credentials()
        trigger.assert_called_once_with(immediate=True)
        self.assertTrue(handle.copy_exists())
        controller.credential_state = {}
        controller._watch_credentials()
        self.assertTrue(handle.copy_exists())
        controller.leases.release("drive:x:y")
        # Another process still runs rclone: the tick does not wait.
        with file_lock(handle.lock_path, 0, shared=True):
            with patch("kp_sync.lock.time.sleep") as slept:
                controller._watch_credentials()
            slept.assert_not_called()
            self.assertTrue(handle.copy_exists())
        controller._watch_credentials()
        self.assertFalse(handle.copy_exists())

    def test_rcd_is_stopped_before_its_credentials_are_drained(self):
        from kp_sync.rclone_credentials import RcloneCredentials

        controller = self.controller()
        external = self.root / "provider" / "rclone.conf"
        external.parent.mkdir()
        external.write_text("v1")
        handle = RcloneCredentials("cloud", external, controller.directory)
        controller.credentials = [handle]
        handle.refresh()
        events = []
        controller.rclone_daemons = {
            "cloud": Mock(stop=lambda: events.append("stop cloud")),
            "other": Mock(stop=lambda: events.append("stop other")),
        }
        finish = handle.finish
        handle.finish = lambda **kw: events.append("finish") or finish(**kw)
        controller._watch_credentials()
        self.assertEqual(events, [])
        external.unlink()
        controller._watch_credentials()
        self.assertEqual(events, ["stop cloud", "finish"])
        self.assertFalse(handle.copy_exists())

    def test_close_stops_rcd_before_discarding_credentials(self):
        from kp_sync.rclone_credentials import RcloneCredentials

        controller = self.controller()
        external = self.root / "provider" / "rclone.conf"
        external.parent.mkdir()
        external.write_text("v1")
        handle = RcloneCredentials("cloud", external, controller.directory)
        controller.credentials = [handle]
        handle.refresh()
        events = []
        failing = Mock(stop=Mock(side_effect=RuntimeError("synthetic")))
        controller.rclone_daemons = {
            "broken": failing,
            "cloud": Mock(stop=lambda: events.append("stop")),
        }
        discard = handle.discard
        handle.discard = lambda **kw: events.append("discard") or discard(**kw)
        controller.close()
        self.assertEqual(events, ["stop", "discard"])
        self.assertFalse(handle.copy_exists())

    def test_daemon_engines_share_one_owned_rcd_per_auto_endpoint(self):
        from dataclasses import replace

        from kp_sync.config import EndpointConfig

        config = replace(
            self.config,
            endpoints=(
                EndpointConfig(
                    "cloud", "rclone", remote="gd", rclone_transport="auto"
                ),
                EndpointConfig(
                    "plain", "rclone", remote="gd2", rclone_transport="cli"
                ),
            ),
        )
        controller = Controller(config)
        self.addCleanup(controller.watch.close)
        self.addCleanup(lambda: controller.pool.shutdown(wait=True))
        self.addCleanup(lambda: controller.requests.shutdown(wait=True))
        first = {e.name: e for e in controller._engine(NoneUI()).endpoints}
        second = {e.name: e for e in controller._engine(NoneUI()).endpoints}
        self.assertTrue(first["cloud"].transport.owner)
        self.assertIs(first["cloud"].transport, second["cloud"].transport)
        self.assertIsNone(first["plain"].transport)
        self.assertEqual(set(controller.rclone_daemons), {"cloud"})

    def test_close_drops_private_copies_after_final_sync(self):
        from kp_sync.rclone_credentials import RcloneCredentials

        controller = self.controller()
        external = self.root / "provider" / "rclone.conf"
        external.parent.mkdir()
        external.write_text("v1")
        handle = RcloneCredentials("cloud", external, controller.directory)
        controller.credentials = [handle]
        handle.refresh()
        controller.close()
        self.assertFalse(handle.copy_exists())
        self.assertTrue(external.exists())

    def test_startup_drops_orphaned_copy(self):
        from kp_sync.rclone_credentials import RcloneCredentials

        controller = self.controller()
        external = self.root / "provider" / "rclone.conf"
        external.parent.mkdir()
        handle = RcloneCredentials("cloud", external, controller.directory)
        external.write_text("v1")
        handle.refresh()
        external.unlink()
        controller.credentials = [handle]
        controller.credential_state = {}
        controller._watch_credentials()
        self.assertFalse(handle.copy_exists())

    def test_shutdown_offers_rescue_even_if_final_sync_fails(self):
        controller = self.controller()
        controller.rescue = Mock()
        failed = __import__("concurrent.futures", fromlist=["Future"]).Future()
        failed.set_exception(RuntimeError("synthetic offline failure"))
        with patch.object(controller, "queue_sync", return_value=failed):
            controller.close()
        controller.rescue.stop.assert_called_once()
        self.assertGreater(
            controller.rescue.stop.call_args.kwargs["timeout"], 0
        )

    def test_shutdown_reserves_rescue_budget_when_final_sync_times_out(self):
        import concurrent.futures

        controller = self.controller()
        controller.rescue = Mock()
        blocked = Mock()
        blocked.result.side_effect = concurrent.futures.TimeoutError
        with (
            patch.dict(os.environ, {"DISPLAY": ":synthetic"}),
            patch.object(controller, "queue_sync", return_value=blocked),
        ):
            controller.close()
        self.assertLessEqual(blocked.result.call_args.kwargs["timeout"], 25)
        controller.rescue.stop.assert_called_once()
        self.assertGreater(
            controller.rescue.stop.call_args.kwargs["timeout"], 0
        )

    def test_timed_out_presync_marks_late_hub_updates_for_tracked_application(
        self,
    ):
        from dataclasses import replace
        import concurrent.futures

        controller = self.controller()
        controller.config = replace(
            self.config,
            daemon={**self.config.daemon, "prestart_timeout": 0.01},
        )
        self.config.hub.write_bytes(b"initial synthetic hub")
        controller.apps = Mock()
        controller.apps.activate_existing.return_value = None
        controller.apps.refresh.return_value = ["app:keepassxc"]
        pending = concurrent.futures.Future()
        with patch.object(controller, "queue_sync", return_value=pending):
            controller.handle({"command": "run", "ui": "none"})
        controller.apps.start.assert_called_once()
        self.assertIn("app:keepassxc", controller.leases.leases)

        def late_sync():
            import hashlib

            data = b"late synchronized synthetic hub"
            self.config.hub.write_bytes(data)
            return SyncResult(
                current=hashlib.sha256(data).hexdigest(),
                action="committed",
                classifications={},
            )

        class Engine:
            def sync(self, *, dry_run=False):
                return late_sync()

        controller.engine_factory = lambda _: Engine()
        controller.sync()
        controller.apps.hub_updated.assert_called_once()


if __name__ == "__main__":
    unittest.main()
