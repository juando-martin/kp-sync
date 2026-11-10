# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Discover remaining modules for internal reference generation."""

from pathlib import Path
import re

from docutils import nodes
from docutils.statemachine import StringList
from sphinx.util.docutils import SphinxDirective


class RemainingModules(SphinxDirective):
    """Render modules not documented on another page."""

    def run(self):
        """Expand source module names into autodoc directives."""
        source = Path(self.env.srcdir)
        package = source.parent / "src" / "kp_sync"
        documented = set()
        for page in source.glob("*.rst"):
            self.env.note_dependency(str(page))
            documented.update(
                re.findall(
                    r"^\.\. automodule:: ([\w.]+)",
                    page.read_text(),
                    re.MULTILINE,
                )
            )
        lines = []
        for path in sorted(package.rglob("*.py")):
            if path.name == "__main__.py":
                continue
            parts = path.relative_to(package.parent).with_suffix("").parts
            if parts[-1] == "__init__":
                parts = parts[:-1]
            module = ".".join(parts)
            if module not in documented:
                lines.append(f".. automodule:: {module}")
                if path.name != "__init__.py":
                    lines.append("   :members:")
                lines.append("")
        container = nodes.container()
        self.state.nested_parse(
            StringList(lines, source=str(source / "modules.rst")),
            self.content_offset,
            container,
        )
        return container.children


def setup(app):
    """Register automatic internal module reference discovery."""
    app.add_directive("kp-sync-modules", RemainingModules)
    return {"version": "1", "parallel_read_safe": True}
