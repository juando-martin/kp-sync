# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Render argparse help without running commands or loading profiles."""

import argparse

from docutils import nodes
from docutils.parsers.rst import Directive


def parser_help(parser, command="kp-sync"):
    """Yield help for the parser and every visible subcommand."""
    yield command, parser.format_help()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                if name != "service-run":
                    yield from parser_help(child, f"{command} {name}")


class CommandHelp(Directive):
    """Insert CLI reference material from argparse definitions."""

    def run(self):
        """Create help headings and blocks without executing a CLI."""
        from kp_sync.cli import _build_parser

        result = []
        for command, help_text in parser_help(_build_parser()):
            section = nodes.section(ids=[command.replace(" ", "-")])
            section += nodes.title(text=command)
            section += nodes.literal_block(text=help_text, language="text")
            result.append(section)
        return result


def setup(app):
    """Register the read-only CLI reference directive."""
    app.add_directive("kp-sync-help", CommandHelp)
    return {"version": "1", "parallel_read_safe": True}
