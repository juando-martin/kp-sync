# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Read udevmon's configuration (udevmon 0.7.0+ drop-ins included).

kp-sync keeps its own udevmon devices and actions in one drop-in file,
``config.d/50-kp-sync.toml``, and changes it only through udevmon's own
editing commands. This module only reads.
"""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Callable, Mapping

from kp_sync.log import logged

DROP_IN = "50-kp-sync"
MIN_VERSION = (0, 7, 0)


def config_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Return udevmon's configuration directory."""
    env = os.environ if environ is None else environ
    base = env.get("XDG_CONFIG_HOME") or str(
        Path(env.get("HOME", "~")).expanduser() / ".config"
    )
    return Path(base) / "udevmon"


def config_files(directory: Path) -> list[Path]:
    """``config.toml`` and the drop-ins, in udevmon's reading order."""
    return [directory / "config.toml"] + sorted(
        path
        for path in (directory / "config.d").glob("*.toml")
        if not path.name.startswith(".")
    )


def load(directory: Path) -> tuple[list[tuple[Path, dict]], list[str]]:
    """Return ``[(file, document)]`` of readable files, and errors."""
    documents, problems = [], []
    for path in config_files(directory):
        if not path.is_file():
            continue
        try:
            documents.append(
                (path, tomllib.loads(path.read_text(encoding="utf-8")))
            )
        except (OSError, tomllib.TOMLDecodeError) as exc:
            problems.append(f"cannot read {path}: {exc}")
    return documents, problems


def version(
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> tuple[int, ...] | None:
    """The installed udevmon's version, or None if it cannot run."""
    try:
        result = logged(runner)(
            ["udevmon", "--version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", result.stdout or "")
    if result.returncode != 0 or match is None:
        return None
    return tuple(int(part) for part in match.groups())


def devices(documents) -> dict[str, dict]:
    """Every named device, by name, with the file it comes from."""
    found = {}
    for path, document in documents:
        for name, device in (document.get("devices") or {}).items():
            if isinstance(device, dict):
                found[name] = {**device, "_file": path}
    return found


def actions(documents) -> list[dict]:
    """Every action, in reading order, with the file it comes from."""
    found = []
    for path, document in documents:
        for action in document.get("actions") or []:
            if isinstance(action, dict):
                found.append({**action, "_file": path})
    return found
