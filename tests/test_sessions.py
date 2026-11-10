# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
import os
import signal
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from kp_sync.endpoints.session import SessionEndpoint
from kp_sync.paths import run_dir
from kp_sync.sessions.authkeys import (
    AuthorizedKeys,
    AuthorizedKeysError,
    SessionLine,
    forced_command,
    render_line,
)
from kp_sync.sessions.records import (
    SessionError,
    SessionRecord,
    SessionStore,
    process_start,
)
from kp_sync.sessions.session import (
    SessionManager,
    finish_session,
    parse_target,
    remote_session_command,
    remote_setup_script,
    run_session,
)
from kp_sync.sessions import sftp_serve
from kp_sync.sessions.diagnostic import self_test

KEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIGdpeHhhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFh"
)
OTHER = b'command="/bin/true" ssh-rsa AAAAB3 someone@host # keep'


def record(
    session_id="aabbccdd",
    *,
    mode="ro",
    confined=True,
    alive=True,
    expires=None,
):
    pid = os.getpid() if alive else 2**22 + 5
    return SessionRecord(
        id=session_id,
        host="remote",
        user=None,
        mode=mode,
        owner_pid=pid,
        owner_start=process_start(os.getpid()) if alive else 1,
        ssh_pid=None,
        ssh_start=None,
        confined=confined,
        created=time.time(),
        expires=expires or time.time() + 3600,
    )


def line(session_id="aabbccdd", expires=None):
    return SessionLine(
        session_id,
        KEY,
        forced_command("/usr/bin/kp-sync", None, session_id),
        expires or time.time() + 3600,
    )


class AuthorizedKeysTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.ssh = root / ".ssh"
        self.ssh.mkdir(mode=0o700)
        self.path = self.ssh / "authorized_keys"
        self.keys = AuthorizedKeys(self.path, runtime_dir=root)

    def write(self, data: bytes):
        self.path.write_bytes(data)
        self.path.chmod(0o600)

    def test_other_lines_stay_byte_for_byte(self):
        original = b"# comment\n" + OTHER + b"\n\n  ssh-rsa AAAA odd spacing  "
        self.write(original)
        self.keys.update(None, add=(line(),))
        data = self.path.read_bytes()
        self.assertIn(b"kp-sync-session-_-aabbccdd", data)
        self.assertIn(b'restrict,from="127.0.0.1,::1"', data)
        self.assertTrue(data.startswith(original))
        self.keys.update(None, remove={"aabbccdd"})
        self.assertEqual(
            self.path.read_bytes().rstrip(b"\n"), original.rstrip(b"\n")
        )
        self.assertEqual(self.keys.read_lines(None), [])

    def test_missing_file_is_created_private(self):
        self.keys.update(None, add=(line(),))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_only_own_profile_is_touched_and_stale_lines_dropped(self):
        self.keys.update("other", add=(line("11111111"),))
        self.keys.update(None, add=(line("22222222"), line("33333333")))
        dropped = self.keys.update(None, keep_only={"22222222"})
        self.assertEqual(dropped, ["33333333"])
        self.assertEqual(
            [i for i, _ in self.keys.read_lines(None)], ["22222222"]
        )
        self.assertEqual(
            [i for i, _ in self.keys.read_lines("other")], ["11111111"]
        )

    def test_expired_lines_are_dropped(self):
        self.keys.update(
            None, add=(line("22222222", expires=time.time() - 7200),)
        )
        dropped = self.keys.update(None, keep_only={"22222222"})
        self.assertEqual(dropped, ["22222222"])

    def test_refuses_symlink_and_group_writable(self):
        target = Path(self.temp.name) / "real"
        target.write_bytes(OTHER + b"\n")
        self.path.symlink_to(target)
        with self.assertRaises(AuthorizedKeysError):
            self.keys.update(None, add=(line(),))
        self.assertEqual(target.read_bytes(), OTHER + b"\n")
        self.path.unlink()
        self.write(OTHER + b"\n")
        self.path.chmod(0o660)
        with self.assertRaises(AuthorizedKeysError):
            self.keys.update(None, add=(line(),))

    def test_command_is_escaped_to_one_option(self):
        rendered = render_line(
            None, SessionLine("aabbccdd", KEY, 'a "b" \\c', time.time() + 60)
        )
        self.assertEqual(rendered.count(b"\n"), 1)
        self.assertIn(b'command="a \\"b\\" \\\\c"', rendered)
        with self.assertRaises(AuthorizedKeysError):
            render_line(
                None, SessionLine("aabbccdd", KEY + "\nx", "c", time.time())
            )


class SftpServeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name)
        self.store = SessionStore(run_dir(None, xdg_runtime_dir=self.runtime))
        self.store.ensure()
        self.calls = []

    def serve(self, **kwargs):
        return sftp_serve.serve(
            None,
            "aabbccdd",
            runtime_dir=self.runtime,
            bwrap="/usr/bin/bwrap",
            sftp_server="/usr/lib/sftp-server",
            execute=lambda path, args: self.calls.append(args),
            **kwargs,
        )

    def stage(self, rec):
        self.store.save(rec)
        self.store.staging_path(rec.id).mkdir(exist_ok=True)

    def test_refuses_dead_owner_expired_and_unknown(self):
        self.assertEqual(self.serve(), 1)
        self.stage(record(alive=False))
        self.assertEqual(self.serve(), 1)
        self.stage(record(expires=time.time() - 5))
        self.assertEqual(self.serve(), 1)
        self.assertEqual(self.calls, [])

    def test_confined_read_only_command(self):
        self.stage(record())
        self.serve()
        (arguments,) = self.calls
        self.assertEqual(arguments[0], "/usr/bin/bwrap")
        self.assertIn("--unshare-all", arguments)
        self.assertIn("-R", arguments)
        self.assertEqual(
            arguments[arguments.index("--bind") + 1],
            str(self.store.staging_path("aabbccdd")),
        )

    def test_read_write_has_no_readonly_flag_and_unconfined_skips_bwrap(self):
        self.stage(record(mode="rw"))
        self.serve()
        self.assertNotIn("-R", self.calls[0])
        self.stage(record(mode="rw", confined=False))
        self.serve()
        self.assertEqual(self.calls[1][0], "/usr/lib/sftp-server")


class RemoteCommandTest(unittest.TestCase):
    def test_parse_target(self):
        self.assertEqual(
            parse_target("bob@host:~/kp"), ("bob", "host", "~/kp")
        )
        self.assertEqual(parse_target("host"), (None, "host", None))
        for bad in ("-oProxyCommand=x", "a b", "bo b@host"):
            with self.assertRaises(SessionError):
                parse_target(bad)

    def test_private_key_only_in_setup_script_not_in_command(self):
        script = remote_setup_script(
            "aabbccdd", "PRIVATE-KEY-DATA", "ssh-ed25519 HOSTKEY", 2222
        )
        self.assertIn("PRIVATE-KEY-DATA", script)
        self.assertIn("[localhost]:2222", script)
        command = remote_session_command("aabbccdd", "ro", "bob", 2222, "~/kp")
        self.assertNotIn("PRIVATE-KEY-DATA", command)
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("IdentitiesOnly=yes", command)


class FakeAuthKeys:
    def __init__(self):
        self.removed = []

    def update(self, profile, *, add=(), remove=(), keep_only=None):
        self.removed.extend(remove)
        return []


class FinishSessionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        config = SimpleNamespace(
            profile=None,
            sessions={"session_key_ttl": 86400},
            sync={"lock_timeout": 1},
        )
        self.manager = SessionManager(
            config,
            Path(self.temp.name),
            authkeys=FakeAuthKeys(),
            executable="/x/kp-sync",
        )
        self.manager.store.ensure()

    def make(self, **kwargs):
        rec = record(**kwargs)
        self.manager.store.save(rec)
        self.manager.store.staging_path(rec.id).mkdir()
        return rec

    def test_rw_session_kept_until_post_sync_is_clean(self):
        rec = self.make(mode="rw")
        order = []
        self.manager.authkeys.update = (
            lambda *a, **k: order.append("revoke") or []
        )
        self.assertFalse(
            finish_session(self.manager, rec, lambda: {"pending": True})
        )
        self.assertFalse(
            finish_session(
                self.manager, rec, lambda: {"action": "unavailable"}
            )
        )
        self.assertTrue(self.manager.store.record_path(rec.id).exists())
        self.assertTrue(
            finish_session(
                self.manager,
                rec,
                lambda: order.append("sync") or {"action": "done"},
            )
        )
        self.assertEqual(order, ["revoke", "revoke", "revoke", "sync"])
        self.assertFalse(self.manager.store.record_path(rec.id).exists())
        self.assertFalse(self.manager.store.staging_path(rec.id).exists())

    def test_failed_sync_keeps_rw_session_but_not_confined_ro(self):
        rw = self.make(mode="rw")

        def boom():
            raise RuntimeError("down")

        self.assertFalse(finish_session(self.manager, rw, boom))
        ro = self.make(session_id="11223344")
        self.assertTrue(finish_session(self.manager, ro, boom))
        self.assertEqual(
            self.manager.authkeys.removed, ["aabbccdd", "11223344"]
        )

    def test_logout_reports_wait_before_sync_and_success_only_after_cleanup(
        self,
    ):
        rec = self.make(mode="rw")
        with patch("kp_sync.sessions.session.info") as log:

            def sync():
                self.assertEqual(log.call_count, 2)
                log.assert_called_with(
                    "Session access closed; finishing synchronization "
                    "before removing local session files..."
                )
                self.assertTrue(
                    self.manager.store.record_path(rec.id).exists()
                )
                return {"action": "unchanged"}

            self.assertTrue(finish_session(self.manager, rec, sync))
            self.assertEqual(log.call_args.args, ("Session cleanup complete",))
            self.assertFalse(self.manager.store.record_path(rec.id).exists())
            self.assertEqual(
                self.manager.store.pending_retirements()[0][:2],
                (rec.identity, rec.id),
            )
        rec = self.make(mode="rw")
        with patch("kp_sync.sessions.session.info") as log:
            self.assertFalse(
                finish_session(
                    self.manager, rec, lambda: {"action": "pending"}
                )
            )
            self.assertEqual(log.call_count, 2)


class RetirementProgressTest(unittest.TestCase):
    def test_progress_owner_must_match_retired_session_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SessionStore(Path(temp))
            with self.assertRaisesRegex(SessionError, "does not match"):
                store.prepare_retirement("session:remote:aabbccdd", "11223344")

    def test_corrupt_progress_cannot_claim_another_session_id(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SessionStore(Path(temp))
            path = store.prepare_retirement(
                "session:remote:aabbccdd", "aabbccdd"
            )
            path.write_text(
                '{"identity":"session:remote:aabbccdd",'
                '"owner_id":"11223344"}\n',
                encoding="utf-8",
            )
            path.chmod(0o600)
            with self.assertRaisesRegex(SessionError, "does not match"):
                store.pending_retirements()


class SessionEndpointTest(unittest.TestCase):
    def test_flags(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SessionStore(Path(temp))
            ro = SessionEndpoint(record(mode="ro"), store, "pass.kdbx")
            rw = SessionEndpoint(record(mode="rw"), store, "pass.kdbx")
            self.assertTrue(ro.never_source)
            self.assertFalse(ro.foreign_writers)
            self.assertFalse(rw.never_source)
            self.assertTrue(rw.foreign_writers)
            self.assertEqual(ro.identity, "session:remote:aabbccdd")


class NeverSourceTest(unittest.TestCase):
    def test_changed_read_only_copy_is_demoted_to_stale(self):
        from kp_sync.classify import Classification, CopyState
        from kp_sync.engine import SyncEngine

        with tempfile.TemporaryDirectory() as temp:
            store = SessionStore(Path(temp))
            ro = SessionEndpoint(record(mode="ro"), store, "pass.kdbx")
            rw = SessionEndpoint(
                record("11223344", mode="rw"), store, "pass.kdbx"
            )
            classified = {
                ident: Classification(ident, CopyState.CHANGED, "x", None)
                for ident in (ro.identity, rw.identity)
            }
            observations = {
                ro.identity: SimpleNamespace(endpoint=ro),
                rw.identity: SimpleNamespace(endpoint=rw),
            }
            result = SyncEngine._demote_never_sources(classified, observations)
            self.assertEqual(result[ro.identity].state, CopyState.STALE)
            self.assertEqual(result[rw.identity].state, CopyState.CHANGED)


class SessionDoctorTest(unittest.TestCase):
    def test_reports_stale_keys_and_unreachable_sshd(self):
        from kp_sync.integration import session_checks

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".ssh").mkdir(mode=0o700)
            keys = AuthorizedKeys(
                root / ".ssh" / "authorized_keys", runtime_dir=root
            )
            keys.update(None, add=(line("99999999"),))
            config = SimpleNamespace(profile=None)
            checks = {
                check.name: check
                for check in session_checks(
                    config,
                    runtime_dir=root,
                    authkeys=keys,
                    sshd_probe=lambda: (False, "down"),
                )
            }
            self.assertTrue(checks["session:authorized_keys"].ok)
            self.assertFalse(checks["session:stale-keys"].ok)
            self.assertIn("99999999", checks["session:stale-keys"].message)
            self.assertFalse(checks["session:sshd"].ok)


class SessionStartupTest(unittest.TestCase):
    def test_staging_rejects_an_identity_retired_while_id_was_selected(self):
        from kp_sync.state import HistoryState
        from kp_sync.sessions.session import stage_session

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "runtime"
            runtime.mkdir(mode=0o700)
            hub = root / "pass.kdbx"
            hub.write_bytes(b"synthetic database")
            key_file = root / "pass.keyx"
            key_file.write_bytes(b"synthetic key file")
            config = SimpleNamespace(
                hub=hub,
                key_file=key_file,
                db_name="pass.kdbx",
                sync={"lock_timeout": 1},
            )
            store = SessionStore(runtime)
            session = record()
            state = HistoryState()
            state.retire(session.identity, "test")
            state.save(runtime / "state.json")
            manager = SimpleNamespace(
                config=config,
                store=store,
                run_directory=runtime,
            )

            with self.assertRaisesRegex(SessionError, "already retired"):
                stage_session(manager, session.id, record=session)
            self.assertFalse(store.staging_path(session.id).exists())

    def test_stages_hub_without_requesting_a_pre_sync(self):
        from kp_sync.sessions.session import (
            SessionManager as RealSessionManager,
        )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            hub = root / "pass.kdbx"
            hub.write_bytes(b"current hub snapshot")
            key_file = root / "pass.keyx"
            key_file.write_bytes(b"synthetic key file")
            host_key = root / "host.pub"
            host_key.write_text(KEY + "\n", encoding="ascii")
            runtime = root / "runtime"
            runtime.mkdir(mode=0o700)
            config = SimpleNamespace(
                profile=None,
                hub=hub,
                key_file=key_file,
                db_name="pass.kdbx",
                sessions={
                    "enabled": True,
                    "default_mode": "ro",
                    "session_key_ttl": 3600,
                    "tunnel_port": 2222,
                    "mount_dir": "~/kp",
                },
                ui={"mode": "none"},
                sync={"lock_timeout": 1},
            )
            calls = []
            ssh_calls = []

            class FakeConnection:
                def send(self, message):
                    calls.append(("lease", message["command"]))

                def receive(self):
                    return {"type": "result", "result": {"action": "lease"}}

                def close(self):
                    pass

            class FakeClient:
                def connect(self):
                    return FakeConnection()

                def request(self, command, **kwargs):
                    calls.append((command, kwargs))
                    return {"action": "unchanged"}

            class SafeSessionManager(RealSessionManager):
                def revoke(self, record):
                    self.authkeys.update(
                        self.config.profile, remove={record.id}
                    )

            class FakeProcess:
                pid = os.getpid()

                def __init__(self):
                    self.terminated = False

                def wait(self, timeout=None):
                    signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
                    return -signal.SIGTERM

                def poll(self):
                    return -signal.SIGTERM if self.terminated else None

                def terminate(self):
                    self.terminated = True

            def runner(argv, **kwargs):
                if argv[0] == "ssh-keygen":
                    private = Path(argv[-1])
                    private.write_text(
                        "synthetic private key\n", encoding="ascii"
                    )
                    private.chmod(0o600)
                    private.with_name(private.name + ".pub").write_text(
                        KEY + " synthetic\n",
                        encoding="ascii",
                    )
                    return SimpleNamespace(returncode=0, stderr="")
                if argv[0] == "ssh":
                    ssh_calls.append(kwargs)
                    return SimpleNamespace(returncode=0, stderr="")
                raise AssertionError(f"unexpected command: {argv[0]}")

            observed_stage = []

            managed_ssh_options = []

            def popen(argv, **kwargs):
                stage = next(
                    (runtime / "kp-sync" / "sessions").glob("*/pass.kdbx")
                )
                observed_stage.append(stage.read_bytes())
                self.assertIn("-R", argv)
                managed_ssh_options.append(kwargs)
                return FakeProcess()

            with (
                patch(
                    "kp_sync.sessions.session.SessionManager",
                    SafeSessionManager,
                ),
                patch(
                    "kp_sync.sessions.session.ensure_run_dir",
                    lambda profile: runtime / "kp-sync",
                ),
                patch(
                    "kp_sync.sessions.session.find_bwrap",
                    return_value="/usr/bin/bwrap",
                ),
            ):
                result = run_session(
                    config,
                    "remote.example:kp",
                    mode="ro",
                    client=FakeClient(),
                    runner=runner,
                    popen=popen,
                    executable="/usr/bin/kp-sync",
                    authkeys=FakeAuthKeys(),
                    host_key=host_key,
                    ui="none",
                    managed=True,
                )

            self.assertEqual(result, 0)
            self.assertEqual(observed_stage, [b"current hub snapshot"])
            self.assertEqual(
                managed_ssh_options,
                [
                    {
                        "stdin": subprocess.PIPE,
                        "stdout": subprocess.DEVNULL,
                        "stderr": subprocess.DEVNULL,
                    }
                ],
            )
            self.assertTrue(ssh_calls[0]["capture_output"])
            self.assertEqual(
                [call[0] for call in calls if isinstance(call, tuple)],
                ["lease", "sync"],
            )


class SessionSelfTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.ssh = self.root / ".ssh"
        self.ssh.mkdir(mode=0o700)
        self.keys = AuthorizedKeys(
            self.ssh / "authorized_keys", runtime_dir=self.root
        )
        self.keys.path.write_bytes(OTHER + b"\n")
        self.keys.path.chmod(0o600)
        self.host_key = self.root / "host.pub"
        self.host_key.write_text(KEY + "\n")
        self.config = SimpleNamespace(profile="test")
        self.calls = []

    def runner(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[0] == "ssh-keygen":
            private = Path(argv[-1])
            private.write_text("synthetic private key\n")
            private.with_suffix(".key.pub").write_text(KEY + "\n")
        elif argv[0] == "sftp":
            records = SessionStore(self.runtime / "diagnostics").records()
            self.assertEqual(len(records), 1)
            rec = records[0]
            self.assertLessEqual(rec.expires - time.time(), 300)
            self.assertIn("--diagnostic", self.keys.path.read_text())
            self.assertTrue(rec.confined)
            work = next((self.runtime / "diagnostics/sessions").glob("*.work"))
            self.assertEqual(
                (work / "known_hosts").read_text(), f"localhost {KEY}\n"
            )
            payload = (work / "challenge").read_bytes()
            (work / "received").write_bytes(payload)
            SessionStore(self.runtime / "diagnostics").staging_path(
                rec.id
            ).joinpath(
                "challenge",
            ).write_bytes(
                payload
            )
        return SimpleNamespace(returncode=0)

    def run_test(self, runner=None):
        with (
            patch(
                "kp_sync.sessions.diagnostic.find_bwrap",
                return_value="/usr/bin/bwrap",
            ),
            patch(
                "kp_sync.sessions.diagnostic.find_sftp_server",
                return_value="/usr/lib/openssh/sftp-server",
            ),
        ):
            self_test(
                self.config,
                runtime_directory=self.runtime,
                authkeys=self.keys,
                runner=runner or self.runner,
                executable="/usr/bin/kp-sync",
                host_key=self.host_key,
            )

    def assert_clean(self):
        self.assertEqual(self.keys.path.read_bytes(), OTHER + b"\n")
        self.assertEqual(
            list((self.runtime / "diagnostics/sessions").iterdir()), []
        )

    def test_round_trip_is_confined_bounded_and_cleans_only_its_key(self):
        self.run_test()
        self.assert_clean()
        self.assertEqual(
            [argv[0] for argv in self.calls], ["ssh-keygen", "sftp"]
        )

    def test_failed_or_changed_round_trips_still_revoke_and_clean(
        self,
    ):
        for failure in ("exit", "timeout", "different-bytes"):
            with self.subTest(failure=failure):

                def runner(argv, **kwargs):
                    if argv[0] == "sftp":
                        if failure == "exit":
                            self.runner(argv, **kwargs)
                            return SimpleNamespace(returncode=1)
                        if failure == "timeout":
                            raise subprocess.TimeoutExpired(argv, 30)
                        result = self.runner(argv, **kwargs)
                        next(
                            (self.runtime / "diagnostics/sessions").glob(
                                "*.work/received"
                            )
                        ).write_bytes(b"wrong")
                        return result
                    return self.runner(argv, **kwargs)

                with self.assertRaises(
                    (SessionError, subprocess.TimeoutExpired)
                ):
                    self.run_test(runner)
                self.assert_clean()

    def test_live_diagnostic_key_is_not_swept_or_exposed_as_database_endpoint(
        self,
    ):
        from kp_sync.engine import session_endpoints

        def runner(argv, **kwargs):
            result = self.runner(argv, **kwargs)
            if argv[0] == "sftp":
                manager = SessionManager(
                    self.config, self.runtime, authkeys=self.keys
                )
                before = self.keys.path.read_bytes()
                self.assertEqual(manager.drop_stale_lines(), [])
                self.assertEqual(self.keys.path.read_bytes(), before)
                self.assertEqual(
                    session_endpoints(self.config, self.runtime), []
                )
            return result

        self.run_test(runner)
        self.assert_clean()

    def test_revocation_failure_is_reported_and_keeps_diagnostic_record(self):
        original = self.keys.update

        def update(profile, **kwargs):
            if kwargs.get("remove"):
                raise AuthorizedKeysError("synthetic revocation failure")
            return original(profile, **kwargs)

        with patch.object(self.keys, "update", side_effect=update):
            with self.assertRaisesRegex(
                AuthorizedKeysError, "revocation failure"
            ):
                self.run_test()
        records = SessionStore(self.runtime / "diagnostics").records()
        self.assertEqual(len(records), 1)
        self.assertEqual(
            [value for value, _ in self.keys.read_lines("test")],
            [records[0].id],
        )
        self.assertLessEqual(records[0].expires - time.time(), 300)

    @unittest.skipUnless(
        shutil.which("sshd")
        and shutil.which("bwrap")
        and shutil.which("sftp")
        and sftp_serve.find_sftp_server(),
        "requires sshd, bwrap, sftp and sftp-server",
    )
    def test_real_localhost_sftp_uses_temporary_authorized_keys(self):
        from kp_sync.paths import atomic_write

        subprocess.run(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                str(self.root / "host"),
            ],
            check=True,
            capture_output=True,
            timeout=10,
        )
        wrapper = self.root / "kp-sync"
        atomic_write(
            wrapper,
            (
                f"#!{sys.executable}\n"
                "import sys\n"
                "sys.path.insert(0, "
                f"{str(Path(__file__).resolve().parents[1] / 'src')!r})\n"
                "from kp_sync.sessions.sftp_serve import serve\n"
                "raise SystemExit(serve('test', sys.argv[-1], "
                "diagnostic='--diagnostic' in sys.argv, "
                f"runtime_dir={str(self.root / 'xdg')!r}))\n"
            ),
        )
        wrapper.chmod(0o700)
        self.runtime = self.root / "xdg/kp-sync-test"
        self.runtime.mkdir(parents=True, mode=0o700)
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        configuration = self.root / "sshd_config"
        configuration.write_text(
            f"Port {port}\nListenAddress 127.0.0.1\n"
            f"HostKey {self.root / 'host'}\n"
            f"PidFile {self.root / 'sshd.pid'}\n"
            f"AuthorizedKeysFile {self.keys.path}\n"
            "StrictModes no\nUsePAM no\nPasswordAuthentication no\n"
            "KbdInteractiveAuthentication no\n"
            "PubkeyAuthentication yes\nAllowTcpForwarding no\n"
            "PermitRootLogin yes\n"
            f"Subsystem sftp {sftp_serve.find_sftp_server()}\n"
        )
        with (self.root / "sshd.log").open("wb") as log:
            server = subprocess.Popen(
                [shutil.which("sshd"), "-D", "-e", "-f", str(configuration)],
                stderr=log,
            )
            try:
                deadline = time.monotonic() + 5
                while True:
                    if server.poll() is not None:
                        self.fail((self.root / "sshd.log").read_text())
                    try:
                        with socket.create_connection(
                            ("127.0.0.1", port), timeout=0.2
                        ):
                            break
                    except OSError:
                        if time.monotonic() >= deadline:
                            self.fail("temporary sshd did not become ready")
                        time.sleep(0.05)
                try:
                    self_test(
                        self.config,
                        runtime_directory=self.runtime,
                        authkeys=self.keys,
                        executable=str(wrapper),
                        host_key=self.root / "host.pub",
                        port=port,
                    )
                except SessionError as exc:
                    self.fail(f"{exc}\n{(self.root / 'sshd.log').read_text()}")
                self.assert_clean()
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()


if __name__ == "__main__":
    unittest.main()
