# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Submit udevmon drive events to the kp-sync daemon.

This is the KeePass half of the former external ``keydisk.py`` hook. It
never reads, writes or deletes rclone credentials.
"""

import fcntl
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Sequence

from kp_sync.drives import (
    _IDENTITY,
    DriveError,
    identify_drive,
    pending_attach_path,
)
from kp_sync.log import error
from kp_sync.paths import atomic_write, ensure_run_dir

MODES = ("main", "secondary")
COMMANDS = {
    ("main", "up"): "up",
    ("main", "down"): "down",
    ("secondary", "up"): "attach",
    ("secondary", "down"): "detach",
}


@contextmanager
def _keydisk_lock() -> Iterator[None]:
    descriptor = os.open(
        ensure_run_dir() / "keydisk.lock",
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
        ):
            raise ValueError("keydisk lock must be a user-owned regular file")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def drive_sync(
    action: str,
    mode: str,
    identity: str,
    mountpoint: str | None,
    *,
    profile: str | None,
    config: str | None,
    submit: Callable[[Sequence[str]], int],
    identify: Callable[[str], str] | None = None,
) -> int:
    """Validate a drive event and submit it with ``--no-wait``.

    On ``up`` the mounted filesystem identity is verified first and a
    pending-up marker is written under the shared ``keydisk.lock``; the
    daemon removes it after registering the drive. A failed submission
    keeps the marker so an older ``down`` cannot remove the hub; retry
    ``up`` to complete it. ``down`` does not inspect the vanished drive.

    Returns the submitted command's exit status.
    """
    if action not in {"up", "down"} or mode not in MODES:
        raise ValueError("action must be up|down and mode main|secondary")
    if not _IDENTITY.fullmatch(identity):
        raise ValueError("a drive identity SERIAL:FS_UUID is required")
    command = COMMANDS[(mode, action)]
    argv = []
    if config:
        argv.extend(("--config", config))
    elif profile:
        argv.extend(("--profile", profile))
    argv.extend(("--no-wait", command, "--drive-id", identity))
    with _keydisk_lock():
        if action == "up":
            if not mountpoint:
                raise ValueError("up requires a mount point")
            mount = Path(mountpoint).resolve()
            try:
                actual = (identify or identify_drive)(mount)
            except DriveError as exc:
                raise ValueError(str(exc)) from exc
            if actual != identity:
                raise ValueError(
                    "drive identity does not match the mounted filesystem"
                )
            atomic_write(
                pending_attach_path(profile, identity), "attachment pending\n"
            )
            argv.append(str(mount))
        status = submit(argv)
    if status:
        error(f"kp-sync {command} failed ({status})")
    return status
