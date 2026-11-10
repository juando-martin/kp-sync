# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Interaction interface for terminal, desktop, and headless UIs."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterable
from collections.abc import Callable


class UIUnavailable(RuntimeError):
    """Raised when an interactive backend cannot complete a request."""

    pass


class UI(ABC):
    """Backend contract for synchronization questions and notifications.

    Cancellation returns ``None`` so callers defer rather than assuming
    approval.
    """

    def credential_lookup(
        self, lookup: Callable[[], str | None]
    ) -> str | None:
        """Run an external credential request that may need approval.

        Wrappers release synchronization locks; headless UIs skip it.
        The callback and returned secret must never cross daemon IPC.
        """
        return lookup()

    @abstractmethod
    def confirm(self, prompt: str, *, default: bool = False) -> bool | None:
        """Ask a yes/no question.

        Args:
            prompt: Human-readable question.
            default: Answer used for an empty response, where supported.

        Returns:
            ``True`` or ``False`` for an answer, otherwise ``None``.
        """
        raise NotImplementedError

    @abstractmethod
    def choose(self, prompt: str, options: Iterable[str]) -> str | None:
        """Ask the user to select one of the supplied values.

        Args:
            prompt: Human-readable prompt.
            options: Permitted choice values.

        Returns:
            The selected value, or ``None`` when cancelled.
        """
        raise NotImplementedError

    @abstractmethod
    def password(self, prompt: str, *, slot: str | None = None) -> str | None:
        """Ask for a password without returning it through daemon IPC.

        Args:
            prompt: Human-readable prompt.
            slot: Optional copy-specific credential slot.

        Returns:
            Password text, or ``None`` when cancelled.
        """
        raise NotImplementedError

    @abstractmethod
    def file_path(self, prompt: str) -> Path | None:
        """Ask the user to select a local file.

        Args:
            prompt: Human-readable prompt.

        Returns:
            Selected path, or ``None`` when cancelled.
        """
        raise NotImplementedError

    def review(
        self, conflicts: list[dict[str, object]]
    ) -> dict[str, str] | None:
        """Review conflicting fields, if the backend supports it.

        Args:
            conflicts: Conflict IDs and their side values.

        Returns:
            Mapping conflict IDs to sides, or ``None`` if unavailable.
        """
        return None

    def warn(self, message: str) -> None:
        """Show a warning the user acknowledges; defaults to notify."""
        self.notify(message)

    def notify(self, message: str) -> None:
        """Report information without requiring a response."""
        from kp_sync.log import info

        info(message)
