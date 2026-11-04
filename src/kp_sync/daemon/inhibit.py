"""Bounded logind delay inhibitor for shutdown and sleep syncs."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from typing import Any

from kp_sync.log import logged


class InhibitError(RuntimeError):
    """Raised when logind inhibitor acquisition or release fails."""

    pass


class LogindInhibitor:
    """Hold a delay inhibitor through systemd-inhibit's open stdin."""

    def __init__(
        self,
        *,
        popen: Callable = subprocess.Popen,
        timeout: float = 5,
        release_timeout: float = 3,
        command: Sequence[str] = ("systemd-inhibit",),
    ) -> None:
        """Configure the inhibitor subprocess and bounded timeouts.

        Args:
            popen: Injectable process launcher.
            timeout: Maximum startup wait in seconds, capped at 30.
            release_timeout: Per-signal wait, capped at 10 seconds.
            command: Executable and fixed arguments for systemd-inhibit.

        Raises:
            ValueError: If either timeout is not positive.
        """
        if timeout <= 0 or release_timeout <= 0:
            raise ValueError("inhibitor timeouts must be positive")
        self._popen = popen
        self.timeout = min(timeout, 30)
        self.release_timeout = min(release_timeout, 10)
        self.command = tuple(command)
        self._process: Any | None = None

    @property
    def acquired(self) -> bool:
        """Whether the inhibitor child is still running."""
        return self._process is not None and self._process.poll() is None

    def acquire(self) -> None:
        """Acquire a delay inhibitor for shutdown and sleep.

        Raises:
            InhibitError: If startup fails or the process exits early.
        """
        if self.acquired:
            return
        self.release()
        argv = [
            *self.command,
            "--what=shutdown:sleep",
            "--mode=delay",
            "--who=kp-sync",
            "--why=finish the pending KeePass sync",
            "--",
            "cat",
        ]
        try:
            process = logged(self._popen)(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=False,
            )
        except OSError as exc:
            raise InhibitError(
                f"cannot acquire logind delay inhibitor: {exc}"
            ) from exc
        self._process = process
        if process.poll() is not None:
            detail = ""
            if process.stderr is not None:
                detail = (
                    process.stderr.read().decode("utf-8", "replace").strip()
                )
            self._process = None
            raise InhibitError(
                "systemd-inhibit exited before acquiring the lock: "
                f"{detail or process.returncode}"
            )

    def release(self) -> None:
        """Release the inhibitor and bound child shutdown."""
        process, self._process = self._process, None
        if process is None:
            return
        if process.poll() is not None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
            process.wait(timeout=self.release_timeout)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=self.release_timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=self.release_timeout)
        except OSError as exc:
            raise InhibitError(
                f"cannot release logind delay inhibitor: {exc}"
            ) from exc

    def update(self, active: bool) -> None:
        """Acquire while active work exists, otherwise release."""
        if active:
            self.acquire()
        else:
            self.release()

    def close(self) -> None:
        """Release the inhibitor."""
        self.release()

    def __enter__(self) -> LogindInhibitor:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
