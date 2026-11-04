"""Tests for message levels, --verbose/--debug and their safety."""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from kp_sync import cli, log
from kp_sync.classify import Classification, CopyState
from kp_sync.endpoints.base import EndpointFingerprint, EndpointUnavailable
from kp_sync.endpoints.command import CommandEndpoint
from kp_sync.engine import _debug_classification
from kp_sync.history import CopyRecord


def _capture(function, *args, **kwargs):
    stream = io.StringIO()
    with contextlib.redirect_stderr(stream):
        function(*args, **kwargs)
    return stream.getvalue()


class LevelTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(log.restore_settings, log.settings())

    def test_details_are_hidden_only_on_an_interactive_terminal(self):
        log.configure(interactive=True, environ={})
        self.assertEqual(_capture(log.detail, "routine"), "")
        self.assertEqual(_capture(log.info, "outcome"), "INFO: outcome\n")
        self.assertIn("ERROR: bad", _capture(log.error, "bad"))
        log.configure(interactive=True, verbose=True, environ={})
        self.assertEqual(_capture(log.detail, "routine"), "INFO: routine\n")
        log.configure(interactive=False, environ={})
        self.assertEqual(_capture(log.detail, "routine"), "INFO: routine\n")

    def test_debug_needs_the_flag_or_environment_and_implies_verbose(self):
        log.configure(interactive=True, environ={})
        self.assertEqual(_capture(log.debug, "why"), "")
        self.assertFalse(log.debug_enabled())
        log.configure(interactive=True, debug=True, environ={})
        self.assertEqual(_capture(log.debug, "why"), "DEBUG: why\n")
        self.assertEqual(_capture(log.detail, "routine"), "INFO: routine\n")
        for value, expected in (
            ("1", True),
            ("yes", True),
            ("", False),
            ("0", False),
            ("False", False),
            ("off", False),
        ):
            with self.subTest(value=value):
                log.configure(environ={log.DEBUG_VARIABLE: value})
                self.assertEqual(log.debug_enabled(), expected)


class CliLoggingTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(log.restore_settings, log.settings())

    def configured(self, argv, terminal):
        args = cli._build_parser().parse_args(argv)
        with (
            patch("kp_sync.cli._stderr_is_terminal", return_value=terminal),
            patch("kp_sync.log.configure") as configure,
        ):
            cli._configure_logging(args)
        return configure.call_args.kwargs

    def test_only_terminal_user_commands_hide_details(self):
        self.assertTrue(
            self.configured(["status"], terminal=True)["interactive"]
        )
        self.assertFalse(
            self.configured(["status"], terminal=False)["interactive"]
        )
        for argv in (
            ["daemon"],
            ["access-op"],
            ["sftp-serve", "ID"],
            ["service-run", "ssh-session", "office"],
            ["drive-sync", "up", "--mode", "main", "--drive-id", "S:U"],
        ):
            with self.subTest(argv=argv):
                self.assertFalse(
                    self.configured(argv, terminal=True)["interactive"]
                )

    def test_flags_are_passed_in_either_position_for_the_daemon(self):
        self.assertTrue(
            self.configured(["--verbose", "sync"], True)["verbose"]
        )
        self.assertTrue(self.configured(["--debug", "sync"], True)["debug"])
        self.assertTrue(self.configured(["daemon", "--debug"], True)["debug"])
        self.assertFalse(self.configured(["sync"], True)["debug"])

    def test_main_restores_settings_and_nested_commands_keep_them(self):
        log.configure(interactive=False, environ={})
        before = log.settings()
        with (
            patch("kp_sync.cli._stderr_is_terminal", return_value=True),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            cli.main([])
        self.assertEqual(log.settings(), before)
        seen = []
        with patch(
            "kp_sync.cli.main",
            side_effect=lambda argv: seen.append(cli._nested.active) or 0,
        ):
            self.assertEqual(cli._submit_nested(["up"]), 0)
        self.assertEqual(seen, [True])
        self.assertFalse(cli._nested.active)


class DebugContentTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(log.restore_settings, log.settings())
        log.configure(debug=True, environ={})

    def test_command_endpoint_timing_never_shows_arguments(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            fetch = [
                sys.executable,
                "-c",
                "import sys; sys.exit(3)",
                "token-SECRET",
                "{file}",
            ]
            endpoint = CommandEndpoint("nas", fetch, ["true", "{file}"])
            stream = io.StringIO()
            with (
                contextlib.redirect_stderr(stream),
                self.assertRaises(EndpointUnavailable),
            ):
                endpoint.fetch(Path(tmp) / "copy")
        output = stream.getvalue()
        self.assertRegex(output, r"DEBUG: command fetch for nas: [0-9.]+s")
        self.assertIn("(failed)", output)
        self.assertNotIn("SECRET", output)
        self.assertNotIn(tmp, output)

    def test_classification_explains_each_copy(self):
        version = "a" * 64
        resave = "b" * 64
        record = CopyRecord(version, 1, "2026-11-04T00:00:00Z", "host")
        downloaded = SimpleNamespace(
            sha256=resave,
            fingerprint=EndpointFingerprint(resave),
            local_path=Path("/run/copy"),
        )
        listed = SimpleNamespace(
            sha256=version,
            fingerprint=EndpointFingerprint(version, token="t"),
            local_path=None,
        )
        missing = SimpleNamespace(
            sha256=None, fingerprint=None, local_path=None
        )
        output = _capture(
            _debug_classification,
            {
                "hub:host": Classification(
                    "hub:host", CopyState.UNCHANGED, version, record
                ),
                "cloud:remote": Classification(
                    "cloud:remote", CopyState.UNCHANGED, version, record
                ),
                "drive:S:U": Classification(
                    "drive:S:U", CopyState.MISSING, None, None
                ),
            },
            {
                "hub:host": downloaded,
                "cloud:remote": listed,
                "drive:S:U": missing,
            },
            {"hub:host": version, "cloud:remote": version, "drive:S:U": None},
        )
        self.assertIn(
            "DEBUG: copy hub:host: unchanged (observed bbbbbbbbbbbb, same "
            "content as aaaaaaaaaaaa, recorded aaaaaaaaaaaa, content read)",
            output,
        )
        self.assertIn(
            "copy cloud:remote: unchanged (observed aaaaaaaaaaaa, recorded "
            "aaaaaaaaaaaa, identified without download)",
            output,
        )
        self.assertIn(
            "copy drive:S:U: missing (observed none, recorded none, "
            "not observed)",
            output,
        )

    def test_daemon_requests_show_field_names_not_values(self):
        from kp_sync.daemon.server import Controller

        controller = SimpleNamespace(_handle=Mock(return_value={"ok": 1}))
        output = _capture(
            Controller.handle,
            controller,
            {"command": "sync", "ui": "none", "mountpoint": "/media/SECRET"},
        )
        self.assertIn("DEBUG: request sync (mountpoint, ui)", output)
        self.assertNotIn("SECRET", output)


if __name__ == "__main__":
    unittest.main()
