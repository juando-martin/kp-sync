"""Run an opt-in confined localhost SFTP diagnostic round trip."""

import getpass
import os
import secrets
import subprocess
import time
from pathlib import Path

from kp_sync.paths import (
    atomic_write,
    ensure_private_directory,
    ensure_run_dir,
)
from kp_sync.sessions.authkeys import SessionLine, forced_command
from kp_sync.sessions.records import SessionError, SessionRecord, process_start
from kp_sync.sessions.session import (
    HOST_KEY,
    SessionManager,
    generate_key,
    kp_sync_executable,
    read_host_key,
)
from kp_sync.sessions.sftp_serve import find_bwrap, find_sftp_server
from kp_sync.log import logged


def self_test(
    config,
    *,
    runtime_directory=None,
    authkeys=None,
    runner=subprocess.run,
    executable=None,
    host_key=HOST_KEY,
    port=22,
) -> None:
    """Test confined SFTP access using a short-lived diagnostic key.

    Args:
        config: Profile config with an isolated runtime directory.
        runtime_directory: Optional runtime directory override.
        authkeys: Injectable authorized_keys editor for tests.
        runner: Injectable subprocess-compatible command runner.
        executable: Optional absolute kp-sync path for forced commands.
        host_key: Public host key for strict localhost verification.
        port: Local SSH port used by the isolated test server.

    Raises:
        SessionError: If prerequisites, key setup, or SFTP checks fail.

    Notes:
        Adds a five-minute diagnostic key, performs a confined localhost
        upload/download, then revokes the key and removes artifacts.
    """
    if find_bwrap() is None or find_sftp_server() is None:
        raise SessionError("the SFTP self-test requires bwrap and sftp-server")
    runtime = runtime_directory or ensure_run_dir(config.profile)
    manager = SessionManager(
        config,
        ensure_private_directory(Path(runtime) / "diagnostics"),
        authkeys=authkeys,
        executable=executable or kp_sync_executable(),
        retire_on_cleanup=False,
    )
    manager.store.ensure()
    session_id = next(
        candidate
        for candidate in iter(lambda: secrets.token_hex(4), None)
        if not manager.store.record_path(candidate).exists()
        and not manager.store.staging_path(candidate).exists()
        and not manager.store.key_path(candidate).exists()
        and not (manager.store.directory / f"{candidate}.work").exists()
    )
    start = process_start(os.getpid())
    if start is None:
        raise SessionError("cannot identify the self-test process")
    now = time.time()
    record = SessionRecord(
        id=session_id,
        host="localhost",
        user=None,
        mode="rw",
        owner_pid=os.getpid(),
        owner_start=start,
        ssh_pid=None,
        ssh_start=None,
        confined=True,
        created=now,
        expires=now + 300,
    )
    work = ensure_private_directory(
        manager.store.directory / f"{session_id}.work"
    )
    try:
        staging = ensure_private_directory(
            manager.store.staging_path(session_id)
        )
        manager.store.save(record)
        _, public = generate_key(manager, session_id, runner=runner)
        known_hosts = work / "known_hosts"
        host_name = "localhost" if port == 22 else f"[localhost]:{port}"
        atomic_write(
            known_hosts,
            f"{host_name} {read_host_key(Path(host_key))}\n",
        )
        challenge = secrets.token_bytes(32).hex().encode("ascii")
        source = work / "challenge"
        received = work / "received"
        atomic_write(source, challenge)
        manager.authkeys.update(
            config.profile,
            add=(
                SessionLine(
                    session_id,
                    public,
                    forced_command(
                        manager.executable,
                        config.profile,
                        session_id,
                        diagnostic=True,
                    ),
                    record.expires,
                ),
            ),
        )

        # SFTP batch input is not a shell; quote local paths separately.
        def quote(path):
            """Quote a local path for the SFTP batch language."""
            return (
                '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'
            )

        result = logged(runner)(
            [
                "sftp",
                "-q",
                "-b",
                "-",
                "-P",
                str(port),
                "-i",
                str(manager.store.key_path(session_id)),
                "-o",
                "BatchMode=yes",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "IdentityAgent=none",
                "-o",
                f"UserKnownHostsFile={known_hosts}",
                "-o",
                "GlobalKnownHostsFile=/dev/null",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "ConnectTimeout=5",
                f"{getpass.getuser()}@localhost",
            ],
            input=(
                f"put {quote(source)} /export/challenge\n"
                f"get /export/challenge {quote(received)}\n"
            ),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise SessionError(
                f"localhost SFTP round trip failed "
                f"(exit {result.returncode}); "
                "check sshd, key restrictions and shell startup output"
            )
        if not received.is_file() or received.read_bytes() != challenge:
            raise SessionError(
                "localhost SFTP round trip returned different bytes"
            )
        if (staging / "challenge").read_bytes() != challenge:
            raise SessionError(
                "localhost SFTP did not write into the diagnostic export"
            )
    finally:
        # If revocation fails, retain the record until expiry.
        manager.authkeys.update(config.profile, remove={session_id})
        manager.discard(record)
        for name in ("known_hosts", "challenge", "received"):
            (work / name).unlink(missing_ok=True)
        work.rmdir()
