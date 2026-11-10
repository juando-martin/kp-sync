# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Select the appropriate UI backend for a request."""

import os
import sys
from typing import Sequence

from kp_sync.ui.base import UI
from kp_sync.ui.gui import CustomCommandUI, KDialogUI, ZenityUI
from kp_sync.ui.none import NoneUI
from kp_sync.ui.tty import TTYUI


def create_ui(
    mode: str,
    *,
    timeout: int = 600,
    gui: str | Sequence[str] = "zenity",
) -> UI:
    """Create an interaction backend from the requested mode.

    Args:
        mode: ``auto``, ``tty``, ``gui``, or ``none``.
        timeout: Maximum wait for an interactive response, in seconds.
        gui: Built-in GUI name or custom command argument vector.

    Returns:
        Backend implementing the ``UI`` interaction contract.

    Raises:
        UIUnavailable: If the interactive backend cannot be created.
    """
    selected = mode
    if mode == "auto":
        if sys.stdin.isatty():
            selected = "tty"
        elif os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
            selected = "gui"
        else:
            selected = "none"
    if selected == "tty":
        return TTYUI(timeout=timeout)
    if selected == "gui":
        if gui == "zenity":
            return ZenityUI(timeout=timeout)
        if gui == "kdialog":
            return KDialogUI(timeout=timeout)
        command = [gui] if isinstance(gui, str) else gui
        return CustomCommandUI(command, timeout=timeout)
    return NoneUI()
