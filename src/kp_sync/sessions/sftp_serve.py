# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Build and run the restricted SFTP command for remote sessions."""

import os
import pwd
import grp
import re
import shutil
import time
from pathlib import Path

from kp_sync.log import error
from kp_sync.paths import run_dir
from kp_sync.sessions.records import SessionError, SessionRecord, SessionStore

SFTP_SERVER_CANDIDATES = (
    "/usr/lib/openssh/sftp-server",
    "/usr/lib/ssh/sftp-server",
    "/usr/libexec/openssh/sftp-server",
    "/usr/libexec/sftp-server",
)
_ACCOUNT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")
EXPORT = "/export"


def find_sftp_server(candidates=SFTP_SERVER_CANDIDATES) -> str | None:
    """Find the first executable OpenSSH SFTP server in known locations.

    Args:
        candidates: Paths to inspect in preference order.

    Returns:
        Executable path, or ``None`` when none is available.
    """
    for candidate in candidates:
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def find_bwrap() -> str | None:
    """Return the bubblewrap executable path, if installed."""
    return shutil.which("bwrap")


def _data_fd(data: bytes) -> int:
    if not hasattr(os, "memfd_create"):
        # Small data fits the pipe buffer; close its write end at once.
        read_end, write_end = os.pipe()
        with os.fdopen(write_end, "wb") as writer:
            writer.write(data)
        os.set_inheritable(read_end, True)
        return read_end
    fd = os.memfd_create("kp-sync-sftp-serve", 0)
    os.write(fd, data)
    os.lseek(fd, 0, os.SEEK_SET)
    os.set_inheritable(fd, True)
    return fd


def identity_files(
    uid: int | None = None, gid: int | None = None
) -> tuple[bytes, bytes]:
    """Build minimal passwd and group files for confined SFTP.

    Args:
        uid: User ID to describe; defaults to the current UID.
        gid: Group ID to describe; defaults to the current GID.

    Returns:
        Encoded one-line passwd and group file contents.

    Raises:
        SessionError: If the account name cannot be represented safely.
    """
    uid = os.getuid() if uid is None else uid
    gid = os.getgid() if gid is None else gid
    try:
        user = pwd.getpwuid(uid).pw_name
    except KeyError:
        user = "kp-sync"
    try:
        group = grp.getgrgid(gid).gr_name
    except KeyError:
        group = "kp-sync"
    for name in (user, group):
        if not _ACCOUNT.fullmatch(name):
            raise SessionError(f"unsupported account name: {name!r}")
    passwd = f"{user}:x:{uid}:{gid}::{EXPORT}:/usr/sbin/nologin\n".encode()
    group_data = f"{group}:x:{gid}:\n".encode()
    return passwd, group_data


def sftp_server_arguments(
    sftp_server: str, mode: str, directory: str
) -> list[str]:
    """Build arguments that restrict SFTP to a staging directory.

    Args:
        sftp_server: OpenSSH server executable.
        mode: ``ro`` adds the server's read-only flag.
        directory: Initial and transfer root directory.
    """
    arguments = [sftp_server, "-d", directory, "-u", "077"]
    if mode == "ro":
        arguments.append("-R")
    return arguments


def bwrap_arguments(
    bwrap: str,
    sftp_server: str,
    staging: Path,
    mode: str,
    passwd_fd: int,
    group_fd: int,
) -> list[str]:
    """Build bubblewrap arguments for a confined SFTP server.

    Args:
        bwrap: Bubblewrap executable path.
        sftp_server: OpenSSH server executable path.
        staging: Only writable session directory exposed in the sandbox.
        mode: Session access mode.
        passwd_fd: Inherited descriptor for minimal passwd data.
        group_fd: Inherited descriptor for minimal group data.
    """
    return [
        bwrap,
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--ro-bind",
        "/usr",
        "/usr",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--symlink",
        "usr/bin",
        "/bin",
        "--perms",
        "0444",
        "--ro-bind-data",
        str(passwd_fd),
        "/etc/passwd",
        "--perms",
        "0444",
        "--ro-bind-data",
        str(group_fd),
        "/etc/group",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--bind",
        str(staging),
        EXPORT,
        "--chdir",
        EXPORT,
        *sftp_server_arguments(sftp_server, mode, EXPORT),
    ]


def build_command(
    record: SessionRecord,
    store: SessionStore,
    *,
    bwrap: str | None,
    sftp_server: str,
) -> tuple[list[str], list[int]]:
    """Build the server invocation and descriptors to inherit.

    Args:
        record: Session confinement and access settings.
        store: Runtime store locating the staged copy.
        bwrap: Optional bubblewrap path.
        sftp_server: OpenSSH server executable path.

    Returns:
        Argument vector and file descriptors that must remain open.

    Raises:
        SessionError: If a confined session lacks bubblewrap.
    """
    staging = store.staging_path(record.id)
    if not record.confined:
        return (
            sftp_server_arguments(sftp_server, record.mode, str(staging)),
            [],
        )
    if bwrap is None:
        raise SessionError("bwrap is required for a confined session")
    passwd, group_data = identity_files()
    passwd_fd, group_fd = _data_fd(passwd), _data_fd(group_data)
    return bwrap_arguments(
        bwrap,
        sftp_server,
        staging,
        record.mode,
        passwd_fd,
        group_fd,
    ), [passwd_fd, group_fd]


def serve(
    profile: str | None,
    session_id: str,
    *,
    runtime_dir=None,
    now=time.time,
    execute=os.execv,
    bwrap=None,
    sftp_server=None,
    diagnostic: bool = False,
) -> int:
    """Run the SFTP forced command for one live session key.

    Args:
        profile: Default or named profile containing the session record.
        session_id: Eight-character session identifier.
        runtime_dir: Optional runtime root override.
        now: Wall-clock function for expiry checks.
        execute: Injectable exec function used to replace this process.
        bwrap: Optional bubblewrap executable override.
        sftp_server: Optional SFTP server executable override.
        diagnostic: Read records from isolated diagnostic storage.

    Returns:
        Nonzero refusal status; success replaces this process.

    Notes:
        Executes SFTP in the session staging directory when confined.
    """
    try:
        directory = run_dir(profile, xdg_runtime_dir=runtime_dir)
        store = SessionStore(
            directory / "diagnostics" if diagnostic else directory
        )
        record = store.load(session_id)
    except (SessionError, RuntimeError, ValueError) as exc:
        error(f"session refused: {exc}")
        return 1
    if not record.owner_alive() or record.expires <= now():
        error(f"session {session_id} is not live")
        return 1
    sftp_server = sftp_server or find_sftp_server()
    if sftp_server is None:
        error("sftp-server not found")
        return 1
    try:
        arguments, descriptors = build_command(
            record,
            store,
            bwrap=bwrap or find_bwrap(),
            sftp_server=sftp_server,
        )
    except (SessionError, OSError) as exc:
        error(f"session refused: {exc}")
        return 1
    if not store.staging_path(session_id).is_dir():
        error(f"session {session_id} has no staged directory")
        return 1
    execute(arguments[0], arguments)
    return 1
