# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Optional persistent ``rclone rcd`` transport for rclone endpoints.

Each rclone call authenticates and resolves its paths again, which costs
seconds on Google Drive. A long-running ``rclone rcd`` keeps both in
memory. It listens only on a Unix socket in a private (0700) directory
of the profile run directory, so it runs with ``--rc-no-auth``; rclone
older than 1.61 cannot listen on a Unix socket and is detected by
starting it, not by its version. See "Optional rcd transport" in the
endpoints design chapter.

Only the daemon starts (owns) an ``rcd``; other processes use its socket
while it answers and plain rclone commands otherwise. An owned ``rcd``
holds the endpoint's ``rclone-credentials.lock`` shared for its whole
lifetime (it inherits the locked descriptor), since it reads its config
once and writes refreshed tokens back. Imports and drains stop it first.

Only argument vectors with the exact shapes ``RcloneEndpoint`` generates
are translated; anything else, including custom commands, runs as a
command. A failure after a request was sent raises the same exceptions
as a failed command, so the engine's retry and restart rules apply.
"""

import http.client
import json
import os
import socket
import stat
import subprocess
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Callable

from kp_sync.lock import LockTimeout, acquire_lock
from kp_sync.log import detail, info, logged
from kp_sync.paths import ensure_private_directory

TRANSPORTS = ("cli", "auto")
SOCKET_DIRECTORY = "rclone-rcd"
# Seconds to wait for a new rcd to answer.
START_TIMEOUT = 10.0
# Seconds to wait for a stopping rcd before killing it.
STOP_TIMEOUT = 5.0
POLL_INTERVAL = 0.05
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_NOT_FOUND = ("object not found", "directory not found")


class RcdUnreachable(Exception):
    """No request reached the rcd; a command can be used instead."""


class RcdError(Exception):
    """An rcd request or job failed after it was sent."""

    def __init__(self, message: str, *, missing: bool = False) -> None:
        super().__init__(message)
        self.missing = missing


class RcdTimeout(RcdError):
    """An rcd job did not finish within the operation timeout."""


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


def socket_path(run_directory: str | os.PathLike[str], name: str) -> Path:
    """Return the private socket path of an endpoint's rcd."""
    return Path(run_directory) / SOCKET_DIRECTORY / f"{name}.sock"


def _missing(message: str) -> bool:
    return any(text in message for text in _NOT_FOUND)


class RcloneDaemon:
    """Start, use and stop one endpoint's ``rclone rcd``.

    ``owner`` processes (the daemon) start it lazily on first use and
    must call :meth:`stop` before importing or draining credentials and
    on exit. Other processes only connect to a running one.
    """

    def __init__(
        self,
        name: str,
        run_directory: str | os.PathLike[str],
        *,
        owner: bool,
        credentials=None,
        rclone_config: str | os.PathLike[str] | None = None,
        popen: Callable = subprocess.Popen,
        start_timeout: float = START_TIMEOUT,
        connection: Callable = _UnixConnection,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Describe an endpoint's rcd without starting it.

        Args:
            name: Endpoint name; names the socket.
            run_directory: Profile run directory.
            owner: Whether this process may start and stop the rcd.
            credentials: Optional ``RcloneCredentials`` whose private
                copy and lock the rcd uses.
            rclone_config: Config file when there is no private copy.
            popen: Injectable process factory.
            start_timeout: Seconds for a started rcd to answer.
            connection: Injectable HTTP-over-Unix-socket factory.
            clock: Monotonic clock for job deadlines.
            sleep: Sleep function used while polling jobs.
        """
        self.name = name
        self.run_directory = Path(run_directory)
        self.socket = socket_path(run_directory, name)
        self.owner = owner
        self.credentials = credentials
        self.rclone_config = (
            str(rclone_config) if rclone_config is not None else None
        )
        self._popen = popen
        self._connection = connection
        self._clock = clock
        self._sleep = sleep
        self.start_timeout = start_timeout
        self.process = None
        # Set once starting failed: this rclone cannot serve a socket.
        self.unsupported = False
        self._guard = threading.RLock()

    # Process lifetime -------------------------------------------------

    def _post(self, method: str, params: dict, timeout: float) -> dict:
        connection = self._connection(str(self.socket), timeout)
        try:
            body = json.dumps(params).encode()
            try:
                connection.connect()
            except OSError as exc:
                raise RcdUnreachable(str(exc)) from exc
            try:
                connection.request(
                    "POST",
                    f"/{method}",
                    body,
                    {"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                payload = response.read(MAX_RESPONSE_BYTES + 1)
            except (OSError, http.client.HTTPException) as exc:
                raise RcdError(
                    f"rcd {method} failed ({type(exc).__name__})"
                ) from exc
        finally:
            connection.close()
        if len(payload) > MAX_RESPONSE_BYTES:
            raise RcdError(f"rcd {method} response is too large")
        try:
            result = json.loads(payload or b"{}")
        except ValueError as exc:
            raise RcdError(f"rcd {method} returned malformed JSON") from exc
        if not isinstance(result, dict):
            raise RcdError(f"rcd {method} returned malformed JSON")
        if response.status != 200:
            message = str(result.get("error", f"HTTP {response.status}"))
            raise RcdError(
                f"rcd {method}: {message}",
                missing=response.status == 404 and _missing(message),
            )
        return result

    def _answers(self, timeout: float = 2.0) -> bool:
        try:
            return self._post("rc/noop", {"kp-sync": 1}, timeout) == {
                "kp-sync": 1
            }
        except (RcdUnreachable, RcdError):
            return False

    def _alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def ready(self) -> bool:
        """Return whether requests may be sent, starting it if owned."""
        with self._guard:
            if self.unsupported:
                return False
            if not self.owner:
                return self.socket.exists() and self._answers()
            if self._alive():
                return True
            self._reap()
            return self._start()

    def _lock_descriptor(self) -> int | None:
        if self.credentials is None:
            return None
        path = Path(self.credentials.lock_path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(
            path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        try:
            metadata = os.fstat(fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
            ):
                raise RuntimeError(
                    f"lock path is not a regular user-owned file: {path}"
                )
            acquire_lock(fd, self.credentials.lock_timeout, shared=True)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _start(self) -> bool:
        directory = ensure_private_directory(self.socket.parent)
        if self.socket.exists() or self.socket.is_symlink():
            if not stat.S_ISSOCK(self.socket.lstat().st_mode):
                raise RuntimeError(
                    f"refusing to replace a non-socket: {self.socket}"
                )
            if self._answers():
                # A leftover rcd from an earlier daemon: stop it, since
                # nothing would stop it before a credentials drain.
                try:
                    self._post("core/quit", {}, 5)
                except (RcdUnreachable, RcdError):
                    pass
                deadline = self._clock() + STOP_TIMEOUT
                while self.socket.exists() and self._clock() < deadline:
                    self._sleep(POLL_INTERVAL)
            self.socket.unlink(missing_ok=True)
        argv = [
            "rclone",
            "rcd",
            "--rc-addr",
            f"unix://{self.socket}",
            # The socket's 0700 directory restricts access to the user.
            "--rc-no-auth",
        ]
        try:
            lock = self._lock_descriptor()
        except LockTimeout:
            detail(f"{self.name}: rclone config in use; rcd not started")
            return False
        try:
            if self.credentials is not None:
                if not self.credentials.copy_exists():
                    return False
                argv += ["--config", str(self.credentials.path)]
            elif self.rclone_config is not None:
                argv += ["--config", self.rclone_config]
            try:
                self.process = logged(self._popen)(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    # The child keeps the shared credentials lock.
                    pass_fds=() if lock is None else (lock,),
                    start_new_session=True,
                )
            except OSError as exc:
                self._unsupported(f"cannot start ({type(exc).__name__})")
                return False
        finally:
            if lock is not None:
                # Close without unlocking: the child holds the lock.
                os.close(lock)
        deadline = self._clock() + self.start_timeout
        while self._clock() < deadline:
            if not self._alive():
                break
            if self.socket.exists() and self._answers():
                os.chmod(directory, 0o700)
                detail(f"{self.name}: rclone rcd started")
                return True
            self._sleep(POLL_INTERVAL)
        self._terminate()
        self._unsupported("it did not answer on its Unix socket")
        return False

    def _unsupported(self, reason: str) -> None:
        self.unsupported = True
        detail(
            f"{self.name}: rclone rcd unavailable ({reason}); "
            "using rclone commands"
        )

    def _terminate(self) -> None:
        process, self.process = self.process, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(STOP_TIMEOUT)
        if self.socket.exists() and not self._answers(0.5):
            self.socket.unlink(missing_ok=True)

    def _reap(self) -> None:
        if self.process is not None and self.process.poll() is not None:
            detail(f"{self.name}: rclone rcd exited; restarting on demand")
            self.process = None

    def stop(self) -> None:
        """Stop an owned rcd and release its credentials lock."""
        with self._guard:
            if not self.owner:
                return
            if self.process is not None:
                self._terminate()
                detail(f"{self.name}: rclone rcd stopped")

    def _abandon(self, jobid: int) -> None:
        """Make sure a timed-out job can no longer write anything.

        A command that times out is killed; a job keeps running in the
        server. Ask it to stop and wait for it; if it is still running,
        stop the whole server, so no write lands after the caller has
        moved on (for example to re-check the target).
        """
        try:
            self._post("job/stop", {"jobid": jobid}, STOP_TIMEOUT)
            deadline = self._clock() + STOP_TIMEOUT
            while self._clock() < deadline:
                status = self._post(
                    "job/status", {"jobid": jobid}, STOP_TIMEOUT
                )
                if status.get("finished"):
                    return
                self._sleep(POLL_INTERVAL)
        except (RcdUnreachable, RcdError):
            pass
        info(f"{self.name}: rclone rcd job did not stop; stopping rcd")
        with self._guard:
            if self.owner:
                self._terminate()
                return
        try:
            # Another process owns it and restarts it on demand.
            self._post("core/quit", {}, STOP_TIMEOUT)
        except (RcdUnreachable, RcdError):
            pass

    # Requests ---------------------------------------------------------

    def call(self, method: str, params: dict, timeout: float) -> dict:
        """Run one rc method as a job and return its output.

        Raises:
            RcdUnreachable: If the rcd could not be reached, so nothing
                was requested.
            RcdError: If the request or job failed after it was sent.
            RcdTimeout: If it did not finish within ``timeout``; the
                job is asked to stop.
        """
        deadline = self._clock() + timeout
        started = self._post(method, {**params, "_async": True}, timeout)
        jobid = started.get("jobid")
        if type(jobid) is not int:
            raise RcdError(f"rcd {method} returned no job id")
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                self._abandon(jobid)
                raise RcdTimeout(f"rcd {method} timed out")
            try:
                status = self._post(
                    "job/status", {"jobid": jobid}, max(remaining, 1)
                )
            except RcdUnreachable as exc:
                # The job was requested; its outcome is unknown.
                raise RcdError(f"rcd {method} lost its server") from exc
            if status.get("finished"):
                if not status.get("success"):
                    message = str(status.get("error") or "job failed")
                    raise RcdError(
                        f"rcd {method}: {message}", missing=_missing(message)
                    )
                output = status.get("output") or {}
                if not isinstance(output, dict):
                    raise RcdError(f"rcd {method} returned malformed output")
                return output
            self._sleep(min(POLL_INTERVAL, max(remaining, 0)))


def _split_remote(path: str, root: str) -> tuple[str, str] | None:
    """Split ``REMOTE:dir/file`` into its directory fs and a name."""
    name = root.split(":", 1)[0] + ":"
    if not path.startswith(name):
        return None
    relative = path[len(name) :]
    pure = PurePosixPath(relative)
    if (
        not relative
        or relative.startswith("/")
        or ".." in pure.parts
        or "\\" in relative
    ):
        return None
    parent = str(pure.parent)
    return (name if parent == "." else f"{name}{parent}"), pure.name


def _split_local(path: str) -> tuple[str, str] | None:
    pure = PurePosixPath(path)
    if not pure.is_absolute() or pure.name in {"", ".", ".."}:
        return None
    return str(pure.parent), pure.name


def translate(argv: list[str], root: str):
    """Map a generated rclone argv onto an rc request.

    Returns a callable taking an ``RcloneDaemon`` and timeout and
    returning the command's equivalent standard output, or ``None``
    when the argv is not one of the shapes kp-sync generates.
    """
    if not argv or argv[0] != "rclone" or len(argv) < 3:
        return None
    command, args = argv[1], argv[2:]
    if command == "lsjson" and len(args) == 2 and args[0] == "--hash":
        split = _split_remote(args[1], root)
        if split is None:
            return None
        return lambda rcd, timeout: _lsjson_single(rcd, split, timeout)
    if (
        command == "lsjson"
        and args[:4] == ["--hash", "-R", "--files-only", "--max-depth"]
        and len(args) >= 6
    ):
        depth, includes, target = args[4], args[5:-1], args[-1]
        if not depth.isdigit() or target != root:
            return None
        if len(includes) % 2 or any(
            flag != "--include" for flag in includes[::2]
        ):
            return None
        params = {
            "fs": root,
            "remote": "",
            "opt": {"recurse": True, "showHash": True, "filesOnly": True},
            "_filter": {
                "IncludeRule": includes[1::2],
                "MaxDepth": int(depth),
            },
        }
        return lambda rcd, timeout: json.dumps(
            _list_items(rcd.call("operations/list", params, timeout))
        )
    if command == "copyto" and len(args) == 2:
        source, destination = args
        remote_source = _split_remote(source, root)
        remote_destination = _split_remote(destination, root)
        if remote_source is not None and remote_destination is None:
            local = _split_local(destination)
            pair = (remote_source, local)
        elif remote_destination is not None and remote_source is None:
            local = _split_local(source)
            pair = (local, remote_destination)
        else:
            return None
        if local is None:
            return None
        params = {
            "srcFs": pair[0][0],
            "srcRemote": pair[0][1],
            "dstFs": pair[1][0],
            "dstRemote": pair[1][1],
        }
        return lambda rcd, timeout: _empty(
            rcd.call("operations/copyfile", params, timeout)
        )
    if command in {"deletefile", "rmdir"} and len(args) == 1:
        split = _split_remote(args[0], root)
        if split is None:
            return None
        params = {"fs": split[0], "remote": split[1]}
        return lambda rcd, timeout: _empty(
            rcd.call(f"operations/{command}", params, timeout)
        )
    return None


def _empty(output: dict) -> str:
    return ""


def _list_items(output: dict) -> list:
    items = output.get("list")
    if not isinstance(items, list):
        raise RcdError("rcd listing has no list")
    return items


def _lsjson_single(rcd: RcloneDaemon, split, timeout: float) -> str:
    """Emulate ``lsjson --hash PATH`` for a file or a directory."""
    directory, name = split
    output = rcd.call(
        "operations/stat",
        {"fs": directory, "remote": name, "opt": {"showHash": True}},
        timeout,
    )
    item = output.get("item")
    if item is None:
        # lsjson exits with "directory not found" (status 3).
        raise RcdError("rcd stat: object not found", missing=True)
    if not isinstance(item, dict):
        raise RcdError("rcd stat returned a malformed item")
    if item.get("IsDir") is True:
        fs = f"{directory}{'' if directory.endswith(':') else '/'}{name}"
        return json.dumps(
            _list_items(
                rcd.call(
                    "operations/list",
                    {"fs": fs, "remote": "", "opt": {"showHash": True}},
                    timeout,
                )
            )
        )
    return json.dumps([item])


__all__ = [
    "RcdError",
    "RcdTimeout",
    "RcdUnreachable",
    "RcloneDaemon",
    "TRANSPORTS",
    "socket_path",
    "translate",
]
