# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Create, run, renew, and clean up remote SSH/SFTP sessions."""

import getpass
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

from kp_sync.log import error, info, logged
from kp_sync.lock import file_lock
from kp_sync.endpoints.base import EndpointFingerprint
from kp_sync.paths import ensure_private_directory, ensure_run_dir
from kp_sync.sessions.authkeys import (
    AuthorizedKeys,
    AuthorizedKeysError,
    SessionLine,
    forced_command,
)
from kp_sync.sessions.records import (
    HOST_NAME,
    USER_NAME,
    SessionError,
    SessionRecord,
    SessionStore,
    process_start,
    stop_ssh,
)
from kp_sync.sessions.sftp_serve import find_bwrap, find_sftp_server
from kp_sync.state import HistoryState

HOST_KEY = Path("/etc/ssh/ssh_host_ed25519_key.pub")
RENEW_WINDOW = 3600
POLL = 2.0
_PUBLIC_KEY = re.compile(r"(ssh-ed25519) ([A-Za-z0-9+/]+={0,2})")


def parse_target(target: str) -> tuple[str | None, str, str | None]:
    """Parse ``[USER@]HOST[:PATH]`` into validated components.

    Args:
        target: User, host, and optional remote mount path.

    Returns:
        ``(user, host, path)``; omitted values are ``None``.

    Raises:
        SessionError: If user, host, or path is invalid.
    """
    rest, _, path = target.partition(":")
    user = None
    host = rest
    if "@" in rest:
        user, _, host = rest.rpartition("@")
        if not USER_NAME.fullmatch(user):
            raise SessionError(f"invalid remote user: {user!r}")
    if not HOST_NAME.fullmatch(host):
        raise SessionError(f"invalid remote host: {host!r}")
    if path and ("\x00" in path or "\n" in path):
        raise SessionError("invalid remote path")
    return user, host, path or None


def remote_directory_expression(session_id: str) -> str:
    """Return the shell expression for a remote session directory."""
    return f'"${{XDG_RUNTIME_DIR:-/dev/shm}}/kp-sync-session-{session_id}"'


def remote_path_expression(path: str) -> str:
    """Quote a path while preserving ``~`` expansion."""
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def remote_setup_script(
    session_id: str, private_key: str, host_key: str, port: int
) -> str:
    """Build a setup script that installs a private key remotely.

    Args:
        session_id: Eight-character session identifier.
        private_key: Private key content sent only over SSH stdin.
        host_key: Pinned local SSH server public key.
        port: Reverse-tunnel port for the later remote mount.

    Returns:
        Shell script consumed remotely through ``sh -s``.

    Raises:
        SessionError: If the private key contains the heredoc marker.
    """
    marker = "KP_SYNC_SESSION_KEY_END"
    if marker in private_key:
        raise SessionError("unexpected private key content")
    return "\n".join(
        [
            "set -eu",
            "umask 077",
            "command -v sshfs >/dev/null || { "
            "echo 'sshfs is not installed here' >&2; exit 3; }",
            f"d={remote_directory_expression(session_id)}",
            'mkdir -m 700 -- "$d"',
            f"cat > \"$d/key\" <<'{marker}'",
            private_key.rstrip("\n"),
            marker,
            f"printf '%s\\n' "
            f"{shlex.quote(f'[localhost]:{port} {host_key}')} "
            '> "$d/known_hosts"',
            "",
        ]
    )


def remote_session_command(
    session_id: str,
    mode: str,
    user: str,
    port: int,
    mount_path: str,
) -> str:
    """Build the remote command that mounts the local staged database.

    Args:
        session_id: Session ID locating the remote key directory.
        mode: Requested ``ro`` or ``rw`` access.
        user: Local SSH account exposed by the reverse tunnel.
        port: Reverse-tunnel port forwarding to the local SSH server.
        mount_path: Remote path at which to mount the staged database.

    Returns:
        A safely quoted ``sh -c`` command that cleans up on exit.
    """
    options = ",".join(
        [
            mode,
            f"port={port}",
            'IdentityFile="$d/key"',
            "IdentitiesOnly=yes",
            'UserKnownHostsFile="$d/known_hosts"',
            "StrictHostKeyChecking=yes",
            "BatchMode=yes",
            "ServerAliveInterval=15",
        ]
    )
    script = "\n".join(
        [
            f"d={remote_directory_expression(session_id)}",
            "mp=$(readlink -m -- "
            f"{remote_path_expression(mount_path)}) || exit 1",
            'cleanup() { fusermount -u "$mp" 2>/dev/null; rm -rf -- "$d"; }',
            "trap cleanup EXIT",
            "trap 'exit 129' HUP",
            "trap 'exit 143' TERM",
            'mkdir -p -- "$mp" || exit 1',
            f'sshfs -o "{options}" {shlex.quote(user)}@localhost:/export '
            '"$mp" || exit 1',
            f'echo "Keepass mounted ({mode}) at $mp"',
            "${SHELL:-/bin/sh} -l",
        ]
    )
    return "sh -c " + shlex.quote(script)


def remote_cleanup_command(session_id: str) -> str:
    """Return the shell command that removes remote session keys."""
    return "sh -c " + shlex.quote(
        f"rm -rf -- {remote_directory_expression(session_id)}"
    )


def read_host_key(path: Path = HOST_KEY) -> str:
    """Read and validate an Ed25519 public host key.

    Args:
        path: Public host-key file.

    Returns:
        Key type and base64 data, without a comment.

    Raises:
        SessionError: If unreadable or not an Ed25519 key.
    """
    try:
        found = _PUBLIC_KEY.match(path.read_text(encoding="ascii"))
    except OSError as exc:
        raise SessionError(f"cannot read the host key {path}: {exc}") from exc
    if found is None:
        raise SessionError(f"{path} is not an ssh-ed25519 public key")
    return f"{found.group(1)} {found.group(2)}"


def kp_sync_executable() -> str:
    """Resolve the absolute path used for forced session commands.

    Raises:
        SessionError: If no installed ``kp-sync`` executable exists.
    """
    argv0 = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if argv0 is not None and argv0.name == "kp-sync" and argv0.is_file():
        return str(argv0.resolve())
    found = shutil.which("kp-sync")
    if found is None:
        raise SessionError(
            "cannot find the absolute path of the kp-sync executable"
        )
    return str(Path(found).resolve())


class SessionManager:
    """Manage a profile's sessions for both clients and the daemon.

    It coordinates private records, key expiry, and updates to only the
    profile-owned ``authorized_keys`` lines.
    """

    def __init__(
        self,
        config,
        run_directory=None,
        *,
        authkeys=None,
        clock=time.time,
        executable=None,
        retire_on_cleanup=True,
    ):
        """Create a manager for one profile's runtime state.

        Args:
            config: Loaded profile configuration.
            run_directory: Optional private runtime directory override.
            authkeys: Injectable authorized_keys editor.
            clock: Wall-clock source for expiry decisions.
            executable: Absolute kp-sync path used in forced commands.
            retire_on_cleanup: Whether clean removal retires history.
        """
        self.config = config
        self.executable = executable
        self.run_directory = (
            Path(run_directory)
            if run_directory is not None
            else ensure_run_dir(config.profile)
        )
        self.store = SessionStore(self.run_directory)
        self.retire_on_cleanup = retire_on_cleanup
        self.authkeys = authkeys or AuthorizedKeys()
        self.clock = clock

    def live_ids(self) -> set[str]:
        """Return live IDs from normal and diagnostic session stores."""
        return self.store.live_ids(now=self.clock()) | SessionStore(
            self.run_directory / "diagnostics",
        ).live_ids(now=self.clock())

    def drop_stale_lines(self) -> list[str]:
        """Remove this profile's expired or ownerless key entries.

        Returns:
            IDs of the key entries removed.
        """
        return self.authkeys.update(
            self.config.profile, keep_only=self.live_ids()
        )

    def revoke(self, record: SessionRecord) -> None:
        """Stop the recorded SSH process, then withdraw its public key.

        Args:
            record: Session whose write access must be revoked.
        """
        stop_ssh(record)
        self.authkeys.update(self.config.profile, remove={record.id})

    def discard(self, record: SessionRecord) -> None:
        """Remove local files after recording recoverable cleanup."""
        if not self.retire_on_cleanup:
            self.store.remove_files(record.id)
            return
        with file_lock(
            self.run_directory / "lock",
            self.config.sync["lock_timeout"],
        ):
            self.store.prepare_retirement(record.identity, record.id)
            self.store.remove_files(record.id)

    def renew(self, record: SessionRecord) -> SessionRecord:
        """Renew a session key when it enters the renewal window.

        Args:
            record: Current record to renew.

        Returns:
            Existing record if not due, or its renewed version.

        Raises:
            SessionError: If the public key cannot be read or validated.

        Notes:
            Replaces the tagged key line and persists its new expiry.
        """
        ttl = self.config.sessions["session_key_ttl"]
        now = self.clock()
        if record.expires - now > min(RENEW_WINDOW, ttl / 2):
            return record
        public = (self.store.directory / f"{record.id}.key.pub").read_text(
            encoding="ascii"
        )
        found = _PUBLIC_KEY.match(public)
        if found is None:
            raise SessionError("session public key is unreadable")
        renewed = replace(record, expires=now + ttl)
        line = SessionLine(
            record.id,
            f"{found.group(1)} {found.group(2)}",
            self.command(record.id),
            renewed.expires,
        )
        self.authkeys.update(self.config.profile, add=(line,))
        self.store.save(renewed)
        return renewed

    def command(self, session_id: str) -> str:
        """Render the forced SFTP command for a session ID.

        Raises:
            SessionError: If no absolute kp-sync executable is set.
        """
        if self.executable is None:
            raise SessionError("the kp-sync executable is not known")
        return forced_command(self.executable, self.config.profile, session_id)


def stage_session(
    manager: SessionManager,
    session_id: str,
    *,
    record: SessionRecord | None = None,
    state_directory: Path | None = None,
) -> Path:
    """Stage the local hub and key file for one remote session.

    Args:
        manager: Session manager locating runtime files and config.
        session_id: Unique session ID for the staging directory.
        record: Optional record whose copy identity may enter history.
        state_directory: History dir; defaults to the manager's.

    Returns:
        Private staging directory containing the database and key file.

    Raises:
        SessionError: If the hub or key file is missing or unsafe.

    Notes:
        Copies hub and key under the engine lock. Hashes in the DAG are
        recorded as session copies.
    """
    config = manager.config
    if not config.hub.is_file() or config.hub.is_symlink():
        raise SessionError("no database copy is available to stage")
    if not config.key_file.is_file():
        raise SessionError(f"key file not found: {config.key_file}")
    manager.store.ensure()
    directory = state_directory or manager.run_directory
    with file_lock(directory / "lock", config.sync["lock_timeout"]):
        state = HistoryState.load(directory / "state.json")
        if manager.store.is_retiring(session_id):
            raise SessionError(
                f"session {session_id} cleanup is still pending"
            )
        if record is not None and record.identity in state.retirements:
            raise SessionError(
                f"session identity {record.identity} was already retired"
            )
        staging = ensure_private_directory(
            manager.store.staging_path(session_id)
        )
        for source, name in (
            (config.hub, config.db_name),
            (config.key_file, config.key_file.name),
        ):
            target = staging / name
            with (
                source.open("rb") as incoming,
                os.fdopen(
                    os.open(
                        target,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                    ),
                    "wb",
                ) as output,
            ):
                shutil.copyfileobj(incoming, output)
                output.flush()
                os.fsync(output.fileno())
        if record is not None:
            sha = EndpointFingerprint.from_file(
                staging / config.db_name
            ).sha256
            if sha in state.versions:
                state.record_copies(sha, [record.identity], config.host)
                state.save(directory / "state.json")
    return staging


def generate_key(
    manager: SessionManager, session_id: str, runner=subprocess.run
) -> tuple[str, str]:
    """Generate an Ed25519 session key pair in the runtime directory.

    Args:
        manager: Session store and runtime directory owner.
        session_id: ID used to name the generated key pair.
        runner: Injectable subprocess-compatible command runner.

    Returns:
        Private key text and validated public key text.

    Raises:
        SessionError: If key generation or output validation fails.

    Notes:
        Creates a mode-``0600`` private key and its public-key file.
    """
    key = manager.store.key_path(session_id)
    key.unlink(missing_ok=True)
    result = logged(runner)(
        [
            "ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            f"kp-sync-session-{session_id}",
            "-f",
            str(key),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise SessionError(f"ssh-keygen failed: {result.stderr.strip()}")
    os.chmod(key, 0o600)
    private = key.read_text(encoding="ascii")
    public_path = key.with_name(key.name + ".pub")
    found = _PUBLIC_KEY.match(public_path.read_text(encoding="ascii"))
    if found is None:
        raise SessionError("ssh-keygen produced an unexpected public key")
    return private, f"{found.group(1)} {found.group(2)}"


def _outcome_clean(outcome, record: SessionRecord) -> bool:
    """Check whether sync results allow removal of this session copy."""
    if record.mode == "ro" and record.confined:
        return True
    return (
        isinstance(outcome, dict)
        and outcome.get("action") not in {"pending", "unavailable"}
        and not outcome.get("pending")
        and record.identity not in outcome.get("missing", ())
    )


def finish_session(
    manager: SessionManager, record: SessionRecord, sync, *, remote=None
) -> bool:
    """Revoke access, post-sync the copy, and remove it only on success.

    Args:
        manager: Session manager responsible for key and file cleanup.
        record: Session being closed.
        sync: Callback that performs the daemon-owned post-sync.
        remote: Optional callback removing the remote key directory.

    Returns:
        ``True`` if cleanup completed; otherwise local files remain.

    Notes:
        Revokes access before sync. Pending sync preserves data.
    """
    info("Closing session access and cleaning up remote session files...")
    manager.revoke(record)
    if remote is not None:
        remote()
    info(
        "Session access closed; finishing synchronization before "
        "removing local session files..."
    )
    try:
        outcome = sync()
    except Exception as exc:
        error(f"post-sync of session {record.id} failed: {exc}")
        outcome = None
    if not _outcome_clean(outcome, record):
        error(
            f"session {record.id} kept at "
            f"{manager.store.staging_path(record.id)}: "
            "its post-sync did not complete; the daemon will retry"
        )
        return False
    manager.discard(record)
    info("Session cleanup complete")
    return True


def run_session(
    config,
    target: str,
    *,
    mode: str | None = None,
    unconfined: bool = False,
    client=None,
    runner=subprocess.run,
    popen=subprocess.Popen,
    executable: str | None = None,
    authkeys: AuthorizedKeys | None = None,
    host_key: Path = HOST_KEY,
    clock=time.time,
    ui: str | None = None,
    mount_dir: str | None = None,
    managed: bool = False,
) -> int:
    """Start an SSH session that exposes the staged database remotely.

    Args:
        config: Loaded profile configuration.
        target: ``[USER@]HOST[:PATH]`` remote target.
        mode: Optional ``ro`` or ``rw`` override.
        unconfined: Allow a session without bubblewrap confinement.
        client: Injectable daemon client.
        runner: Injectable subprocess-compatible command runner.
        popen: Injectable process launcher for the SSH command.
        executable: Absolute kp-sync path for the forced command.
        authkeys: Injectable authorized_keys editor.
        host_key: Public key used to pin the local SSH server.
        clock: Wall-clock source for record creation and expiry.
        ui: Optional daemon UI mode for post-sync.
        mount_dir: Optional remote mount path override.
        managed: Run without an attached terminal for named services.

    Returns:
        SSH process exit status.

    Raises:
        SessionError: If sessions are disabled or setup fails.

    Notes:
        Stages the database, registers a key, and holds a daemon lease.
        Finalization revokes access and preserves data on pending sync.
    """
    settings = config.sessions
    if not settings["enabled"]:
        raise SessionError(
            "sessions are disabled; set enabled = true in [sessions]"
        )
    mode = mode or settings["default_mode"]
    user, host, path = parse_target(target)
    if managed:
        info(f"Starting named SSH session to {host} ({mode})")
    bwrap = None if unconfined else find_bwrap()
    if bwrap is None and not unconfined:
        raise SessionError(
            "bwrap is not installed; use --unconfined to accept an "
            "unconfined session"
        )
    if unconfined:
        error(
            "WARNING: --unconfined: the remote is not confined to the staged "
            "copy"
        )
    if find_sftp_server() is None:
        raise SessionError("sftp-server not found")
    if client is None:
        from kp_sync.daemon.client import Client

        client = Client(config)
    from kp_sync.daemon.client import LeaseConnection

    manager = SessionManager(
        config,
        authkeys=authkeys,
        clock=clock,
        executable=executable or kp_sync_executable(),
    )
    manager.command("00000000")
    ui_mode = ui or config.ui["mode"]

    def sync():
        return client.request("sync", ui=ui_mode)

    host_public = read_host_key(host_key)
    store = manager.store
    from kp_sync.state import HistoryState

    retired_ids = {
        candidate.rsplit(":", 1)[-1]
        for candidate in HistoryState.load(
            manager.run_directory / "state.json"
        ).retirements
        if candidate.startswith(f"session:{host}:")
    }
    session_id = next(
        candidate
        for candidate in iter(lambda: secrets.token_hex(4), None)
        if not store.record_path(candidate).exists()
        and not store.staging_path(candidate).exists()
        and not store.is_retiring(candidate)
        and candidate not in retired_ids
    )
    local_user = getpass.getuser()
    now = clock()
    record = SessionRecord(
        id=session_id,
        host=host,
        user=user,
        mode=mode,
        owner_pid=os.getpid(),
        owner_start=process_start(os.getpid()) or 0,
        ssh_pid=None,
        ssh_start=None,
        confined=not unconfined,
        created=now,
        expires=now + settings["session_key_ttl"],
    )
    lease = LeaseConnection(client, f"session:{session_id}")
    destination = f"{user}@{host}" if user else host
    port = settings["tunnel_port"]
    started = False
    returncode = 1
    try:
        stage_session(manager, session_id, record=record)
        private, public = generate_key(manager, session_id, runner)
        store.save(record)
        manager.authkeys.update(
            config.profile,
            add=(
                SessionLine(
                    session_id,
                    public,
                    manager.command(session_id),
                    record.expires,
                ),
            ),
            keep_only=manager.live_ids() | {session_id},
        )
        lease.renew()
        setup_options = {
            "input": remote_setup_script(
                session_id, private, host_public, port
            ),
            "text": True,
            "timeout": 120,
        }
        if managed:
            setup_options["capture_output"] = True
        setup = logged(runner)(
            ["ssh", "-T", "--", destination, "sh -s"],
            **setup_options,
        )
        if setup.returncode != 0:
            raise SessionError(
                f"remote setup failed (ssh exit status {setup.returncode})"
            )
        remote_command = remote_session_command(
            session_id,
            mode,
            local_user,
            port,
            path or mount_dir or settings["mount_dir"],
        )
        old_handlers = {
            signal.SIGINT: signal.signal(signal.SIGINT, signal.SIG_IGN),
        }
        proc = None
        stop_requested = False
        try:
            ssh_argv = [
                "ssh",
                "-t",
                "-o",
                "ExitOnForwardFailure=yes",
                "-o",
                "ServerAliveInterval=30",
                "-o",
                "ServerAliveCountMax=3",
                "-R",
                f"{port}:localhost:22",
                "--",
                destination,
                remote_command,
            ]
            if managed:
                proc = logged(popen)(
                    ssh_argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                proc = logged(popen)(ssh_argv)
            started = True
            record = replace(
                record,
                ssh_pid=proc.pid,
                ssh_start=process_start(proc.pid) or 0,
            )
            store.save(record)

            def stop(*_):
                nonlocal stop_requested
                stop_requested = True
                proc.terminate()

            for sig in (signal.SIGTERM, signal.SIGHUP):
                old_handlers[sig] = signal.signal(sig, stop)
            began = time.monotonic()
            while True:
                try:
                    returncode = proc.wait(timeout=POLL)
                    break
                except subprocess.TimeoutExpired:
                    lease.ensure()
                    record = manager.renew(record)
            if returncode == 255 and time.monotonic() - began < 10:
                error(
                    f"ssh failed at once; if port {port} is already in use "
                    f"on {host}, another session is open there "
                    "(see [sessions] tunnel_port)"
                )
        finally:
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            stdin = getattr(proc, "stdin", None) if proc is not None else None
            if stdin is not None:
                stdin.close()
        if managed and stop_requested:
            returncode = 0
        return returncode
    finally:
        lease.close()

        def remote_cleanup():
            if not started:
                return
            try:
                logged(runner)(
                    [
                        "ssh",
                        "-T",
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        "ConnectTimeout=10",
                        "--",
                        destination,
                        remote_cleanup_command(session_id),
                    ],
                    capture_output=True,
                    timeout=30,
                )
            except (OSError, subprocess.SubprocessError):
                info(
                    f"could not remove the session key on {host}; "
                    "it expires with its line"
                )

        try:
            current = store.load(session_id)
        except SessionError:
            current = record
        finish_session(manager, current, sync, remote=remote_cleanup)
