# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kp_sync.cli import main
from kp_sync.completion import (
    alias_script,
    completion_dir,
    install,
    registration,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/bash-completion/kps"


class CompletionTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.env = {"HOME": str(self.home)}
        self.dir = self.home / ".local/share/bash-completion/completions"

    def run_install(self, **kwargs):
        lines = []
        status = install(environ=self.env, out=lines.append, **kwargs)
        return status, "\n".join(lines)

    def test_directory_follows_bash_completion(self):
        self.assertEqual(completion_dir(self.env), self.dir)
        self.assertEqual(
            completion_dir(self.env | {"XDG_DATA_HOME": "/data"}),
            Path("/data/bash-completion/completions"),
        )
        self.assertEqual(
            completion_dir(
                self.env
                | {"XDG_DATA_HOME": "/d", "BASH_COMPLETION_USER_DIR": "/u"}
            ),
            Path("/u/completions"),
        )

    def test_alias_script_is_the_documented_example(self):
        self.assertEqual(alias_script("kps"), EXAMPLE.read_text())
        other = alias_script("kp-work")
        self.assertIn("_kp_work_complete()", other)
        self.assertIn("${COMP_LINE#kp-work}", other)
        self.assertIn("${#prefix} - 7))", other)
        for bad in ("", "kp-sync", "a b", "x;rm", "9x"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                alias_script(bad)

    def test_install_update_and_dry_run(self):
        status, text = self.run_install(aliases=["kps"], dry_run=True)
        self.assertEqual(status, 0)
        self.assertIn("kp-sync: would install", text)
        self.assertFalse(self.dir.exists())
        status, text = self.run_install(aliases=["kps"])
        self.assertIn("kps: installed", text)
        self.assertIn("alias kps='kp-sync --profile HOST'", text)
        self.assertEqual((self.dir / "kp-sync").read_text(), registration())
        self.assertEqual((self.dir / "kps").read_text(), EXAMPLE.read_text())
        _, text = self.run_install(aliases=["kps"])
        self.assertEqual(text.count(": unchanged"), 2)
        (self.dir / "kp-sync").write_text("# old\n")
        _, text = self.run_install()
        self.assertIn("kp-sync: updated", text)
        self.assertEqual((self.dir / "kp-sync").read_text(), registration())
        with self.assertRaises(ValueError):
            self.run_install(aliases=["bad name"])

    def test_command_needs_no_profile(self):
        output = io.StringIO()
        with (
            patch.dict(os.environ, {"HOME": str(self.home)}),
            patch(
                "kp_sync.cli.load_config",
                side_effect=AssertionError("no configuration"),
            ),
            contextlib.redirect_stdout(output),
        ):
            os.environ.pop("XDG_DATA_HOME", None)
            os.environ.pop("BASH_COMPLETION_USER_DIR", None)
            self.assertEqual(
                main(["completion", "install", "--alias", "kps"]), 0
            )
        self.assertTrue((self.dir / "kps").is_file())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                main(["completion", "install", "--alias", "a b"]), 1
            )

    @unittest.skipUnless(shutil.which("bash"), "bash is missing")
    def test_alias_script_completes_the_expansion_in_bash(self):
        self.run_install(aliases=["kps"])
        script = (
            '_python_argcomplete() { echo "$COMP_LINE|$COMP_POINT|$1"; }\n'
            'source "$1/kps" 2>/dev/null\n'
            '_python_argcomplete() { echo "$COMP_LINE|$COMP_POINT|$1"; }\n'
            "shopt -s expand_aliases\n"
            "alias kps='kp-sync --profile beta'\n"
            "COMP_LINE='kps sta'; COMP_POINT=7\n"
            "_kps_complete\n"
        )
        result = subprocess.run(
            ["bash", "-c", script, "bash", str(self.dir)],
            capture_output=True,
            text=True,
            timeout=10,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )
        self.assertEqual(
            result.stdout.strip(),
            "kp-sync --profile beta sta|26|kp-sync",
        )


if __name__ == "__main__":
    unittest.main()
