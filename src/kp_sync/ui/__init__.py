# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Public UI backends and interaction exceptions."""

from kp_sync.ui.base import UI, UIUnavailable
from kp_sync.ui.fake import FakeUI
from kp_sync.ui.none import NoneUI
from kp_sync.ui.tty import TTYUI

__all__ = ["FakeUI", "NoneUI", "TTYUI", "UI", "UIUnavailable"]
