"""Write prefixed diagnostics to stderr and an optional log."""

import sys
import os
import threading
import stat
from pathlib import Path
from typing import TextIO


_log_file: TextIO | None = None
_log_lock = threading.Lock()


def set_log_file(path: Path | None) -> None:
    """Replace the optional private log destination.

    Existing file handles are closed. A supplied destination must be a
    regular, user-owned file and is forced to mode 0600.
    """
    global _log_file
    with _log_lock:
        if _log_file is not None:
            _log_file.close()
            _log_file = None
        if path is not None:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
            )
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
            ):
                os.close(descriptor)
                raise RuntimeError(
                    "daemon log must be a regular user-owned file"
                )
            os.fchmod(descriptor, 0o600)
            try:
                stderr_metadata = os.fstat(sys.stderr.fileno())
            except (AttributeError, OSError):
                stderr_metadata = None
            if stderr_metadata is not None and (
                stderr_metadata.st_dev,
                stderr_metadata.st_ino,
            ) == (metadata.st_dev, metadata.st_ino):
                os.close(descriptor)
                return
            _log_file = os.fdopen(descriptor, "a", buffering=1)


def _write(message: str, stream: TextIO | None) -> None:
    print(message, file=sys.stderr if stream is None else stream)
    if stream is None:
        with _log_lock:
            if _log_file is not None:
                print(message, file=_log_file)


def info(message: str, *, stream: TextIO | None = None) -> None:
    """Write an INFO message to ``stream`` or stderr and the log."""
    _write(f"INFO: {message}", stream)


def error(message: str, *, stream: TextIO | None = None) -> None:
    """Write an ERROR message to ``stream`` or stderr and the log."""
    _write(f"ERROR: {message}", stream)
