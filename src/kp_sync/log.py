"""Write prefixed diagnostics to stderr and an optional log.

Three kinds of message exist. ``info`` and ``error`` report outcomes,
warnings and problems and are always written. ``detail`` reports
routine diagnostics with the same ``INFO:`` prefix; an interactive
terminal command hides them unless ``--verbose`` or debugging is on,
while the daemon, hooks and non-terminal callers always write them.
``debug`` explains decisions with a ``DEBUG:`` prefix and is written
only with ``--debug`` or ``KP_SYNC_DEBUG``. No kind ever carries
passwords, command argument lists or command output.
"""

import shlex
import sys
import os
import threading
import stat
from pathlib import Path
from collections.abc import Mapping
from typing import TextIO

_log_file: TextIO | None = None
_log_lock = threading.Lock()
_show_details = True
_debug = False

DEBUG_VARIABLE = "KP_SYNC_DEBUG"
"""Environment variable that enables debug messages when set."""


def debug_requested(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether ``KP_SYNC_DEBUG`` asks for debug messages.

    Any non-empty value other than ``0``, ``false``, ``no`` or ``off``
    (in any letter case) enables them.
    """
    value = (os.environ if environ is None else environ).get(
        DEBUG_VARIABLE, ""
    )
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


def configure(
    *,
    verbose: bool = False,
    debug: bool = False,
    interactive: bool = False,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Select which optional messages this process writes.

    Args:
        verbose: Write ``detail`` messages even on a terminal.
        debug: Write ``debug`` messages (implies ``verbose``).
        interactive: The command runs for a person at a terminal;
            ``detail`` messages are then hidden unless requested.
        environ: Environment checked for ``KP_SYNC_DEBUG``.
    """
    global _show_details, _debug
    _debug = debug or debug_requested(environ)
    _show_details = verbose or _debug or not interactive


def settings() -> tuple[bool, bool]:
    """Return the current settings for ``restore_settings``."""
    return _show_details, _debug


def restore_settings(saved: tuple[bool, bool]) -> None:
    """Reinstate settings returned earlier by ``settings``."""
    global _show_details, _debug
    _show_details, _debug = saved


def debug_enabled() -> bool:
    """Return whether ``debug`` messages are currently written."""
    return _debug


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
    # Logging must never fail its caller: a full log file or a closed
    # stderr would otherwise abort work midway (for example after the
    # daemon has queued a sync but before it runs).
    try:
        print(message, file=sys.stderr if stream is None else stream)
    except (OSError, ValueError):
        pass
    if stream is None:
        with _log_lock:
            if _log_file is not None:
                try:
                    print(message, file=_log_file)
                except (OSError, ValueError):
                    pass


def info(message: str, *, stream: TextIO | None = None) -> None:
    """Write an INFO message to ``stream`` or stderr and the log."""
    _write(f"INFO: {message}", stream)


def detail(message: str, *, stream: TextIO | None = None) -> None:
    """Write a routine INFO diagnostic unless details are hidden."""
    if _show_details:
        _write(f"INFO: {message}", stream)


def debug(message: str, *, stream: TextIO | None = None) -> None:
    """Write a DEBUG message when debugging is enabled."""
    if _debug:
        _write(f"DEBUG: {message}", stream)


def debug_command(argv, *, hide_text: bool = False) -> None:
    """Write the command line of an external program when debugging.

    Never pass stdin or output here. With ``hide_text``, only the
    program and its ``--option`` names are shown; use it for dialogs
    and notifications, whose text can quote database content.
    """
    if not _debug:
        return
    words = [str(arg) for arg in argv]
    if hide_text and words:
        words = [words[0]] + [
            word.split("=", 1)[0] for word in words[1:] if word[:2] == "--"
        ]
        words.append("(text not shown)")
        debug("run: " + " ".join(words))
        return
    debug("run: " + shlex.join(words))


def logged(run, *, hide_text: bool = False):
    """Return ``run`` (``subprocess.run``, ``Popen`` or a fake) wrapped
    so that its command line is logged with :func:`debug_command`."""

    def logged_run(argv, *args, **kwargs):
        debug_command(argv, hide_text=hide_text)
        return run(argv, *args, **kwargs)

    return logged_run


def error(message: str, *, stream: TextIO | None = None) -> None:
    """Write an ERROR message to ``stream`` or stderr and the log."""
    _write(f"ERROR: {message}", stream)
