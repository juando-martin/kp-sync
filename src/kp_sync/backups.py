# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Create and prune timestamped backups in private runtime storage."""

import os
import re
import shutil
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from kp_sync.paths import ensure_private_directory


def backup_file(
    source: str | os.PathLike[str],
    backup_dir: str | os.PathLike[str],
    *,
    identity: str,
    keep: int = 20,
    now: datetime | None = None,
) -> Path:
    """Create and retain a private backup for one physical copy.

    Args:
        source: Existing database file to preserve.
        backup_dir: Private runtime directory for backup files.
        identity: Copy identity used to group and name backups.
        keep: Maximum number of backups retained for this identity.
        now: Optional timestamp override for deterministic callers.

    Returns:
        The path of the newly fsynced backup.
    """
    if keep < 1:
        raise ValueError("backup retention must be at least one")
    source_path = Path(source)
    directory = ensure_private_directory(backup_dir)
    timestamp = (now or datetime.now(timezone.utc)).strftime(
        "%Y%m%dT%H%M%S.%fZ"
    )
    safe_identity = re.sub(r"[^A-Za-z0-9_.-]", "_", identity)
    safe_identity = (
        f"{safe_identity[:48]}-"
        f"{hashlib.sha256(identity.encode()).hexdigest()[:12]}"
    )
    destination = directory / f"{timestamp}-{safe_identity}.kdbx"
    suffix = 1
    while destination.exists():
        destination = directory / f"{timestamp}-{safe_identity}-{suffix}.kdbx"
        suffix += 1

    with (
        source_path.open("rb") as source_file,
        destination.open("xb") as backup,
    ):
        os.fchmod(backup.fileno(), 0o600)
        shutil.copyfileobj(source_file, backup)
        backup.flush()
        os.fsync(backup.fileno())
    _fsync_directory(directory)

    backups = sorted(
        (
            path
            for path in directory.glob("*.kdbx")
            if re.search(
                rf"-{re.escape(safe_identity)}(?:-\d+)?\.kdbx\Z", path.name
            )
        ),
        key=lambda path: path.name,
        reverse=True,
    )
    for old in backups[keep:]:
        old.unlink()
    if len(backups) > keep:
        _fsync_directory(directory)
    return destination


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
