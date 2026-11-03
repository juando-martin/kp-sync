"""Delayed, non-cancelling Zenity indicator for desktop syncs."""

import subprocess
import threading
from contextlib import contextmanager

from kp_sync.log import error


class SyncProgress:
    """Manage a delayed progress dialog that does not cancel sync work.

    It appears after its configured delay and closes on context exit.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        delay: float = 2,
        database_info: str = "",
    ) -> None:
        """Configure whether and when to display progress.

        Args:
            enabled: Whether a desktop progress window may be shown.
            delay: Seconds to wait before starting the dialog.
            database_info: Optional profile and database description.
        """
        self.enabled = enabled
        self.delay = delay
        self.database_info = database_info.replace("\n", " ").strip()
        self.lock = threading.Lock()
        self.timer = None
        self.process = None

    def __enter__(self):
        """Arm the delayed dialog and return this progress manager."""
        if self.enabled:
            with self.lock:
                self.timer = threading.Timer(self.delay, self._show)
                self.timer.daemon = True
                self.timer.start()
        return self

    def _show(self):
        with self.lock:
            if self.timer is None:
                return
            try:
                text = (
                    "Synchronizing KeePass copies\n"
                    "Hiding this window will not stop Synchronization"
                )
                if self.database_info:
                    text += f"\n{self.database_info}"
                self.process = subprocess.Popen(
                    [
                        "zenity",
                        "--progress",
                        "--pulsate",
                        "--cancel-label=Hide",
                        "--title=KeePass synchronization",
                        "--width=420",
                        f"--text={text}",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as exc:
                error(f"sync progress dialog failed: {exc}")

    def __exit__(self, *_):
        """Cancel the timer and close a running dialog."""
        with self.lock:
            if self.timer is not None:
                self.timer.cancel()
                self.timer = None
            if self.process is not None:
                process, self.process = self.process, None
                status = process.poll()
                if status is None:
                    self._signal(process.terminate)
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self._signal(process.kill)
                        process.wait()
                elif status not in {0, 1}:
                    error(f"sync progress dialog exited with status {status}")
                if process.stdin is not None:
                    process.stdin.close()

    @staticmethod
    def _signal(method):
        try:
            method()
        except ProcessLookupError:
            pass

    @contextmanager
    def paused(self):
        """Hide progress while a decision dialog is active."""
        self.__exit__()
        try:
            yield
        finally:
            self.__enter__()
