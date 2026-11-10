# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Interactive UI backend that asks questions on a client's terminal."""

import select
import sys
import termios
from pathlib import Path
from typing import Iterable

from kp_sync.ui.base import UI, UIUnavailable


class TTYUI(UI):
    """Read questions from an interactive terminal and write to stderr.

    Password input is hidden. Timeout, EOF, or invalid answers return
    ``None`` to defer safely.
    """

    def __init__(
        self, *, input_stream=None, output_stream=None, timeout=600
    ) -> None:
        """Bind the backend to terminal streams.

        Args:
            input_stream: Input stream; defaults to ``sys.stdin``.
            output_stream: Prompt stream; defaults to ``sys.stderr``.
            timeout: Maximum seconds to wait for each response.

        Raises:
            UIUnavailable: If the selected input stream is not a TTY.
        """
        self.input = input_stream or sys.stdin
        self.output = output_stream or sys.stderr
        self.timeout = timeout
        if not self.input.isatty():
            raise UIUnavailable("tty UI requires an interactive terminal")

    def _readline(self, prompt: str) -> str:
        self.output.write(prompt)
        self.output.flush()
        try:
            descriptor = self.input.fileno()
        except (AttributeError, OSError):
            return self.input.readline()
        if not select.select([descriptor], [], [], self.timeout)[0]:
            return ""
        return self.input.readline()

    def confirm(self, prompt: str, *, default: bool = False) -> bool | None:
        """Ask a terminal yes/no question; invalid input defers."""
        suffix = " [Y/n] " if default else " [y/N] "
        try:
            raw = self._readline(prompt + suffix)
            if not raw:
                return None
            answer = raw.strip().lower()
        except EOFError:
            return None
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        return None

    def choose(self, prompt: str, options: Iterable[str]) -> str | None:
        """Display options and accept an index or exact option value."""
        values = tuple(options)
        self.output.write(f"{prompt}\n")
        for index, value in enumerate(values, 1):
            self.output.write(f"  {index}. {value}\n")
        try:
            answer = self._readline("Choice (or blank to defer): ").strip()
        except EOFError:
            return None
        if not answer:
            return None
        if answer.isdecimal() and 1 <= int(answer) <= len(values):
            return values[int(answer) - 1]
        if answer in values:
            return answer
        return None

    def password(self, prompt: str, *, slot: str | None = None) -> str | None:
        """Read a hidden password from the terminal.

        Higher-level credential handling consumes the optional slot;
        this backend never sends it over the daemon connection.
        """
        try:
            descriptor = self.input.fileno()
            original = termios.tcgetattr(descriptor)
            hidden = list(original)
            hidden[3] &= ~termios.ECHO
            try:
                termios.tcsetattr(descriptor, termios.TCSAFLUSH, hidden)
                value = self._readline(prompt + ": ").rstrip("\r\n")
            finally:
                termios.tcsetattr(descriptor, termios.TCSAFLUSH, original)
                self.output.write("\n")
        except (EOFError, OSError, termios.error):
            return None
        return value or None

    def file_path(self, prompt: str) -> Path | None:
        """Read a terminal path and expand a leading home marker."""
        try:
            value = self._readline(prompt + " (blank to defer): ").strip()
        except EOFError:
            return None
        return Path(value).expanduser() if value else None

    def review(
        self, conflicts: list[dict[str, object]]
    ) -> dict[str, str] | None:
        """Review conflicts sequentially and return a side per ID.

        Secret values are masked. Cancelling any choice defers the whole
        review and returns ``None``.
        """
        choices: dict[str, str] = {}
        for conflict in conflicts:
            identifier = str(conflict["id"])
            self.output.write(f"\n{conflict['label']}\n")
            for side in ("base", "hub", "other"):
                value = (
                    "[hidden]"
                    if conflict.get("secret") and conflict.get(side)
                    else conflict.get(side, "")
                )
                self.output.write(f"  {side}: {value}\n")
            choice = self.choose("Choose value", ("hub", "other", "base"))
            if choice is None:
                return None
            choices[identifier] = choice
        return choices
