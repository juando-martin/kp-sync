"""Daemon socket client and connection-bound leases."""

import os
import select
import socket
import stat
import subprocess
import sys
import time

from kp_sync.credentials import COPY_SLOT_RE, CredentialProvider
from kp_sync.daemon.protocol import (
    Connection,
    ENVIRONMENT,
    ProtocolError,
    peer_uid,
)
from kp_sync.paths import ensure_run_dir
from kp_sync.ui.factory import create_ui


class ClientError(RuntimeError):
    """Base error for an incomplete daemon client request."""

    pass


class DaemonUnavailable(ClientError):
    """Raised when the daemon socket cannot be activated or reached."""

    pass


class Client:
    """Exchange protocol messages with one profile daemon.

    Activates on demand, checks peer credentials, and answers terminal
    questions on the waiting client.
    """

    def __init__(
        self, config, *, starter=None, clock=time.monotonic, sleep=time.sleep
    ):
        """Create a client bound to the configured profile.

        Args:
            config: Loaded profile configuration.
            starter: Optional daemon activation callback.
            clock: Monotonic clock used for connection deadlines.
            sleep: Injectable delay while waiting for socket readiness.

        Notes:
            Ensures the profile's private runtime directory.
        """
        self.config = config
        self.directory = ensure_run_dir(config.profile)
        self.path = self.directory / "control.sock"
        self.starter = starter or self._start
        self.clock = clock
        self.sleep = sleep

    def _start(self):
        unit = (
            f"kp-sync@{self.config.profile}.socket"
            if self.config.profile
            else "kp-sync.socket"
        )
        try:
            result = subprocess.run(
                ["systemctl", "--user", "start", unit],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            from kp_sync.log import info

            info(
                f"systemd activation unavailable: {type(exc).__name__}; "
                "starting private daemon"
            )
        else:
            if result.returncode == 0:
                return
            from kp_sync.log import info

            info(
                "systemd socket activation unavailable; "
                "starting private daemon"
            )
        argv = [sys.executable, "-m", "kp_sync"]
        if self.config.profile:
            argv += ["--profile", self.config.profile]
        elif self.config.source:
            argv += ["--config", str(self.config.source)]
        argv += ["daemon", "--foreground"]
        descriptor = os.open(
            self.directory / "log",
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "ab") as output:
            metadata = os.fstat(output.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
            ):
                raise ClientError(
                    "daemon log must be a regular user-owned file"
                )
            os.fchmod(output.fileno(), 0o600)
            subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                start_new_session=True,
                close_fds=True,
            )

    def connect(self, *, activate=True):
        """Connect to the daemon, optionally activating it when absent.

        Args:
            activate: Start the daemon when its socket is absent.

        Returns:
            An open versioned protocol connection.

        Raises:
            ClientError: If socket or peer identity is invalid.
            DaemonUnavailable: If activation is disabled or times out.
        """
        deadline = self.clock() + 10
        started = False
        while True:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                metadata = self.path.lstat()
                if (
                    not stat.S_ISSOCK(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                    or metadata.st_mode & 0o077
                ):
                    raise ClientError(
                        "daemon socket must be private and user-owned"
                    )
                connection.connect(str(self.path))
                if peer_uid(connection) != os.getuid():
                    raise ClientError("daemon peer belongs to another user")
                return Connection(connection)
            except (FileNotFoundError, ConnectionRefusedError):
                connection.close()
                if not activate:
                    raise DaemonUnavailable("daemon is not running")
                if not started:
                    self.starter()
                    started = True
                if self.clock() >= deadline:
                    raise DaemonUnavailable("daemon did not become ready")
                self.sleep(0.05)
            except BaseException:
                connection.close()
                raise

    def request(
        self, command, *, wait=True, ui="none", activate=True, **fields
    ):
        """Send a request and return its result or accepted response.

        Args:
            command: Valid daemon command name.
            wait: Wait for completion rather than return on acceptance.
            ui: Requested interaction mode.
            activate: Activate the daemon if no socket is available.
            **fields: Command-specific protocol fields.

        Returns:
            The daemon result, or an accepted-action mapping.

        Raises:
            ClientError: If the daemon rejects or disconnects.
            :exc:`kp_sync.daemon.protocol.ProtocolError`:
                Malformed reply.
        """
        for attempt in range(2):
            try:
                return self._request_once(
                    command,
                    wait=wait,
                    ui=ui,
                    activate=activate,
                    **fields,
                )
            except (EOFError, ConnectionResetError, BrokenPipeError) as exc:
                if (
                    attempt
                    or not activate
                    or command
                    not in {
                        "sync",
                        "status",
                        "resolve",
                        "attach",
                        "detach",
                    }
                ):
                    raise ClientError(
                        "daemon connection lost before the result was received"
                    ) from exc
                self.sleep(0.05)
        raise AssertionError("unreachable")

    def _request_once(self, command, *, wait, ui, activate, **fields):
        selected_ui = ui
        if ui == "auto":
            selected_ui = "tty" if wait and sys.stdin.isatty() else "auto"
        message = {
            "command": command,
            "wait": wait,
            "ui": selected_ui,
            "environment": {
                key: os.environ[key]
                for key in ENVIRONMENT
                if key in os.environ
            },
            **fields,
        }
        connection = self.connect(activate=activate)
        try:
            connection.send(message)
            while True:
                reply = connection.receive()
                if reply.get("type") == "question":
                    if not wait or selected_ui != "tty":
                        raise ProtocolError("unexpected terminal UI request")
                    answer = self._answer(reply)
                    connection.send(
                        {
                            "type": "ui-reply",
                            "id": reply["id"],
                            "answer": answer,
                        }
                    )
                elif reply.get("type") == "error":
                    raise ClientError(
                        reply.get("message", "daemon request failed")
                    )
                elif reply.get("type") in {"result", "accepted"}:
                    return reply.get("result", {"action": "accepted"})
                else:
                    raise ProtocolError("unexpected daemon reply")
        finally:
            connection.close()

    def _answer(self, question):
        backend = create_ui("tty", timeout=self.config.ui["dialog_timeout"])
        method = question.get("method")
        args = question.get("args", [])
        if method == "password":
            if (
                not isinstance(args, list)
                or len(args) not in {1, 2}
                or not all(isinstance(item, str) for item in args)
                or (len(args) == 2 and not COPY_SLOT_RE.fullmatch(args[1]))
            ):
                raise ProtocolError("invalid password question")
            password = backend.password(args[0])
            if password is None:
                return None
            provider = CredentialProvider(self.config)
            # A copy's password goes to its slot, not the main key.
            if len(args) == 2:
                provider.cache_copy_password(args[1], password)
            else:
                provider.cache_password(password)
            return {"cached": True}
        if method not in {"confirm", "choose", "file_path", "review"}:
            raise ProtocolError("invalid terminal question")
        answer = getattr(backend, method)(*args)
        return (
            str(answer)
            if method == "file_path" and answer is not None
            else answer
        )


class LeaseConnection:
    """Hold a connection-bound lease across daemon restarts.

    Closing this object closes its socket and releases the daemon lease.
    """

    def __init__(self, client, name, kind="session"):
        """Create a connection lease handle.

        Args:
            client: ``Client`` used to connect to the daemon.
            name: Stable name for this lease.
            kind: Connection lease kind: ``client`` or ``session``.

        Raises:
            ValueError: If ``kind`` is not connection-bound.
        """
        if kind not in {"client", "session"}:
            raise ValueError("connection lease must be a client or session")
        self.client = client
        self.name = name
        self.kind = kind
        self.connection = None

    def renew(self):
        """Take or reestablish the lease on a live daemon connection.

        Returns:
            The daemon's lease result.

        Raises:
            ClientError: If the lease is refused or reconnect fails.
        """
        for attempt in range(2):
            try:
                if self.connection is None:
                    self.connection = self.client.connect()
                self.connection.send(
                    {
                        "command": "lease",
                        "name": self.name,
                        "kind": self.kind,
                        "wait": True,
                        "ui": "none",
                    }
                )
                reply = self.connection.receive()
                if reply.get("type") != "result":
                    raise ClientError(
                        reply.get("message", "lease request refused")
                    )
                return reply["result"]
            except (EOFError, ConnectionResetError, BrokenPipeError) as exc:
                self.close()
                if attempt:
                    raise ClientError(
                        "daemon lease could not be reestablished"
                    ) from exc
        raise AssertionError("unreachable")

    def alive(self):
        """Return whether the socket appears connected to the daemon."""
        if self.connection is None:
            return False
        try:
            sock = self.connection.socket
            readable, _, _ = select.select([sock], [], [], 0)
            return not (readable and sock.recv(1, socket.MSG_PEEK) == b"")
        except (OSError, ValueError):
            return False

    def ensure(self):
        """Retake the lease after a disconnect or daemon restart."""
        if self.alive():
            return None
        self.close()
        return self.renew()

    def close(self):
        """Close the socket and forget the current connection."""
        if self.connection is not None:
            self.connection.close()
            self.connection = None
