import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path

from kp_sync.cli import COMMANDS, main
from kp_sync.paths import atomic_write, ensure_run_dir, run_dir


class SkeletonTest(unittest.TestCase):
    def test_daemon_service_cli_routes_without_changing_named_syntax(self):
        from unittest.mock import patch

        with (
            patch("kp_sync.cli.load_config") as load,
            patch(
                "kp_sync.services.manage_daemon_service", return_value=0
            ) as daemon,
            patch("kp_sync.services.manage_service", return_value=0) as named,
        ):
            for action in ("start", "stop", "restart", "status"):
                self.assertEqual(main(["service", action, "daemon"]), 0)
                daemon.assert_called_with(load.return_value, action)
            self.assertEqual(
                main(["service", "start", "ssh-session", "remote"]), 0
            )
            named.assert_called_once_with(
                load.return_value, "start", "ssh-session", "remote"
            )
            for action, option in (
                ("enable", "--now"),
                ("disable", "--now"),
                ("uninstall", "--desktop"),
                ("install", "--desktop"),
            ):
                self.assertEqual(
                    main(["service", action, "daemon", option]), 0
                )
                daemon.assert_called_with(
                    load.return_value,
                    action,
                    now=option == "--now",
                    desktop=option == "--desktop",
                )
            for args in (
                ["service", "start", "daemon", "extra"],
                ["service", "stop", "ssh-session"],
                ["service", "restart", "access-owner", "office"],
                ["service", "enable", "ssh-session", "remote"],
                ["service", "disable", "access-owner", "office"],
                ["service", "uninstall", "ssh-session", "remote"],
                ["service", "install", "access-owner", "office"],
            ):
                load.reset_mock()
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main(args), 1)
                load.assert_not_called()

    def test_version_exits_without_loading_configuration(self) -> None:
        from unittest.mock import patch
        from kp_sync import __version__

        output = io.StringIO()
        with (
            patch("kp_sync.cli.load_config") as load,
            contextlib.redirect_stdout(output),
        ):
            with self.assertRaises(SystemExit) as raised:
                main(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(output.getvalue(), f"kp-sync {__version__}\n")
        load.assert_not_called()

    def test_help_lists_all_commands(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                main(["--help"])

        self.assertEqual(raised.exception.code, 0)
        for command, _ in COMMANDS:
            self.assertIn(command, output.getvalue())

    def test_service_help_includes_daemon_and_named_resources(self) -> None:
        for args in (["--help"], ["service", "--help"]):
            with self.subTest(args=args):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    with self.assertRaises(SystemExit) as raised:
                        main(args)
                self.assertEqual(raised.exception.code, 0)
                text = " ".join(output.getvalue().split())
                self.assertIn(
                    "Manage the daemon, named SSH sessions and "
                    "kp-access owners",
                    text,
                )

    def test_service_run_help_identifies_internal_use(self) -> None:
        for args in (["--help"], ["service-run", "--help"]):
            with self.subTest(args=args):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    with self.assertRaises(SystemExit) as raised:
                        main(args)
                self.assertEqual(raised.exception.code, 0)
                self.assertIn("service-run", output.getvalue())
                self.assertIn("internal use only", output.getvalue())
                self.assertNotIn("==SUPPRESS==", output.getvalue())

    def test_removed_install_units_command_does_not_load_configuration(self):
        from unittest.mock import patch

        with (
            patch("kp_sync.cli.load_config") as load,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as raised:
                main(["install-units"])
        self.assertEqual(raised.exception.code, 2)
        load.assert_not_called()

    def test_commands_are_not_implemented_yet(self) -> None:
        for command, _ in COMMANDS:
            if command in {
                "sync",
                "status",
                "resolve",
                "forget",
                "keepassxc-disconnect",
                "daemon",
                "run",
                "up",
                "attach",
                "detach",
                "eject",
                "down",
                "drive-sync",
                "drive-setup",
                "drive-retire",
                "completion",
                "doctor",
                "session",
                "service",
                "sftp-serve",
                "access-op",
                "restore",
                "history",
                "retire-session",
                "cleanup",
                "repair-history",
            }:
                continue
            with self.subTest(command=command):
                output = io.StringIO()
                with contextlib.redirect_stderr(output):
                    with self.assertRaises(SystemExit) as raised:
                        main([command])
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("not implemented yet", output.getvalue())

    def test_repair_apply_requires_confirmation_before_loading_config(
        self,
    ) -> None:
        from unittest.mock import patch

        output = io.StringIO()
        with (
            patch("kp_sync.cli.load_config") as load,
            contextlib.redirect_stderr(output),
        ):
            self.assertEqual(main(["repair-history", "--apply"]), 2)
        load.assert_not_called()
        self.assertIn("--confirm-writers-paused", output.getvalue())

    def test_retirement_confirmation_mismatch_does_not_load_config(
        self,
    ) -> None:
        from unittest.mock import patch

        output = io.StringIO()
        with (
            patch("kp_sync.cli.load_config") as load,
            contextlib.redirect_stderr(output),
        ):
            status = main(
                [
                    "retire-session",
                    "--identity",
                    "session:remote:aabbccdd",
                    "--confirm",
                    "session:remote:11223344",
                ]
            )
        self.assertEqual(status, 2)
        self.assertIn(
            "--confirm must exactly match --identity", output.getvalue()
        )
        load.assert_not_called()

    def test_profile_run_directories_and_private_atomic_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir)
            self.assertEqual(
                run_dir(xdg_runtime_dir=runtime), runtime / "kp-sync"
            )
            self.assertEqual(
                run_dir("work", xdg_runtime_dir=runtime),
                runtime / "kp-sync-work",
            )

            default_dir = ensure_run_dir(xdg_runtime_dir=runtime)
            named_dir = ensure_run_dir("work", xdg_runtime_dir=runtime)
            self.assertEqual(os.stat(default_dir).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(named_dir).st_mode & 0o777, 0o700)

            target = default_dir / "state.json"
            atomic_write(target, '{"ok": true}\n')
            self.assertEqual(
                target.read_text(encoding="utf-8"), '{"ok": true}\n'
            )
            self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)

    def test_invalid_profile_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            run_dir("../unsafe", xdg_runtime_dir="/run/user/1000")

    def test_sync_uses_daemon_unless_explicitly_disabled(self) -> None:
        from unittest.mock import patch
        from kp_sync.config import load_config

        with tempfile.TemporaryDirectory() as root:
            config = load_config(
                environ={"HOME": root, "XDG_RUNTIME_DIR": root}
            )
            with (
                patch("kp_sync.cli.load_config", return_value=config),
                patch.dict(os.environ, {"XDG_RUNTIME_DIR": root}),
                patch(
                    "kp_sync.daemon.client.Client.request",
                    return_value={"action": "unchanged"},
                ) as request,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(main(["sync"]), 0)
            request.assert_called_once_with(
                "sync", wait=True, ui="auto", dry_run=False
            )

    def test_unavailable_sync_returns_failure_even_without_missing_endpoints(
        self,
    ):
        from unittest.mock import patch
        from kp_sync.config import load_config
        from kp_sync.engine import SyncResult

        with tempfile.TemporaryDirectory() as root:
            config = load_config(
                environ={"HOME": root, "XDG_RUNTIME_DIR": root}
            )
            result = SyncResult(
                current=None,
                action="unavailable",
                classifications={"hub:test": "missing"},
            )
            with (
                patch("kp_sync.cli.load_config", return_value=config),
                patch.dict(os.environ, {"XDG_RUNTIME_DIR": root}),
                patch(
                    "kp_sync.daemon.client.Client.request",
                    return_value=result.to_dict(),
                ),
                patch("kp_sync.cli.SyncEngine.sync", return_value=result),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                for args in (["sync"], ["--no-daemon", "sync"]):
                    with self.subTest(args=args):
                        self.assertEqual(main(args), 4)

    def test_keepassxc_disconnect_is_local_and_profile_scoped(self):
        from unittest.mock import patch
        from kp_sync.config import load_config

        with tempfile.TemporaryDirectory() as root:
            config = load_config(
                environ={"HOME": root, "XDG_RUNTIME_DIR": root}
            )
            with (
                patch("kp_sync.cli.load_config", return_value=config),
                patch("kp_sync.keepassxc.disconnect") as disconnect,
                patch("kp_sync.daemon.client.Client.request") as request,
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(main(["keepassxc-disconnect"]), 0)
                disconnect.assert_called_once_with(config)
                request.assert_not_called()
                self.assertIn("Revoke its connection", output.getvalue())

    def test_run_raise_existing_forces_interactive_request_without_tty(self):
        from unittest.mock import patch
        from kp_sync.config import load_config

        with tempfile.TemporaryDirectory() as root:
            config = load_config(
                environ={"HOME": root, "XDG_RUNTIME_DIR": root}
            )
            with (
                patch("kp_sync.cli.load_config", return_value=config),
                patch.dict(os.environ, {"XDG_RUNTIME_DIR": root}),
                patch("kp_sync.cli.sys.stdin") as stdin,
                patch(
                    "kp_sync.daemon.client.Client.request",
                    return_value={"action": "unchanged"},
                ) as request,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                stdin.isatty.return_value = False
                self.assertEqual(
                    main(["run", "--raise-existing", "--app", "keepassxc"]),
                    0,
                )
            request.assert_called_once_with(
                "run",
                wait=True,
                ui="auto",
                app="keepassxc",
                args=[],
                interactive=True,
            )

    def test_doctor_self_test_is_explicit_and_sftp_diagnostic_is_routed(self):
        from unittest.mock import patch
        from kp_sync.integration import Check

        with (
            patch("kp_sync.cli.load_config") as config,
            patch(
                "kp_sync.integration.doctor",
                return_value=[Check("test", True, "ok")],
            ) as doctor,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(["doctor"]), 0)
            doctor.assert_called_with(config.return_value, self_test=False)
            self.assertEqual(
                main(["--profile", "test", "doctor", "--self-test"]), 0
            )
            doctor.assert_called_with(config.return_value, self_test=True)
        with patch(
            "kp_sync.sessions.sftp_serve.serve", return_value=1
        ) as serve:
            self.assertEqual(
                main(
                    [
                        "--profile",
                        "test",
                        "sftp-serve",
                        "--diagnostic",
                        "aabbccdd",
                    ]
                ),
                1,
            )
            serve.assert_called_once_with("test", "aabbccdd", diagnostic=True)

    def test_restore_and_history_cli_routes(self):
        from unittest.mock import patch
        from types import SimpleNamespace

        config = SimpleNamespace(ui={"mode": "auto"})
        output = io.StringIO()
        with (
            patch("kp_sync.cli.load_config", return_value=config),
            patch("kp_sync.daemon.client.Client") as client,
            contextlib.redirect_stdout(output),
        ):
            client.return_value.request.return_value = {"action": "committed"}
            self.assertEqual(main(["restore", "--version", "a" * 64]), 0)
        client.return_value.request.assert_called_once_with(
            "restore",
            wait=True,
            ui="auto",
            dry_run=False,
            source_kind="version",
            source_value="a" * 64,
            restore_mode="merge",
        )

        output = io.StringIO()
        with (
            patch("kp_sync.cli.load_config", return_value=config),
            patch(
                "kp_sync.history_command.history_text",
                return_value="history\n",
            ),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(main(["history"]), 0)
        self.assertEqual(output.getvalue(), "history\n")

    def test_service_cli_routes_named_lifecycle(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        with (
            patch(
                "kp_sync.cli.load_config", return_value=SimpleNamespace()
            ) as load,
            patch("kp_sync.services.manage_service", return_value=0) as manage,
        ):
            self.assertEqual(
                main(
                    [
                        "--profile",
                        "work",
                        "service",
                        "start",
                        "ssh-session",
                        "home",
                    ]
                ),
                0,
            )
        load.assert_called_once_with(None, profile="work")
        manage.assert_called_once_with(
            load.return_value,
            "start",
            "ssh-session",
            "home",
        )

    def test_service_completion_reads_names_from_selected_config(self):
        from types import SimpleNamespace
        from kp_sync.cli import _service_name_completer

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.toml"
            path.write_text(
                '[ssh_sessions.office]\ntarget = "host"\n'
                '[access_owners.windows]\ncommand = ["kp-access-owner"]\n',
                encoding="utf-8",
            )
            parsed = SimpleNamespace(
                config=str(path),
                profile=None,
                resource="ssh-session",
            )
            self.assertEqual(_service_name_completer("of", parsed), ["office"])
            parsed.resource = "access-owner"
            self.assertEqual(
                _service_name_completer("wi", parsed), ["windows"]
            )

    def test_run_directory_does_not_follow_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir)
            outside = runtime / "outside"
            outside.mkdir()
            outside.chmod(0o755)
            os.symlink(outside, runtime / "kp-sync")

            with self.assertRaises(RuntimeError):
                ensure_run_dir(xdg_runtime_dir=runtime)

            self.assertEqual(os.stat(outside).st_mode & 0o777, 0o755)


if __name__ == "__main__":
    unittest.main()
