"""Resolve and safely create profile-scoped runtime paths."""

import os
import re
import stat
import tempfile
from pathlib import Path

_PROFILE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")


def run_dir(
    profile: str | None = None,
    *,
    xdg_runtime_dir: str | os.PathLike[str] | None = None,
) -> Path:
    """Return the selected profile's runtime state directory.

    Raises ``RuntimeError`` when ``XDG_RUNTIME_DIR`` is unavailable and
    ``ValueError`` for a malformed profile name.
    """
    runtime = (
        os.environ.get("XDG_RUNTIME_DIR")
        if xdg_runtime_dir is None
        else xdg_runtime_dir
    )
    if not runtime:
        raise RuntimeError("XDG_RUNTIME_DIR is not set")

    if profile is None:
        dirname = "kp-sync"
    else:
        if not _PROFILE_PATTERN.fullmatch(profile):
            raise ValueError(f"invalid profile name: {profile!r}")
        dirname = f"kp-sync-{profile}"

    return Path(runtime) / dirname


def ensure_run_dir(
    profile: str | None = None,
    *,
    xdg_runtime_dir: str | os.PathLike[str] | None = None,
) -> Path:
    """Return the runtime directory with private ownership and mode."""
    path = run_dir(profile, xdg_runtime_dir=xdg_runtime_dir)
    ensure_private_directory(path)
    return path


def ensure_private_directory(path: str | os.PathLike[str]) -> Path:
    """Create or secure a user-owned directory with mode 0700."""
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise RuntimeError(
            f"private runtime path is not a directory: {path}"
        ) from exc
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
        ):
            raise RuntimeError(
                f"private runtime path is not a directory: {path}"
            )
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)
    return path


def atomic_write(path: str | os.PathLike[str], data: str | bytes) -> None:
    """Atomically write private bytes and fsync both paths."""
    target = Path(path)
    payload = data.encode("utf-8") if isinstance(data, str) else data
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", dir=target.parent
    )
    temporary = Path(temporary_name)

    try:
        with os.fdopen(fd, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())

        os.replace(temporary, target)
        directory_fd = os.open(
            target.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


KEEPASSXC_FLATPAK_ID = "org.keepassxc.KeePassXC"


def keepassxc_settings_path(
    home: str | os.PathLike[str], environ: dict | None = None
) -> Path:
    """Return the KeePassXC settings file to read.

    Checks the native location (``$XDG_CONFIG_HOME``, else
    ``~/.config``) and then the Flatpak one; the first that exists
    wins. Without either, returns the native path.

    Args:
        home: Home directory.
        environ: Environment providing ``XDG_CONFIG_HOME``; a relative
            value is ignored, as the XDG specification requires.
    """
    home = Path(home)
    config_home = (environ or {}).get("XDG_CONFIG_HOME", "")
    base = (
        Path(config_home)
        if config_home and os.path.isabs(config_home)
        else home / ".config"
    )
    native = base / "keepassxc" / "keepassxc.ini"
    flatpak = (
        home
        / ".var"
        / "app"
        / KEEPASSXC_FLATPAK_ID
        / "config"
        / "keepassxc"
        / "keepassxc.ini"
    )
    for candidate in (native, flatpak):
        if os.path.lexists(candidate):
            return candidate
    return native
