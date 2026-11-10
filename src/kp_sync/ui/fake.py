# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Scripted UI backend for deterministic interaction tests."""

from collections import deque
from pathlib import Path
from typing import Iterable

from kp_sync.ui.base import UI


class FakeUI(UI):
    """Return queued answers and record interactions for assertions."""

    def __init__(
        self,
        *,
        confirmations=(),
        choices=(),
        passwords=(),
        file_paths=(),
        reviews=(),
    ) -> None:
        """Create a fake UI with independent answer queues.

        Args:
            confirmations: Boolean or ``None`` confirmation answers.
            choices: Choice strings or ``None`` responses.
            passwords: Password strings or ``None`` responses.
            file_paths: Paths or ``None`` responses.
            reviews: Conflict-choice mappings or ``None`` responses.
        """
        self.confirmations = deque(confirmations)
        self.choices = deque(choices)
        self.passwords = deque(passwords)
        self.file_paths = deque(file_paths)
        self.reviews = deque(reviews)
        self.events: list[tuple[str, object]] = []

    def confirm(self, prompt: str, *, default: bool = False) -> bool | None:
        """Record a confirmation and consume its next queued answer."""
        self.events.append(("confirm", prompt))
        return self.confirmations.popleft() if self.confirmations else None

    def choose(self, prompt: str, options: Iterable[str]) -> str | None:
        """Record the prompt and options, then consume an answer."""
        self.events.append(("choose", (prompt, tuple(options))))
        return self.choices.popleft() if self.choices else None

    def password(self, prompt: str, *, slot: str | None = None) -> str | None:
        """Record a password prompt and consume the next answer."""
        self.events.append(("password", prompt))
        return self.passwords.popleft() if self.passwords else None

    def file_path(self, prompt: str) -> Path | None:
        """Record file selection and normalize its answer."""
        self.events.append(("file_path", prompt))
        value = self.file_paths.popleft() if self.file_paths else None
        return Path(value) if value is not None else None

    def review(
        self, conflicts: list[dict[str, object]]
    ) -> dict[str, str] | None:
        """Record conflicts and consume the next queued review."""
        self.events.append(("review", conflicts))
        return self.reviews.popleft() if self.reviews else None
