"""Debounced sync scheduling and filesystem notifications."""

import ctypes
import os
import struct
import time
from pathlib import Path


class Triggers:
    """Track debounce, polling, lease activity, and idle-exit deadlines.

    Triggers during a sync coalesce into one immediate follow-up.
    Poll-only runs carry a silent marker so they do not prompt the user.
    """

    def __init__(
        self, *, debounce, poll_interval, idle_exit, clock=time.monotonic
    ):
        """Configure scheduling intervals and the monotonic clock.

        Args:
            debounce: Delay for coalescing ordinary event triggers.
            poll_interval: Poll interval while leases are active.
            idle_exit: Delay before exit with no leases or queued work.
            clock: Monotonic clock used for all deadline calculations.
        """
        self.clock = clock
        self.debounce = debounce
        self.poll_interval = poll_interval
        self.idle_exit = idle_exit
        self.due = None
        self.next_poll = clock() + poll_interval
        self.idle_since = clock()
        self.active = False
        self.running = False
        self.again = False
        self.silent = False
        self.again_silent = False

    def trigger(self, *, immediate=False, silent=False):
        """Schedule a sync, coalescing triggers while one is running.

        Args:
            immediate: Make the next run due without a debounce delay.
            silent: Mark this as a background poll without prompts.
        """
        if self.running:
            if not self.again:
                self.again_silent = silent
            elif not silent:
                self.again_silent = False
            self.again = True
        else:
            if self.due is None:
                self.silent = silent
            elif not silent:
                self.silent = False
            self.due = self.clock() + (0 if immediate else self.debounce)

    def leases_changed(self, active):
        """Update activity and start the idle timer when leases end."""
        if self.active and not active:
            self.idle_since = self.clock()
        self.active = active

    def ready(self):
        """Return whether a scheduled run is due now."""
        now = self.clock()
        if self.active and self.poll_interval and now >= self.next_poll:
            self.trigger(immediate=True, silent=True)
            self.next_poll = now + self.poll_interval
        return not self.running and self.due is not None and now >= self.due

    def start(self):
        """Mark a due run active; return whether it should be silent."""
        self.running = True
        self.due = None
        silent = self.silent
        self.silent = False
        return silent

    def finish(self):
        """Finish a run and immediately queue a coalesced follow-up."""
        self.running = False
        self.idle_since = self.clock()
        if self.again:
            silent = self.again_silent
            self.again = False
            self.again_silent = False
            self.trigger(immediate=True, silent=silent)
        else:
            self.again_silent = False

    def should_exit(self):
        """Return whether the idle-exit deadline has elapsed."""
        return (
            not self.active
            and not self.running
            and self.due is None
            and self.clock() - self.idle_since >= self.idle_exit
        )


class HubWatch:
    """Read inotify events for the hub file and its parent directory."""

    IN_OPEN = 0x20
    IN_MOVED_TO = 0x80
    IN_CLOSE_WRITE = 0x8
    IN_DELETE_SELF = 0x400
    IN_IGNORED = 0x8000
    IN_Q_OVERFLOW = 0x4000

    def __init__(self, hub: Path):
        """Watch for saves and opens of ``hub``.

        Args:
            hub: Database path whose parent directory is monitored.
        """
        self.hub = Path(hub)
        self.fd = None
        self.watched = None
        self.libc = ctypes.CDLL(None, use_errno=True)

    def _watch(self):
        parent = self.hub.parent
        if not parent.is_dir():
            return
        if self.fd is None:
            self.libc.inotify_init1.argtypes = [ctypes.c_int]
            descriptor = self.libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
            if descriptor < 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))
            self.fd = descriptor
        if self.watched is None:
            self.libc.inotify_add_watch.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint32,
            ]
            watch = self.libc.inotify_add_watch(
                self.fd,
                os.fsencode(parent),
                self.IN_OPEN
                | self.IN_MOVED_TO
                | self.IN_CLOSE_WRITE
                | self.IN_DELETE_SELF,
            )
            if watch < 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error), str(parent))
            self.watched = watch

    def read(self):
        """Drain pending filesystem events.

        Returns:
            Tuple ``(saved, opened)`` indicating hub writes and opens.

        Raises:
            OSError: If inotify initialization or reading fails.
        """
        self._watch()
        saved, opened = False, False
        if self.fd is None:
            return saved, opened
        while True:
            try:
                payload = os.read(self.fd, 65536)
            except BlockingIOError:
                break
            if not payload:
                break
            offset = 0
            while offset < len(payload):
                _, mask, _, length = struct.unpack_from(
                    "iIII", payload, offset
                )
                name = os.fsdecode(
                    payload[offset + 16 : offset + 16 + length].split(
                        b"\0", 1
                    )[0]
                )
                offset += 16 + length
                if mask & (self.IN_IGNORED | self.IN_DELETE_SELF):
                    self.watched = None
                if mask & self.IN_Q_OVERFLOW:
                    saved = True
                if name == self.hub.name:
                    saved |= bool(
                        mask & (self.IN_MOVED_TO | self.IN_CLOSE_WRITE)
                    )
                    opened |= bool(mask & self.IN_OPEN)
        return saved, opened

    def close(self):
        """Close the inotify descriptor if it has been initialized."""
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
