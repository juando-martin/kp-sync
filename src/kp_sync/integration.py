"""Systemd, desktop-entry, and installation diagnostics for kp-sync."""

from __future__ import annotations

import configparser
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import uuid
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from kp_sync.config import (
    Config,
    ConfigError,
    baseline_store_problems,
    require_encrypted_hub,
)
from kp_sync.daemon.systemd import SystemdError, SystemdUser
from kp_sync.endpoints.rclone import RcloneEndpoint
from kp_sync.paths import atomic_write, run_dir
from kp_sync.pending import PendingError, PendingStore
from kp_sync.log import logged


class InstallError(RuntimeError):
    """Raised when unit, desktop-entry, or diagnostic setup fails."""

    pass


@dataclass(frozen=True)
class InstallPaths:
    """Destination directories for user units and desktop launchers."""

    unit_dir: Path
    applications_dir: Path
    autostart_dir: Path

    def __post_init__(self) -> None:
        for name in ("unit_dir", "applications_dir", "autostart_dir"):
            object.__setattr__(
                self, name, Path(getattr(self, name)).expanduser()
            )


@dataclass(frozen=True)
class Check:
    """One named diagnostic result and its user-facing explanation."""

    name: str
    ok: bool
    message: str


def default_install_paths(
    home: str | os.PathLike[str] | None = None,
) -> InstallPaths:
    """Return standard user-level unit and desktop paths."""
    root = Path.home() if home is None else Path(home).expanduser()
    return InstallPaths(
        root / ".config/systemd/user",
        root / ".local/share/applications",
        root / ".config/autostart",
    )


def unit_names(config: Config) -> dict[str, str]:
    """Return systemd unit names scoped to the selected profile."""
    if config.profile is None:
        return {
            "service": "kp-sync.service",
            "socket": "kp-sync.socket",
            "path": "kp-sync-hub.path",
            "timer": "kp-sync-sync.timer",
            "sync_service": "kp-sync-sync.service",
        }
    profile = config.profile
    return {
        "service": f"kp-sync@{profile}.service",
        "socket": f"kp-sync@{profile}.socket",
        "path": f"kp-sync-hub@{profile}.path",
        "timer": f"kp-sync-sync@{profile}.timer",
        "sync_service": f"kp-sync-sync@{profile}.service",
    }


def render_units(
    config: Config, *, executable: str = "kp-sync"
) -> dict[str, str]:
    """Return default templates and units for the selected profile."""
    if not re.fullmatch(r"[A-Za-z0-9_./+-]+", executable):
        raise ValueError("executable must be a non-empty command name")
    generated: dict[str, str] = {}
    generated["kp-sync@.service"] = _daemon_service(
        f"{executable} --profile %i daemon"
    )
    generated["kp-sync@.socket"] = _socket_unit(
        "%t/kp-sync-%i/control.sock", "kp-sync@%i.service"
    )
    generated["kp-sync-hub@.path"] = _path_unit(
        "%t/%i/pass.kdbx", "kp-sync@%i.service"
    )
    generated["kp-sync-sync@.timer"] = _timer_unit(
        "kp-sync-sync@%i.service", config.daemon["idle_sync_interval"]
    )
    generated["kp-sync-sync@.service"] = _sync_service(
        f"{executable} --profile %i sync"
    )

    names = unit_names(config)
    profile_arg = f" --profile {config.profile}" if config.profile else ""
    runtime_path = (
        f"%t/kp-sync-{config.profile}/control.sock"
        if config.profile
        else "%t/kp-sync/control.sock"
    )
    generated[names["service"]] = _daemon_service(
        f"{executable}{profile_arg} daemon"
    )
    generated[names["socket"]] = _socket_unit(runtime_path, names["service"])
    generated[names["path"]] = _path_unit(
        _systemd_path(str(config.hub)),
        names["service"],
    )
    generated[names["timer"]] = _timer_unit(
        names["sync_service"], config.daemon["idle_sync_interval"]
    )
    generated[names["sync_service"]] = _sync_service(
        f"{executable}{profile_arg} sync"
    )
    return generated


def render_desktop_entries(
    config: Config, *, executable: str = "kp-sync"
) -> dict[str, str]:
    """Render KeePassXC launch entries without writing files."""
    if not re.fullmatch(r"[A-Za-z0-9_./+-]+", executable):
        raise ValueError("executable must be a non-empty command name")
    profile = f" --profile {config.profile}" if config.profile else ""
    keepass = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=KeePassXC (kp-sync)\n"
        "Comment=Open the synchronized password database\n"
        "Icon=keepassxc\n"
        "Terminal=false\n"
        "Categories=Utility;Security;\n"
        "StartupNotify=true\n"
        f"Exec={executable}{profile} run --raise-existing "
        "--app keepassxc -- %f\n"
        "MimeType=application/x-keepass2;\n"
    )
    autostart = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=KeePassXC (kp-sync)\n"
        "Comment=Open the synchronized password database at login\n"
        "Icon=keepassxc\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
        f"Exec={executable}{profile} run --app keepassxc\n"
    )
    return {
        "org.keepassxc.KeePassXC.desktop": keepass,
        "kp-sync-keepassxc.desktop": autostart,
    }


def installed_executable() -> str:
    """Return the absolute path of this kp-sync for installed files.

    systemd resolves a bare ``ExecStart`` name only in fixed system
    directories, never in ``~/.local/bin`` or a virtual environment, so
    units name the program by path. Symlinks are kept, so retargeting
    a link such as ``~/.local/bin/kp-sync`` updates the installation.
    Fall back to the bare name when no usable path is found.
    """
    argv0 = Path(sys.argv[0])
    found = (
        str(argv0)
        if argv0.name == "kp-sync" and argv0.is_file()
        else shutil.which("kp-sync")
    )
    if found is None:
        return "kp-sync"
    path = os.path.abspath(found)
    if Path(path).name != "kp-sync" or not re.fullmatch(
        r"[A-Za-z0-9_./+-]+", path
    ):
        return "kp-sync"
    return path


def install_units(
    config: Config,
    *,
    home: str | os.PathLike[str] | None = None,
    paths: InstallPaths | None = None,
    unit_dir: str | os.PathLike[str] | None = None,
    applications_dir: str | os.PathLike[str] | None = None,
    autostart_dir: str | os.PathLike[str] | None = None,
    executable: str = "kp-sync",
    enable: bool = True,
    desktop: bool = True,
    runner: Callable = subprocess.run,
) -> tuple[Path, ...]:
    """Install user unit and desktop files; optionally enable units."""
    if paths is not None and any(
        value is not None
        for value in (unit_dir, applications_dir, autostart_dir)
    ):
        raise ValueError(
            "use either paths or individual installation directories"
        )
    if paths is None:
        defaults = default_install_paths(home)
        paths = InstallPaths(
            (
                Path(unit_dir).expanduser()
                if unit_dir is not None
                else defaults.unit_dir
            ),
            (
                Path(applications_dir).expanduser()
                if applications_dir is not None
                else defaults.applications_dir
            ),
            (
                Path(autostart_dir).expanduser()
                if autostart_dir is not None
                else defaults.autostart_dir
            ),
        )
    outputs: list[Path] = []
    directories = [paths.unit_dir]
    if desktop:
        directories.extend((paths.applications_dir, paths.autostart_dir))
    for directory in directories:
        _ensure_safe_directory(directory)
    for filename, contents in render_units(
        config, executable=executable
    ).items():
        outputs.append(_safe_write(paths.unit_dir / filename, contents))
    if desktop:
        desktops = render_desktop_entries(config, executable=executable)
        outputs.append(
            _safe_write(
                paths.applications_dir / "org.keepassxc.KeePassXC.desktop",
                desktops["org.keepassxc.KeePassXC.desktop"],
            )
        )
        outputs.append(
            _safe_write(
                paths.autostart_dir / "kp-sync-keepassxc.desktop",
                desktops["kp-sync-keepassxc.desktop"],
            )
        )
    if enable:
        names = unit_names(config)
        enabled = [names["socket"]]
        if config.daemon["hub_path_unit"]:
            enabled.append(names["path"])
        if config.daemon["idle_sync"]:
            enabled.append(names["timer"])
        try:
            SystemdUser(runner=runner).enable(*enabled)
        except SystemdError as exc:
            raise InstallError(
                f"unit files were written, but enabling them failed: {exc}"
            ) from exc
    return tuple(outputs)


def uninstall_files(
    config: Config,
    *,
    paths: InstallPaths | None = None,
    desktop: bool = False,
) -> list[dict[Path, str]]:
    """Inventory selected-profile files; keep shared templates intact.

    Remove only unmodified generated files. Include desktop entries only
    when their removal is explicitly requested.
    """
    import shlex

    paths = paths or default_install_paths()
    units = render_units(config)
    desktops = render_desktop_entries(config)
    groups = (
        [
            (paths.unit_dir / name, units[name])
            for name in unit_names(config).values()
        ],
        [
            (
                paths.applications_dir / "org.keepassxc.KeePassXC.desktop",
                desktops["org.keepassxc.KeePassXC.desktop"],
            ),
            (
                paths.autostart_dir / "kp-sync-keepassxc.desktop",
                desktops["kp-sync-keepassxc.desktop"],
            ),
        ],
    )
    result = []
    for group in groups if desktop else groups[:1]:
        found = {}
        for path, expected in group:
            for parent in path.parents:
                if parent.is_symlink():
                    raise InstallError(f"refusing symlink directory: {parent}")
            if not os.path.lexists(path):
                continue
            actual = _read_regular_file(path)
            # Installations may use an absolute executable path.
            key = "Exec=" if path.suffix == ".desktop" else "ExecStart="
            actual_exec = next(
                (
                    line[len(key) :]
                    for line in actual.splitlines()
                    if line.startswith(key)
                ),
                None,
            )
            if actual_exec is not None:
                words = shlex.split(actual_exec)
                if not words or Path(words[0]).name != "kp-sync":
                    raise InstallError(f"invalid executable in {path}")
                expected_exec = next(
                    line[len(key) :]
                    for line in expected.splitlines()
                    if line.startswith(key)
                )
                expected = expected.replace(
                    f"{key}{shlex.split(expected_exec)[0]}",
                    f"{key}{words[0]}",
                )
            if actual != expected:
                raise InstallError(
                    f"refusing modified or other-profile file: {path}"
                )
            found[path] = actual
        result.append(found)
    return result


def remove_installed_files(files: dict[Path, str]) -> None:
    """Remove preflighted files, refusing intervening changes."""
    from kp_sync.log import info

    for path, expected in files.items():
        if any(parent.is_symlink() for parent in path.parents):
            raise InstallError(f"installation directory changed: {path}")
        if _read_regular_file(path) != expected:
            raise InstallError(f"installation file changed: {path}")
        path.unlink()
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        info(f"removed {path}")


def session_checks(
    config: Config,
    *,
    runtime_dir: str | os.PathLike[str] | None = None,
    authkeys=None,
    runner: Callable = subprocess.run,
    sshd_probe: Callable[[], tuple[bool, str]] | None = None,
) -> list[Check]:
    """Read-only checks of the sshfs session prerequisites."""
    from kp_sync.sessions.authkeys import AuthorizedKeys, AuthorizedKeysError
    from kp_sync.sessions.sftp_serve import find_bwrap, find_sftp_server

    checks: list[Check] = []
    bwrap = find_bwrap()
    if bwrap is None:
        checks.append(Check("session:bwrap", False, "bwrap is not installed"))
    else:
        try:
            trial = logged(runner)(
                [
                    bwrap,
                    "--unshare-all",
                    "--ro-bind",
                    "/usr",
                    "/usr",
                    "--symlink",
                    "usr/bin",
                    "/bin",
                    "--symlink",
                    "usr/lib",
                    "/lib",
                    "--symlink",
                    "usr/lib64",
                    "/lib64",
                    "/usr/bin/true",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            ok = trial.returncode == 0
            message = (
                "bwrap runs unprivileged"
                if ok
                else f"bwrap trial failed: {trial.stderr.strip()}"
            )
        except (OSError, subprocess.SubprocessError) as exc:
            ok, message = False, f"bwrap trial failed: {exc}"
        checks.append(Check("session:bwrap", ok, message))
    server = find_sftp_server()
    checks.append(
        Check(
            "session:sftp-server",
            server is not None,
            server or "sftp-server not found",
        )
    )
    try:
        keys = authkeys or AuthorizedKeys(runtime_dir=runtime_dir)
        keys.check()
        stale = keys.read_lines(config.profile)
    except (AuthorizedKeysError, OSError) as exc:
        checks.append(Check("session:authorized_keys", False, str(exc)))
    else:
        checks.append(
            Check(
                "session:authorized_keys",
                True,
                "authorized_keys can be edited",
            )
        )
        from kp_sync.sessions.records import SessionStore

        directory = run_dir(config.profile, xdg_runtime_dir=runtime_dir)
        live = (
            SessionStore(directory).live_ids()
            | SessionStore(directory / "diagnostics").live_ids()
        )
        leftover = sorted(
            session_id for session_id, _ in stale if session_id not in live
        )
        checks.append(
            Check(
                "session:stale-keys",
                not leftover,
                (
                    "no stale session keys"
                    if not leftover
                    else f"stale session keys: {', '.join(leftover)}"
                ),
            )
        )
    if sshd_probe is None:

        def sshd_probe() -> tuple[bool, str]:
            try:
                with socket.create_connection(
                    ("localhost", 22), timeout=3
                ) as connection:
                    banner = connection.recv(64)
            except OSError as exc:
                return False, f"sshd is not reachable on localhost:22: {exc}"
            if banner.startswith(b"SSH-"):
                return True, "sshd answers on localhost:22"
            return False, "localhost:22 does not speak ssh"

    ok, message = sshd_probe()
    checks.append(Check("session:sshd", ok, message))
    return checks


def doctor(
    config: Config,
    *,
    home: str | os.PathLike[str] | None = None,
    paths: InstallPaths | None = None,
    runtime_dir: str | os.PathLike[str] | None = None,
    runner: Callable = subprocess.run,
    socket_probe: Callable[[Path], tuple[bool, str]] | None = None,
    keyring_probe: Callable[[], tuple[bool, str]] | None = None,
    swap_probe: Callable[[], tuple[bool, str]] | None = None,
    encrypted_hub_probe: Callable[[Config], tuple[bool, str]] | None = None,
    self_test: bool = False,
    environ: dict | None = None,
) -> list[Check]:
    """Run diagnostics; session round trips may edit authorized_keys.

    ``environ`` supplies ``XDG_CONFIG_HOME``; it defaults to the process
    environment only when ``home`` is not given, so an explicit home
    never mixes with the real user's configuration.
    """
    root = Path.home() if home is None else Path(home).expanduser()
    if environ is None:
        environ = dict(os.environ) if home is None else {}
    install_paths = paths or default_install_paths(root)
    checks: list[Check] = []
    checks.extend(
        Check(f"baseline:{index}", True, f"WARNING: {problem}")
        for index, problem in enumerate(
            baseline_store_problems(
                config.endpoints, config.baseline["stores"]
            ),
            1,
        )
    )
    units = unit_names(config)
    expected_files = {
        "daemon service": install_paths.unit_dir / units["service"],
        "socket unit": install_paths.unit_dir / units["socket"],
        "hub path unit": install_paths.unit_dir / units["path"],
        "idle sync timer": install_paths.unit_dir / units["timer"],
        "idle sync service": install_paths.unit_dir / units["sync_service"],
    }
    for label, path in expected_files.items():
        checks.append(
            _path_check(f"unit:{label}", path, "unit file is installed")
        )
    try:
        service_text = _read_regular_file(expected_files["daemon service"])
        service_ok = all(
            directive in service_text
            for directive in (
                "KeyringMode=shared",
                "Restart=on-failure",
                "TimeoutStopSec=90",
            )
        )
        checks.append(
            Check(
                "unit:daemon-safety",
                service_ok,
                (
                    "daemon service has shared keyring, failure "
                    "restart, and bounded stop"
                    if service_ok
                    else (
                        "daemon service must set KeyringMode=shared, "
                        "Restart=on-failure, and TimeoutStopSec=90"
                    )
                ),
            )
        )
    except (OSError, InstallError) as exc:
        checks.append(
            Check(
                "unit:daemon-safety",
                False,
                f"cannot inspect daemon service safety: {exc}",
            )
        )
    try:
        socket_text = _read_regular_file(expected_files["socket unit"])
        socket_ok = (
            "SocketMode=0600" in socket_text and "ListenStream=" in socket_text
        )
        checks.append(
            Check(
                "unit:socket-safety",
                socket_ok,
                (
                    "control socket has a configured path and mode 0600"
                    if socket_ok
                    else (
                        "control socket unit must configure ListenStream "
                        "and SocketMode=0600"
                    )
                ),
            )
        )
    except (OSError, InstallError) as exc:
        checks.append(
            Check(
                "unit:socket-safety",
                False,
                f"cannot inspect control socket unit: {exc}",
            )
        )

    enabled_units = [units["socket"]]
    if config.daemon["hub_path_unit"]:
        enabled_units.append(units["path"])
    if config.daemon["idle_sync"]:
        enabled_units.append(units["timer"])
    for unit in enabled_units:
        try:
            result = _run(
                runner, ["systemctl", "--user", "is-enabled", unit], timeout=5
            )
            if result.returncode == 0 and result.stdout.strip() in {
                "enabled",
                "enabled-runtime",
                "static",
                "indirect",
            }:
                checks.append(
                    Check(f"unit-enabled:{unit}", True, f"{unit} is enabled")
                )
            else:
                detail = (result.stdout or result.stderr or "").strip()
                checks.append(
                    Check(
                        f"unit-enabled:{unit}",
                        False,
                        f"{unit} is not enabled; run systemctl --user "
                        f"enable {unit}" + (f" ({detail})" if detail else ""),
                    )
                )
        except (OSError, subprocess.SubprocessError) as exc:
            checks.append(
                Check(
                    f"unit-enabled:{unit}",
                    False,
                    f"cannot query {unit}: {exc}",
                )
            )

    run_directory = run_dir(
        config.profile,
        xdg_runtime_dir=runtime_dir,
    )
    try:
        PendingStore(run_directory).load()
    except PendingError as exc:
        checks.append(
            Check(
                "pending-state", False, f"pending decisions need repair: {exc}"
            )
        )
    else:
        checks.append(
            Check("pending-state", True, "pending decisions file is valid")
        )
    control_socket = run_directory / "control.sock"
    if socket_probe is None:
        socket_probe = _probe_control_socket
    try:
        ok, detail = socket_probe(control_socket)
    except (OSError, ValueError, RuntimeError) as exc:
        ok, detail = False, str(exc)
    checks.append(
        Check(
            "socket-protocol",
            ok,
            (
                "control socket is reachable and speaks protocol version 1"
                if ok
                else (
                    "control socket/protocol check failed at "
                    f"{control_socket}: {detail}"
                )
            ),
        )
    )

    desktop = (
        install_paths.applications_dir / "org.keepassxc.KeePassXC.desktop"
    )
    try:
        contents = _read_regular_file(desktop)
        exec_line = next(
            (
                line.removeprefix("Exec=").strip()
                for line in contents.splitlines()
                if line.startswith("Exec=")
            ),
            "",
        )
        good = (
            bool(re.search(r"(?:^|\s)run(?:\s|$)", exec_line))
            and "kp-sync" in exec_line
        )
        checks.append(
            Check(
                "desktop-override",
                good,
                (
                    "KeePassXC desktop override calls kp-sync run"
                    if good
                    else (
                        "KeePassXC desktop override is missing or no "
                        "longer calls kp-sync run; reinstall it"
                    )
                ),
            )
        )
    except (OSError, InstallError) as exc:
        checks.append(
            Check(
                "desktop-override",
                False,
                f"KeePassXC desktop override is unavailable: {exc}",
            )
        )

    autostart = install_paths.autostart_dir / "kp-sync-keepassxc.desktop"
    try:
        contents = _read_regular_file(autostart)
        exec_line = next(
            (
                line.removeprefix("Exec=").strip()
                for line in contents.splitlines()
                if line.startswith("Exec=")
            ),
            "",
        )
        good = "kp-sync" in exec_line and bool(
            re.search(r"(?:^|\s)run(?:\s|$)", exec_line)
        )
        checks.append(
            Check(
                "autostart-entry",
                good,
                (
                    "desktop autostart calls kp-sync run"
                    if good
                    else (
                        "desktop autostart entry is missing or no "
                        "longer calls kp-sync run"
                    )
                ),
            )
        )
    except (OSError, InstallError) as exc:
        checks.append(
            Check(
                "autostart-entry",
                False,
                f"desktop autostart entry is unavailable: {exc}",
            )
        )

    from kp_sync.paths import keepassxc_settings_path

    ini_path = keepassxc_settings_path(root, environ)
    try:
        ini = configparser.ConfigParser(interpolation=None)
        with ini_path.open(encoding="utf-8") as source:
            ini.read_file(source)
        reloads = _keepassxc_reload_enabled(ini)
        checks.append(
            Check(
                "keepassxc-reload",
                reloads,
                (
                    "KeePassXC reloads external database changes"
                    if reloads
                    else (
                        "enable KeePassXC automatic reload of externally "
                        "modified databases"
                    )
                ),
            )
        )
    except (OSError, configparser.Error) as exc:
        checks.append(
            Check(
                "keepassxc-reload",
                False,
                "cannot verify KeePassXC external-reload setting in "
                f"{ini_path}: {exc}",
            )
        )

    key_path = config.key_file
    key_ok = (
        key_path.is_file()
        and not key_path.is_symlink()
        and os.access(key_path, os.R_OK)
    )
    checks.append(
        Check(
            "key-file",
            key_ok,
            (
                f"key file is readable: {key_path}"
                if key_ok
                else (
                    "configured key file is missing, unreadable, or a "
                    f"symlink: {key_path}"
                )
            ),
        )
    )

    checks.extend(_rclone_checks(config, runner, runtime_dir))
    if config.require_encrypted:
        try:
            ok, detail = (
                encrypted_hub_probe(config)
                if encrypted_hub_probe is not None
                else _encrypted_hub(config, runner)
            )
        except (
            OSError,
            subprocess.SubprocessError,
            ConfigError,
            ValueError,
        ) as exc:
            ok, detail = False, str(exc)
        checks.append(
            Check(
                "encrypted-hub",
                ok,
                (
                    "hub is on an encrypted filesystem"
                    if ok
                    else f"hub encryption check failed: {detail}"
                ),
            )
        )
    if swap_probe is None:

        def swap_probe() -> tuple[bool, str]:
            return _swap_encryption(runner)

    try:
        ok, detail = swap_probe()
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        ok, detail = False, str(exc)
    checks.append(
        Check(
            "encrypted-swap",
            ok,
            (
                "swap is absent or encrypted"
                if ok
                else f"swap is not verified encrypted: {detail}"
            ),
        )
    )
    if keyring_probe is None:
        keyring_probe = _diagnostic_keyring_probe(runner)
    try:
        ok, detail = keyring_probe()
    except (
        OSError,
        subprocess.SubprocessError,
        RuntimeError,
        ValueError,
    ) as exc:
        ok, detail = False, str(exc)
    checks.append(
        Check(
            "keyring-possession",
            ok,
            (
                "daemon user service can read a temporary diagnostic key"
                if ok
                else f"keyring possession check failed: {detail}"
            ),
        )
    )
    if config.sessions["enabled"]:
        checks.extend(session_checks(config, runtime_dir=runtime_dir))
    if self_test:
        from kp_sync.sessions.diagnostic import self_test as session_self_test

        try:
            session_self_test(
                config,
                runtime_directory=run_dir(
                    config.profile, xdg_runtime_dir=runtime_dir
                ),
                runner=runner,
            )
        except (
            OSError,
            RuntimeError,
            ValueError,
            subprocess.SubprocessError,
        ) as exc:
            checks.append(Check("session:self-test", False, str(exc)))
        else:
            checks.append(
                Check(
                    "session:self-test",
                    True,
                    "confined localhost SFTP put/get passed; key removed",
                )
            )
    return checks


def _daemon_service(exec_start: str) -> str:
    return (
        "[Unit]\n"
        "Description=kp-sync synchronization daemon\n"
        "After=default.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=2s\n"
        "KeyringMode=shared\n"
        "TimeoutStopSec=90\n"
        "PassEnvironment=DISPLAY WAYLAND_DISPLAY "
        "DBUS_SESSION_BUS_ADDRESS XAUTHORITY XDG_RUNTIME_DIR\n"
    )


def _socket_unit(path: str, service: str) -> str:
    return (
        "[Unit]\n"
        "Description=kp-sync control socket\n\n"
        "[Socket]\n"
        f"ListenStream={path}\n"
        "SocketMode=0600\n"
        "DirectoryMode=0700\n"
        "RemoveOnStop=true\n"
        f"Service={service}\n\n"
        "[Install]\n"
        "WantedBy=sockets.target\n"
    )


def _path_unit(path: str, service: str) -> str:
    return (
        "[Unit]\n"
        "Description=Watch the kp-sync hub database\n\n"
        "[Path]\n"
        f"PathChanged={path}\n"
        f"Unit={service}\n"
        "MakeDirectory=false\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _timer_unit(service: str, interval: int) -> str:
    return (
        "[Unit]\n"
        "Description=Periodic kp-sync idle synchronization\n\n"
        "[Timer]\n"
        "OnStartupSec=1min\n"
        f"OnUnitActiveSec={interval}s\n"
        "Persistent=true\n"
        f"Unit={service}\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def _sync_service(exec_start: str) -> str:
    return (
        "[Unit]\n"
        "Description=One kp-sync idle synchronization\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={exec_start}\n"
        "PassEnvironment=DISPLAY WAYLAND_DISPLAY "
        "DBUS_SESSION_BUS_ADDRESS XAUTHORITY XDG_RUNTIME_DIR\n"
    )


def _systemd_path(value: str) -> str:
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError("hub path contains a control character")
    return (
        value.replace("%", "%%")
        .replace("\\", "\\\\")
        .replace(" ", "\\x20")
        .replace("\t", "\\x09")
    )


def _ensure_safe_directory(path: Path) -> None:
    path = path.expanduser()
    if not path.is_absolute():
        raise InstallError(f"installation directory must be absolute: {path}")
    if ".." in path.parts:
        raise InstallError(
            f"installation path must not traverse parent directories: {path}"
        )
    components = [Path(path.anchor)]
    for part in path.parts[1:]:
        components.append(components[-1] / part)
    for component in components:
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise InstallError(
                f"cannot inspect installation path {component}: {exc}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise InstallError(
                f"refusing symlink in installation path: {component}"
            )
        if not stat.S_ISDIR(metadata.st_mode):
            raise InstallError(
                f"installation path component is not a directory: {component}"
            )
        if metadata.st_mode & 0o022:
            raise InstallError(
                f"installation path is group- or world-writable: {component}"
            )
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
    except OSError as exc:
        raise InstallError(
            f"cannot create installation directory {path}: {exc}"
        ) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o022
    ):
        raise InstallError(
            f"installation directory is not safely user-owned: {path}"
        )
    for component in components:
        if not os.path.lexists(component):
            continue
        metadata = component.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(
            metadata.st_mode
        ):
            raise InstallError(
                f"refusing unsafe installation path component: {component}"
            )
        if metadata.st_mode & 0o022:
            raise InstallError(
                f"installation path is group- or world-writable: {component}"
            )


def _safe_write(path: Path, content: str) -> Path:
    _ensure_safe_directory(path.parent)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        metadata = None
    except OSError as exc:
        raise InstallError(
            f"cannot inspect installation file {path}: {exc}"
        ) from exc
    if metadata is not None and (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o022
    ):
        raise InstallError(
            f"refusing unsafe existing installation file: {path}"
        )
    try:
        atomic_write(path, content)
    except OSError as exc:
        raise InstallError(
            f"cannot write installation file {path}: {exc}"
        ) from exc
    return path


def _read_regular_file(path: Path) -> str:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise InstallError(f"cannot open {path}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o022
        ):
            raise InstallError(f"unsafe file permissions or owner: {path}")
        with os.fdopen(descriptor, encoding="utf-8") as source:
            descriptor = -1
            return source.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _path_check(name: str, path: Path, success: str) -> Check:
    try:
        contents = _read_regular_file(path)
    except (OSError, InstallError) as exc:
        return Check(name, False, f"{path} is missing or unsafe: {exc}")
    if not contents:
        return Check(name, False, f"{path} is empty")
    return Check(name, True, success)


def _probe_control_socket(path: Path) -> tuple[bool, str]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        return False, str(exc)
    if not stat.S_ISSOCK(metadata.st_mode):
        return False, "control path is not a Unix socket"
    if (
        metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        return False, "control socket owner or permissions are unsafe"
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(3)
    try:
        client.connect(str(path))
        client.sendall(
            b'{"version":1,"command":"status","wait":true,"ui":"none"}\n'
        )
        response = bytearray()
        while len(response) < 1024 * 1024 + 1:
            chunk = client.recv(16384)
            if not chunk:
                break
            response.extend(chunk)
            if b"\n" in response:
                break
    except OSError as exc:
        return False, str(exc)
    finally:
        client.close()
    try:
        reply = json.loads(response.split(b"\n", 1)[0])
    except (ValueError, IndexError):
        return False, "daemon returned no valid JSON-lines protocol reply"
    if not isinstance(reply, dict) or reply.get("version") != 1:
        return False, "daemon protocol version does not match client version 1"
    if not any(key in reply for key in ("ok", "result", "error", "status")):
        return False, "daemon returned an unrecognized protocol reply"
    return True, "protocol version 1"


def _keepassxc_reload_enabled(ini: configparser.ConfigParser) -> bool:
    for section in ini.sections():
        for option in (
            "AutoReloadOnChange",
            "AutoReloadDatabase",
            "ReloadOnChange",
        ):
            if ini.has_option(section, option):
                return ini.getboolean(section, option, fallback=False)
    return True


def _rclone_checks(
    config: Config, runner: Callable, runtime_dir=None
) -> list[Check]:
    checks: list[Check] = []
    for endpoint in config.endpoints:
        # Hold the credentials lock shared while rclone uses the copy.
        with ExitStack() as stack:
            if endpoint.type != "rclone" or not endpoint.enabled:
                continue
            remote = endpoint.remote or ""
            configured = f"{remote.split(':', 1)[0]}:"
            argv = ["rclone", "listremotes"]
            config_path = None
            if endpoint.rclone_config is not None:
                from kp_sync.paths import run_dir
                from kp_sync.rclone_credentials import RcloneCredentials

                credentials = RcloneCredentials(
                    endpoint.name,
                    endpoint.rclone_config,
                    run_dir(config.profile, xdg_runtime_dir=runtime_dir),
                )
                try:
                    credentials.refresh()
                    if not credentials.external_present():
                        raise OSError("external config not published")
                    config_path = stack.enter_context(credentials.in_use())
                except (OSError, RuntimeError, ValueError):
                    checks.append(
                        Check(
                            f"rclone:{endpoint.name}",
                            True,
                            "WARNING: rclone credentials unavailable "
                            f"({endpoint.rclone_config} is not published)",
                        )
                    )
                    continue
                argv.extend(("--config", config_path))
            try:
                listing = _run(runner, argv, timeout=endpoint.timeout)
                remotes = {
                    line.strip()
                    for line in listing.stdout.splitlines()
                    if line.strip()
                }
                if listing.returncode or configured not in remotes:
                    raise RuntimeError(
                        f"configured remote {configured} is unavailable"
                    )
                database_remote = RcloneEndpoint(
                    endpoint.name,
                    remote,
                    database_name=config.db_name,
                ).database_remote
                probe = list(
                    endpoint.probe
                    or ("rclone", "lsjson", "--hash", "{remote}")
                )
                probe = [
                    arg.replace("{remote}", database_remote) for arg in probe
                ]
                if config_path is not None:
                    probe.extend(("--config", config_path))
                probed = _run(runner, probe, timeout=endpoint.timeout)
                if probed.returncode:
                    raise RuntimeError(
                        "remote probe failed "
                        f"(exit status {probed.returncode})"
                    )
                checks.append(
                    Check(
                        f"rclone:{endpoint.name}",
                        True,
                        f"rclone remote {configured} and its database "
                        "probe work",
                    )
                )
            except (
                OSError,
                subprocess.SubprocessError,
                RuntimeError,
                ValueError,
            ) as exc:
                checks.append(
                    Check(
                        f"rclone:{endpoint.name}",
                        False,
                        "rclone remote/probe check failed for "
                        f"{remote}: {exc}",
                    )
                )
    if not any(
        item.type == "rclone" and item.enabled for item in config.endpoints
    ):
        checks.append(
            Check("rclone", True, "no enabled rclone endpoint is configured")
        )
    return checks


def _run(
    runner: Callable, argv: list[str], *, timeout: float
) -> subprocess.CompletedProcess:
    return logged(runner)(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _encrypted_hub(config: Config, runner: Callable) -> tuple[bool, str]:
    try:
        require_encrypted_hub(config.hub, runner=runner)
    except ConfigError as exc:
        return False, str(exc)
    return True, "encrypted block device"


def _swap_encryption(runner: Callable) -> tuple[bool, str]:
    swaps_file = Path("/proc/swaps")
    try:
        lines = swaps_file.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return False, f"cannot read {swaps_file}: {exc}"
    devices = [line.split()[0] for line in lines[1:] if line.split()]
    unprotected: list[str] = []
    for device in devices:
        if device.startswith("/dev/zram"):
            continue
        try:
            result = _run(
                runner,
                ["lsblk", "--json", "--inverse", "--output", "TYPE", device],
                timeout=5,
            )
            if result.returncode:
                unprotected.append(device)
                continue
            payload = json.loads(result.stdout)
            if not any(
                item.get("type") == "crypt" for item in _walk_devices(payload)
            ):
                unprotected.append(device)
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            unprotected.append(device)
    if unprotected:
        return False, "unencrypted or unverifiable swap: " + ", ".join(
            unprotected
        )
    return True, (
        "no swap is active"
        if not devices
        else "all active swap is encrypted or zram"
    )


def _walk_devices(payload):
    if not isinstance(payload, dict):
        return
    for item in payload.get("blockdevices", []):
        if isinstance(item, dict):
            yield item
            yield from _walk_devices(item)


def _diagnostic_keyring_probe(
    runner: Callable,
) -> Callable[[], tuple[bool, str]]:
    def probe() -> tuple[bool, str]:
        import linux_keyctl as keyctl

        from linux_keyctl import (
            KEY_POS_READ,
            KEY_POS_SEARCH,
            KEY_POS_SETATTR,
            KEY_POS_WRITE,
            KEY_SPEC_USER_KEYRING,
        )

        description = f"kp-sync:doctor:{uuid.uuid4().hex}".encode("ascii")
        token = os.urandom(32)
        key_id: int | None = None
        failure = ""
        try:
            key_id = keyctl.add_key(
                description,
                token,
                KEY_SPEC_USER_KEYRING,
                b"user",
            )
            keyctl.set_timeout(key_id, 60)
            keyctl.set_perm(
                key_id,
                KEY_POS_READ
                | KEY_POS_SEARCH
                | KEY_POS_WRITE
                | KEY_POS_SETATTR,
            )
            command = shutil.which("keyctl") or "keyctl"
            try:
                result = logged(runner)(
                    [
                        "systemd-run",
                        "--user",
                        "--pipe",
                        "--wait",
                        "--collect",
                        "--property=KeyringMode=shared",
                        "--",
                        command,
                        "pipe",
                        str(key_id),
                    ],
                    capture_output=True,
                    text=False,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise RuntimeError(
                    f"systemd-run keyring read failed: {exc}"
                ) from exc
            if result.returncode:
                stderr = result.stderr or b""
                failure = (
                    stderr.decode("utf-8", "replace").strip()
                    or "systemd-run keyring read failed"
                )
            elif result.stdout != token:
                failure = (
                    "daemon-shared user keyring returned a different "
                    "diagnostic key"
                )
        except (
            OSError,
            subprocess.SubprocessError,
            AttributeError,
            TypeError,
        ) as exc:
            failure = str(exc)
        finally:
            if key_id is not None:
                try:
                    keyctl.revoke(key_id)
                except OSError as exc:
                    failure = (
                        f"temporary diagnostic key could not be revoked: {exc}"
                        if not failure
                        else (
                            f"{failure}; temporary diagnostic key "
                            f"revoke failed: {exc}"
                        )
                    )
        return (not failure, failure or "temporary key round-trip succeeded")

    return probe
