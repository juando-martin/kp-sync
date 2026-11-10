# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
import io
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from kp_sync.ui.factory import create_ui
from kp_sync.ui.gui import CustomCommandUI, KDialogUI, ZenityUI
from kp_sync.ui.none import NoneUI
from kp_sync.ui.tty import TTYUI
from kp_sync.ui.progress import SyncProgress


class TerminalInput(io.StringIO):
    def isatty(self):
        return True


class UITest(unittest.TestCase):
    def test_progress_is_delayed_and_reaped_even_when_sync_fails(self):
        with (
            patch("kp_sync.ui.progress.threading.Timer") as timer,
            patch(
                "kp_sync.ui.progress.subprocess.Popen",
            ) as popen,
        ):
            process = popen.return_value
            process.poll.return_value = None
            with self.assertRaisesRegex(RuntimeError, "sync failed"):
                with SyncProgress(enabled=True):
                    popen.assert_not_called()
                    timer.call_args.args[1]()
                    raise RuntimeError("sync failed")
            process.terminate.assert_called_once()
            process.wait.assert_called_once_with(timeout=2)
            process.stdin.close.assert_called_once()

    def test_fast_disabled_and_closed_progress_never_launch_late(self):
        with (
            patch("kp_sync.ui.progress.threading.Timer") as timer,
            patch(
                "kp_sync.ui.progress.subprocess.Popen",
            ) as popen,
        ):
            with SyncProgress(enabled=False):
                pass
            timer.assert_not_called()
            with SyncProgress(enabled=True):
                callback = timer.call_args.args[1]
            callback()
            popen.assert_not_called()

    def test_progress_pauses_for_decisions_and_restarts_delay(self):
        with (
            patch("kp_sync.ui.progress.threading.Timer") as timer,
            patch(
                "kp_sync.ui.progress.subprocess.Popen",
            ) as popen,
        ):
            popen.return_value.poll.return_value = None
            with SyncProgress(enabled=True) as progress:
                timer.call_args.args[1]()
                with progress.paused():
                    popen.return_value.terminate.assert_called_once()
                    self.assertIsNone(progress.timer)
                self.assertEqual(timer.call_count, 2)

    def test_hiding_progress_does_not_interrupt_work_or_reopen_dialog(self):
        with (
            patch("kp_sync.ui.progress.threading.Timer") as timer,
            patch(
                "kp_sync.ui.progress.subprocess.Popen",
            ) as popen,
            patch("kp_sync.ui.progress.error") as log,
        ):
            process = popen.return_value
            process.poll.return_value = 1
            completed = []
            with SyncProgress(enabled=True):
                timer.call_args.args[1]()
                argv = popen.call_args.args[0]
                self.assertIn("--cancel-label=Hide", argv)
                self.assertNotIn("--no-cancel", argv)
                self.assertNotIn("--auto-kill", argv)
                completed.append("sync completed after dialog closed")
            self.assertEqual(completed, ["sync completed after dialog closed"])
            popen.assert_called_once()
            process.terminate.assert_not_called()
            process.kill.assert_not_called()
            process.stdin.close.assert_called_once()
            log.assert_not_called()

    def test_progress_message_names_profile_and_database(self):
        with (
            patch("kp_sync.ui.progress.threading.Timer") as timer,
            patch(
                "kp_sync.ui.progress.subprocess.Popen",
            ) as popen,
        ):
            popen.return_value.poll.return_value = 0
            with SyncProgress(
                enabled=True,
                database_info=(
                    "Profile: work | Database: /run/user/1000/work/pass.kdbx"
                ),
            ):
                timer.call_args.args[1]()
            argv = popen.call_args.args[0]
            text = next(
                argument.removeprefix("--text=")
                for argument in argv
                if argument.startswith("--text=")
            )
            self.assertEqual(
                text.splitlines(),
                [
                    "Synchronizing KeePass copies",
                    "Hiding this window will not stop Synchronization",
                    "Profile: work | Database: /run/user/1000/work/pass.kdbx",
                ],
            )
            self.assertIn("--width=420", argv)

    def test_missing_progress_tool_is_logged_without_failing_sync(self):
        with (
            patch("kp_sync.ui.progress.threading.Timer") as timer,
            patch(
                "kp_sync.ui.progress.subprocess.Popen",
                side_effect=FileNotFoundError("zenity"),
            ),
            patch("kp_sync.ui.progress.error") as log,
        ):
            with SyncProgress(enabled=True):
                timer.call_args.args[1]()
            log.assert_called_once()

    def test_tty_review_reads_injected_terminal_and_masks_secrets(self):
        output = io.StringIO()
        ui = TTYUI(input_stream=TerminalInput("2\n"), output_stream=output)
        self.assertEqual(
            ui.review(
                [
                    {
                        "id": "entry:password",
                        "label": "Password",
                        "secret": True,
                        "base": "base-secret",
                        "hub": "hub-secret",
                        "other": "other-secret",
                    }
                ]
            ),
            {"entry:password": "other"},
        )
        self.assertNotIn("secret", output.getvalue())
        self.assertIn("[hidden]", output.getvalue())

    def test_none_eof_and_timeouts_defer(self):
        none = NoneUI()
        self.assertIsNone(none.confirm("overwrite?"))
        self.assertIsNone(none.review([{"id": "entry"}]))
        tty = TTYUI(
            input_stream=TerminalInput(""), output_stream=io.StringIO()
        )
        self.assertIsNone(tty.confirm("overwrite?", default=True))
        with patch(
            "kp_sync.ui.gui.subprocess.run",
            side_effect=subprocess.TimeoutExpired("zenity", 1),
        ):
            self.assertIsNone(ZenityUI(timeout=1).confirm("overwrite?"))
        with (
            patch.object(tty.input, "fileno", return_value=42),
            patch(
                "kp_sync.ui.tty.select.select",
                return_value=([], [], []),
            ),
        ):
            self.assertIsNone(tty.confirm("overwrite?", default=True))

    def test_auto_backend_choice(self):
        with (
            patch("sys.stdin.isatty", return_value=False),
            patch.dict(os.environ, {}, clear=True),
        ):
            self.assertIsInstance(create_ui("auto"), NoneUI)
            os.environ["DISPLAY"] = ":test"
            self.assertIsInstance(create_ui("auto"), ZenityUI)
        with patch("sys.stdin.isatty", return_value=True):
            self.assertIsInstance(create_ui("auto"), TTYUI)

    def test_gui_backend_factory_accepts_kdialog_and_custom_argv(self):
        self.assertIsInstance(create_ui("gui", gui="kdialog"), KDialogUI)
        custom = create_ui("gui", gui=["my-dialog", "--json"])
        self.assertIsInstance(custom, CustomCommandUI)
        self.assertEqual(custom.command, ["my-dialog", "--json"])

    def test_kdialog_maps_responses_and_preserves_arguments(self):
        with patch(
            "kp_sync.ui.gui.subprocess.run",
            side_effect=[
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 1, "", ""),
                subprocess.CompletedProcess([], 0, "1\n", ""),
                subprocess.CompletedProcess([], 0, "secret value\n", ""),
                subprocess.CompletedProcess([], 0, "/tmp/key file\n", ""),
            ],
        ) as run:
            ui = KDialogUI()
            self.assertTrue(ui.confirm("continue?"))
            self.assertFalse(ui.confirm("continue?"))
            self.assertEqual(
                ui.choose("pick one", ["first", "second"]), "second"
            )
            self.assertEqual(ui.password("enter password"), "secret value")
            self.assertEqual(ui.file_path("select key"), Path("/tmp/key file"))
        self.assertEqual(
            run.call_args_list[0].args[0],
            ["kdialog", "--title", "KeePass sync", "--yesno", "continue?"],
        )
        self.assertNotIn("secret value", run.call_args_list[3].args[0])

    def test_kdialog_cancel_and_invalid_choice_are_safe(self):
        with patch(
            "kp_sync.ui.gui.subprocess.run",
            side_effect=[
                subprocess.CompletedProcess([], 2, "", ""),
                subprocess.CompletedProcess([], 0, "not-an-index\n", ""),
            ],
        ):
            ui = KDialogUI()
            self.assertIsNone(ui.confirm("continue?"))
            with self.assertRaisesRegex(RuntimeError, "invalid menu choice"):
                ui.choose("pick one", ["first"])

    def test_custom_command_uses_json_protocol_without_shell_or_password_args(
        self,
    ):
        with patch(
            "kp_sync.ui.gui.subprocess.run",
            side_effect=[
                subprocess.CompletedProcess([], 0, "true\n", ""),
                subprocess.CompletedProcess(
                    [], 0, '"password with spaces"\n', ""
                ),
                subprocess.CompletedProcess([], 0, '"/tmp/key file"\n', ""),
            ],
        ) as run:
            ui = CustomCommandUI(["dialog-helper", "--json"])
            self.assertTrue(ui.confirm("continue?", default=False))
            self.assertEqual(
                ui.password("database password"), "password with spaces"
            )
            self.assertEqual(
                ui.file_path("select a key"), Path("/tmp/key file")
            )
        self.assertEqual(
            run.call_args_list[0].args[0], ["dialog-helper", "--json"]
        )
        self.assertEqual(
            run.call_args_list[0].kwargs["input"],
            '{"operation": "confirm", "prompt": "continue?", '
            '"default": false}\n',
        )
        self.assertNotIn(
            "password with spaces", str(run.call_args_list[1].args[0])
        )
        self.assertNotIn(
            "password with spaces", run.call_args_list[1].kwargs["input"]
        )

    def test_custom_command_invalid_choice_and_timeout_defer(self):
        with patch(
            "kp_sync.ui.gui.subprocess.run",
            side_effect=[
                subprocess.CompletedProcess([], 0, '"unexpected"\n', ""),
                subprocess.TimeoutExpired("dialog-helper", 1),
            ],
        ):
            ui = CustomCommandUI(["dialog-helper"])
            with self.assertRaisesRegex(RuntimeError, "unknown choice"):
                ui.choose("pick one", ["known"])
            self.assertIsNone(ui.confirm("continue?"))
