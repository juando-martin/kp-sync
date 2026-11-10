# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Small systemd --user adapter used by desktop integration."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from kp_sync.log import logged


class SystemdError(RuntimeError):
    """Raised when a systemd user operation cannot be completed."""

    pass


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_DISPLAY_ENV = (
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "DBUS_SESSION_BUS_ADDRESS",
    "XAUTHORITY",
    "XDG_RUNTIME_DIR",
)


def app_unit_name(profile: str | None, app_name: str) -> str:
    """Return a validated transient unit name for one application.

    Args:
        profile: Optional named profile.
        app_name: Application identifier.

    Raises:
        ValueError: If a name is invalid or too long for a unit.
    """
    if not _IDENTIFIER.fullmatch(app_name):
        raise ValueError(f"invalid application name: {app_name!r}")
    if profile is not None and not _IDENTIFIER.fullmatch(profile):
        raise ValueError(f"invalid profile name: {profile!r}")
    stem = (
        f"kp-sync-app-{app_name}"
        if profile is None
        else f"kp-sync-app-{profile}-{app_name}"
    )
    if len(stem) > 240:
        raise ValueError(
            "profile and application name are too long for a systemd unit"
        )
    return stem + ".service"


@dataclass(frozen=True)
class UnitState:
    """Observed active and sub-state values for one systemd unit."""

    name: str
    active: bool
    sub_state: str = ""


class SystemdUser:
    """Argument-vector adapter for systemctl and systemd-run."""

    def __init__(
        self,
        *,
        runner: Callable = subprocess.run,
        environment: Mapping[str, str] | None = None,
        timeout: float = 15,
    ) -> None:
        """Create the adapter with an injectable runner and environment.

        Args:
            runner: Subprocess-compatible command runner.
            environment: Base environment for display-aware services.
            timeout: Maximum duration of each synchronous command.
        """
        self._runner = runner
        self.environment = dict(
            os.environ if environment is None else environment
        )
        self.timeout = timeout

    def _run(
        self, argv: Sequence[str], *, check: bool = True
    ) -> subprocess.CompletedProcess:
        try:
            result = logged(self._runner)(
                list(argv),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SystemdError(f"{argv[0]} failed: {exc}") from exc
        if check and result.returncode:
            detail = (result.stderr or result.stdout or "").strip()
            suffix = f": {detail}" if detail else ""
            raise SystemdError(
                f"{argv[0]} exited with status {result.returncode}{suffix}"
            )
        return result

    def start_transient(
        self,
        unit: str,
        command: Sequence[str],
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        """Start an application command in a transient user service.

        Args:
            unit: Valid ``.service`` unit name.
            command: Executable and arguments; no shell is used.
            environment: Additional string environment values.

        Raises:
            ValueError: If the unit, command, or environment is invalid.
            SystemdError: If systemd-run fails.
        """
        if not unit.endswith(".service") or not re.fullmatch(
            r"[A-Za-z0-9_.@-]+\.service", unit
        ):
            raise ValueError(f"invalid transient service name: {unit!r}")
        if (
            not command
            or not command[0]
            or any(
                not isinstance(arg, str) or "\x00" in arg for arg in command
            )
        ):
            raise ValueError(
                "application command must be a non-empty argument list"
            )
        argv = [
            "systemd-run",
            "--user",
            "--unit",
            unit.removesuffix(".service"),
            "--collect",
        ]
        merged = {
            key: value
            for key, value in self.environment.items()
            if key in _DISPLAY_ENV and value
        }
        if environment is not None:
            for key, value in environment.items():
                if (
                    not isinstance(key, str)
                    or not isinstance(value, str)
                    or "\x00" in value
                ):
                    raise ValueError(
                        "application environment must contain string values"
                    )
                merged[key] = value
        for key, value in sorted(merged.items()):
            if "\x00" in key or "=" in key:
                raise ValueError(f"invalid environment variable name: {key!r}")
            argv.extend(("--setenv", f"{key}={value}"))
        argv.extend(("--", *command))
        self._run(argv)

    def unit_state(self, unit: str) -> UnitState:
        """Query a unit's active and sub-state values."""
        result = self._run(
            [
                "systemctl",
                "--user",
                "show",
                "--property=ActiveState",
                "--property=SubState",
                "--value",
                unit,
            ],
            check=False,
        )
        if result.returncode:
            return UnitState(unit, False)
        lines = result.stdout.splitlines()
        active_state = lines[0].strip() if lines else ""
        sub_state = lines[1].strip() if len(lines) > 1 else ""
        return UnitState(unit, active_state == "active", sub_state)

    def is_active(self, unit: str) -> bool:
        """Return whether ``unit`` is in the active state."""
        return self.unit_state(unit).active

    def active_units(self, prefix: str = "kp-sync-app-") -> set[str]:
        """List active service names matching a validated prefix.

        Raises:
            ValueError: If ``prefix`` cannot be used in a unit glob.
            SystemdError: If systemctl cannot list units.
        """
        if not re.fullmatch(r"[A-Za-z0-9_.@-]+", prefix):
            raise ValueError(f"invalid systemd unit prefix: {prefix!r}")
        result = self._run(
            [
                "systemctl",
                "--user",
                "list-units",
                "--type=service",
                "--state=active",
                "--plain",
                "--no-legend",
                "--no-pager",
                f"{prefix}*.service",
            ]
        )
        units: set[str] = set()
        for line in result.stdout.splitlines():
            columns = line.split()
            if (
                columns
                and columns[0].startswith(prefix)
                and columns[0].endswith(".service")
            ):
                units.add(columns[0])
        return units

    def stop(self, unit: str) -> None:
        """Stop one user service."""
        self._run(["systemctl", "--user", "stop", unit])

    def enable(self, *units: str) -> None:
        """Enable the given user units when any were supplied."""
        if units:
            self._run(["systemctl", "--user", "enable", *units])

    def disable(self, *units: str) -> None:
        """Disable the given user units when any were supplied."""
        if units:
            self._run(["systemctl", "--user", "disable", *units])


def display_environment(
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return recognized, nonempty desktop environment variables."""
    source = os.environ if environment is None else environment
    return {key: source[key] for key in _DISPLAY_ENV if source.get(key)}
