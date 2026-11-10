# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Monitor KeePassXC D-Bus ownership without a Python binding."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Mapping
from typing import Any

from kp_sync.log import logged

KEEPASSXC_NAME = "org.keepassxc.KeePassXC.MainWindow"


class DBusError(RuntimeError):
    """Raised when a session D-Bus query or monitor fails."""

    pass


class DBusMonitor:
    """Monitor application owners and systemd unit changes over D-Bus.

    Uses command-line ``busctl`` and ``gdbus`` tools.
    """

    _SIGNAL = re.compile(
        r"NameOwnerChanged\s*\(\s*'([^']+)'\s*,\s*"
        r"'([^']*)'\s*,\s*'([^']*)'\s*\)"
    )
    _SYSTEMD_SIGNAL = re.compile(
        r"org\.freedesktop\.systemd1\.Manager\."
        r"(?:JobRemoved|UnitNew|UnitRemoved)\b"
    )

    def __init__(
        self,
        *,
        runner: Callable = subprocess.run,
        popen: Callable = subprocess.Popen,
        environment: Mapping[str, str] | None = None,
        names: tuple[str, ...] = (KEEPASSXC_NAME,),
        on_change: Callable[[str, str, str, int | None], None] | None = None,
        resolve_pid_on_signal: bool = True,
    ) -> None:
        """Configure monitored names and injectable process adapters.

        Args:
            runner: Subprocess runner for synchronous busctl queries.
            popen: Process launcher for asynchronous gdbus monitors.
            environment: Environment passed to both command-line tools.
            names: D-Bus names whose owner changes should be reported.
            on_change: Callback with name, old/new owner, and PID.
            resolve_pid_on_signal: Resolve owner PID for change signals.
        """
        self._runner = runner
        self._popen = popen
        self.environment = dict(
            os.environ if environment is None else environment
        )
        self.names = names
        self.on_change = on_change
        self.resolve_pid_on_signal = resolve_pid_on_signal
        self._process: Any | None = None
        self._systemd_process: Any | None = None
        self._owners: dict[str, tuple[str, int]] = {}
        self._buffer = ""
        self._systemd_buffer = ""
        self.owner_events = 0
        self.systemd_events = 0

    def _busctl(self, *args: str) -> subprocess.CompletedProcess:
        try:
            return logged(self._runner)(
                ["busctl", "--user", *args],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                env=self.environment,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DBusError(f"session D-Bus query failed: {exc}") from exc

    def owner_pid(self, name: str) -> int | None:
        """Resolve the current process ID owning a D-Bus name.

        Args:
            name: Well-known or unique D-Bus name.

        Returns:
            Process ID, or ``None`` if the name has no owner.

        Raises:
            ValueError: If ``name`` is empty or contains NUL.
            DBusError: If busctl returns an invalid or failed response.
        """
        if not name or "\x00" in name:
            raise ValueError("D-Bus name must be non-empty")
        has_owner = self._busctl(
            "call",
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            "s",
            name,
        )
        if has_owner.returncode:
            message = (has_owner.stderr or "").strip()
            if "NameHasNoOwner" in message:
                return None
            raise DBusError(
                f"cannot query D-Bus owner for {name}: "
                f"{message or 'busctl failed'}"
            )
        if has_owner.stdout.strip() == "b false":
            return None
        if has_owner.stdout.strip() != "b true":
            raise DBusError(f"unexpected NameHasOwner reply for {name}")
        owner = self._busctl(
            "call",
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "GetNameOwner",
            "s",
            name,
        )
        if owner.returncode:
            message = (owner.stderr or "").strip()
            raise DBusError(
                f"cannot resolve D-Bus owner for {name}: "
                f"{message or 'busctl failed'}"
            )
        match = re.fullmatch(r's "([^"]+)"\s*', owner.stdout)
        if not match:
            raise DBusError(f"unexpected GetNameOwner reply for {name}")
        return self._connection_pid(match.group(1))

    def _connection_pid(self, connection: str) -> int:
        result = self._busctl(
            "call",
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "GetConnectionUnixProcessID",
            "s",
            connection,
        )
        if result.returncode:
            raise DBusError(
                "cannot get pid for D-Bus connection: "
                f"{(result.stderr or '').strip()}"
            )
        match = re.fullmatch(r"u ([1-9][0-9]*)\s*", result.stdout)
        if not match:
            raise DBusError("unexpected GetConnectionUnixProcessID reply")
        return int(match.group(1))

    def start(self) -> None:
        """Start or restart the D-Bus and systemd signal monitors."""
        if self._process is None or self._process.poll() is not None:
            try:
                self._process = logged(self._popen)(
                    [
                        "gdbus",
                        "monitor",
                        "--session",
                        "--dest",
                        "org.freedesktop.DBus",
                        "--object-path",
                        "/org/freedesktop/DBus",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    env=self.environment,
                )
            except OSError as exc:
                raise DBusError(
                    f"cannot start D-Bus signal monitor: {exc}"
                ) from exc
            if self._process.stdout is not None:
                os.set_blocking(self._process.stdout.fileno(), False)
        if (
            self._systemd_process is None
            or self._systemd_process.poll() is not None
        ):
            try:
                self._systemd_process = logged(self._popen)(
                    [
                        "gdbus",
                        "monitor",
                        "--session",
                        "--dest",
                        "org.freedesktop.systemd1",
                        "--object-path",
                        "/org/freedesktop/systemd1",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    env=self.environment,
                )
            except OSError as exc:
                raise DBusError(
                    f"cannot monitor systemd unit changes: {exc}"
                ) from exc
            if self._systemd_process.stdout is not None:
                os.set_blocking(self._systemd_process.stdout.fileno(), False)

    def stop(self) -> None:
        """Stop monitor subprocesses and close their streams."""
        processes = (self._process, self._systemd_process)
        self._process = None
        self._systemd_process = None
        for process in processes:
            if process is None:
                continue
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
            finally:
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
        self._buffer = ""
        self._systemd_buffer = ""

    def fileno(self) -> int:
        """Return the D-Bus monitor output descriptor for polling."""
        self.start()
        if self._process is None or self._process.stdout is None:
            raise DBusError("D-Bus monitor did not expose its output stream")
        return self._process.stdout.fileno()

    def poll_signals(self) -> int:
        """Read available monitor output and return the event count."""
        self.start()
        process = self._process
        if process is None or process.stdout is None:
            raise DBusError("D-Bus monitor did not expose its output stream")
        if process.poll() is not None:
            detail = (
                process.stderr.read().strip()
                if process.stderr is not None
                else ""
            )
            raise DBusError(
                f"D-Bus signal monitor exited: {detail or process.returncode}"
            )
        self._buffer += _read_nonblocking(process.stdout)
        self.owner_events = 0
        self.systemd_events = 0
        events = 0
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            match = self._SIGNAL.search(line)
            if match is None:
                continue
            name, old_owner, new_owner = match.groups()
            if name not in self.names:
                continue
            pid = (
                self._connection_pid(new_owner)
                if new_owner and self.resolve_pid_on_signal
                else None
            )
            if self.on_change is not None:
                self.on_change(name, old_owner, new_owner, pid)
            events += 1
            self.owner_events += 1
        systemd_process = self._systemd_process
        if systemd_process is None or systemd_process.stdout is None:
            raise DBusError(
                "systemd D-Bus monitor did not expose its output stream"
            )
        if systemd_process.poll() is not None:
            detail = (
                systemd_process.stderr.read().strip()
                if systemd_process.stderr is not None
                else ""
            )
            raise DBusError(
                "systemd unit monitor exited: "
                f"{detail or systemd_process.returncode}"
            )
        self._systemd_buffer += _read_nonblocking(systemd_process.stdout)
        while "\n" in self._systemd_buffer:
            line, self._systemd_buffer = self._systemd_buffer.split("\n", 1)
            if self._SYSTEMD_SIGNAL.search(line):
                events += 1
                self.systemd_events += 1
        return events

    def discover(self) -> dict[str, int]:
        """Return process IDs for monitored names that have owners."""
        discovered: dict[str, int] = {}
        for name in self.names:
            pid = self.owner_pid(name)
            if pid is not None:
                discovered[name] = pid
        return discovered

    def close(self) -> None:
        """Stop both signal monitors."""
        self.stop()

    def __enter__(self) -> DBusMonitor:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()


def _read_nonblocking(stream: Any) -> str:
    try:
        return os.read(stream.fileno(), 65536).decode("utf-8", "replace")
    except BlockingIOError:
        return ""
