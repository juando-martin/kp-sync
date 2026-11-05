"""Manage named SSH-session and access-owner transient services."""

import hashlib
import os
import re
import signal
import socket
import stat
import subprocess
import time
from pathlib import Path

from kp_sync.log import error, info, logged

_RESOURCE_KINDS = {"ssh-session", "access-owner"}
_ACTIVE_STATES = {"active", "activating", "deactivating", "reloading"}
_DAEMON_READINESS_TIMEOUT = 10
_DAEMON_SOCKET_TIMEOUT = 2
_DAEMON_SYSTEMCTL_TIMEOUT = 15
_DAEMON_STOP_TIMEOUT = 180 + 15


class ServiceError(RuntimeError):
    """Raised when a configured transient service cannot be managed."""

    pass


def service_unit(config, resource: str, name: str) -> str:
    """Return the transient systemd unit name for a resource."""
    if resource not in _RESOURCE_KINDS:
        raise ServiceError(f"unknown service type: {resource}")
    services = (
        config.ssh_sessions
        if resource == "ssh-session"
        else config.access_owners
    )
    if name not in services:
        raise ServiceError(f"no configured {resource} named {name!r}")

    profile = f"profile-{config.profile}" if config.profile else "default"
    if config.profile is None and config.source is not None:
        digest = hashlib.sha256(
            str(config.source.resolve()).encode()
        ).hexdigest()[:8]
        profile = f"{config.source.stem[:20]}-{digest}"
    profile = re.sub(r"[^A-Za-z0-9_-]", "-", profile)
    if len(profile) > 32:
        digest = hashlib.sha256(profile.encode()).hexdigest()[:8]
        profile = f"{profile[:23]}-{digest}"
    return f"kp-sync-{resource}-{profile}-{name}.service"


def _systemctl(args, *, runner=subprocess.run):
    try:
        return logged(runner)(
            ["systemctl", "--user", *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise ServiceError(f"cannot run systemctl --user: {exc}") from exc


def _state(unit: str, *, runner=subprocess.run) -> str:
    result = _systemctl(["is-active", unit], runner=runner)
    if result.returncode not in {0, 3, 4}:
        raise ServiceError(
            result.stderr.strip() or f"systemctl failed for {unit}"
        )
    return result.stdout.strip() or "inactive"


def _daemon_unit(config) -> str:
    """Return the unit, rejecting unsupported config-file mappings."""
    from kp_sync.integration import unit_names

    if config.profile is None and config.source is not None:
        default_config = (
            Path.home() / ".config" / "kp-sync" / "config.toml"
        ).resolve()
        if config.source.resolve() != default_config:
            raise ServiceError(
                "daemon service controls do not support --config; "
                "use the default config path or a named --profile"
            )
    return unit_names(config)["service"]


def _daemon_systemctl(
    args, *, runner=subprocess.run, timeout=_DAEMON_SYSTEMCTL_TIMEOUT
):
    try:
        return logged(runner)(
            ["systemctl", "--user", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ServiceError(f"cannot run systemctl --user: {exc}") from exc


def _daemon_state(unit: str, *, runner=subprocess.run) -> str:
    result = _daemon_systemctl(
        ["is-active", unit],
        runner=runner,
    )
    if result.returncode not in {0, 3, 4}:
        raise ServiceError(
            result.stderr.strip() or f"systemctl failed for {unit}"
        )
    return result.stdout.strip() or "inactive"


def _connect_daemon_socket(client, *, timeout: float):
    """Connect with a timeout and validate the socket peer."""
    from kp_sync.daemon.protocol import Connection, peer_uid

    try:
        metadata = client.path.lstat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
    ):
        raise ServiceError("daemon socket must be private and user-owned")

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(client.path))
    except (FileNotFoundError, ConnectionRefusedError, TimeoutError):
        sock.close()
        return None
    except OSError:
        sock.close()
        raise
    try:
        uid = peer_uid(sock)
    except OSError:
        sock.close()
        raise
    if uid != os.getuid():
        sock.close()
        raise ServiceError("daemon peer belongs to another user")
    return Connection(sock)


def _wait_for_daemon_socket(config, *, timeout: float) -> bool:
    """Wait for a successful, bounded daemon status round trip."""
    from kp_sync.daemon.client import Client
    from kp_sync.daemon.protocol import ProtocolError

    client = Client(config, starter=lambda: None)
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        connection = _connect_daemon_socket(
            client,
            timeout=min(_DAEMON_SOCKET_TIMEOUT, remaining),
        )
        if connection is None:
            time.sleep(min(0.05, remaining))
            continue
        try:
            socket_timeout = min(_DAEMON_SOCKET_TIMEOUT, remaining)
            connection.socket.settimeout(socket_timeout)
            connection.send(
                {
                    "command": "status",
                    "wait": True,
                    "ui": "none",
                    "environment": {},
                }
            )
            response = connection.receive()
            response_type = response.get("type")
            if response_type == "error":
                message = response.get("message")
                if not isinstance(message, str) or not message:
                    raise ServiceError(
                        "daemon returned an invalid status error"
                    )
                raise ServiceError(f"daemon status request failed: {message}")
            if response_type != "result" or not isinstance(
                response.get("result"), dict
            ):
                raise ServiceError(
                    "daemon returned an invalid status response"
                )
            return True
        except (OSError, EOFError, ProtocolError):
            pass
        finally:
            connection.close()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))


def _check_daemon_shutdown(unit: str, *, runner=subprocess.run) -> None:
    """Reject systemd stops that required abnormal termination."""
    result = _daemon_systemctl(
        [
            "show",
            "--property=Result",
            "--property=ExecMainCode",
            "--property=ExecMainStatus",
            "--property=ActiveState",
            "--no-pager",
            unit,
        ],
        runner=runner,
    )
    if result.returncode != 0:
        raise ServiceError(
            result.stderr.strip() or f"could not inspect shutdown of {unit}"
        )
    values = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    required = {"Result", "ExecMainCode", "ExecMainStatus"}
    if not required.issubset(values):
        raise ServiceError(f"could not verify shutdown result for {unit}")
    failure = values.get("Result", "")
    code = values.get("ExecMainCode", "")
    code = {
        "1": "exited",
        "2": "killed",
        "3": "dumped",
    }.get(code, code)
    status_text = values.get("ExecMainStatus", "")
    try:
        status = int(status_text)
    except ValueError as exc:
        raise ServiceError(
            f"systemd reported an invalid exit status for {unit}"
        ) from exc
    if (
        code == "0"
        and failure == "success"
        and values.get("ActiveState") == "inactive"
    ):
        # No exit record: once its triggers are stopped, nothing refers
        # to the stopped unit, so systemd unloads it at once. An unclean
        # stop leaves the unit failed, and failed units keep their
        # record, so this combination means a clean stop.
        return
    if not failure or code not in {"exited", "killed", "dumped"}:
        raise ServiceError(f"could not verify shutdown result for {unit}")
    if failure and failure != "success":
        raise ServiceError(
            f"daemon shutdown was not clean ({failure}) for {unit}"
        )
    if code == "exited" and status != 0:
        raise ServiceError(
            f"daemon exited with status {status} while stopping {unit}"
        )
    if code == "dumped" or (code == "killed" and status != signal.SIGTERM):
        raise ServiceError(
            f"daemon was terminated by signal {status} while stopping {unit}"
        )


def manage_daemon_service(
    config,
    action: str,
    *,
    runner=subprocess.run,
    readiness=None,
    now=False,
    desktop=False,
    paths=None,
) -> int:
    """Manage the configured daemon service without using daemon IPC.

    Status only queries systemd. Starting and restarting wait up to ten
    seconds for a successful status response from the daemon protocol.
    Stopping leaves socket activation enabled, so a later request can
    start the daemon again.
    """
    if action not in {
        "start",
        "stop",
        "restart",
        "status",
        "enable",
        "disable",
        "install",
        "uninstall",
    }:
        raise ServiceError(f"unknown daemon service action: {action}")
    unit = _daemon_unit(config)
    if now and action not in {"enable", "disable"}:
        raise ServiceError("--now is only valid for enable/disable")
    if desktop and action not in {"install", "uninstall"}:
        raise ServiceError("--desktop is only valid for install/uninstall")
    if action == "install":
        from kp_sync.integration import installed_executable, install_units

        installed = install_units(
            config,
            paths=paths,
            executable=installed_executable(),
            enable=False,
            desktop=desktop,
            runner=runner,
            check_executable=True,
        )
        result = _daemon_systemctl(["daemon-reload"], runner=runner)
        if result.returncode:
            raise ServiceError(
                result.stderr.strip()
                or "files installed, but systemd daemon-reload failed"
            )
        for path in installed:
            info(f"installed {path}")
        info("activation unchanged; use service enable daemon [--now]")
        return 0
    if action in {"enable", "disable", "uninstall"}:
        return _manage_activation(
            config,
            action,
            now=now,
            desktop=desktop,
            paths=paths,
            runner=runner,
        )
    state = _daemon_state(unit, runner=runner)

    if action == "status":
        print(f"{unit}: {state}")
        from kp_sync.integration import unit_names

        names = unit_names(config)
        for kind in ("socket", "path", "timer"):
            trigger = names[kind]
            result = _daemon_systemctl(["is-enabled", trigger], runner=runner)
            enabled = result.stdout.strip()
            if result.returncode not in {0, 1, 3, 4} or not enabled:
                raise ServiceError(
                    result.stderr.strip()
                    or f"could not inspect enablement of {trigger}"
                )
            active = _daemon_state(trigger, runner=runner)
            print(f"{trigger}: {active}; {enabled}")
        return 0 if state in _ACTIVE_STATES else 1

    if action == "stop":
        if state not in _ACTIVE_STATES:
            info(f"{unit} is not running; activation settings unchanged")
            return 0
        result = _daemon_systemctl(
            ["stop", unit], runner=runner, timeout=_DAEMON_STOP_TIMEOUT
        )
        if result.returncode != 0:
            raise ServiceError(
                result.stderr.strip() or f"could not stop {unit}"
            )
        _check_daemon_shutdown(unit, runner=runner)
        info(
            f"stopped {unit}; activation settings unchanged; enabled "
            "triggers can start it again"
        )
        return 0

    if action == "restart":
        if state in _ACTIVE_STATES:
            result = _daemon_systemctl(
                ["stop", unit], runner=runner, timeout=_DAEMON_STOP_TIMEOUT
            )
            if result.returncode != 0:
                raise ServiceError(
                    result.stderr.strip() or f"could not stop {unit}"
                )
            _check_daemon_shutdown(unit, runner=runner)
        result = _daemon_systemctl(["start", unit], runner=runner)
        if result.returncode != 0:
            raise ServiceError(
                result.stderr.strip() or f"could not start {unit}"
            )
    elif state not in _ACTIVE_STATES:
        result = _daemon_systemctl(["start", unit], runner=runner)
        if result.returncode != 0:
            raise ServiceError(
                result.stderr.strip() or f"could not start {unit}"
            )

    check_ready = readiness or _wait_for_daemon_socket
    try:
        ready = check_ready(config, timeout=_DAEMON_READINESS_TIMEOUT)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise ServiceError(
            f"daemon socket did not become ready: {exc}"
        ) from exc
    if not ready:
        raise ServiceError(
            f"{unit} started but its control socket did not become ready "
            f"within {_DAEMON_READINESS_TIMEOUT} seconds"
        )
    info(f"{unit} is ready")
    return 0


def _manage_activation(config, action, *, now, desktop, paths, runner):
    from kp_sync.integration import (
        unit_names,
        uninstall_files,
        remove_installed_files,
    )

    names = unit_names(config)
    all_triggers = [names[kind] for kind in ("socket", "path", "timer")]
    configured = [names["socket"]]
    if config.daemon["hub_path_unit"]:
        configured.append(names["path"])
    if config.daemon["idle_sync"]:
        configured.append(names["timer"])

    def command(args, *, timeout=_DAEMON_SYSTEMCTL_TIMEOUT):
        result = _daemon_systemctl(args, runner=runner, timeout=timeout)
        if result.returncode:
            raise ServiceError(
                result.stderr.strip() or f"systemctl {' '.join(args)} failed"
            )
        return result.stdout.strip()

    files = (
        uninstall_files(config, paths=paths, desktop=desktop)
        if action == "uninstall"
        else []
    )
    loaded = {}
    for name in names.values():
        load = command(["show", "--property=LoadState", "--value", name])
        if load not in {"loaded", "not-found"}:
            raise ServiceError(f"cannot manage {name}: load state {load!r}")
        loaded[name] = load == "loaded"
    if action == "enable":
        required = [names["service"], *configured]
        if config.daemon["idle_sync"]:
            required.append(names["sync_service"])
        missing = [name for name in required if not loaded[name]]
        if missing:
            raise ServiceError(
                "units are not installed; run service install daemon first: "
                + ", ".join(missing)
            )
        command(["enable", *configured])
        if now:
            command(["start", *configured])
        info(
            "enabled daemon activation"
            + (" and started triggers" if now else "")
        )
        return 0

    triggers = [name for name in all_triggers if loaded[name]]
    if triggers:
        command(["disable", *triggers])
    if now or action == "uninstall":
        if triggers:
            command(["stop", *triggers], timeout=_DAEMON_STOP_TIMEOUT)
        if loaded[names["sync_service"]]:
            command(
                ["stop", names["sync_service"]], timeout=_DAEMON_STOP_TIMEOUT
            )
        if loaded[names["service"]]:
            manage_daemon_service(config, "stop", runner=runner)
    if action == "uninstall":
        for group in files:
            remove_installed_files(group)
        command(["daemon-reload"])
        info("removed selected-profile unit files; shared templates retained")
    info(
        "daemon activation disabled; explicit requests and desktop/udev "
        "launchers can still start it"
    )
    return 0


def manage_service(
    config,
    action: str,
    resource: str,
    name: str,
    *,
    runner=subprocess.run,
) -> int:
    """Start, stop, or report a configured transient user service.

    Commands use ``systemctl --user`` and ``systemd-run --user`` through
    ``runner``. Raises ``ServiceError`` if systemd rejects it.
    """
    unit = service_unit(config, resource, name)
    if (
        action == "start"
        and resource == "ssh-session"
        and not config.sessions["enabled"]
    ):
        raise ServiceError(
            "sessions are disabled; set enabled = true in [sessions]"
        )
    state = _state(unit, runner=runner)
    if action == "status":
        print(f"{unit}: {state}")
        return 0 if state in _ACTIVE_STATES else 1
    if action == "start":
        if state in _ACTIVE_STATES:
            info(f"{unit} is already running")
            return 0
        from kp_sync.sessions.session import kp_sync_executable

        command = [kp_sync_executable()]
        if config.profile:
            command.extend(("--profile", config.profile))
        elif config.source is not None:
            command.extend(("--config", str(config.source)))
        command.extend(("service-run", resource, name))
        result = logged(runner)(
            [
                "systemd-run",
                "--user",
                f"--unit={unit}",
                "--collect",
                f"--description=kp-sync {resource} {name}",
                "--property=Type=exec",
                "--property=KillMode=mixed",
                "--property=TimeoutStopSec=180",
                "--property=StandardOutput=null",
                "--property=StandardError=journal",
                "--",
                *command,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise ServiceError(
                result.stderr.strip() or f"could not start {unit}"
            )
        info(
            f"started {unit}; logs are available with "
            f"journalctl --user -u {unit}"
        )
        return 0
    if action == "stop":
        if state not in _ACTIVE_STATES:
            info(f"{unit} is not running")
            return 0
        result = _systemctl(["stop", unit], runner=runner)
        if result.returncode != 0:
            raise ServiceError(
                result.stderr.strip() or f"could not stop {unit}"
            )
        info(f"stopped {unit}")
        return 0
    raise ServiceError(f"unknown service action: {action}")


def run_service(config, resource: str, name: str) -> int:
    """Run one configured service owner in the foreground."""
    services = (
        config.ssh_sessions
        if resource == "ssh-session"
        else config.access_owners
    )
    settings = services.get(name)
    if settings is None:
        error(f"no configured {resource} named {name!r}")
        return 1
    try:
        if resource == "ssh-session":
            from kp_sync.sessions.session import run_session

            return run_session(
                config,
                settings["target"],
                mode=settings.get("mode"),
                mount_dir=settings.get("mount_dir"),
                unconfined=settings.get("unconfined", False),
                managed=True,
                ui="none",
            )
        if resource == "access-owner":
            return _run_owner(name, settings["command"])
        error(f"unknown service type: {resource}")
        return 1
    except (OSError, RuntimeError, ValueError) as exc:
        error(str(exc))
        return 1


def _run_owner(name: str, command) -> int:
    info(f"Starting configured kp-access-owner {name}")
    process = logged(subprocess.Popen)(
        list(command),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    handlers = {}

    def stop(*_):
        if process.poll() is None:
            process.terminate()

    try:
        for sig in (signal.SIGTERM, signal.SIGHUP):
            handlers[sig] = signal.signal(sig, stop)
        returncode = process.wait()
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    if returncode == -signal.SIGTERM:
        info(f"Stopped configured kp-access-owner {name}")
        return 0
    if returncode:
        error(
            f"configured kp-access-owner {name} exited with "
            f"status {returncode}"
        )
    else:
        info(f"Configured kp-access-owner {name} exited")
    return returncode
