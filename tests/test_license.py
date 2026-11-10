# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Every source file carries the license header; GPLv3+ metadata."""

import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEADER = (
    "# SPDX-License-Identifier: GPL-3.0-or-later\n"
    "# Copyright (C) 2026 Juan Domingo Martín\n"
)
SCRIPTS = ("examples/keepass", "examples/bash-completion/kps")


def sources():
    for directory in ("src", "tests", "tools", "examples", "docs"):
        for path in sorted((ROOT / directory).rglob("*.py")):
            if "__pycache__" not in path.parts and "_build" not in path.parts:
                yield path
    for name in SCRIPTS:
        yield ROOT / name


class LicenseTest(unittest.TestCase):
    def test_every_source_file_starts_with_the_header(self):
        missing = []
        for path in sources():
            text = path.read_text(encoding="utf-8")
            if text.startswith("#!"):
                text = text.partition("\n")[2]
            if not text.startswith(HEADER):
                missing.append(str(path.relative_to(ROOT)))
        self.assertEqual(
            missing,
            [],
            "add the two-line SPDX header (see CLAUDE.md, Conventions)",
        )

    def test_license_text_and_metadata(self):
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("GNU GENERAL PUBLIC LICENSE", license_text[:200])
        self.assertIn("Version 3, 29 June 2007", license_text[:200])
        self.assertIn("END OF TERMS AND CONDITIONS", license_text)
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())[
            "project"
        ]
        self.assertEqual(project["license"], "GPL-3.0-or-later")
        self.assertEqual(project["license-files"], ["LICENSE"])


if __name__ == "__main__":
    unittest.main()
