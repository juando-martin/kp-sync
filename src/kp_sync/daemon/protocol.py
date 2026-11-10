# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Versioned JSON-lines protocol for daemon socket traffic."""

import json
import re
import socket
import struct
import threading

VERSION = 1
MAX_MESSAGE = 1024 * 1024
COMMANDS = {
    "sync",
    "status",
    "resolve",
    "attach",
    "detach",
    "lease",
    "release",
    "run",
    "up",
    "down",
    "eject",
    "access-op",
    "restore",
}
ENVIRONMENT = {
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "DBUS_SESSION_BUS_ADDRESS",
    "XAUTHORITY",
}


class ProtocolError(ValueError):
    """Raised for malformed, oversized, or incompatible messages."""

    pass


def peer_uid(connection: socket.socket) -> int:
    """Read the user ID of the peer connected to a Unix socket.

    Args:
        connection: Connected Unix-domain socket.

    Returns:
        Effective user ID reported by ``SO_PEERCRED``.

    Raises:
        OSError: If peer credentials cannot be obtained.
    """
    credentials = connection.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, 12
    )
    return struct.unpack("3i", credentials)[1]


class Connection:
    """Thread-safe sender and bounded reader for one daemon socket.

    Messages are newline-terminated JSON dictionaries containing the
    protocol version. Incoming messages must use the same version.
    """

    def __init__(self, connection: socket.socket):
        """Wrap an already-connected socket.

        Args:
            connection: Connected Unix-domain stream socket.
        """
        self.socket = connection
        self.reader = connection.makefile("rb")
        self.write_lock = threading.Lock()

    def send(self, message: dict) -> None:
        """Serialize and send one versioned protocol message.

        Args:
            message: JSON-serializable fields; version is added here.

        Raises:
            ProtocolError: If too large or containing NaN/Inf.
            OSError: If the socket write fails.
        """
        payload = (
            json.dumps(
                {"version": VERSION, **message}, allow_nan=False
            ).encode()
            + b"\n"
        )
        if len(payload) > MAX_MESSAGE:
            raise ProtocolError("protocol message is too large")
        with self.write_lock:
            self.socket.sendall(payload)

    def receive(self) -> dict:
        """Read and decode one bounded protocol message.

        Returns:
            Decoded message dictionary, including its protocol version.

        Raises:
            EOFError: If the peer closed the connection.
            ProtocolError: If malformed, oversized, or incompatible.
        """
        line = self.reader.readline(MAX_MESSAGE + 1)
        if not line:
            raise EOFError("daemon connection closed")
        if len(line) > MAX_MESSAGE or not line.endswith(b"\n"):
            raise ProtocolError(
                "protocol message is too large or unterminated"
            )
        try:
            payload = json.loads(line, parse_constant=_invalid_constant)
        except (ValueError, UnicodeError) as exc:
            raise ProtocolError("invalid protocol JSON") from exc
        if (
            not isinstance(payload, dict)
            or type(payload.get("version")) is not int
        ):
            raise ProtocolError("protocol version is required")
        if payload["version"] != VERSION:
            raise ProtocolError(
                f"incompatible protocol version; expected {VERSION}"
            )
        return payload

    def close(self):
        """Close the buffered reader and underlying socket."""
        self.reader.close()
        self.socket.close()


def _invalid_constant(value):
    raise ValueError("non-finite JSON value")


def validate_request(message: dict) -> dict:
    """Validate request fields and command-specific argument shapes.

    Args:
        message: Decoded JSON object received from a daemon client.

    Returns:
        The same mapping after successful validation.

    Raises:
        ProtocolError: If a command, field, or argument is invalid.
    """
    command = message.get("command")
    if not isinstance(command, str) or command not in COMMANDS:
        raise ProtocolError("unknown daemon request")
    allowed = {
        "version",
        "command",
        "wait",
        "ui",
        "environment",
        "dry_run",
        "mountpoint",
        "drive_id",
        "app",
        "args",
        "interactive",
        "name",
        "kind",
        "operation",
        "file_path",
        "destination",
        "db_path",
        "peer_address",
        "identified_user",
        "upload_id",
        "source_kind",
        "source_value",
        "restore_mode",
    }
    if set(message) - allowed:
        raise ProtocolError("unknown request field")
    specific = {
        "dry_run": {"sync", "restore"},
        "mountpoint": {"attach", "detach", "up", "down", "eject"},
        "drive_id": {"attach", "detach", "up", "down", "eject"},
        "app": {"run", "up"},
        "args": {"run", "up"},
        "interactive": {"run", "up"},
        "name": {"lease", "release"},
        "kind": {"lease", "release"},
        "operation": {"access-op"},
        "file_path": {"access-op"},
        "destination": {"access-op"},
        "db_path": {"access-op"},
        "peer_address": {"access-op"},
        "identified_user": {"access-op"},
        "upload_id": {"access-op"},
        "source_kind": {"restore"},
        "source_value": {"restore"},
        "restore_mode": {"restore"},
    }
    if any(
        field in message and command not in commands
        for field, commands in specific.items()
    ):
        raise ProtocolError("field is not valid for this request")
    for flag in ("wait", "dry_run", "interactive"):
        if flag in message and type(message[flag]) is not bool:
            raise ProtocolError(f"{flag} must be boolean")
    ui = message.get("ui", "none")
    if not isinstance(ui, str) or ui not in {"none", "tty", "gui", "auto"}:
        raise ProtocolError("invalid UI mode")
    environment = message.get("environment", {})
    if (
        not isinstance(environment, dict)
        or set(environment) - ENVIRONMENT
        or any(
            not isinstance(value, str) or "\0" in value or len(value) > 4096
            for value in environment.values()
        )
    ):
        raise ProtocolError("invalid display environment")
    for field in (
        "mountpoint",
        "drive_id",
        "app",
        "name",
        "kind",
        "operation",
        "file_path",
        "destination",
        "db_path",
        "peer_address",
        "identified_user",
        "upload_id",
        "source_kind",
        "source_value",
        "restore_mode",
    ):
        if field in message and (
            not isinstance(message[field], str)
            or not message[field]
            or "\0" in message[field]
            or len(message[field]) > 4096
        ):
            raise ProtocolError(f"invalid {field}")
    args = message.get("args", [])
    if (
        not isinstance(args, list)
        or len(args) > 100
        or any(
            not isinstance(arg, str) or "\0" in arg or len(arg) > 4096
            for arg in args
        )
    ):
        raise ProtocolError("invalid application arguments")
    if command == "access-op":
        if message.get("operation") not in {
            "GET",
            "PUT",
            "DELETE",
            "MOVE",
            "BEGIN",
            "END",
        }:
            raise ProtocolError("invalid access operation")
        if (
            not isinstance(message.get("db_path"), str)
            or not message["db_path"]
        ):
            raise ProtocolError("access operation requires db_path")
        for field in (
            "db_path",
            "file_path",
            "destination",
            "peer_address",
            "identified_user",
        ):
            if field in message and (
                not isinstance(message[field], str)
                or "\0" in message[field]
                or len(message[field]) > 4096
            ):
                raise ProtocolError(f"invalid access {field}")
        if message["operation"] == "PUT":
            if not isinstance(
                message.get("upload_id"), str
            ) or not re.fullmatch(
                r"[0-9a-f]{32}",
                message["upload_id"],
            ):
                raise ProtocolError("PUT requires a valid upload id")
        elif "upload_id" in message:
            raise ProtocolError("upload id is only valid for PUT")
    if command == "restore":
        if message.get("source_kind") not in {"version", "backup", "file"}:
            raise ProtocolError("restore requires a valid source kind")
        if (
            not isinstance(message.get("source_value"), str)
            or not message["source_value"]
        ):
            raise ProtocolError("restore requires a source value")
        if message.get("restore_mode", "merge") not in {"merge", "replace"}:
            raise ProtocolError("invalid restore mode")
    return message
