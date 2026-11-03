"""Headless UI backend that safely defers every question."""

from pathlib import Path
from typing import Iterable

from kp_sync.ui.base import UI


class NoneUI(UI):
    """Unattended backend that never prompts or approves."""

    def credential_lookup(self, lookup):
        """Skip browser requests that may show approval dialogs."""
        return None

    def confirm(self, prompt: str, *, default: bool = False) -> bool | None:
        """Return ``None`` so the caller defers the question."""
        return None

    def choose(self, prompt: str, options: Iterable[str]) -> str | None:
        """Return ``None`` because no choice can be made unattended."""
        return None

    def password(self, prompt: str, *, slot: str | None = None) -> str | None:
        """Return ``None`` because passwords require interaction."""
        return None

    def file_path(self, prompt: str) -> Path | None:
        """Return ``None`` because file selection requires a user."""
        return None
