import json
import os
import re
import shutil
import socket
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from kp_sync.endpoints.base import EndpointUnavailable
from kp_sync.endpoints.rclone import EndpointMissing, RcloneEndpoint
from kp_sync.rclone_credentials import RcloneCredentials
from kp_sync.rclone_rcd import (
    RcdError,
    RcdTimeout,
    RcdUnreachable,
    RcloneDaemon,
    socket_path,
    translate,
)
from tests.fixtures import private_tempdir as _private_tempdir

ROOT = "GoogleDrive:kp"


def private_tempdir(test):
    temporary = _private_tempdir()
    test.addCleanup(temporary.cleanup)
    path = Path(temporary.name)
    os.chmod(path, 0o700)
    return path


class Recorder:
    """Stands in for an RcloneDaemon when testing translations."""

    def __init__(self, outputs=()):
        self.calls = []
        self.outputs = list(outputs)

    def call(self, method, params, timeout):
        self.calls.append((method, params, timeout))
        output = self.outputs.pop(0) if self.outputs else {}
        if isinstance(output, Exception):
            raise output
        return output


class TranslateTest(unittest.TestCase):
    def run_request(self, argv, *outputs):
        request = translate(argv, ROOT)
        self.assertIsNotNone(request, argv)
        recorder = Recorder(outputs)
        return request(recorder, 7), recorder.calls

    def test_single_file_listing_is_a_stat_in_its_directory(self):
        item = {"Path": "state.json", "Name": "state.json", "IsDir": False}
        stdout, calls = self.run_request(
            ["rclone", "lsjson", "--hash", "GoogleDrive:kp/h/state.json"],
            {"item": item},
        )
        self.assertEqual(json.loads(stdout), [item])
        self.assertEqual(
            calls,
            [
                (
                    "operations/stat",
                    {
                        "fs": "GoogleDrive:kp/h",
                        "remote": "state.json",
                        "opt": {"showHash": True},
                    },
                    7,
                )
            ],
        )

    def test_missing_file_is_missing_and_directories_are_listed(self):
        with self.assertRaises(RcdError) as raised:
            self.run_request(
                ["rclone", "lsjson", "--hash", "GoogleDrive:kp/x"],
                {"item": None},
            )
        self.assertTrue(raised.exception.missing)
        listed = [{"Path": "a.kdbx", "Name": "a.kdbx"}]
        stdout, calls = self.run_request(
            ["rclone", "lsjson", "--hash", "GoogleDrive:kp/h/bases"],
            {"item": {"IsDir": True}},
            {"list": listed},
        )
        self.assertEqual(json.loads(stdout), listed)
        self.assertEqual(calls[1][1]["fs"], "GoogleDrive:kp/h/bases")
        stdout, calls = self.run_request(
            ["rclone", "lsjson", "--hash", "GoogleDrive:top"],
            {"item": {"IsDir": True}},
            {"list": []},
        )
        self.assertEqual(calls[0][1]["fs"], "GoogleDrive:")
        self.assertEqual(calls[1][1]["fs"], "GoogleDrive:top")

    def test_filtered_listing_keeps_the_include_rules_and_depth(self):
        argv = [
            "rclone",
            "lsjson",
            "--hash",
            "-R",
            "--files-only",
            "--max-depth",
            "3",
            "--include",
            "/pass.kdbx",
            "--include",
            "/h/bases/*.kdbx",
            ROOT,
        ]
        stdout, calls = self.run_request(argv, {"list": [{"Path": "x"}]})
        self.assertEqual(json.loads(stdout), [{"Path": "x"}])
        self.assertEqual(
            calls[0][:2],
            (
                "operations/list",
                {
                    "fs": ROOT,
                    "remote": "",
                    "opt": {
                        "recurse": True,
                        "showHash": True,
                        "filesOnly": True,
                    },
                    "_filter": {
                        "IncludeRule": ["/pass.kdbx", "/h/bases/*.kdbx"],
                        "MaxDepth": 3,
                    },
                },
            ),
        )
        with self.assertRaises(RcdError):
            self.run_request(argv, {"other": []})

    def test_transfers_and_removals(self):
        _, calls = self.run_request(
            ["rclone", "copyto", "GoogleDrive:kp/pass.kdbx", "/run/x/.p.1"]
        )
        self.assertEqual(
            calls[0][:2],
            (
                "operations/copyfile",
                {
                    "srcFs": "GoogleDrive:kp",
                    "srcRemote": "pass.kdbx",
                    "dstFs": "/run/x",
                    "dstRemote": ".p.1",
                },
            ),
        )
        _, calls = self.run_request(
            ["rclone", "copyto", "/run/x/s.json", "GoogleDrive:kp/state.json"]
        )
        self.assertEqual(calls[0][1]["dstFs"], "GoogleDrive:kp")
        self.assertEqual(calls[0][1]["srcRemote"], "s.json")
        for command in ("deletefile", "rmdir"):
            _, calls = self.run_request(
                ["rclone", command, "GoogleDrive:kp/bases/a.kdbx"]
            )
            self.assertEqual(
                calls[0][:2],
                (
                    f"operations/{command}",
                    {"fs": "GoogleDrive:kp/bases", "remote": "a.kdbx"},
                ),
            )

    def test_other_shapes_run_as_commands(self):
        for argv in (
            [],
            ["download", "GoogleDrive:kp/pass.kdbx", "/tmp/x"],
            ["rclone", "copyto", "GoogleDrive:kp/a", "GoogleDrive:kp/b"],
            ["rclone", "copyto", "/tmp/a", "/tmp/b"],
            ["rclone", "copyto", "GoogleDrive:kp/a", "relative"],
            ["rclone", "copyto", "Other:kp/a", "/tmp/a"],
            ["rclone", "copyto", "GoogleDrive:kp/../a", "/tmp/a"],
            ["rclone", "copyto", "GoogleDrive:/abs", "/tmp/a"],
            ["rclone", "copyto", "--checksum", "GoogleDrive:kp/a", "/tmp/a"],
            ["rclone", "copy", "--files-from-raw", "l", "/tmp", ROOT],
            ["rclone", "delete", "--files-from-raw", "l", ROOT],
            ["rclone", "lsjson", "GoogleDrive:kp/a"],
            ["rclone", "lsjson", "--hash", "-R", "GoogleDrive:kp"],
            ["rclone", "lsjson", "--hash", "--fast", "GoogleDrive:kp/a"],
            [
                "rclone",
                "lsjson",
                "--hash",
                "-R",
                "--files-only",
                "--max-depth",
                "2",
                "--exclude",
                "*",
                ROOT,
            ],
            ["rclone", "deletefile", "GoogleDrive:"],
            ["rclone", "purge", "GoogleDrive:kp"],
        ):
            with self.subTest(argv=argv):
                self.assertIsNone(translate(argv, ROOT))


class FakeHTTP:
    """Scripted rc server keyed by method; records requests."""

    def __init__(self, handler):
        self.handler = handler
        self.requests = []
        self.reachable = True

    def factory(self, path, timeout):
        server = self

        class Connection:
            def connect(self):
                if not server.reachable:
                    raise ConnectionRefusedError

            def request(self, verb, url, body, headers):
                self.method = url[1:]
                self.params = json.loads(body)
                server.requests.append((self.method, self.params))

            def getresponse(self):
                status, payload = server.handler(self.method, self.params)

                class Response:
                    def read(self, limit):
                        return json.dumps(payload).encode()

                response = Response()
                response.status = status
                return response

            def close(self):
                pass

        return Connection()


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(seconds, 0.01)


class RcloneDaemonCallTest(unittest.TestCase):
    def setUp(self):
        self.directory = private_tempdir(self)
        self.clock = Clock()

    def daemon(self, handler, **kwargs):
        self.http = FakeHTTP(handler)
        return RcloneDaemon(
            "cloud",
            self.directory,
            owner=False,
            connection=self.http.factory,
            clock=self.clock,
            sleep=self.clock.sleep,
            **kwargs,
        )

    def test_jobs_are_polled_until_finished(self):
        polls = []

        def handler(method, params):
            if method == "job/status":
                polls.append(params["jobid"])
                finished = len(polls) > 2
                return 200, {
                    "finished": finished,
                    "success": True,
                    "output": {"list": []},
                }
            self.assertTrue(params["_async"])
            return 200, {"jobid": 4}

        rcd = self.daemon(handler)
        self.assertEqual(
            rcd.call("operations/list", {"fs": ROOT}, 10), {"list": []}
        )
        self.assertEqual(polls, [4, 4, 4])

    def test_not_found_errors_are_missing_but_unknown_methods_are_not(self):
        responses = {
            "a": (404, {"error": "object not found", "status": 404}),
            "b": (404, {"error": "directory not found", "status": 404}),
            "c": (404, {"error": 'couldn\'t find method "c"'}),
            "d": (500, {"error": "object not found"}),
            "e": (500, {"error": "googleapi: Error 403"}),
            "f": (200, ["not", "a", "dict"]),
        }
        rcd = self.daemon(lambda method, params: responses[method])
        for method, missing in (
            ("a", True),
            ("b", True),
            ("c", False),
            ("d", False),
            ("e", False),
            ("f", False),
        ):
            with self.subTest(method=method):
                with self.assertRaises(RcdError) as raised:
                    rcd.call(method, {}, 10)
                self.assertIs(raised.exception.missing, missing)

    def test_failed_jobs_map_not_found_to_missing(self):
        for message, missing in (
            ("object not found", True),
            ("failed to copy: googleapi: Error 500", False),
        ):

            def handler(method, params):
                if method == "job/status":
                    return 200, {
                        "finished": True,
                        "success": False,
                        "error": message,
                    }
                return 200, {"jobid": 1}

            with self.subTest(message=message):
                with self.assertRaises(RcdError) as raised:
                    self.daemon(handler).call("operations/copyfile", {}, 10)
                self.assertIs(raised.exception.missing, missing)

    def test_timeout_stops_the_job_and_waits_for_it(self):
        stopped = []

        def handler(method, params):
            if method == "job/status":
                return 200, {"finished": bool(stopped)}
            if method == "job/stop":
                stopped.append(params["jobid"])
                return 200, {}
            return 200, {"jobid": 9}

        rcd = self.daemon(handler)
        with self.assertRaises(RcdTimeout):
            rcd.call("operations/copyfile", {}, 2)
        self.assertEqual(stopped, [9])
        self.assertEqual(self.http.requests[-1][0], "job/status")
        self.assertNotIn("core/quit", [r[0] for r in self.http.requests])
        self.assertGreaterEqual(self.clock.now, 2)

    def test_job_that_does_not_stop_takes_the_server_down(self):
        def handler(method, params):
            if method == "job/status":
                return 200, {"finished": False}
            return 200, {"jobid": 9} if method != "job/stop" else {}

        client = self.daemon(handler)
        with self.assertRaises(RcdTimeout):
            client.call("operations/copyfile", {}, 2)
        self.assertEqual(self.http.requests[-1], ("core/quit", {}))
        owner = self.daemon(handler)
        owner.owner = True
        owner._terminate = lambda: self.http.requests.append(("kill", {}))
        with self.assertRaises(RcdTimeout):
            owner.call("operations/copyfile", {}, 2)
        self.assertEqual(self.http.requests[-1], ("kill", {}))

    def test_unreachable_before_request_and_lost_server_after_it(self):
        rcd = self.daemon(lambda method, params: (200, {"jobid": 1}))
        self.http.reachable = False
        with self.assertRaises(RcdUnreachable):
            rcd.call("operations/list", {}, 10)
        self.assertEqual(self.http.requests, [])
        calls = []

        def handler(method, params):
            calls.append(method)
            self.http.reachable = False
            return 200, {"jobid": 1}

        rcd = self.daemon(handler)
        with self.assertRaises(RcdError) as raised:
            rcd.call("operations/copyfile", {}, 10)
        self.assertNotIsInstance(raised.exception, RcdUnreachable)
        self.assertEqual(calls, ["operations/copyfile"])


class FakeTransport:
    def __init__(self, *, ready=True, outputs=()):
        self.is_ready = ready
        self.outputs = list(outputs)
        self.calls = []
        self.stopped = 0

    def ready(self):
        return self.is_ready

    def call(self, method, params, timeout):
        self.calls.append((method, params, timeout))
        output = self.outputs.pop(0) if self.outputs else {}
        if isinstance(output, Exception):
            raise output
        return output

    def stop(self):
        self.stopped += 1


class Runner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[1] == "lsjson":
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        if argv[1] == "copyto" and not argv[3].startswith("GoogleDrive:"):
            Path(argv[3]).write_bytes(b"downloaded")
        return subprocess.CompletedProcess(argv, 0, "", "")


class EndpointTransportTest(unittest.TestCase):
    def setUp(self):
        self.directory = private_tempdir(self)
        self.runner = Runner()

    def endpoint(self, transport, **kwargs):
        return RcloneEndpoint(
            "cloud",
            ROOT,
            runner=self.runner,
            transport=transport,
            timeout=12,
            **kwargs,
        )

    def test_ready_rcd_replaces_generated_commands(self):
        item = {"Path": "pass.kdbx", "Name": "pass.kdbx", "Size": 5}
        transport = FakeTransport(outputs=[{"list": [item]}, {}, {}, {}])
        endpoint = self.endpoint(transport)
        endpoint.track_directory("h")
        self.assertEqual(endpoint.probe().size, 5)
        source = self.directory / "s.json"
        source.write_bytes(b"{}")
        endpoint.upload_file(source, endpoint.remote_path("h/state.json"))
        endpoint.delete_file(endpoint.remote_path("h/bases/a.kdbx"))
        endpoint.remove_directory(endpoint.remote_path("h/bases"))
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(
            [call[0] for call in transport.calls],
            [
                "operations/list",
                "operations/copyfile",
                "operations/deletefile",
                "operations/rmdir",
            ],
        )
        self.assertTrue(all(call[2] == 12 for call in transport.calls))

    def test_commands_run_when_rcd_is_not_ready_or_unreachable(self):
        endpoint = self.endpoint(FakeTransport(ready=False))
        self.assertIsNone(endpoint.probe())
        self.assertEqual(len(self.runner.calls), 1)
        transport = FakeTransport(outputs=[RcdUnreachable("gone")])
        endpoint = self.endpoint(transport)
        self.assertIsNone(endpoint.probe())
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(len(self.runner.calls), 2)

    def test_rcd_failures_are_endpoint_failures_without_a_command(self):
        cases = (
            (RcdError("x", missing=True), EndpointMissing),
            (RcdError("x"), EndpointUnavailable),
            (RcdTimeout("x"), EndpointUnavailable),
        )
        source = self.directory / "s.json"
        source.write_bytes(b"{}")
        for failure, expected in cases:
            with self.subTest(failure=failure):
                endpoint = self.endpoint(FakeTransport(outputs=[failure]))
                with self.assertRaises(expected) as raised:
                    endpoint.upload_file(source, endpoint.remote_path("a"))
                if expected is EndpointUnavailable:
                    self.assertNotIsInstance(raised.exception, EndpointMissing)
        self.assertEqual(self.runner.calls, [])
        # A missing listing is an absent copy (lsjson's status 3).
        endpoint = self.endpoint(
            FakeTransport(outputs=[RcdError("x", missing=True)])
        )
        self.assertIsNone(endpoint.probe())

    def test_custom_and_batch_commands_never_use_the_rcd(self):
        transport = FakeTransport()
        endpoint = self.endpoint(
            transport,
            download=("rclone", "copyto", "--checksum", "{remote}", "{file}"),
        )
        endpoint._listing = {"pass.kdbx": {"Size": 1}}
        endpoint.fetch(self.directory / "db.kdbx")
        work = self.directory / "work"
        work.mkdir(mode=0o700)
        endpoint.delete_files(
            endpoint.remote_path("h/bases"), ["a.kdbx"], temporary=work
        )
        self.assertEqual(transport.calls, [])
        self.assertEqual(
            [argv[1] for argv in self.runner.calls], ["copyto", "delete"]
        )

    def test_credentials_import_and_drain_stop_the_rcd_first(self):
        external = self.directory / "provider.conf"
        credentials = RcloneCredentials("cloud", external, self.directory)
        transport = FakeTransport()
        endpoint = self.endpoint(transport, credentials=credentials)
        events = []
        transport.stop = lambda: events.append("stop")
        original = credentials.refresh
        credentials.refresh = lambda **kw: events.append("refresh") or (
            original(**kw)
        )
        endpoint.begin_run()
        self.assertEqual(events, ["refresh"])
        external.write_bytes(b"[GoogleDrive]\n")
        endpoint.begin_run()
        self.assertEqual(events, ["refresh", "stop", "refresh"])
        self.assertTrue(credentials.copy_exists())
        endpoint.begin_run()
        self.assertEqual(events[-1], "refresh")
        self.assertEqual(events.count("stop"), 1)
        endpoint.end_run()
        self.assertEqual(events.count("stop"), 1)
        external.unlink()
        endpoint.end_run()
        self.assertEqual(events.count("stop"), 2)
        self.assertFalse(credentials.copy_exists())


class FakeProcess:
    def __init__(self, *, alive=True):
        self.alive = alive
        self.terminated = False

    def poll(self):
        return None if self.alive else 1

    def terminate(self):
        self.terminated = True
        self.alive = False

    def kill(self):
        self.alive = False

    def wait(self, timeout=None):
        return 0


class RcloneDaemonLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.directory = private_tempdir(self)
        self.clock = Clock()
        self.started = []

    def owner(self, process, **kwargs):
        def popen(argv, **options):
            self.started.append((argv, options))
            return process

        self.http = FakeHTTP(lambda method, params: (200, params))
        return RcloneDaemon(
            "cloud",
            self.directory,
            owner=True,
            popen=popen,
            connection=self.http.factory,
            clock=self.clock,
            sleep=self.clock.sleep,
            start_timeout=1,
            **kwargs,
        )

    def bind(self):
        path = socket_path(self.directory, "cloud")
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(path))
        self.addCleanup(listener.close)

    def test_old_rclone_is_detected_once_and_commands_are_used(self):
        rcd = self.owner(FakeProcess(alive=False))
        with patch("kp_sync.rclone_rcd.detail") as logged:
            self.assertFalse(rcd.ready())
            self.assertFalse(rcd.ready())
        self.assertTrue(rcd.unsupported)
        self.assertEqual(len(self.started), 1)
        self.assertIn("using rclone commands", logged.call_args[0][0])
        argv, options = self.started[0]
        socket_file = socket_path(self.directory, "cloud")
        self.assertEqual(
            argv,
            [
                "rclone",
                "rcd",
                "--rc-addr",
                f"unix://{socket_file}",
                "--rc-no-auth",
            ],
        )
        self.assertEqual(options["stdout"], subprocess.DEVNULL)
        self.assertEqual(options["stderr"], subprocess.DEVNULL)
        self.assertEqual(os.stat(socket_file.parent).st_mode & 0o777, 0o700)

    def test_unanswered_socket_counts_as_unsupported(self):
        process = FakeProcess()
        rcd = self.owner(process)
        self.assertFalse(rcd.ready())
        self.assertTrue(process.terminated)
        self.assertTrue(rcd.unsupported)

    def test_answering_rcd_is_ready_and_stopped_by_its_owner(self):
        process = FakeProcess()
        rcd = self.owner(process, rclone_config="/cfg/rclone.conf")
        spawn = rcd._popen

        def popen(argv, **options):
            # Like rclone, the started process creates the socket.
            self.bind()
            return spawn(argv, **options)

        rcd._popen = popen
        self.assertTrue(rcd.ready())
        self.assertTrue(rcd.ready())
        self.assertEqual(len(self.started), 1)
        self.assertEqual(
            self.started[0][0][-2:], ["--config", "/cfg/rclone.conf"]
        )
        rcd.stop()
        self.assertTrue(process.terminated)
        # A leftover answering socket is told to quit, then replaced.
        process.alive = True
        self.assertTrue(rcd.ready())
        self.assertEqual(len(self.started), 2)
        self.assertIn(("core/quit", {}), self.http.requests)

    def test_clients_never_start_an_rcd(self):
        rcd = RcloneDaemon(
            "cloud",
            self.directory,
            owner=False,
            popen=lambda *a, **k: self.fail("client started rcd"),
        )
        self.assertFalse(rcd.ready())
        rcd.stop()

    def test_refuses_a_non_socket_in_the_socket_path(self):
        path = socket_path(self.directory, "cloud")
        path.parent.mkdir(mode=0o700)
        path.write_text("not a socket")
        with self.assertRaises(RuntimeError):
            self.owner(FakeProcess()).ready()
        self.assertEqual(self.started, [])

    def test_missing_credentials_copy_starts_nothing(self):
        credentials = RcloneCredentials(
            "cloud", self.directory / "provider.conf", self.directory
        )
        rcd = self.owner(FakeProcess(), credentials=credentials)
        self.assertFalse(rcd.ready())
        self.assertEqual(self.started, [])
        self.assertFalse(rcd.unsupported)


HOLDER = (
    "import socket, sys, time\n"
    "s = socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]); s.listen()\n"
    "time.sleep(60)\n"
)


class CredentialsLockTest(unittest.TestCase):
    def test_rcd_child_holds_the_credentials_lock_until_stopped(self):
        directory = private_tempdir(self)
        external = directory / "provider.conf"
        external.write_bytes(b"[GoogleDrive]\ntoken=1\n")
        credentials = RcloneCredentials(
            "cloud", external, directory, lock_timeout=0.2
        )
        self.assertTrue(credentials.refresh())
        http = FakeHTTP(lambda method, params: (200, params))

        spawned = []

        def popen(argv, **options):
            spawned.append(argv)
            address = argv[argv.index("--rc-addr") + 1][len("unix://") :]
            # A stand-in rcd: binds the socket and inherits the lock.
            return subprocess.Popen(
                [sys.executable, "-c", HOLDER, address], **options
            )

        rcd = RcloneDaemon(
            "cloud",
            directory,
            owner=True,
            credentials=credentials,
            popen=popen,
            connection=http.factory,
        )
        self.addCleanup(rcd.stop)
        self.assertTrue(rcd.ready())
        self.assertEqual(spawned[0][-2:], ["--config", str(credentials.path)])
        external.write_bytes(b"[GoogleDrive]\ntoken=2\n")
        self.assertFalse(credentials.refresh())
        self.assertIn(b"token=1", credentials.path.read_bytes())
        rcd.stop()
        self.assertTrue(credentials.refresh())
        self.assertIn(b"token=2", credentials.path.read_bytes())


def _rclone_version():
    executable = shutil.which("rclone")
    if executable is None:
        return None
    output = subprocess.run(
        [executable, "version"], capture_output=True, text=True, timeout=30
    ).stdout
    match = re.search(r"v(\d+)\.(\d+)", output)
    return (int(match[1]), int(match[2])) if match else None


@unittest.skipIf(_rclone_version() is None, "rclone is not installed")
class RealRcdTest(unittest.TestCase):
    """Round trip through a real rcd over a local alias remote."""

    def test_rcd_round_trip_or_fallback_on_old_rclone(self):
        directory = private_tempdir(self)
        remote = directory / "remote"
        (remote / "kp-sync").mkdir(parents=True)
        config = directory / "rclone.conf"
        config.write_text(f"[fake]\ntype = alias\nremote = {remote}\n")
        rcd = RcloneDaemon(
            "cloud", directory, owner=True, rclone_config=config
        )
        self.addCleanup(rcd.stop)
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return subprocess.run(argv, **kwargs)

        endpoint = RcloneEndpoint(
            "cloud",
            "fake:",
            rclone_config=config,
            runner=runner,
            transport=rcd,
        )
        endpoint.track_directory("kp-sync")
        if _rclone_version() < (1, 61):
            self.assertFalse(rcd.ready())
            self.assertTrue(rcd.unsupported)
            self.assertIsNone(endpoint.probe())
            self.assertTrue(calls)
            return
        self.assertTrue(rcd.ready())
        source = directory / "candidate.kdbx"
        source.write_bytes(b"database bytes")
        endpoint.upload_file(source, endpoint.database_remote)
        fingerprint = endpoint.probe()
        self.assertEqual(fingerprint.size, len(b"database bytes"))
        self.assertTrue(fingerprint.token_is_content)
        fetched = directory / "fetched.kdbx"
        endpoint.fetch(fetched)
        self.assertEqual(fetched.read_bytes(), b"database bytes")
        state = endpoint.remote_path("kp-sync/state.json")
        endpoint.upload_file(source, state)
        self.assertEqual(endpoint.stat(state)["Size"], len(b"database bytes"))
        self.assertIsNone(endpoint.stat(endpoint.remote_path("kp-sync/x")))
        self.assertFalse(
            endpoint.download_file(
                endpoint.remote_path("kp-sync/absent"), directory / "absent"
            )
        )
        endpoint.delete_file(state)
        endpoint.delete_file(state)
        self.assertFalse((remote / "kp-sync" / "state.json").exists())
        endpoint.remove_directory(endpoint.remote_path("kp-sync"))
        self.assertFalse((remote / "kp-sync").exists())
        self.assertEqual(calls, [])
        sock = socket_path(directory, "cloud")
        self.assertTrue(sock.exists())
        rcd.stop()
        self.assertFalse(sock.exists())
        # A command after the rcd stopped still works.
        self.assertEqual(endpoint.probe().size, len(b"database bytes"))


if __name__ == "__main__":
    unittest.main()


class EndpointsFromConfigTest(unittest.TestCase):
    def test_only_auto_endpoints_get_a_transport_and_clients_never_own(self):
        from dataclasses import replace

        from kp_sync.config import EndpointConfig, load_config
        from kp_sync.engine import endpoints_from_config

        directory = private_tempdir(self)
        config = replace(
            load_config(
                environ={
                    "HOME": str(directory),
                    "XDG_RUNTIME_DIR": str(directory),
                }
            ),
            endpoints=(
                EndpointConfig(
                    "auto", "rclone", remote="gd", rclone_transport="auto"
                ),
                EndpointConfig(
                    "cli", "rclone", remote="gd2", rclone_transport="cli"
                ),
            ),
        )
        with patch("kp_sync.engine.list_drives", return_value=[]):
            endpoints = {
                e.name: e for e in endpoints_from_config(config, directory)
            }
        self.assertFalse(endpoints["auto"].transport.owner)
        self.assertIsNone(endpoints["cli"].transport)
        self.assertEqual(
            endpoints["auto"].transport.socket,
            directory / "rclone-rcd" / "auto.sock",
        )
