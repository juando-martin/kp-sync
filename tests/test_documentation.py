# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Offline reference generation and documentation-tool safety tests."""

import contextlib
import importlib.util
import io
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools import docs

ROOT = Path(__file__).resolve().parents[1]


class DocumentationTest(unittest.TestCase):
    def test_build_uses_strict_fresh_sphinx_invocation(self):
        output = ROOT / "docs" / "_build" / "test-html"
        with patch("tools.docs.subprocess.run") as run:
            docs.build(output)
        argv = run.call_args.args[0]
        for flag in ("-E", "-a", "-n", "-W", "--keep-going"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[-2:], [str(ROOT / "docs"), str(output)])
        self.assertTrue(run.call_args.kwargs["check"])

    def test_preview_binds_only_loopback_and_closes_on_interrupt(self):
        server = Mock(server_port=8123)
        server.serve_forever.side_effect = KeyboardInterrupt
        manager = Mock()
        manager.__enter__ = Mock(return_value=server)
        manager.__exit__ = Mock(return_value=False)
        with (
            patch("tools.docs.build") as build,
            patch(
                "tools.docs.ThreadingHTTPServer", return_value=manager
            ) as factory,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(docs.main(["serve", "--port", "8123"]), 0)
        build.assert_called_once()
        self.assertEqual(factory.call_args.args[0], ("127.0.0.1", 8123))
        manager.__exit__.assert_called_once()

    def test_build_resolves_relative_output_before_changing_working_dir(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            contextlib.chdir(temp),
            patch("tools.docs.subprocess.run") as run,
        ):
            docs.build(Path("html"))
            self.assertEqual(
                run.call_args.args[0][-1], str(Path(temp) / "html")
            )
            self.assertEqual(run.call_args.kwargs["cwd"], ROOT)

    def test_failed_build_is_not_served_or_reported_as_success(self):
        with (
            patch(
                "tools.docs.build",
                side_effect=subprocess.CalledProcessError(1, ["sphinx"]),
            ),
            patch("tools.docs.ThreadingHTTPServer") as serve,
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            self.assertEqual(docs.main(["serve"]), 1)
        serve.assert_not_called()
        self.assertIn("ERROR:", errors.getvalue())

    @unittest.skipUnless(
        importlib.util.find_spec("sphinx"), "optional docs extra not installed"
    )
    def test_html_build_is_offline_and_never_activates_profiles(self):
        from sphinx.application import Sphinx

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            warnings = io.StringIO()
            forbidden = AssertionError("documentation must be read-only")
            with (
                patch(
                    "kp_sync.cli.load_config",
                    side_effect=forbidden,
                    autospec=True,
                ),
                patch(
                    "kp_sync.config.load_config",
                    side_effect=forbidden,
                    autospec=True,
                ),
                patch(
                    "kp_sync.engine.SyncEngine.__init__",
                    side_effect=forbidden,
                    autospec=True,
                ),
                patch(
                    "kp_sync.credentials.CredentialProvider.__init__",
                    side_effect=forbidden,
                    autospec=True,
                ),
                patch(
                    "kp_sync.daemon.client.Client.__init__",
                    side_effect=forbidden,
                    autospec=True,
                ),
                patch("subprocess.Popen", side_effect=forbidden),
                patch.object(socket.socket, "connect", side_effect=forbidden),
            ):
                app = Sphinx(
                    srcdir=str(ROOT / "docs"),
                    confdir=str(ROOT / "docs"),
                    outdir=str(root / "html"),
                    doctreedir=str(root / "doctrees"),
                    buildername="html",
                    status=io.StringIO(),
                    warning=warnings,
                    freshenv=True,
                    warningiserror=True,
                    confoverrides={"nitpicky": True},
                )
                app.build(force_all=True)
            self.assertEqual(app.statuscode, 0, warnings.getvalue())
            self.assertEqual(warnings.getvalue(), "")
            html = (root / "html" / "cli.html").read_text()
            self.assertIn("--raise-existing", html)
            self.assertIn("service start", html)
            self.assertNotIn("kp-sync service-run</", html)
            api = (root / "html" / "api.html").read_text()
            self.assertIn("SyncEngine", api)
            self.assertIn("_static/layout.css", api)
            self.assertEqual(
                (root / "html" / "_static" / "layout.css").read_bytes(),
                (ROOT / "docs" / "_static" / "layout.css").read_bytes(),
            )
            modules = (root / "html" / "modules.html").read_text()
            self.assertIn("kp_sync.paths", modules)


if __name__ == "__main__":
    unittest.main()
