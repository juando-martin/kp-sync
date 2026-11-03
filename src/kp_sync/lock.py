"""Acquire private interprocess flock locks with bounded waiting."""

import fcntl
import os
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class LockTimeout(TimeoutError):
    """Raised when an exclusive lock cannot be acquired in time."""

    pass


def acquire_lock(fd: int, timeout: float) -> None:
    """Acquire an exclusive nonblocking flock on an open descriptor."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise LockTimeout("timed out waiting for sync lock")
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))


@contextmanager
def file_lock(
    path: str | os.PathLike[str], timeout: float = 60
) -> Iterator[int]:
    """Hold an exclusive private file lock for the context's duration.

    Yields the open descriptor. The lock is released on context exit,
    including when the body raises.
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
        acquire_lock(fd, timeout)
        yield fd
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
