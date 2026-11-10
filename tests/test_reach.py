# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
import contextlib
import io
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from kp_sync import reach
from kp_sync.config import ConfigError, EndpointConfig
from kp_sync.config import _parse_endpoint as _endpoint
from kp_sync.endpoints.base import EndpointUnavailable
from kp_sync.endpoints.rclone import RcloneEndpoint
from kp_sync.engine import _reach_check
from kp_sync.integration import _reach_doctor_check
from kp_sync.reach import (
    Probes,
    ReachCheck,
    Target,
    connect_any,
    nm_connectivity,
    parse_target,
    rclone_config_path,
    remote_target,
    through_proxy,
)

CONFIG = """
[GoogleDrive]
type = drive
token = {"access_token":"SECRET"}

[Vault]
type = crypt
remote = GoogleDrive:vault
password = SECRET

[Deep]
type = alias
remote = Vault:x

[Local]
type = alias
remote = /srv/backup

[Minio]
type = s3
provider = Minio
endpoint = http://minio.lan:9000

[Aws]
type = s3
provider = AWS
region = eu-west-1

[NoEndpoint]
type = s3
provider = Wasabi

[Dav]
type = webdav
url = https://dav.example.org/remote.php/dav

[Box2]
type = sftp
host = files.example.org
port = 2222

[Both]
type = union
upstreams = GoogleDrive: Aws:

[Odd]
type = mega

[Cn]
type = onedrive
region = cn
"""


class Recorder:
    """Fake probes that record what the check asked."""

    def __init__(self, connectivity="unknown", connect=None):
        self.state = connectivity
        self.result = connect
        self.calls = []

    def probes(self):
        def connectivity(timeout):
            self.calls.append(("connectivity", timeout))
            return self.state

        def connect(host, port, timeout):
            self.calls.append(("connect", host, port))
            return self.result

        return Probes(connectivity=connectivity, connect=connect)


class TargetTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = self.root / "rclone.conf"
        self.config.write_text(CONFIG)

    def target(self, remote, environ=None):
        return remote_target(remote, self.config, environ or {})

    def test_parse_target(self):
        self.assertEqual(
            parse_target("proxy.lan:3128"), Target("proxy.lan", 3128)
        )
        self.assertEqual(parse_target("[::1]:443"), Target("::1", 443))
        self.assertEqual(str(Target("::1", 443)), "[::1]:443")
        for bad in ("", "host", "host:0", "host:99999", "a b:1", "::1:443"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_target(bad)

    def test_known_types_and_wrappers(self):
        cases = {
            "GoogleDrive:kp": Target("www.googleapis.com", 443, "https"),
            "Vault:": Target("www.googleapis.com", 443, "https"),
            "Deep:": Target("www.googleapis.com", 443, "https"),
            "Minio:bucket": Target("minio.lan", 9000, "http"),
            "Aws:bucket": Target("s3.eu-west-1.amazonaws.com", 443, "https"),
            "Dav:": Target("dav.example.org", 443, "https"),
            "Box2:": Target("files.example.org", 2222),
        }
        for remote, expected in cases.items():
            with self.subTest(remote=remote):
                self.assertEqual(self.target(remote), (expected, ""))

    def test_no_target_means_no_check(self):
        for remote, why in {
            "Local:": "local path",
            "NoEndpoint:": "no endpoint",
            "Both:": "no known server",
            "Odd:": "no known server",
            "Cn:": "region",
            "Missing:": "no type",
            "a,b:": "not a named remote",
        }.items():
            with self.subTest(remote=remote):
                target, reason = self.target(remote)
                self.assertIsNone(target)
                self.assertIn(why, reason)
                self.assertNotIn("SECRET", reason)

    def test_wrapper_depth_is_limited(self):
        text = "".join(
            f"[W{i}]\ntype = alias\nremote = W{i + 1}:\n" for i in range(6)
        )
        self.config.write_text(text + "[W6]\ntype = drive\n")
        self.assertIn("nested", self.target("W0:")[1])
        self.assertEqual(self.target("W2:")[0].host, "www.googleapis.com")

    def test_environment_remotes_and_unreadable_configs(self):
        self.assertEqual(
            remote_target("Env:", None, {"RCLONE_CONFIG_ENV_TYPE": "dropbox"})[
                0
            ],
            Target("api.dropboxapi.com", 443, "https"),
        )
        self.config.write_text("# Encrypted rclone configuration File\n\nX")
        self.assertIn("encrypted", self.target("GoogleDrive:")[1])
        self.config.write_text("[broken\n")
        self.assertIsNone(self.target("GoogleDrive:")[0])

    def test_config_path_follows_rclone(self):
        home = self.root / "home"
        default = home / ".config" / "rclone" / "rclone.conf"
        legacy = home / ".rclone.conf"
        env = {"HOME": str(home)}
        self.assertIsNone(rclone_config_path(None, env))
        legacy.parent.mkdir(parents=True)
        legacy.write_text("")
        self.assertEqual(rclone_config_path(None, env), legacy)
        default.parent.mkdir(parents=True)
        default.write_text("")
        self.assertEqual(rclone_config_path(None, env), default)
        self.assertEqual(
            rclone_config_path(None, env | {"RCLONE_CONFIG": "/x.conf"}),
            Path("/x.conf"),
        )
        self.assertEqual(rclone_config_path("/y.conf", env), Path("/y.conf"))

    def test_proxies_as_go_reads_them(self):
        api = Target("www.googleapis.com", 443, "https")
        plain = Target("minio.lan", 9000, "http")
        sftp = Target("files.example.org", 22)
        env = {
            "HTTPS_PROXY": "http://proxy.lan:3128",
            "http_proxy": "cache.lan",
            "ALL_PROXY": "socks5://ignored:1080",
        }
        self.assertEqual(through_proxy(api, env), Target("proxy.lan", 3128))
        self.assertEqual(through_proxy(plain, env), Target("cache.lan", 80))
        self.assertEqual(through_proxy(sftp, env), sftp)
        self.assertEqual(through_proxy(api, {"ALL_PROXY": "x:1"}), api)
        for no_proxy in ("googleapis.com", ".googleapis.com", "*"):
            with self.subTest(no_proxy=no_proxy):
                self.assertEqual(
                    through_proxy(api, env | {"NO_PROXY": no_proxy}), api
                )
        self.assertEqual(
            through_proxy(api, env | {"no_proxy": "example.org"}).host,
            "proxy.lan",
        )


class ProbeTest(unittest.TestCase):
    def test_network_manager_connectivity(self):
        def answer(stdout, code=0):
            def runner(argv, **kwargs):
                self.assertEqual(argv[:2], ["busctl", "--system"])
                self.assertIn("Connectivity", argv)
                return subprocess.CompletedProcess(argv, code, stdout, "")

            return runner

        for out, expected in (
            ("u 1\n", "none"),
            ("u 2\n", "portal"),
            ("u 4\n", "full"),
            ("u 9\n", "unknown"),
            ("garbage\n", "unknown"),
        ):
            with self.subTest(out=out):
                self.assertEqual(
                    nm_connectivity(1, runner=answer(out)), expected
                )
        self.assertEqual(
            nm_connectivity(1, runner=answer("u 4", 1)), "unknown"
        )

        def missing(argv, **kwargs):
            raise FileNotFoundError("busctl")

        self.assertEqual(nm_connectivity(1, runner=missing), "unknown")

    def test_connect_on_loopback(self):
        server = socket.socket()
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        self.assertIsNone(connect_any("127.0.0.1", port, 2))
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        free = closed.getsockname()[1]
        closed.close()
        reason = connect_any("127.0.0.1", free, 2)
        self.assertIn(f"connect to 127.0.0.1:{free} failed", reason)

    def test_a_hanging_or_failing_resolver(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def hanging(*args, **kwargs):
            release.wait(10)
            raise OSError("late")

        started = time.monotonic()
        reason = connect_any("slow.example", 443, 0.2, resolver=hanging)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(reason, "cannot resolve slow.example within 0.2 s")

        def failing(*args, **kwargs):
            raise socket.gaierror("no network")

        self.assertEqual(
            connect_any("x.example", 443, 1, resolver=failing),
            "cannot resolve x.example",
        )

    def test_a_silent_address_times_out_but_another_may_answer(self):
        server = socket.socket()
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        # 192.0.2.1 (TEST-NET-1) is never routed; the loopback answers.
        silent = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 9))
        alive = (
            socket.AF_INET,
            socket.SOCK_STREAM,
            6,
            "",
            ("127.0.0.1", port),
        )
        self.assertIsNone(
            connect_any("h", port, 2, resolver=lambda *a, **k: [silent, alive])
        )


class ReachCheckTest(unittest.TestCase):
    def setUp(self):
        quiet = contextlib.redirect_stderr(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.config = Path(temp.name) / "rclone.conf"
        self.config.write_text(CONFIG)

    def check(
        self, recorder, setting="auto", remote="GoogleDrive:kp", env=None
    ):
        return ReachCheck(
            setting,
            3,
            remote,
            environ=env or {},
            probes=recorder.probes(),
        )

    def test_verdicts(self):
        ok = Recorder("full")
        self.assertTrue(self.check(ok).run(self.config).ok)
        self.assertEqual(ok.calls[-1], ("connect", "www.googleapis.com", 443))
        portal = Recorder("portal")
        verdict = self.check(portal).run(self.config)
        self.assertEqual(
            (verdict.ok, verdict.reason),
            (False, "captive portal (NetworkManager)"),
        )
        self.assertNotIn("connect", [call[0] for call in portal.calls])
        # NetworkManager's "none" is not trusted; the connect decides.
        unmanaged = Recorder("none")
        self.assertTrue(self.check(unmanaged).run(self.config).ok)
        down = Recorder("none", connect="connect to x failed (unreachable)")
        verdict = self.check(down).run(self.config)
        self.assertFalse(verdict.ok)
        self.assertIn("unreachable", verdict.reason)

    def test_no_target_skips_every_probe(self):
        recorder = Recorder("portal")
        self.assertTrue(
            self.check(recorder, remote="Odd:").run(self.config).ok
        )
        self.assertEqual(recorder.calls, [])

    def test_explicit_target_and_proxy(self):
        recorder = Recorder()
        env = {"HTTPS_PROXY": "proxy.lan:3128"}
        self.check(recorder, env=env).run(self.config)
        self.assertEqual(recorder.calls[-1], ("connect", "proxy.lan", 3128))
        self.check(recorder, "gw.lan:8443", env=env).run(None)
        self.assertEqual(recorder.calls[-1], ("connect", "gw.lan", 8443))


class EndpointTest(unittest.TestCase):
    def setUp(self):
        quiet = contextlib.redirect_stderr(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def test_failed_check_skips_every_call_of_the_run(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "[]", "")

        recorder = Recorder(connect="connect to g:443 timed out after 3 s")
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "rclone.conf"
            config.write_text(CONFIG)
            endpoint = RcloneEndpoint(
                "cloud",
                "GoogleDrive:kp",
                rclone_config=config,
                runner=runner,
                reach=ReachCheck(
                    "auto", 3, "GoogleDrive:kp", probes=recorder.probes()
                ),
            )
            endpoint.begin_run()
            for _ in range(3):
                with self.assertRaisesRegex(
                    EndpointUnavailable, "reach check: connect to g:443"
                ):
                    endpoint.probe()
            self.assertEqual(calls, [])
            connects = [c for c in recorder.calls if c[0] == "connect"]
            self.assertEqual(len(connects), 1)  # once per run
            recorder.result = None
            endpoint.begin_run()
            endpoint.probe()
            self.assertEqual(len(calls), 1)
            endpoint.probe()
            connects = [c for c in recorder.calls if c[0] == "connect"]
            self.assertEqual(len(connects), 2)

    def test_a_command_timeout_ends_the_endpoint_for_the_run(self):
        calls = []
        outcomes = []

        def runner(argv, **kwargs):
            calls.append(argv)
            outcome = outcomes.pop(0) if outcomes else 0
            if outcome == "timeout":
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            return subprocess.CompletedProcess(argv, outcome, "[]", "")

        endpoint = RcloneEndpoint("cloud", "GoogleDrive", runner=runner)
        endpoint.begin_run()
        outcomes[:] = ["timeout"]
        with self.assertRaisesRegex(EndpointUnavailable, "timed out after"):
            endpoint.probe()
        for _ in range(2):
            with self.assertRaisesRegex(
                EndpointUnavailable,
                "unavailable for the rest of this run .*timed out",
            ):
                endpoint.probe()
        self.assertEqual(len(calls), 1)
        # A pause for a question (end_run) or a new run tries again.
        endpoint.end_run()
        endpoint.probe()
        self.assertEqual(len(calls), 2)
        outcomes[:] = ["timeout"]
        with self.assertRaises(EndpointUnavailable):
            endpoint.probe()
        endpoint.begin_run()
        endpoint.probe()
        self.assertEqual(len(calls), 4)
        # A failed or missing call is not a timeout: the next one runs.
        outcomes[:] = [1, 3]
        with self.assertRaises(EndpointUnavailable):
            endpoint.probe()
        self.assertIsNone(endpoint.probe())
        endpoint.probe()
        self.assertEqual(len(calls), 7)

    def test_without_a_check_nothing_changes(self):
        endpoint = RcloneEndpoint(
            "cloud",
            "GoogleDrive",
            runner=lambda argv, **kw: subprocess.CompletedProcess(
                argv, 0, "[]", ""
            ),
        )
        self.assertIsNone(endpoint.reach)
        endpoint.probe()


class ConfigTest(unittest.TestCase):
    def endpoint(self, **values):
        return _endpoint(
            {"name": "cloud", "type": "rclone", "remote": "Drive", **values},
            0,
        )

    def test_values(self):
        item = self.endpoint()
        self.assertEqual((item.reach_check, item.reach_timeout), ("auto", 3))
        self.assertIsInstance(_reach_check(item), ReachCheck)
        self.assertIsNone(_reach_check(self.endpoint(reach_check=False)))
        explicit = _reach_check(
            self.endpoint(reach_check="gw.lan:8443", reach_timeout=1.5)
        )
        self.assertEqual(explicit.explicit, Target("gw.lan", 8443))
        self.assertEqual(explicit.timeout, 1.5)
        for bad in (
            {"reach_check": True},
            {"reach_check": "nowhere"},
            {"reach_timeout": 0},
            {"reach_timeout": 60},
        ):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                self.endpoint(**bad)
        with self.assertRaisesRegex(ConfigError, "invalid key"):
            _endpoint(
                {"name": "d", "type": "file", "path": "/x", "reach_check": 1},
                0,
            )


class DoctorTest(unittest.TestCase):
    def test_check_and_probe_must_agree(self):
        item = EndpointConfig(
            "cloud", "rclone", remote="Box:", reach_check="h:1"
        )
        cases = (
            (Recorder(), True, "connects to h:1"),
            (Recorder(connect="connect to h:1 failed (x)"), False, "set"),
        )
        for recorder, ok, text in cases:
            with patch.object(reach, "PROBES", recorder.probes()):
                found = _reach_doctor_check(item, None)
            self.assertEqual(found.name, "reach:cloud")
            self.assertEqual(found.ok, ok)
            self.assertIn(text, found.message)
        skipped = _reach_doctor_check(
            EndpointConfig("cloud", "rclone", remote="Nowhere:"), None
        )
        self.assertTrue(skipped.ok)
        self.assertIn("skipped", skipped.message)
        self.assertIsNone(
            _reach_doctor_check(
                EndpointConfig("c", "rclone", remote="X:", reach_check=False),
                None,
            )
        )


if __name__ == "__main__":
    unittest.main()
