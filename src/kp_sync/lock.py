# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Acquire private interprocess flock locks with bounded waiting."""

import fcntl
import os
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class LockTimeout(TimeoutError):
    """Raised when a lock cannot be acquired in time."""

    pass


def acquire_lock(fd: int, timeout: float, *, shared: bool = False) -> None:
    """Acquire a nonblocking flock on an open descriptor.

    ``shared`` takes a shared lock instead of an exclusive one. A zero
    ``timeout`` tries once.
    """
    mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise LockTimeout("timed out waiting for sync lock")
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))


@contextmanager
def file_lock(
    path: str | os.PathLike[str],
    timeout: float = 60,
    *,
    shared: bool = False,
) -> Iterator[int]:
    """Hold a private file lock for the context's duration.

    The lock is exclusive unless ``shared`` is set. Yields the open
    descriptor. The lock is released on context exit, including when
    the body raises.
    """
    if timeout < 0:
        raise ValueError("lock timeout must not be negative")
    lock_path = Path(path)
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(
        lock_path,
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
        ):
            raise RuntimeError(
                f"lock path is not a regular user-owned file: {lock_path}"
            )
        os.fchmod(fd, 0o600)
        acquire_lock(fd, timeout, shared=shared)
        yield fd
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
