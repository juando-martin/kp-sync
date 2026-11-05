import os
import socket
import subprocess
import signal
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from kp_sync.config import load_config
from kp_sync.services import (
    ServiceError,
    manage_daemon_service,
    manage_service,
    run_service,
    service_unit,
)


class ServicesTest(unittest.TestCase):
    def setUp(self):
        # Fixture files model kp-sync-installed files, which are never
        # group-writable; do not inherit a permissive user umask.
        self.addCleanup(os.umask, os.umask(0o022))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "config.toml"
        self.path.write_text(
            "[sessions]\nenabled = true\n"
            '[ssh_sessions.remote]\ntarget = "bob@server:~/kp"\n'
            '[access_owners.office]\ncommand = ["kp-access-owner", '
            '"--config", "owner.toml"]\n',
            encoding="utf-8",
        )
        self.config = load_config(
            self.path,
            environ={
                "HOME": str(self.root),
                "XDG_RUNTIME_DIR": str(self.root),
            },
        )

    def default_daemon_config(self):
        return replace(
            self.config,
            source=Path.home() / ".config" / "kp-sync" / "config.toml",
        )

    def test_service_unit_is_unique_to_custom_config(self):
        unit = service_unit(self.config, "ssh-session", "remote")
        self.assertTrue(unit.startswith("kp-sync-ssh-session-config-"))
        self.assertTrue(unit.endswith("-remote.service"))
        with self.assertRaisesRegex(ServiceError, "no configured"):
            service_unit(self.config, "access-owner", "missing")
        named_default = replace(self.config, profile="default")
        self.assertNotEqual(
            service_unit(self.config, "ssh-session", "remote"),
            service_unit(named_default, "ssh-session", "remote"),
        )

    def test_start_runs_named_service_in_systemd_without_a_shell(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            if argv[0] == "systemctl":
                return subprocess.CompletedProcess(argv, 3, "inactive\n", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with (
            patch("kp_sync.services.info") as log,
            patch(
                "kp_sync.sessions.session.kp_sync_executable",
                return_value="/venv/bin/kp-sync",
            ),
        ):
            self.assertEqual(
                manage_service(
                    self.config,
                    "start",
                    "ssh-session",
                    "remote",
                    runner=runner,
                ),
                0,
            )
        self.assertEqual(calls[0][:3], ["systemctl", "--user", "is-active"])
        launch = calls[1]
        self.assertEqual(launch[0:2], ["systemd-run", "--user"])
        self.assertIn("--property=KillMode=mixed", launch)
        self.assertIn("--property=TimeoutStopSec=180", launch)
        self.assertIn("--", launch)
        self.assertIn("/venv/bin/kp-sync", launch)
        self.assertEqual(launch[-3:], ["service-run", "ssh-session", "remote"])
        self.assertIn("journalctl --user", log.call_args.args[0])

    def test_start_is_idempotent_and_stop_uses_systemctl(self):
        calls = []

        def active_runner(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "active\n", "")

        self.assertEqual(
            manage_service(
                self.config,
                "start",
                "access-owner",
                "office",
                runner=active_runner,
            ),
            0,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            manage_service(
                self.config,
                "stop",
                "access-owner",
                "office",
                runner=active_runner,
            ),
            0,
        )
        self.assertEqual(
            calls[-1][2:4],
            [
                "stop",
                service_unit(
                    self.config,
                    "access-owner",
                    "office",
                ),
            ],
        )

    def test_start_rejects_disabled_ssh_sessions_before_systemd(self):
        config = replace(
            self.config,
            sessions={**self.config.sessions, "enabled": False},
        )
        with self.assertRaisesRegex(ServiceError, "sessions are disabled"):
            manage_service(config, "start", "ssh-session", "remote")

    def test_status_reports_inactive_unit(self):
        output = []

        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 4, "", "")

        with patch(
            "builtins.print", side_effect=lambda value: output.append(value)
        ):
            self.assertEqual(
                manage_service(
                    self.config,
                    "status",
                    "ssh-session",
                    "remote",
                    runner=runner,
                ),
                1,
            )
        self.assertIn(": inactive", output[0])

    def test_session_runner_uses_named_configuration_and_managed_mode(self):
        with patch(
            "kp_sync.sessions.session.run_session", return_value=0
        ) as run:
            self.assertEqual(
                run_service(self.config, "ssh-session", "remote"), 0
            )
        run.assert_called_once_with(
            self.config,
            "bob@server:~/kp",
            mode=None,
            mount_dir=None,
            unconfined=False,
            managed=True,
            ui="none",
        )

    def test_owner_command_runs_as_argv_without_a_shell(self):
        expected = ["kp-access-owner", "--config", "owner.toml"]
        process = Mock()
        process.wait.return_value = 7
        process.poll.return_value = 7
        with patch(
            "kp_sync.services.subprocess.Popen",
            return_value=process,
        ) as popen:
            self.assertEqual(
                run_service(self.config, "access-owner", "office"), 7
            )
        popen.assert_called_once_with(
            expected,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def test_owner_stop_signal_is_forwarded_to_the_child(self):
        process = Mock()
        process.wait.side_effect = lambda: (
            handlers[signal.SIGTERM](signal.SIGTERM, None),
            -signal.SIGTERM,
        )[1]
        process.poll.side_effect = [None, -signal.SIGTERM]
        handlers = {}

        def set_handler(sig, handler):
            previous = handlers.get(sig, signal.SIG_DFL)
            handlers[sig] = handler
            return previous

        with (
            patch("kp_sync.services.subprocess.Popen", return_value=process),
            patch("kp_sync.services.signal.signal", side_effect=set_handler),
            patch("kp_sync.services.info") as log_info,
        ):
            self.assertEqual(
                run_service(self.config, "access-owner", "office"), 0
            )
        process.terminate.assert_called_once()
        self.assertIn("Stopped configured", log_info.call_args.args[0])

    def test_daemon_status_does_not_activate_or_use_socket_readiness(self):
        calls = []
        config = self.default_daemon_config()

        def runner(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(
                argv,
                1 if argv[2] == "is-enabled" else 3,
                "disabled\n" if argv[2] == "is-enabled" else "inactive\n",
                "",
            )

        with patch("builtins.print") as output:
            self.assertEqual(
                manage_daemon_service(config, "status", runner=runner),
                1,
            )
        self.assertEqual(len(calls), 7)
        self.assertEqual(
            calls[0],
            ["systemctl", "--user", "is-active", "kp-sync.service"],
        )
        self.assertIn(
            unittest.mock.call("kp-sync.service: inactive"),
            output.call_args_list,
        )
        self.assertIn(
            unittest.mock.call("kp-sync.socket: inactive; disabled"),
            output.call_args_list,
        )

    def test_activation_enable_and_disable_now_order_and_profile_scope(self):
        config = replace(self.config, profile="work")
        for action, now in (
            ("enable", False),
            ("enable", True),
            ("disable", False),
            ("disable", True),
        ):
            with self.subTest(action=action, now=now):
                calls = []

                def runner(argv, **kwargs):
                    calls.append(argv[2:])
                    if "--property=LoadState" in argv:
                        text = "loaded"
                    elif argv[2] == "is-active":
                        text = "active"
                    elif argv[2] == "show":
                        text = (
                            "Result=success\nExecMainCode=1\nExecMainStatus=0"
                        )
                    else:
                        text = ""
                    return subprocess.CompletedProcess(argv, 0, text, "")

                self.assertEqual(
                    manage_daemon_service(
                        config, action, now=now, runner=runner
                    ),
                    0,
                )
                writes = [
                    call
                    for call in calls
                    if call[0] not in {"show", "is-active"}
                ]
                triggers = ["kp-sync@work.socket", "kp-sync-hub@work.path"]
                if action == "enable":
                    self.assertEqual(
                        writes,
                        [["enable", *triggers]]
                        + ([["start", *triggers]] if now else []),
                    )
                else:
                    all_triggers = [*triggers, "kp-sync-sync@work.timer"]
                    self.assertEqual(
                        writes,
                        [["disable", *all_triggers]]
                        + (
                            [
                                ["stop", *all_triggers],
                                ["stop", "kp-sync-sync@work.service"],
                                ["stop", "kp-sync@work.service"],
                            ]
                            if now
                            else []
                        ),
                    )

    def test_enable_requires_installed_units_and_propagates_start_failure(
        self,
    ):
        config = self.default_daemon_config()
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "not-found", "")

        with self.assertRaisesRegex(ServiceError, "service install daemon"):
            manage_daemon_service(config, "enable", runner=runner)
        self.assertFalse(any(argv[2] == "enable" for argv in calls))

        def failed(argv, **kwargs):
            return subprocess.CompletedProcess(
                argv,
                1 if argv[2] == "start" else 0,
                "loaded",
                "synthetic start failure",
            )

        with self.assertRaisesRegex(ServiceError, "synthetic start failure"):
            manage_daemon_service(config, "enable", now=True, runner=failed)

    def test_install_writes_only_units_unless_desktop_is_explicit(self):
        from kp_sync.integration import InstallPaths

        config = replace(self.config, profile="work")
        paths = InstallPaths(
            self.root / "units", self.root / "apps", self.root / "autostart"
        )
        run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        manage_daemon_service(config, "install", paths=paths, runner=run)
        self.assertTrue((paths.unit_dir / "kp-sync@work.service").is_file())
        self.assertTrue((paths.unit_dir / "kp-sync@.service").is_file())
        self.assertFalse((paths.unit_dir / "kp-sync.service").exists())
        self.assertFalse(paths.applications_dir.exists())
        self.assertFalse(paths.autostart_dir.exists())
        self.assertEqual(
            run.call_args.args[0], ["systemctl", "--user", "daemon-reload"]
        )
        self.assertEqual(run.call_count, 1)
        run.reset_mock()
        manage_daemon_service(
            config, "install", paths=paths, desktop=True, runner=run
        )
        self.assertIn(
            "--profile work run",
            (
                paths.applications_dir / "org.keepassxc.KeePassXC.desktop"
            ).read_text(),
        )
        self.assertTrue(
            (paths.autostart_dir / "kp-sync-keepassxc.desktop").is_file()
        )
        self.assertEqual(run.call_count, 1)

    def test_install_names_the_program_by_absolute_path(self):
        from kp_sync.integration import InstallPaths

        config = replace(self.config, profile="work")
        paths = InstallPaths(
            self.root / "units", self.root / "apps", self.root / "autostart"
        )
        run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        program = self.root / "bin" / "kp-sync"
        program.parent.mkdir()
        program.write_text("#!/bin/sh\n")
        program.chmod(0o755)
        with patch(
            "kp_sync.integration.installed_executable",
            return_value=str(program),
        ):
            manage_daemon_service(
                config, "install", paths=paths, desktop=True, runner=run
            )
        self.assertIn(
            f"ExecStart={program} --profile work daemon\n",
            (paths.unit_dir / "kp-sync@work.service").read_text(),
        )
        self.assertIn(
            f"Exec={program} --profile work run",
            (
                paths.applications_dir / "org.keepassxc.KeePassXC.desktop"
            ).read_text(),
        )

    def test_install_reports_every_problem_before_writing(self):
        from kp_sync.integration import InstallError, InstallPaths

        config = replace(self.config, profile="work")
        paths = InstallPaths(
            self.root / "units", self.root / "apps", self.root / "autostart"
        )
        for directory in (paths.unit_dir, paths.applications_dir):
            directory.mkdir()
            directory.chmod(0o775)
        run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        with (
            patch(
                "kp_sync.integration.installed_executable",
                return_value=str(self.root / "missing" / "kp-sync"),
            ),
            self.assertRaises(InstallError) as raised,
        ):
            manage_daemon_service(
                config, "install", paths=paths, desktop=True, runner=run
            )
        message = str(raised.exception)
        self.assertIn("missing/kp-sync is not an executable file", message)
        self.assertIn(f"group- or world-writable: {paths.unit_dir}", message)
        self.assertIn(
            f"group- or world-writable: {paths.applications_dir}", message
        )
        self.assertEqual(list(paths.unit_dir.iterdir()), [])
        self.assertFalse(paths.autostart_dir.exists())
        run.assert_not_called()

    def test_installed_executable_prefers_this_program_and_keeps_links(
        self,
    ):
        from kp_sync.integration import installed_executable

        link = self.root / "bin" / "kp-sync"
        link.parent.mkdir()
        target = self.root / "venv-kp-sync"
        target.write_text("#!/bin/sh\n")
        link.symlink_to(target)
        with patch("sys.argv", [str(link)]):
            self.assertEqual(installed_executable(), str(link))
        with (
            patch("sys.argv", ["/usr/lib/python3/unittest/__main__.py"]),
            patch("shutil.which", return_value="/opt/kp/bin/kp-sync"),
        ):
            self.assertEqual(installed_executable(), "/opt/kp/bin/kp-sync")
        for found in (None, "/opt/with space/kp-sync"):
            with (
                self.subTest(found=found),
                patch("sys.argv", ["python"]),
                patch("shutil.which", return_value=found),
            ):
                self.assertEqual(installed_executable(), "kp-sync")

    def test_install_reports_reload_failure_without_enabling(self):
        from kp_sync.integration import InstallPaths

        paths = InstallPaths(
            self.root / "units", self.root / "apps", self.root / "autostart"
        )
        run = Mock(return_value=subprocess.CompletedProcess([], 1, "", ""))
        with self.assertRaisesRegex(ServiceError, "files installed"):
            manage_daemon_service(
                self.default_daemon_config(),
                "install",
                paths=paths,
                runner=run,
            )
        self.assertTrue((paths.unit_dir / "kp-sync.service").is_file())
        run.assert_called_once()

    def test_uninstall_preserves_templates_profiles_and_runtime_data(self):
        from kp_sync.integration import (
            InstallPaths,
            render_units,
            render_desktop_entries,
        )

        config = replace(self.config, profile="work")
        paths = InstallPaths(
            self.root / "units",
            self.root / "applications",
            self.root / "autostart",
        )
        for directory in (
            paths.unit_dir,
            paths.applications_dir,
            paths.autostart_dir,
        ):
            directory.mkdir()
        rendered = render_units(config, executable="/venv/bin/kp-sync")
        for name, text in rendered.items():
            (paths.unit_dir / name).write_text(text)
        other = paths.unit_dir / "kp-sync@other.service"
        other.write_text("unrelated")
        desktop = render_desktop_entries(config)
        app = paths.applications_dir / "org.keepassxc.KeePassXC.desktop"
        app.write_text(desktop[app.name])
        auto = paths.autostart_dir / "kp-sync-keepassxc.desktop"
        auto.write_text(desktop[auto.name])
        recovery = self.root / "pending.json"
        recovery.write_text("keep")
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv[2:])
            if "--property=LoadState" in argv:
                text = "loaded"
            elif argv[2] == "is-active":
                text = "active"
            elif argv[2] == "show":
                text = "Result=success\nExecMainCode=1\nExecMainStatus=0"
            else:
                text = ""
            return subprocess.CompletedProcess(argv, 0, text, "")

        manage_daemon_service(config, "uninstall", paths=paths, runner=runner)
        self.assertFalse((paths.unit_dir / "kp-sync@work.service").exists())
        self.assertTrue((paths.unit_dir / "kp-sync@.service").exists())
        self.assertTrue(other.exists())
        self.assertTrue(app.exists())
        self.assertTrue(auto.exists())
        self.assertEqual(recovery.read_text(), "keep")
        self.assertEqual(calls[-1], ["daemon-reload"])
        manage_daemon_service(
            config, "uninstall", paths=paths, desktop=True, runner=runner
        )
        self.assertFalse(app.exists())
        self.assertFalse(auto.exists())
        self.assertTrue(other.exists())

    def test_uninstall_failure_preserves_files(self):
        from kp_sync.integration import InstallPaths, render_units

        config = self.default_daemon_config()
        paths = InstallPaths(
            self.root / "units", self.root / "apps", self.root / "autostart"
        )
        paths.unit_dir.mkdir()
        path = paths.unit_dir / "kp-sync.service"
        path.write_text(render_units(config)[path.name])

        def runner(argv, **kwargs):
            if argv[2] == "disable":
                return subprocess.CompletedProcess(
                    argv, 1, "", "synthetic disable failure"
                )
            return subprocess.CompletedProcess(argv, 0, "loaded", "")

        with self.assertRaisesRegex(ServiceError, "synthetic disable failure"):
            manage_daemon_service(
                config, "uninstall", paths=paths, runner=runner
            )
        self.assertTrue(path.exists())
        path.write_text("modified unit")
        run = Mock()
        with self.assertRaisesRegex(RuntimeError, "modified"):
            manage_daemon_service(config, "uninstall", paths=paths, runner=run)
        run.assert_not_called()
        path.unlink()
        path.symlink_to(self.path)
        with self.assertRaises(RuntimeError):
            manage_daemon_service(config, "uninstall", paths=paths, runner=run)
        run.assert_not_called()

    def test_uninstall_abnormal_shutdown_does_not_remove_units(self):
        from kp_sync.integration import InstallPaths, render_units

        config = self.default_daemon_config()
        paths = InstallPaths(
            self.root / "units", self.root / "apps", self.root / "autostart"
        )
        paths.unit_dir.mkdir()
        path = paths.unit_dir / "kp-sync.service"
        path.write_text(render_units(config)[path.name])
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv[2:])
            if "--property=LoadState" in argv:
                text = "loaded"
            elif argv[2] == "is-active":
                text = "active"
            elif argv[2] == "show":
                text = "Result=timeout\nExecMainCode=2\nExecMainStatus=9"
            else:
                text = ""
            return subprocess.CompletedProcess(argv, 0, text, "")

        with self.assertRaisesRegex(ServiceError, "shutdown was not clean"):
            manage_daemon_service(
                config, "uninstall", paths=paths, runner=runner
            )
        self.assertTrue(path.exists())
        self.assertNotIn(["daemon-reload"], calls)

    def test_uninstall_refuses_other_profile_desktop_and_changed_file(self):
        from kp_sync.integration import (
            InstallError,
            InstallPaths,
            render_desktop_entries,
            render_units,
            remove_installed_files,
            uninstall_files,
        )

        config = self.default_daemon_config()
        paths = InstallPaths(
            self.root / "units", self.root / "apps", self.root / "autostart"
        )
        paths.applications_dir.mkdir()
        app = paths.applications_dir / "org.keepassxc.KeePassXC.desktop"
        app.write_text(
            render_desktop_entries(replace(config, profile="other"))[app.name]
        )
        run = Mock()
        with self.assertRaisesRegex(InstallError, "other-profile"):
            manage_daemon_service(
                config, "uninstall", paths=paths, desktop=True, runner=run
            )
        run.assert_not_called()
        paths.unit_dir.mkdir()
        unit = paths.unit_dir / "kp-sync.service"
        unit.write_text(render_units(config)[unit.name])
        files = uninstall_files(config, paths=paths)[0]
        unit.write_text("changed after preflight")
        with self.assertRaisesRegex(InstallError, "changed"):
            remove_installed_files(files)
        self.assertEqual(unit.read_text(), "changed after preflight")
        self.assertTrue(app.exists())

    def test_missing_units_allow_idempotent_disable_and_uninstall(self):
        from kp_sync.integration import InstallPaths

        config = self.default_daemon_config()
        paths = InstallPaths(
            self.root / "units", self.root / "apps", self.root / "autostart"
        )
        for action in ("disable", "uninstall"):
            calls = []

            def runner(argv, **kwargs):
                calls.append(argv[2:])
                return subprocess.CompletedProcess(
                    argv, 0, "not-found" if argv[2] == "show" else "", ""
                )

            self.assertEqual(
                manage_daemon_service(
                    config,
                    action,
                    now=action == "disable",
                    paths=paths,
                    runner=runner,
                ),
                0,
            )
            self.assertFalse(
                any(call[0] in {"disable", "stop"} for call in calls)
            )
        self.assertFalse(paths.unit_dir.exists())

    def test_daemon_controls_reject_custom_config_before_systemctl(self):
        runner = Mock()
        with self.assertRaisesRegex(ServiceError, "--config"):
            manage_daemon_service(self.config, "start", runner=runner)
        runner.assert_not_called()

    def test_daemon_start_targets_named_profile_and_waits_for_readiness(self):
        config = replace(self.config, profile="work")
        calls = []
        ready = Mock(return_value=True)

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            if argv[2] == "is-active":
                return subprocess.CompletedProcess(argv, 3, "inactive\n", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        self.assertEqual(
            manage_daemon_service(
                config,
                "start",
                runner=runner,
                readiness=ready,
            ),
            0,
        )
        self.assertEqual(
            [call[0] for call in calls],
            [
                [
                    "systemctl",
                    "--user",
                    "is-active",
                    "kp-sync@work.service",
                ],
                ["systemctl", "--user", "start", "kp-sync@work.service"],
            ],
        )
        self.assertEqual(calls[1][1]["timeout"], 15)
        ready.assert_called_once_with(config, timeout=10)

    def test_daemon_start_is_idempotent_but_still_checks_readiness(self):
        calls = []
        ready = Mock(return_value=True)
        config = self.default_daemon_config()

        def runner(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "active\n", "")

        self.assertEqual(
            manage_daemon_service(
                config,
                "start",
                runner=runner,
                readiness=ready,
            ),
            0,
        )
        self.assertEqual(len(calls), 1)
        ready.assert_called_once_with(config, timeout=10)

    def test_daemon_stop_checks_shutdown_and_keeps_activation_enabled(self):
        calls = []
        timeouts = []
        config = self.default_daemon_config()

        def runner(argv, **kwargs):
            calls.append(argv)
            timeouts.append(kwargs["timeout"])
            if argv[2] == "is-active":
                return subprocess.CompletedProcess(argv, 0, "active\n", "")
            if argv[2] == "show":
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    "Result=success\nExecMainCode=1\n" "ExecMainStatus=0\n",
                    "",
                )
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch("kp_sync.services.info") as log:
            self.assertEqual(
                manage_daemon_service(config, "stop", runner=runner), 0
            )
        self.assertEqual(
            calls[1],
            ["systemctl", "--user", "stop", "kp-sync.service"],
        )
        self.assertIn("ExecMainStatus", " ".join(calls[2]))
        self.assertNotIn("disable", " ".join(calls[1]))
        self.assertEqual(timeouts[1], 195)
        self.assertIn("activation settings unchanged", log.call_args.args[0])

    def test_daemon_stop_propagates_forced_shutdown(self):
        config = self.default_daemon_config()

        def runner(argv, **kwargs):
            if argv[2] == "is-active":
                return subprocess.CompletedProcess(argv, 0, "active\n", "")
            if argv[2] == "show":
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    "Result=timeout\nExecMainCode=killed\n"
                    "ExecMainStatus=9\n",
                    "",
                )
            return subprocess.CompletedProcess(argv, 0, "", "")

        with self.assertRaisesRegex(ServiceError, "shutdown was not clean"):
            manage_daemon_service(config, "stop", runner=runner)

    def test_daemon_stop_detects_systemd_signal_exit_code(self):
        config = self.default_daemon_config()

        def runner(argv, **kwargs):
            if argv[2] == "is-active":
                return subprocess.CompletedProcess(argv, 0, "active\n", "")
            if argv[2] == "show":
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    "Result=success\nExecMainCode=2\n" "ExecMainStatus=9\n",
                    "",
                )
            return subprocess.CompletedProcess(argv, 0, "", "")

        with self.assertRaisesRegex(ServiceError, "terminated by signal 9"):
            manage_daemon_service(config, "stop", runner=runner)

    def test_daemon_stop_accepts_an_unloaded_unit_only_when_clean(self):
        config = self.default_daemon_config()

        def runner_with(shown):
            def runner(argv, **kwargs):
                if argv[2] == "is-active":
                    return subprocess.CompletedProcess(argv, 0, "active\n", "")
                if argv[2] == "show":
                    return subprocess.CompletedProcess(argv, 0, shown, "")
                return subprocess.CompletedProcess(argv, 0, "", "")

            return runner

        unloaded = (
            "Result=success\nExecMainCode=0\nExecMainStatus=0\n"
            "ActiveState=inactive\n"
        )
        with patch("kp_sync.services.info"):
            self.assertEqual(
                manage_daemon_service(
                    config, "stop", runner=runner_with(unloaded)
                ),
                0,
            )
        for shown in (
            unloaded.replace("ActiveState=inactive", "ActiveState=failed"),
            unloaded.replace("Result=success", "Result=timeout"),
            unloaded.replace("ActiveState=inactive", "ActiveState=active"),
            unloaded.replace("ActiveState=inactive\n", ""),
        ):
            with (
                self.subTest(shown=shown),
                self.assertRaises(ServiceError),
            ):
                manage_daemon_service(
                    config, "stop", runner=runner_with(shown)
                )

    def test_daemon_restart_stops_then_starts_and_checks_readiness(self):
        calls = []
        ready = Mock(return_value=True)
        config = self.default_daemon_config()

        def runner(argv, **kwargs):
            calls.append(argv)
            if argv[2] == "is-active":
                return subprocess.CompletedProcess(argv, 0, "active\n", "")
            if argv[2] == "show":
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    "Result=success\nExecMainCode=1\n" "ExecMainStatus=0\n",
                    "",
                )
            return subprocess.CompletedProcess(argv, 0, "", "")

        self.assertEqual(
            manage_daemon_service(
                config,
                "restart",
                runner=runner,
                readiness=ready,
            ),
            0,
        )
        self.assertEqual(
            calls[1:3],
            [
                ["systemctl", "--user", "stop", "kp-sync.service"],
                [
                    "systemctl",
                    "--user",
                    "show",
                    "--property=Result",
                    "--property=ExecMainCode",
                    "--property=ExecMainStatus",
                    "--property=ActiveState",
                    "--no-pager",
                    "kp-sync.service",
                ],
            ],
        )
        self.assertEqual(
            calls[3],
            ["systemctl", "--user", "start", "kp-sync.service"],
        )
        ready.assert_called_once_with(config, timeout=10)

    def test_daemon_start_fails_when_socket_does_not_become_ready(self):
        config = self.default_daemon_config()

        def runner(argv, **kwargs):
            if argv[2] == "is-active":
                return subprocess.CompletedProcess(argv, 3, "inactive\n", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with self.assertRaisesRegex(ServiceError, "did not become ready"):
            manage_daemon_service(
                config,
                "start",
                runner=runner,
                readiness=Mock(return_value=False),
            )

    def test_daemon_readiness_requires_a_protocol_status_round_trip(self):
        from kp_sync.daemon.protocol import Connection
        from kp_sync.services import _wait_for_daemon_socket

        client_socket, server_socket = socket.socketpair()
        client_connection = Connection(client_socket)
        server_connection = Connection(server_socket)
        with (
            patch("kp_sync.daemon.client.Client"),
            patch(
                "kp_sync.services._connect_daemon_socket",
                return_value=client_connection,
            ),
        ):

            def answer_status():
                request = server_connection.receive()
                self.assertEqual(request["command"], "status")
                self.assertTrue(request["wait"])
                self.assertEqual(request["ui"], "none")
                server_connection.send(
                    {"type": "result", "result": {"status": "ready"}}
                )

            worker = threading.Thread(target=answer_status)
            worker.start()
            self.assertTrue(_wait_for_daemon_socket(self.config, timeout=1))
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive())
        server_connection.close()

    def test_daemon_readiness_rejects_listener_without_protocol_reply(self):
        from kp_sync.daemon.protocol import Connection
        from kp_sync.services import _wait_for_daemon_socket

        client_socket, listener_socket = socket.socketpair()
        connection = Connection(client_socket)
        with (
            patch("kp_sync.daemon.client.Client"),
            patch(
                "kp_sync.services._connect_daemon_socket",
                return_value=connection,
            ) as connect,
            patch("kp_sync.services._DAEMON_SOCKET_TIMEOUT", 0.02),
        ):
            self.assertFalse(
                _wait_for_daemon_socket(self.config, timeout=0.05)
            )
        listener_socket.close()
        connect.assert_called_once()
        self.assertLessEqual(connect.call_args.kwargs["timeout"], 0.02)

    def test_daemon_readiness_rejects_protocol_error_reply(self):
        from kp_sync.daemon.protocol import Connection
        from kp_sync.services import _wait_for_daemon_socket

        client_socket, server_socket = socket.socketpair()
        client_connection = Connection(client_socket)
        server_connection = Connection(server_socket)
        with (
            patch("kp_sync.daemon.client.Client"),
            patch(
                "kp_sync.services._connect_daemon_socket",
                return_value=client_connection,
            ),
        ):
            server_connection.send(
                {"type": "error", "message": "status unavailable"}
            )
            with self.assertRaisesRegex(ServiceError, "status unavailable"):
                _wait_for_daemon_socket(self.config, timeout=1)
        server_connection.close()


if __name__ == "__main__":
    unittest.main()
