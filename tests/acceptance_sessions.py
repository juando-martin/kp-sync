"""Opt-in real SSH/sshfs acceptance.

Uses only the user-provided kp-test profile. Run without other sessions.
Does not restart services or cache credentials in the shared keyring.
"""

import getpass
import json
import os
import secrets
import shlex
import subprocess
import time
from dataclasses import replace
from pathlib import Path

from kp_sync.config import load_config
from kp_sync.endpoints.session import SessionEndpoint
from kp_sync.engine import SyncEngine, endpoints_from_config
from kp_sync.paths import (
    atomic_write,
    ensure_private_directory,
    ensure_run_dir,
)
from kp_sync.pending import PendingStore
from kp_sync.sessions.authkeys import (
    AuthorizedKeys,
    SessionLine,
    forced_command,
)
from kp_sync.sessions.records import SessionRecord, SessionStore, process_start
from kp_sync.sessions.session import (
    SessionManager,
    generate_key,
    kp_sync_executable,
    read_host_key,
    remote_cleanup_command,
    remote_session_command,
    remote_setup_script,
    stage_session,
)
from kp_sync.ui.none import NoneUI


HOST = "remote.example"
MOUNT = "~/kp-test"
PORT = 2223
KEY_TAG = "kp-test-acceptance"


class TestCredentials:
    def __init__(self, config):
        self.key = config.key_file
        self._password = (
            (Path.home() / "data/Personal/kp-test/kp-test.password")
            .read_text()
            .rstrip("\r\n")
        )
        if not self._password:
            raise RuntimeError("test password is empty")

    def cached_password(self):
        return self._password

    def password(self, ui):
        return self._password

    def key_file(self, ui):
        return self.key

    def cache_password(self, password):
        if password != self._password:
            raise RuntimeError("unexpected credential change in test database")

    def input(self):
        return (self._password + "\n").encode()


def ssh(command, *, input=None, timeout=30):
    result = subprocess.run(
        [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=8",
            "--",
            HOST,
            command,
        ],
        input=input,
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(
            f"remote acceptance operation failed (exit {result.returncode})"
        )
    return result.stdout


def sync(config, credentials, *, record=None, store=None):
    # A test daemon may still be finishing a slow cloud publication.
    config = replace(config, sync={**config.sync, "lock_timeout": 300})
    directory = ensure_run_dir(config.profile)
    endpoints = list(endpoints_from_config(config, directory))
    if record is not None:
        endpoints.append(SessionEndpoint(record, store, config.db_name))
    started = time.monotonic()
    outcome = SyncEngine(
        config,
        endpoints,
        run_directory=directory,
        ui=NoneUI(),
        credentials=credentials,
    ).sync()
    elapsed = time.monotonic() - started
    if not outcome.consistent or outcome.missing:
        raise RuntimeError(
            f"test sync incomplete: action={outcome.action}, "
            f"missing={outcome.missing}, pending={outcome.pending}"
        )
    return {"action": outcome.action, "seconds": round(elapsed, 3)}


def run():
    config = load_config(profile="kp-test")
    runtime = ensure_run_dir(config.profile)
    if (
        config.sessions["tunnel_port"] != PORT
        or config.sessions["mount_dir"] != MOUNT
    ):
        raise RuntimeError("kp-test must use ~/kp-test and port 2223")
    if (
        config.hub != Path(os.environ["XDG_RUNTIME_DIR"]) / "kp-test/pass.kdbx"
        or config.db_name != "kp-test.kdbx"
        or config.key_file
        != Path.home() / "data/Personal/kp-test/kp-test.keyx"
    ):
        raise RuntimeError("unexpected test hub, database name or key file")
    for endpoint in config.endpoints:
        if endpoint.type not in {"file", "rclone"}:
            raise RuntimeError(
                "acceptance supports only the configured test file/cloud "
                "endpoints"
            )
        if (
            endpoint.type == "rclone"
            and endpoint.remote != "GoogleDrive:kp-tests"
        ):
            raise RuntimeError("refusing a non-test cloud endpoint")
        if endpoint.type == "file" and endpoint.path != (
            Path.home() / "media/KEYDRIVE/kp-test/kp-test.kdbx"
        ):
            raise RuntimeError("refusing a non-test file endpoint")
    if config.baseline["dir"] != "kp-test-history":
        raise RuntimeError("refusing a non-test baseline directory")
    if (
        SessionStore(runtime).records()
        or SessionStore(runtime / "diagnostics").records()
    ):
        raise RuntimeError(
            "another session/diagnostic is active; not starting acceptance"
        )
    if (
        PendingStore(runtime).load()["decisions"]
        or (runtime / "write-race.json").exists()
    ):
        raise RuntimeError(
            "test profile has unresolved decisions/recovery; resolve first"
        )
    ssh(
        'if mountpoint -q "$HOME/kp-test"; then exit 1; fi; '
        "if ss -ltn | grep -qE ':2223[[:space:]]'; then exit 1; fi; "
        "command -v sshfs >/dev/null && command -v keepassxc-cli >/dev/null"
    )
    credentials = TestCredentials(config)
    report = {"preflight": sync(config, credentials), "sessions": []}
    keys = AuthorizedKeys()
    original_keys = keys.path.read_bytes()
    manager = SessionManager(
        replace(config, sync={**config.sync, "lock_timeout": 300}),
        ensure_private_directory(runtime / "diagnostics"),
        authkeys=keys,
        executable=kp_sync_executable(),
    )
    manager.store.ensure()
    for mode in ("ro", "rw"):
        session_id = next(
            candidate
            for candidate in iter(lambda: secrets.token_hex(4), None)
            if not manager.store.record_path(candidate).exists()
            and not manager.store.staging_path(candidate).exists()
            and not manager.store.key_path(candidate).exists()
        )
        now = time.time()
        rec = SessionRecord(
            id=session_id,
            host=HOST,
            user=None,
            mode=mode,
            owner_pid=os.getpid(),
            owner_start=process_start(os.getpid()),
            ssh_pid=None,
            ssh_start=None,
            confined=True,
            created=now,
            expires=now + 900,
        )
        title = "kp-sync-acceptance-" + session_id
        proc = None
        setup = False
        clean = False
        result = {"mode": mode, "session": session_id}
        log_path = runtime / f"acceptance-{session_id}.log"
        try:
            stage_session(
                manager, session_id, record=rec, state_directory=runtime
            )
            manager.store.save(rec)
            private, public = generate_key(manager, session_id)
            keys.update(
                KEY_TAG,
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
                        rec.expires,
                    ),
                ),
            )
            # A diagnostic tag avoids older daemons' session sweeps.
            setup = True
            ssh(
                "sh -s",
                input=remote_setup_script(
                    session_id, private, read_host_key(), PORT
                ).encode(),
            )
            started = time.monotonic()
            with log_path.open("wb") as log:
                os.chmod(log_path, 0o600)
                proc = subprocess.Popen(
                    [
                        "ssh",
                        "-T",
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        "ExitOnForwardFailure=yes",
                        "-o",
                        "ConnectTimeout=8",
                        "-R",
                        f"{PORT}:localhost:22",
                        "--",
                        HOST,
                        remote_session_command(
                            session_id, mode, getpass.getuser(), PORT, MOUNT
                        ),
                    ],
                    stdin=subprocess.PIPE,
                    stdout=log,
                    stderr=log,
                )
                rec = replace(
                    rec, ssh_pid=proc.pid, ssh_start=process_start(proc.pid)
                )
                manager.store.save(rec)
                deadline = time.monotonic() + 30
                while b"Keepass mounted" not in log_path.read_bytes():
                    if proc.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError(
                            "remote session did not become ready"
                        )
                    time.sleep(0.1)
                result["startup_seconds"] = round(
                    time.monotonic() - started, 3
                )
                db = f"$HOME/kp-test/{config.db_name}"
                key = f"$HOME/kp-test/{config.key_file.name}"
                ssh(
                    f'keepassxc-cli db-info -q -k "{key}" "{db}"',
                    input=credentials.input(),
                )
                if mode == "ro":
                    ssh(
                        'if touch "$HOME/kp-test/kp-sync-write-probe-'
                        f'{session_id}" 2>/dev/null; '
                        "then exit 1; fi"
                    )
                    result["write_refused"] = True
                else:
                    ssh(
                        f'keepassxc-cli add -q -k "{key}" '
                        f'-u acceptance-user "{db}" {shlex.quote(title)}',
                        input=credentials.input(),
                    )
                    result["edit_sync"] = sync(
                        config,
                        credentials,
                        record=rec,
                        store=manager.store,
                    )
                    local = subprocess.run(
                        [
                            "keepassxc-cli",
                            "show",
                            "-q",
                            "-k",
                            str(config.key_file),
                            "-a",
                            "UserName",
                            str(config.hub),
                            title,
                        ],
                        input=credentials.input(),
                        capture_output=True,
                        timeout=30,
                    )
                    if (
                        local.returncode
                        or local.stdout.strip() != b"acceptance-user"
                    ):
                        raise RuntimeError(
                            "remote entry did not arrive in local test hub"
                        )
                    result["entry_propagated"] = True
                    ssh(
                        f'keepassxc-cli rm -q -k "{key}" "{db}" '
                        f"{shlex.quote(title)}",
                        input=credentials.input(),
                    )
                    result["deletion_sync"] = sync(
                        config,
                        credentials,
                        record=rec,
                        store=manager.store,
                    )
                    local = subprocess.run(
                        [
                            "keepassxc-cli",
                            "ls",
                            "-q",
                            "-k",
                            str(config.key_file),
                            str(config.hub),
                        ],
                        input=credentials.input(),
                        capture_output=True,
                        timeout=30,
                    )
                    if (
                        local.returncode
                        or title.encode() in local.stdout.splitlines()
                    ):
                        raise RuntimeError(
                            "test entry removal did not reach the local "
                            "root group"
                        )
                    result["entry_removed"] = True
                proc.stdin.write(b"exit\n")
                proc.stdin.flush()
                proc.wait(timeout=30)
                if proc.returncode:
                    raise RuntimeError(
                        f"remote shell failed (exit {proc.returncode})"
                    )
            keys.update(KEY_TAG, remove={session_id})
            result["post_sync"] = sync(
                config, credentials, record=rec, store=manager.store
            )
            clean = True
        finally:
            if proc is not None:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                if proc.stdin is not None:
                    proc.stdin.close()
            keys.update(KEY_TAG, remove={session_id})
            if setup:
                ssh(remote_cleanup_command(session_id))
                ssh(
                    'if mountpoint -q "$HOME/kp-test"; then exit 1; fi; '
                    'test ! -e "${XDG_RUNTIME_DIR:-/dev/shm}/'
                    f'kp-sync-session-{session_id}"'
                )
            if clean or mode == "ro":
                manager.discard(rec)
            else:
                print(
                    "Retained unsynced test data at "
                    f"{manager.store.staging_path(session_id)}"
                )
            if clean:
                log_path.unlink()
        result["cleanup"] = True
        report["sessions"].append(result)
    report["unchanged_sync"] = sync(config, credentials)
    # Do not overwrite concurrent authorized_keys edits; only compare.
    report["authorized_keys_unchanged"] = (
        keys.path.read_bytes() == original_keys
    )
    if not report["authorized_keys_unchanged"]:
        raise RuntimeError(
            "authorized_keys differs after acceptance; inspect "
            "concurrent edits"
        )
    atomic_write(
        runtime / "phase5-acceptance.json", json.dumps(report, indent=2) + "\n"
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    run()
