# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Run the kp-sync command-line entry point as a Python module."""

from kp_sync.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
