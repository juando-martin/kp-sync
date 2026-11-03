"""Application launch, discovery, and stale-hub tracking."""

from __future__ import annotations

import errno
import configparser
import os
import re
import select
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kp_sync.daemon.dbus import DBusMonitor, KEEPASSXC_NAME
from kp_sync.daemon.systemd import SystemdUser, app_unit_name
from kp_sync.log import error


class AppError(RuntimeError):
    """Raised when an application cannot be started or managed."""

    pass


class NotificationError(RuntimeError):
    """Raised when desktop notification delivery fails."""

    pass


def notify_user(
    message: str,
    *,
    environment: Mapping[str, str] | None = None,
    runner: Callable = subprocess.run,
    timeout: float = 5,
) -> bool:
    """Show a bounded desktop notification when a display is available.

    Args:
        message: Nonempty notification text.
        environment: Environment used to reach the display.
        runner: Injectable subprocess-compatible command runner.
        timeout: Requested timeout, bounded to 0.1–30 seconds.

    Returns:
        ``False`` if there is no display, otherwise ``True`` on success.

    Raises:
        ValueError: If the message is empty or contains NUL.
        NotificationError: If notify-send cannot deliver it.
    """
    env = dict(os.environ if environment is None else environment)
    if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        return False
    if not isinstance(message, str) or not message or "\x00" in message:
        raise ValueError(
            "notification message must be a non-empty string without NUL bytes"
        )
    try:
        result = runner(
            [
                "notify-send",
                "--app-name=kp-sync",
                "--expire-time=10000",
                message,
            ],
            capture_output=True,
            text=True,
            timeout=min(max(timeout, 0.1), 30),
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise NotificationError(
            f"cannot show desktop notification: {exc}"
        ) from exc
    if result.returncode:
        detail = (result.stderr or "").strip()
        raise NotificationError(
            f"notify-send exited with status {result.returncode}"
            + (f": {detail}" if detail else "")
        )
    return True


@dataclass(frozen=True)
class AppStart:
    """Result describing how an application start was handled."""

    app: str
    unit: str
    started: bool
    raised: bool = False
    forwarded: bool = False


@dataclass(frozen=True)
class AppSpec:
    """Configured launch and process-discovery properties for an app."""

    name: str
    command: tuple[str, ...]
    processes: tuple[str, ...]
    reloads: bool
    single_instance: bool


def app_specs(config: Any) -> dict[str, AppSpec]:
    """Build built-in and configured application specifications.

    Args:
        config: Loaded configuration with an ``apps`` mapping.

    Returns:
        Application specs keyed by configured application name.
    """
    specs: dict[str, AppSpec] = {
        "keepassxc": AppSpec(
            "keepassxc", ("keepassxc",), ("keepassxc",), True, True
        ),
        "keepass-legacy": AppSpec(
            "keepass-legacy", ("keepass",), ("keepass", "KeePass"), False, True
        ),
    }
    for name, value in config.apps.items():
        specs[name] = AppSpec(
            name=name,
            command=tuple(value.get("command", ())),
            processes=tuple(value.get("process", ())),
            reloads=bool(value.get("reloads", False)),
            single_instance=bool(value.get("single_instance", False)),
        )
    return specs


class AppManager:
    """Keep app lifetime independent of its requesting client."""

    def __init__(
        self,
        config: Any,
        systemd: Any | None = None,
        dbus: Any | None = None,
        *,
        runner: Callable = subprocess.run,
        proc_root: str | os.PathLike[str] = "/proc",
        environment: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        notifier: Callable[[str], bool] | None = None,
    ) -> None:
        """Create an application manager with injectable adapters.

        Args:
            config: Loaded profile configuration.
            systemd: Optional systemd user adapter.
            dbus: Optional D-Bus monitor.
            runner: Injectable subprocess runner for process inspection.
            proc_root: Process filesystem root used for discovery.
            environment: Environment for GUI and D-Bus operations.
            clock: Monotonic time source for refresh scheduling.
            notifier: Optional callback for desktop notifications.

        Notes:
            Discovers application processes and may notify if KeePassXC
            reload support is unavailable.
        """
        self.config = config
        self.systemd = systemd or SystemdUser(environment=environment)
        self.environment = dict(
            os.environ if environment is None else environment
        )
        self._runner = runner
        self._clock = clock
        self._notify_enabled = bool(config.ui.get("notify", True))
        self._notifier = (
            notifier
            if notifier is not None
            else lambda message: notify_user(
                message,
                environment=self.environment,
                runner=self._runner,
            )
        )
        self.proc_root = Path(proc_root)
        self.specs = app_specs(config)
        self._keepassxc_reload_warning = ""
        keepassxc = self.specs["keepassxc"]
        if keepassxc.reloads and not self._keepassxc_reload_enabled():
            self.specs["keepassxc"] = AppSpec(
                keepassxc.name,
                keepassxc.command,
                keepassxc.processes,
                False,
                keepassxc.single_instance,
            )
            self._keepassxc_reload_warning = (
                "KeePassXC automatic external reload is disabled or "
                "unverified; "
                "treating its saves as foreign after hub updates"
            )
            self._notify(self._keepassxc_reload_warning)
        self._rescan_interval = max(
            0, int(config.daemon.get("poll_interval", 900))
        )
        self._last_discovery: float | None = None
        self.dbus = dbus or DBusMonitor(
            environment=self.environment,
            on_change=self._dbus_changed,
            resolve_pid_on_signal=False,
        )
        if hasattr(self.dbus, "on_change"):
            self.dbus.on_change = self._dbus_changed
        self._dbus_pids: dict[str, int] = {}
        self._dbus_owner_active = False
        self._known_pids: dict[str, set[int]] = {
            name: set() for name in self.specs
        }
        self._process_pids: dict[str, set[int]] = {
            name: set() for name in self.specs
        }
        self._pidfds: dict[int, int] = {}
        self._pidfd_fallback: set[int] = set()
        self._stale_apps: set[str] = set()
        self._active_units: set[str] = set()
        self._started_units: set[str] = set()
        self.refresh(force=True)

    @property
    def foreign_saves(self) -> frozenset[str]:
        return frozenset(self._stale_apps)

    @property
    def foreign_writers(self) -> bool:
        return bool(self._stale_apps)

    @property
    def reload_warnings(self) -> tuple[str, ...]:
        warnings = [
            f"{name} does not reload external hub updates; "
            "saves remain foreign until reopened"
            for name in sorted(self._stale_apps)
        ]
        if self._keepassxc_reload_warning:
            warnings.insert(0, self._keepassxc_reload_warning)
        return tuple(warnings)

    def _notify(self, message: str) -> None:
        if not self._notify_enabled:
            return
        try:
            self._notifier(message)
        except NotificationError as exc:
            error(f"desktop notification failed: {exc}")

    def _keepassxc_reload_enabled(self) -> bool:
        home = Path(self.environment.get("HOME", str(Path.home())))
        settings = home / ".config/keepassxc/keepassxc.ini"
        parser = configparser.ConfigParser(interpolation=None)
        try:
            with settings.open(encoding="utf-8") as source:
                parser.read_file(source)
        except FileNotFoundError:
            return False
        except (OSError, configparser.Error) as exc:
            raise AppError(
                f"cannot read KeePassXC reload setting at {settings}: {exc}"
            ) from exc
        for section in parser.sections():
            for option in (
                "AutoReloadOnChange",
                "AutoReloadDatabase",
                "ReloadOnChange",
            ):
                if parser.has_option(section, option):
                    try:
                        return parser.getboolean(
                            section, option, fallback=False
                        )
                    except ValueError as exc:
                        raise AppError(
                            "invalid KeePassXC external-reload setting in "
                            f"{settings}"
                        ) from exc
        return True

    @property
    def pidfd_fallback_pids(self) -> frozenset[int]:
        return frozenset(self._pidfd_fallback)

    def _unit(self, app_name: str) -> str:
        if app_name not in self.specs:
            raise AppError(f"unknown application: {app_name}")
        return app_unit_name(self.config.profile, app_name)

    def _dbus_changed(
        self, name: str, old_owner: str, new_owner: str, pid: int | None
    ) -> None:
        if name == KEEPASSXC_NAME:
            if new_owner and pid:
                self._dbus_pids[name] = pid
                self._dbus_owner_active = True
            elif new_owner:
                self._dbus_owner_active = True
            else:
                self._dbus_pids.pop(name, None)
                self._dbus_owner_active = False

    def refresh(self, *, force: bool = False) -> set[str]:
        """Refresh leases; poll_interval bounds fallback discovery."""
        self.dbus.start()
        if hasattr(self.dbus, "poll_signals"):
            self.dbus.poll_signals()
        systemd_events = getattr(self.dbus, "systemd_events", 0)
        now = self._clock()
        due = (
            force
            or self._last_discovery is None
            or (
                self._rescan_interval > 0
                and now - self._last_discovery >= self._rescan_interval
            )
        )
        if due:
            discovered = self.dbus.discover()
            if KEEPASSXC_NAME in discovered:
                self._dbus_owner_active = True
            else:
                self._dbus_owner_active = False
                self._dbus_pids.pop(KEEPASSXC_NAME, None)
            for bus_name, pid in discovered.items():
                if bus_name == KEEPASSXC_NAME:
                    self._dbus_pids[bus_name] = pid
            prefix = (
                f"kp-sync-app-{self.config.profile}-"
                if self.config.profile
                else "kp-sync-app-"
            )
            self._active_units = set(self.systemd.active_units(prefix))
            known_units = {self._unit(name) for name in self.specs}
            self._started_units.intersection_update(self._active_units)
            self._started_units.update(self._active_units & known_units)
            self._process_pids = {name: set() for name in self.specs}
            for app_name, spec in self.specs.items():
                if spec.processes:
                    self._process_pids[app_name] = self._scan_processes(
                        spec.processes
                    )
            self._last_discovery = now
        elif systemd_events:
            prefix = (
                f"kp-sync-app-{self.config.profile}-"
                if self.config.profile
                else "kp-sync-app-"
            )
            self._active_units = set(self.systemd.active_units(prefix))
        self._drop_exited_pidfds()
        self._known_pids = {
            name: set(pids) for name, pids in self._process_pids.items()
        }
        for app_name in self.specs:
            if self._unit(app_name) in self._active_units:
                self._known_pids[app_name].add(-1)
        keepassxc_pid = self._dbus_pids.get(KEEPASSXC_NAME)
        if keepassxc_pid is not None:
            self._known_pids["keepassxc"].add(keepassxc_pid)
        if self._dbus_owner_active:
            self._known_pids["keepassxc"].add(-2)
        active_pids = set().union(*self._known_pids.values()) - {-1, -2}
        self._refresh_pidfds(active_pids)
        return {
            f"app:{name}" for name, pids in self._known_pids.items() if pids
        }

    def _drop_exited_pidfds(self) -> None:
        if not self._pidfds:
            return
        poller = select.poll()
        for descriptor in self._pidfds.values():
            poller.register(
                descriptor, select.POLLIN | select.POLLHUP | select.POLLERR
            )
        ready = {descriptor for descriptor, _ in poller.poll(0)}
        exited = [
            pid
            for pid, descriptor in self._pidfds.items()
            if descriptor in ready
        ]
        for pid in exited:
            os.close(self._pidfds.pop(pid))
            for pids in self._process_pids.values():
                pids.discard(pid)

    def _scan_processes(self, names: Sequence[str]) -> set[int]:
        wanted = {name.casefold() for name in names}
        matches: set[int] = set()
        try:
            entries = tuple(self.proc_root.iterdir())
        except OSError as exc:
            raise AppError(
                f"cannot inspect application processes: {exc}"
            ) from exc
        for entry in entries:
            if not entry.name.isdecimal():
                continue
            try:
                if entry.stat().st_uid != os.getuid():
                    continue
                comm = (entry / "comm").read_text(encoding="utf-8").strip()
                raw = (entry / "cmdline").read_bytes()
            except FileNotFoundError:
                continue
            except PermissionError as exc:
                raise AppError(
                    f"cannot inspect process {entry.name}: {exc}"
                ) from exc
            argv = [
                part.decode("utf-8", "replace")
                for part in raw.split(b"\0")
                if part
            ]
            executable = Path(argv[0]).name.casefold() if argv else ""
            if comm.casefold() in wanted or executable in wanted:
                matches.add(int(entry.name))
        return matches

    def _refresh_pidfds(self, pids: set[int]) -> None:
        for pid in set(self._pidfds) - pids:
            os.close(self._pidfds.pop(pid))
        self._pidfd_fallback.intersection_update(pids)
        if not hasattr(os, "pidfd_open"):
            self._pidfd_fallback.update(pids)
            return
        for pid in pids:
            if pid in self._pidfds or pid in self._pidfd_fallback:
                continue
            try:
                self._pidfds[pid] = os.pidfd_open(pid, 0)
            except OSError as exc:
                if exc.errno == errno.ESRCH:
                    continue
                if exc.errno in {
                    errno.ENOSYS,
                    errno.EINVAL,
                    errno.EPERM,
                    errno.EMFILE,
                }:
                    self._pidfd_fallback.add(pid)
                    continue
                raise AppError(
                    f"cannot track application process {pid}: {exc}"
                ) from exc

    def activate_existing(
        self,
        name: str,
        args: Sequence[str] = (),
        *,
        interactive: bool = False,
        environment: Mapping[str, str] | None = None,
    ) -> AppStart | None:
        spec = self.specs.get(name)
        if spec is None:
            raise AppError(f"unknown application: {name}")
        command = spec.command
        if not command:
            raise AppError(f"application {name} has no configured command")
        if any(not isinstance(arg, str) or "\x00" in arg for arg in args):
            raise AppError(
                "application arguments must be strings without NUL bytes"
            )
        if environment is not None:
            self.environment.update(environment)
            dbus_environment = getattr(self.dbus, "environment", None)
            if isinstance(dbus_environment, dict):
                dbus_environment.update(environment)
        leases = self.refresh(force=True)
        unit = self._unit(name)
        if spec.single_instance and f"app:{name}" in leases:
            if args:
                self._forward(command, args)
                return AppStart(name, unit, False, forwarded=True)
            raised = (
                self._raise_window(name, environment=environment)
                if interactive
                else False
            )
            return AppStart(name, unit, False, raised=raised)
        return None

    def start(
        self,
        name: str,
        args: Sequence[str] = (),
        *,
        interactive: bool = False,
        environment: Mapping[str, str] | None = None,
    ) -> AppStart:
        existing = self.activate_existing(
            name,
            args,
            interactive=interactive,
            environment=environment,
        )
        if existing is not None:
            return existing
        command = self.specs[name].command
        unit = self._unit(name)
        if not args and name in {"keepassxc", "keepass-legacy"}:
            args = (str(self.config.hub),)
        self.systemd.start_transient(
            unit,
            (*command, *args),
            environment=environment,
        )
        self._started_units.add(unit)
        self._active_units.add(unit)
        self._known_pids.setdefault(name, set()).add(-1)
        return AppStart(name, unit, True)

    def _forward(self, command: Sequence[str], args: Sequence[str]) -> None:
        try:
            result = self._runner(
                [*command, *args],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise AppError(
                f"cannot pass arguments to the running application: {exc}"
            ) from exc
        if result.returncode:
            detail = (result.stderr or "").strip()
            raise AppError(
                "application did not accept the forwarded arguments"
                + (f": {detail}" if detail else "")
            )

    def _raise_window(
        self, name: str, *, environment: Mapping[str, str] | None = None
    ) -> bool:
        current_environment = dict(self.environment)
        if environment is not None:
            current_environment.update(environment)
        if name != "keepassxc" or not current_environment.get("DISPLAY"):
            return False
        try:
            result = self._runner(
                ["wmctrl", "-x", "-a", "keepassxc"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise AppError(
                f"cannot raise the KeePassXC window: {exc}"
            ) from exc
        if result.returncode:
            self._forward(self.specs[name].command, (str(self.config.hub),))
        return True

    def stop(self, name: str) -> None:
        self.systemd.stop(self._unit(name))

    def stop_all(self, *, names: Sequence[str] | None = None) -> None:
        for name in names if names is not None else tuple(self.specs):
            if self.systemd.is_active(self._unit(name)):
                self.systemd.stop(self._unit(name))

    def stop_started(self) -> None:
        for unit in sorted(self._started_units):
            if self.systemd.is_active(unit):
                self.systemd.stop(unit)
            self._active_units.discard(unit)
            for name in self.specs:
                if self._unit(name) == unit:
                    self._known_pids[name].discard(-1)
                    if not self._known_pids[name]:
                        self._stale_apps.discard(name)
            self._started_units.discard(unit)

    def hub_updated(self) -> None:
        newly_stale: list[str] = []
        for name, pids in self._known_pids.items():
            if (
                pids
                and not self.specs[name].reloads
                and name not in self._stale_apps
            ):
                self._stale_apps.add(name)
                newly_stale.append(name)
        for name in newly_stale:
            self._notify(
                f"{name} may still hold an older database. "
                "Reopen it before saving "
                "to avoid overwriting newer changes."
            )

    def hub_opened(
        self, pid: int | None = None, *, app_name: str | None = None
    ) -> bool:
        """Record user reopens after updates, excluding daemon opens."""
        if app_name is None and pid is not None:
            candidates = [
                name
                for name, pids in self._known_pids.items()
                if pid in pids and name in self._stale_apps
            ]
            if len(candidates) == 1:
                app_name = candidates[0]
        elif app_name is None:
            active_stale = [
                name
                for name, pids in self._known_pids.items()
                if pids and name in self._stale_apps
            ]
            active_all = [
                name for name, pids in self._known_pids.items() if pids
            ]
            if len(active_stale) == 1 and active_all == active_stale:
                app_name = active_stale[0]
        if app_name not in self._stale_apps:
            return False
        active = [name for name, pids in self._known_pids.items() if pids]
        if len(active) != 1 or active[0] != app_name:
            return False
        if pid is not None and pid not in self._known_pids[app_name]:
            return False
        self._stale_apps.remove(app_name)
        return True

    def save_is_foreign(self, app_name: str) -> bool:
        if app_name not in self.specs:
            raise AppError(f"unknown application: {app_name}")
        return app_name in self._stale_apps

    def close(self) -> None:
        self.dbus.close()
        for descriptor in self._pidfds.values():
            os.close(descriptor)
        self._pidfds.clear()
        self._pidfd_fallback.clear()

    def __enter__(self) -> AppManager:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
