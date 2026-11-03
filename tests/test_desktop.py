import errno
import hashlib
import io
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from kp_sync.config import load_config
from kp_sync.daemon.apps import AppManager, NotificationError, notify_user
from kp_sync.daemon.dbus import DBusMonitor, KEEPASSXC_NAME
from kp_sync.daemon.inhibit import LogindInhibitor
from kp_sync.daemon.rescue import (
    RescueCoordinator,
    RescueManager,
    RescueResult,
    hub_is_safe_to_remove,
    removable_mounts,
)
from kp_sync.daemon.systemd import SystemdUser, app_unit_name
from kp_sync.drives import Drive
from kp_sync.state import HistoryState
from kp_sync.pending import PendingStore

from tests.fixtures import private_tempdir, write_kdbx4


class FakeSystemd:
    def __init__(self, active=()):
        self.active = set(active)
        self.starts = []
        self.stops = []
        self.discovery_calls = 0

    def active_units(self, prefix):
        self.discovery_calls += 1
        return {unit for unit in self.active if unit.startswith(prefix)}

    def is_active(self, unit):
        return unit in self.active

    def start_transient(self, unit, command, *, environment=None):
        self.starts.append((unit, tuple(command), dict(environment or {})))
        self.active.add(unit)

    def stop(self, unit):
        self.stops.append(unit)
        self.active.discard(unit)


class FakeDBus:
    def __init__(self, owners=None):
        self.owners = dict(owners or {})
        self.on_change = None

    def start(self):
        pass

    def poll_signals(self):
        return 0

    def discover(self):
        return dict(self.owners)

    def close(self):
        pass


class DesktopTest(unittest.TestCase):
    def setUp(self):
        self.temp = private_tempdir()
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.config = load_config(
            environ={
                "HOME": str(self.root),
                "XDG_RUNTIME_DIR": str(self.runtime),
            },
            hostname="test-host",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_systemd_transient_unit_passes_display_environment_as_arguments(
        self,
    ):
        commands = []

        def runner(argv, **kwargs):
            commands.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        systemd = SystemdUser(
            runner=runner,
            environment={"DISPLAY": ":4", "SECRET": "not-forwarded"},
        )
        systemd.start_transient(
            "kp-sync-app-keepassxc.service",
            ("keepassxc", "/vault/pass.kdbx"),
            environment={"LANG": "C"},
        )
        self.assertIn("--setenv", commands[0])
        self.assertIn("DISPLAY=:4", commands[0])
        self.assertIn("LANG=C", commands[0])
        self.assertNotIn("SECRET=not-forwarded", commands[0])
        self.assertEqual(
            commands[0][-3:], ["--", "keepassxc", "/vault/pass.kdbx"]
        )

    def test_notify_user_is_display_only_and_uses_argument_list(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, "", "")

        self.assertFalse(
            notify_user("Sync pending", environment={}, runner=runner)
        )
        self.assertEqual(calls, [])
        self.assertTrue(
            notify_user(
                "Sync pending",
                environment={"WAYLAND_DISPLAY": "wayland-0"},
                runner=runner,
            )
        )
        argv, kwargs = calls[0]
        self.assertEqual(
            argv,
            [
                "notify-send",
                "--app-name=kp-sync",
                "--expire-time=10000",
                "Sync pending",
            ],
        )
        self.assertEqual(kwargs["timeout"], 5)
        self.assertEqual(kwargs["env"]["WAYLAND_DISPLAY"], "wayland-0")

    def test_notify_user_reports_missing_or_failed_notification_backend(self):
        def missing(argv, **kwargs):
            raise FileNotFoundError("notify-send")

        with self.assertRaisesRegex(
            NotificationError, "cannot show desktop notification"
        ):
            notify_user(
                "A sync endpoint was skipped",
                environment={"DISPLAY": ":0"},
                runner=missing,
            )

        def failed(argv, **kwargs):
            return subprocess.CompletedProcess(
                argv, 1, "", "notification service unavailable"
            )

        with self.assertRaisesRegex(
            NotificationError, "notification service unavailable"
        ):
            notify_user(
                "A sync endpoint was skipped",
                environment={"DISPLAY": ":0"},
                runner=failed,
            )

    def test_profile_scoped_fixed_unit_names_reject_injection(self):
        self.assertEqual(
            app_unit_name("work-2", "keepassxc"),
            "kp-sync-app-work-2-keepassxc.service",
        )
        with self.assertRaises(ValueError):
            app_unit_name("../outside", "keepassxc")

    def test_wrapper_reuses_instance_and_only_raises_interactively(
        self,
    ):
        unit = app_unit_name(None, "keepassxc")
        systemd = FakeSystemd({unit})
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        manager = AppManager(
            self.config,
            systemd,
            FakeDBus(),
            runner=runner,
            proc_root=self.root,
            environment={"DISPLAY": ":2"},
            notifier=lambda message: True,
        )
        quiet = manager.start("keepassxc")
        self.assertFalse(quiet.started)
        self.assertFalse(quiet.raised)
        self.assertEqual(calls, [])
        interactive = manager.start(
            "keepassxc",
            interactive=True,
            environment={"DISPLAY": ":9"},
        )
        self.assertTrue(interactive.raised)
        self.assertEqual(calls, [["wmctrl", "-x", "-a", "keepassxc"]])
        manager.close()

    def test_activate_existing_never_starts_an_absent_application(self):
        systemd = FakeSystemd()
        manager = AppManager(
            self.config,
            systemd,
            FakeDBus(),
            proc_root=self.root,
            environment={},
            notifier=lambda message: True,
        )
        self.addCleanup(manager.close)
        self.assertIsNone(
            manager.activate_existing("keepassxc", interactive=True)
        )
        self.assertEqual(systemd.starts, [])

    def test_raise_uses_single_instance_forwarding_when_window_is_not_listed(
        self,
    ):
        unit = app_unit_name(None, "keepassxc")
        systemd = FakeSystemd({unit})
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            status = 1 if argv[0] == "wmctrl" else 0
            return subprocess.CompletedProcess(argv, status, "", "")

        manager = AppManager(
            self.config,
            systemd,
            FakeDBus(),
            runner=runner,
            proc_root=self.root,
            environment={"DISPLAY": ":2"},
            notifier=lambda message: True,
        )
        outcome = manager.start("keepassxc", interactive=True)
        self.assertTrue(outcome.raised)
        self.assertEqual(
            calls,
            [
                ["wmctrl", "-x", "-a", "keepassxc"],
                ["keepassxc", str(self.config.hub)],
            ],
        )
        manager.close()

    def test_stop_started_stops_only_transient_units_started_by_manager(self):
        systemd = FakeSystemd()
        manager = AppManager(
            self.config,
            systemd,
            FakeDBus(),
            proc_root=self.root,
            environment={},
        )
        result = manager.start("keepassxc")
        self.assertTrue(result.started)
        self.assertIn(result.unit, systemd.active)
        manager.stop_started()
        self.assertNotIn(result.unit, systemd.active)
        self.assertEqual(systemd.stops, [result.unit])
        manager.close()

    def test_launch_request_argument_does_not_look_like_running_application(
        self,
    ):
        process = self.root / "12345"
        process.mkdir()
        (process / "comm").write_text("python\n", encoding="utf-8")
        (process / "cmdline").write_bytes(
            b"/usr/bin/python3\0kp-sync\0run\0--app\0keepassxc\0"
        )
        systemd = FakeSystemd()
        manager = AppManager(
            self.config,
            systemd,
            FakeDBus(),
            proc_root=self.root,
            environment={},
            runner=lambda argv, **kwargs: subprocess.CompletedProcess(
                argv, 0, "", ""
            ),
        )

        result = manager.start("keepassxc")

        self.assertTrue(result.started)
        self.assertEqual(len(systemd.starts), 1)
        manager.close()

    def test_default_launch_uses_hub_preserving_explicit_commands(
        self,
    ):
        config = replace(
            self.config, apps={"viewer": {"command": ["viewer", "--flag"]}}
        )
        for name, args, expected in (
            ("keepassxc", (), ("keepassxc", str(config.hub))),
            ("keepass-legacy", (), ("keepass", str(config.hub))),
            (
                "keepassxc",
                ("/explicit.kdbx",),
                ("keepassxc", "/explicit.kdbx"),
            ),
            ("viewer", (), ("viewer", "--flag")),
        ):
            with self.subTest(name=name, args=args):
                systemd = FakeSystemd()
                manager = AppManager(
                    config,
                    systemd,
                    FakeDBus(),
                    proc_root=self.root,
                    environment={},
                    notifier=lambda message: True,
                )
                self.addCleanup(manager.close)
                manager.start(name, args)
                self.assertEqual(systemd.starts[0][1], expected)

    def test_app_reload_state_marks_saves_foreign_until_reopen(self):
        unit = app_unit_name(None, "legacy")
        systemd = FakeSystemd({unit})
        config = load_config(
            environ={
                "HOME": str(self.root),
                "XDG_RUNTIME_DIR": str(self.runtime),
            },
            hostname="test-host",
        )
        config = config.__class__(
            **{
                **config.__dict__,
                "apps": {
                    "legacy": {
                        "command": ["legacy-app"],
                        "process": [],
                        "reloads": False,
                        "single_instance": True,
                    }
                },
            }
        )
        notifications = []
        keepass_settings = self.root / ".config/keepassxc/keepassxc.ini"
        keepass_settings.parent.mkdir(parents=True)
        keepass_settings.write_text(
            "[General]\nAutoReloadOnChange=true\n", encoding="utf-8"
        )
        manager = AppManager(
            config,
            systemd,
            FakeDBus(),
            proc_root=self.root,
            environment={"HOME": str(self.root)},
            notifier=lambda message: notifications.append(message) or True,
        )
        self.assertIn("app:legacy", manager.refresh())
        manager.hub_updated()
        self.assertTrue(manager.foreign_writers)
        self.assertTrue(manager.save_is_foreign("legacy"))
        self.assertEqual(len(notifications), 1)
        manager.hub_updated()
        self.assertEqual(len(notifications), 1)
        self.assertTrue(manager.hub_opened())
        self.assertFalse(manager.foreign_writers)
        manager.close()

    def test_app_notifications_respect_ui_notify_setting(self):
        notifications = []
        config = replace(self.config, ui={**self.config.ui, "notify": False})
        manager = AppManager(
            config,
            FakeSystemd({app_unit_name(None, "keepass-legacy")}),
            FakeDBus(),
            proc_root=self.root,
            environment={},
            notifier=lambda message: notifications.append(message) or True,
        )
        manager.hub_updated()
        self.assertTrue(manager.foreign_writers)
        self.assertEqual(notifications, [])
        manager.close()

    def test_keepassxc_reload_setting_controls_foreign_save_protection(self):
        ini = self.root / ".config/keepassxc/keepassxc.ini"
        ini.parent.mkdir(parents=True)
        unit = app_unit_name(None, "keepassxc")
        for option, enabled in (
            ("AutoReloadOnChange=false\n", False),
            ("AutoReloadOnChange=true\n", True),
            ("", True),
        ):
            with self.subTest(option=option):
                ini.write_text(f"[General]\n{option}", encoding="utf-8")
                with patch(
                    "kp_sync.daemon.apps.notify_user", return_value=True
                ) as notify:
                    manager = AppManager(
                        self.config,
                        FakeSystemd({unit}),
                        FakeDBus(),
                        proc_root=self.root,
                        environment={
                            "HOME": str(self.root),
                            "DISPLAY": ":test",
                        },
                    )
                    self.addCleanup(manager.close)
                    self.assertEqual(
                        manager.specs["keepassxc"].reloads, enabled
                    )
                    manager.hub_updated()
                    self.assertEqual(
                        manager.save_is_foreign("keepassxc"), not enabled
                    )
                    if enabled:
                        notify.assert_not_called()
                    else:
                        self.assertTrue(manager.reload_warnings)
                        self.assertEqual(notify.call_count, 2)
                        self.assertIn(
                            "older database", notify.call_args.args[0]
                        )

    def test_app_refresh_throttles_fallback_discovery_to_poll_interval(self):
        unit = app_unit_name(None, "legacy")
        systemd = FakeSystemd({unit})
        now = [0.0]
        config = self.config.__class__(
            **{
                **self.config.__dict__,
                "apps": {
                    "legacy": {
                        "command": ["legacy-app"],
                        "process": [],
                        "reloads": False,
                    }
                },
            }
        )
        manager = AppManager(
            config,
            systemd,
            FakeDBus(),
            proc_root=self.root,
            environment={},
            clock=lambda: now[0],
        )
        self.assertEqual(systemd.discovery_calls, 1)
        manager.refresh()
        manager.refresh()
        self.assertEqual(systemd.discovery_calls, 1)
        now[0] += config.daemon["poll_interval"]
        manager.refresh()
        self.assertEqual(systemd.discovery_calls, 2)
        manager.close()

    def test_process_discovery_uses_pidfd_when_available_and_marks_fallback(
        self,
    ):
        manager = AppManager(
            self.config,
            FakeSystemd(),
            FakeDBus(),
            proc_root=self.root,
            environment={},
        )
        with patch(
            "kp_sync.daemon.apps.os.pidfd_open",
            side_effect=OSError(errno.ENOSYS, "unsupported"),
            create=True,
        ):
            manager._refresh_pidfds({987654})
        self.assertIn(987654, manager.pidfd_fallback_pids)
        manager.close()

    def test_down_safety_requires_current_hub_and_another_committed_copy(self):
        hub = self.root / "pass.kdbx"
        write_kdbx4(hub, b"database")
        hub.chmod(0o600)
        config = replace(self.config, hub=hub, host="test-host")
        digest = hashlib.sha256(hub.read_bytes()).hexdigest()
        state = HistoryState()
        state.commit(
            digest,
            (),
            config.host,
            ("hub:test-host", "cloud:Drive"),
            size=hub.stat().st_size,
        )
        state.save(self.runtime / "state.json")
        self.assertTrue(hub_is_safe_to_remove(config, self.runtime))
        hub.write_bytes(b"changed")
        self.assertFalse(hub_is_safe_to_remove(config, self.runtime))
        self.assertTrue(hub.is_file())

    def test_down_safety_never_removes_persistent_hub(self):
        config = replace(self.config, hub_persistent=True)
        self.assertFalse(hub_is_safe_to_remove(config, self.runtime))

    def test_pending_decisions_make_hub_unsafe_to_remove(self):
        hub = self.root / "pass.kdbx"
        write_kdbx4(hub, b"pending")
        hub.chmod(0o600)
        config = replace(self.config, hub=hub)
        digest = hashlib.sha256(hub.read_bytes()).hexdigest()
        state = HistoryState()
        state.commit(
            digest,
            (),
            config.host,
            ("hub:test-host", "cloud:Drive"),
            size=hub.stat().st_size,
        )
        state.save(self.runtime / "state.json")
        PendingStore(self.runtime).save("merge", "needs review")
        self.assertFalse(hub_is_safe_to_remove(config, self.runtime))

    def test_rescue_update_notifies_once_and_saves_each_other_removable_drive(
        self,
    ):
        hub = self.root / "pass.kdbx"
        write_kdbx4(hub, b"unsynced")
        hub.chmod(0o600)
        config = replace(self.config, hub=hub)
        mounts = self.root / "mounts"
        mounts.mkdir()
        drives = [
            Drive("A:uuid-a", mounts, "now"),
            Drive("B:uuid-b", mounts, "now"),
        ]
        notifications = []

        class FakeManager:
            def __init__(self):
                self.calls = []

            def save(self, source, drive, *, host, expected_sha256):
                self.calls.append(
                    (source, drive.identity, host, expected_sha256)
                )
                return RescueResult(
                    drive.mountpoint / "rescue.kdbx",
                    expected_sha256,
                    drive.identity,
                )

        manager = FakeManager()
        coordinator = RescueCoordinator(
            config,
            self.runtime,
            mounts_provider=lambda: drives,
            notifier=lambda message, action: notifications.append(
                (message, action)
            ),
            manager=manager,
            environ={},
        )
        self.assertTrue(coordinator.update())
        self.assertTrue(coordinator.rescue_needed)
        self.assertTrue(coordinator.rescued)
        self.assertEqual(len(manager.calls), 2)
        self.assertEqual(len(notifications), 1)
        coordinator.update()
        self.assertEqual(len(manager.calls), 2)
        self.assertEqual(len(notifications), 1)
        self.assertNotIn("key", repr(manager.calls).lower())

    def test_rescue_desktop_action_saves_to_newly_inserted_pendrive(self):
        hub = self.root / "pass.kdbx"
        write_kdbx4(hub, b"action rescue")
        hub.chmod(0o600)
        config = replace(self.config, hub=hub)
        mount = self.root / "new-drive"
        mount.mkdir()
        available = []
        saved = []

        class FakeManager:
            def save(self, source, drive, *, host, expected_sha256):
                saved.append(drive.identity)
                return RescueResult(
                    mount / "rescue.kdbx", expected_sha256, drive.identity
                )

        read_fd, write_fd = os.pipe()

        class Process:
            returncode = 0

            def __init__(self):
                self.stdout = os.fdopen(read_fd, "r")
                self.stderr = io.StringIO()

            def poll(self):
                return 0

        process = Process()
        argv_seen = []

        def popen(argv, **kwargs):
            argv_seen.append(argv)
            os.write(write_fd, b"save")
            os.close(write_fd)
            return process

        coordinator = RescueCoordinator(
            config,
            self.runtime,
            mounts_provider=lambda: available,
            manager=FakeManager(),
            environ={"DISPLAY": ":1"},
            popen=popen,
        )
        try:
            self.assertTrue(coordinator.update())
            self.assertFalse(saved)
            self.assertIn("--action=save=Save to pendrive", argv_seen[0])
            available.append(Drive("USB:uuid-action", mount, "now"))
            coordinator._poll_notification()
            self.assertEqual(saved, ["USB:uuid-action"])
        finally:
            coordinator.close()

    def test_rescue_skips_normal_attached_drive_and_detects_new_content(self):
        hub = self.root / "pass.kdbx"
        write_kdbx4(hub, b"first")
        hub.chmod(0o600)
        config = replace(self.config, hub=hub)
        mount = self.root / "mounts"
        mount.mkdir()
        drive_record = self.runtime / "drives"
        drive_record.mkdir()
        (drive_record / "A.json").write_text(
            '{"identity":"A:uuid-a","mountpoint":"/not-used",'
            '"attached_at":"now"}\n',
            encoding="utf-8",
        )
        (drive_record / "A.json").chmod(0o600)
        notifications = []
        saved = []

        class FakeManager:
            def save(self, source, drive, *, host, expected_sha256):
                saved.append((drive.identity, expected_sha256))
                return RescueResult(
                    mount / "rescue", expected_sha256, drive.identity
                )

        drives = [
            Drive("A:uuid-a", mount, "now"),
            Drive("B:uuid-b", mount, "now"),
        ]
        coordinator = RescueCoordinator(
            config,
            self.runtime,
            mounts_provider=lambda: drives,
            notifier=lambda message, action: notifications.append(message),
            manager=FakeManager(),
            environ={},
        )
        coordinator.update()
        self.assertEqual([identity for identity, _ in saved], ["B:uuid-b"])
        hub.write_bytes(b"second")
        coordinator.update()
        self.assertEqual(len(notifications), 2)
        self.assertEqual(len(saved), 2)
        self.assertNotEqual(saved[0][1], saved[1][1])

    def test_shutdown_rescue_wait_is_bounded_and_requires_display(self):
        hub = self.root / "pass.kdbx"
        write_kdbx4(hub, b"shutdown")
        hub.chmod(0o600)
        config = replace(
            self.config,
            hub=hub,
            daemon={**self.config.daemon, "rescue_wait": 2},
        )
        now = [0.0]
        prompts = []
        saves = []

        class FakeManager:
            def save(self, source, drive, *, host, expected_sha256):
                saves.append(expected_sha256)
                return RescueResult(source, expected_sha256, drive.identity)

        drive = Drive("USB:uuid-1", self.root, "now")
        coordinator = RescueCoordinator(
            config,
            self.runtime,
            clock=lambda: now[0],
            sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
            mounts_provider=lambda: [drive] if now[0] >= 0.5 else [],
            notifier=lambda message, action: None,
            manager=FakeManager(),
            environ={"DISPLAY": ":1"},
            prompt=lambda message, timeout: prompts.append(timeout) or True,
        )
        self.assertTrue(coordinator.stop(timeout=0.75))
        self.assertEqual(len(prompts), 1)
        self.assertLessEqual(prompts[0], 0.75)
        self.assertTrue(saves)

        no_display = RescueCoordinator(
            config,
            self.runtime,
            mounts_provider=lambda: [],
            notifier=lambda message, action: None,
            manager=FakeManager(),
            environ={},
            prompt=lambda *_: self.fail("must not prompt without a display"),
        )
        self.assertFalse(no_display.stop(timeout=1))

    def test_removable_mount_discovery_filters_ro_and_nonremovable(self):
        removable = self.root / "removable"
        readonly = self.root / "readonly"
        regular = self.root / "regular"
        for path in (removable, readonly, regular):
            path.mkdir()
        payload = {
            "blockdevices": [
                {
                    "name": "usb",
                    "rm": True,
                    "ro": False,
                    "serial": "SERIAL",
                    "children": [
                        {
                            "type": "part",
                            "rm": False,
                            "ro": False,
                            "uuid": "uuid-rw",
                            "mountpoints": [str(removable)],
                        },
                        {
                            "type": "part",
                            "rm": False,
                            "ro": False,
                            "uuid": "uuid-ro",
                            "mountpoints": [str(readonly)],
                        },
                    ],
                },
                {
                    "type": "part",
                    "rm": False,
                    "ro": False,
                    "serial": "DISK",
                    "uuid": "uuid-regular",
                    "mountpoints": [str(regular)],
                },
            ]
        }
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            if argv[0] == "lsblk":
                import json

                return subprocess.CompletedProcess(
                    argv, 0, json.dumps(payload), ""
                )
            options = "ro,nosuid" if str(readonly) in argv else "rw,nosuid"
            return subprocess.CompletedProcess(argv, 0, options + "\n", "")

        drives = removable_mounts(runner=runner)
        self.assertEqual(
            [(drive.identity, drive.mountpoint) for drive in drives],
            [("SERIAL:uuid-rw", removable)],
        )
        self.assertEqual(sum(argv[0] == "findmnt" for argv in calls), 2)

    def test_keepassxc_dbus_owner_is_resolved_to_process_id(self):
        outputs = iter(
            (
                subprocess.CompletedProcess([], 0, "b true\n", ""),
                subprocess.CompletedProcess([], 0, 's ":1.27"\n', ""),
                subprocess.CompletedProcess([], 0, "u 1234\n", ""),
            )
        )
        monitor = DBusMonitor(runner=lambda *args, **kwargs: next(outputs))
        self.assertEqual(monitor.owner_pid(KEEPASSXC_NAME), 1234)

    def test_name_owner_changed_signal_emits_pid_and_owner(self):
        read_fd, write_fd = os.pipe()
        systemd_read_fd, systemd_write_fd = os.pipe()
        os.set_blocking(read_fd, False)
        os.set_blocking(systemd_read_fd, False)

        class FakeProcess:
            def __init__(self, descriptor):
                self.stdout = os.fdopen(descriptor, "rb", buffering=0)
                self.stderr = io.BytesIO()

            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

        processes = iter((FakeProcess(read_fd), FakeProcess(systemd_read_fd)))
        calls = []
        monitor = DBusMonitor(
            popen=lambda *args, **kwargs: next(processes),
            runner=lambda *args, **kwargs: subprocess.CompletedProcess(
                [], 0, "u 456\n", ""
            ),
            on_change=lambda *args: calls.append(args),
        )
        line = (
            "/org/freedesktop/DBus: org.freedesktop.DBus.NameOwnerChanged "
            "('org.keepassxc.KeePassXC.MainWindow', '', ':1.33')\n"
        )
        os.write(write_fd, line.encode())
        self.assertEqual(monitor.poll_signals(), 1)
        self.assertEqual(calls[0], (KEEPASSXC_NAME, "", ":1.33", 456))
        monitor.stop()
        os.close(write_fd)
        os.close(systemd_write_fd)

    def test_inhibitor_holds_until_update_releases_and_is_bounded(self):
        class Process:
            def __init__(self):
                self.stdin = io.BytesIO()
                self.stderr = io.BytesIO()
                self.returncode = None
                self.terminated = False

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                self.returncode = 0
                return 0

            def terminate(self):
                self.terminated = True
                self.returncode = -15

            def kill(self):
                self.returncode = -9

        launched = []
        process = Process()
        inhibitor = LogindInhibitor(
            popen=lambda argv, **kwargs: launched.append(argv) or process
        )
        inhibitor.update(True)
        self.assertTrue(inhibitor.acquired)
        self.assertIn("--mode=delay", launched[0])
        inhibitor.update(False)
        self.assertFalse(inhibitor.acquired)
        inhibitor.close()

    def test_inhibitor_forces_release_after_bounded_wait(self):
        class Process:
            def __init__(self):
                self.stdin = io.BytesIO()
                self.stderr = io.BytesIO()
                self.returncode = None
                self.waits = 0
                self.terminated = False
                self.killed = False

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                self.waits += 1
                if self.waits == 1:
                    raise subprocess.TimeoutExpired("systemd-inhibit", timeout)
                self.returncode = 0
                return 0

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.killed = True

        process = Process()
        inhibitor = LogindInhibitor(
            popen=lambda argv, **kwargs: process,
            release_timeout=0.01,
        )
        inhibitor.acquire()
        inhibitor.release()
        self.assertTrue(process.terminated)
        self.assertFalse(process.killed)
        self.assertEqual(process.waits, 2)

    def test_rescue_creates_verified_additional_copy_without_removing_hub(
        self,
    ):
        hub = self.root / "pass.kdbx"
        write_kdbx4(hub, b"valid fixture")
        hub.chmod(0o600)
        mount = self.root / "drive"
        mount.mkdir()
        drive = Drive("SERIAL:fs-123", mount, "now")
        manager = RescueManager(identity_resolver=lambda _: drive.identity)
        result = manager.save(hub, drive, host="laptop")
        self.assertTrue(result.path.is_file())
        self.assertEqual(result.path.read_bytes(), hub.read_bytes())
        self.assertTrue(hub.is_file())
        self.assertNotEqual(result.path, hub)

    def test_rescue_refuses_unverified_database_and_preserves_source(self):
        hub = self.root / "not-a-database.kdbx"
        hub.write_bytes(b"not kdbx")
        hub.chmod(0o600)
        mount = self.root / "drive"
        mount.mkdir()
        drive = Drive("SERIAL:fs-123", mount, "now")
        manager = RescueManager(identity_resolver=lambda _: drive.identity)
        with self.assertRaisesRegex(RuntimeError, "verified KDBX"):
            manager.save(hub, drive, host="laptop")
        self.assertEqual(hub.read_bytes(), b"not kdbx")

    def test_rescue_rejects_changed_content_after_copy(self):
        hub = self.root / "pass.kdbx"
        write_kdbx4(hub, b"original")
        hub.chmod(0o600)
        original = hub.read_bytes()
        mount = self.root / "drive"
        mount.mkdir()
        drive = Drive("SERIAL:fs-123", mount, "now")

        class ChangedEndpoint:
            def __init__(self, name, mountpoint, filename, identity, **kwargs):
                self.path = mountpoint / filename

            def store_if_unchanged(self, source, expected, **kwargs):
                write_kdbx4(self.path, b"concurrent valid but different save")

        manager = RescueManager(identity_resolver=lambda _: drive.identity)
        with patch("kp_sync.daemon.rescue.DriveEndpoint", ChangedEndpoint):
            with self.assertRaisesRegex(RuntimeError, "SHA-256 verification"):
                manager.save(hub, drive, host="laptop")
        self.assertEqual(hub.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
