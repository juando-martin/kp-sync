# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Read passwords through KeePassXC's optional browser-protocol client.

The association/get-logins flow follows the proxy adapter in ssspwd,
without importing ssspwd or retaining its provider/configuration layers.
"""

import base64
import errno
import json
import os
import stat
import time
from pathlib import Path

from kp_sync.config import Config
from kp_sync.credentials import copy_slot
from kp_sync.lock import file_lock
from kp_sync.log import info
from kp_sync.paths import atomic_write, ensure_private_directory
from kp_sync.pending import _read_private_json

MAX_REPLY = 1024 * 1024


class ConnectorError(RuntimeError):
    """Raised for invalid protocol data without secret payloads."""


def association_directory(config: Config) -> Path:
    """Return the machine/profile-specific pairing directory."""
    root = Path(
        os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))
    )
    if not root.is_absolute():
        raise ConnectorError("XDG_STATE_HOME must be absolute")
    return (
        root
        / "kp-sync"
        / copy_slot(config.host)
        / (f"profile-{config.profile}" if config.profile else "default")
    )


def _private_directory(config: Config) -> Path:
    directory = association_directory(config)
    for path in (directory.parent.parent, directory.parent, directory):
        ensure_private_directory(path)
    return directory


def disconnect(config: Config) -> None:
    """Delete local pairing without revoking remote authorization."""
    directory = _private_directory(config)
    with file_lock(directory / "association.lock", 0):
        path = directory / "association.json"
        if os.path.lexists(path):
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                metadata = os.fstat(fd)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                    or metadata.st_mode & 0o077
                ):
                    raise ConnectorError("unsafe pairing file")
            finally:
                os.close(fd)
            path.unlink()
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)


def _connection(timeout: float):
    """Load the optional client with bounded, validated JSON framing."""
    from keepassxc_proxy_client.protocol import Connection

    class BoundedConnection(Connection):
        get_socket_path = staticmethod(Connection.get_socket_path)

        def __init__(self):
            super().__init__()
            self.deadline = time.monotonic() + timeout
            self.expected_action = "change-public-keys"
            self.response_hash = None
            self.buffer = b""
            self.socket.settimeout(min(timeout, 5))
            self._expect_nonce()

        def _expect_nonce(self):
            # KeePassXC uses sodium_increment (little-endian), unlike
            # the client's own outgoing nonce counter.
            value = (int.from_bytes(self.nonce, "little") + 1) % (1 << 192)
            self.reply_nonce = base64.b64encode(
                value.to_bytes(24, "little")
            ).decode("ascii")

        def _set_timeout(self):
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            self.socket.settimeout(remaining)

        def send_encrypted_message(self, message, trigger_unlock=False):
            self._set_timeout()
            self.expected_action = message["action"]
            self._expect_nonce()
            super().send_encrypted_message(message, trigger_unlock=False)

        def get_unencrypted_response(self):
            data = bytearray(self.buffer)
            self.buffer = b""
            while len(data) < MAX_REPLY:
                try:
                    text = data.decode("utf-8").lstrip()
                    result, end = json.JSONDecoder().raw_decode(text)
                except (ValueError, UnicodeError):
                    self._set_timeout()
                    chunk = self.socket.recv(min(4096, MAX_REPLY - len(data)))
                    if not chunk:
                        raise ConnectorError("connection closed before reply")
                    data.extend(chunk)
                    continue
                self.buffer = text[end:].encode("utf-8")
                if not isinstance(result, dict):
                    raise ConnectorError("invalid reply")
                # Ignore asynchronous lock/unlock events, not replies.
                if result.get("action") in {
                    "database-locked",
                    "database-unlocked",
                }:
                    data = bytearray(self.buffer)
                    self.buffer = b""
                    continue
                if result.get("action") != self.expected_action:
                    raise ConnectorError("unexpected reply")
                if "error" not in result:
                    if result.get("nonce") != self.reply_nonce:
                        raise ConnectorError("unexpected reply nonce")
                return result
            raise ConnectorError("reply exceeds size limit")

        def get_encrypted_response(self):
            result = super().get_encrypted_response()
            if result.get("nonce") != self.reply_nonce or result.get(
                "success"
            ) not in (True, "true"):
                raise ConnectorError("invalid encrypted reply")
            self.response_hash = result.get("hash")
            return result

    return BoundedConnection()


def retrieve(config: Config, warn) -> str | None:
    """Retrieve one entry in an interactive, unlocked credential step.

    Uses a fresh connection per operation. Only association metadata is
    persisted; no password is cached. Warnings omit secret payloads.
    """
    settings = config.credentials.get("keepassxc")
    if not settings:
        return None
    connection = None
    try:
        from keepassxc_proxy_client.protocol import (
            ResponseUnsuccesfulException,
        )
        from nacl.exceptions import CryptoError
    except ImportError:
        warn("KeePassXC lookup unavailable: install the keepassxc extra.")
        return None
    try:
        directory = _private_directory(config)
        with file_lock(directory / "association.lock", 0):
            connection = _connection(min(config.ui["dialog_timeout"], 60))
            path = Path(connection.get_socket_path())
            metadata = path.lstat()
            if not stat.S_ISSOCK(metadata.st_mode) or (
                metadata.st_uid != os.getuid()
            ):
                raise ConnectorError("unsafe browser socket")
            connection.connect(str(path))
            database = connection.get_databasehash()
            if not isinstance(database, str) or not database:
                raise ConnectorError("missing database identity")
            state_path = directory / "association.json"
            if os.path.lexists(state_path):
                saved = _read_private_json(state_path)
                if (
                    not isinstance(saved, dict)
                    or saved.get("version") != 1
                    or not isinstance(saved.get("id"), str)
                    or not saved["id"]
                    or not isinstance(saved.get("key"), str)
                    or not isinstance(saved.get("database"), str)
                ):
                    raise ConnectorError("invalid pairing state")
                if database != saved["database"]:
                    warn(
                        "KeePassXC lookup skipped: select the paired "
                        "database, or disconnect to pair another."
                    )
                    return None
                connection.load_associate(
                    saved["id"], base64.b64decode(saved["key"], validate=True)
                )
                connection.test_associate()
            else:
                if not connection.associate():
                    return None
                if connection.response_hash != database:
                    raise ConnectorError("database changed during pairing")
                identity, public_key = connection.dump_associate()
                atomic_write(
                    state_path,
                    json.dumps(
                        {
                            "version": 1,
                            "database": database,
                            "id": identity,
                            "key": base64.b64encode(public_key).decode(
                                "ascii"
                            ),
                        }
                    ),
                )
            entries = connection.get_logins(settings["url"])
            if connection.response_hash != database:
                raise ConnectorError("database changed during lookup")
            if entries is False or entries == []:
                return None
            if not isinstance(entries, list) or any(
                not isinstance(entry, dict)
                or not isinstance(entry.get("password"), str)
                for entry in entries
            ):
                raise ConnectorError("invalid credential reply")
            if len(entries) != 1:
                warn(
                    "Multiple KeePassXC entries match the configured URL; "
                    "none was selected. Make the URL unique."
                )
                return None
            return entries[0]["password"] or None
    except ResponseUnsuccesfulException as exc:
        payload = exc.args[0] if exc.args else {}
        code = payload.get("errorCode") if isinstance(payload, dict) else None
        if str(code) in {"1", "6", "11", "15", "19"}:
            info("KeePassXC lookup unavailable, denied, or has no match")
        elif str(code) == "8":
            warn(
                "KeePassXC association rejected; use keepassxc-disconnect "
                "before explicitly pairing again."
            )
        else:
            warn("KeePassXC lookup failed: protocol request rejected.")
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ECONNREFUSED}:
            info("KeePassXC browser integration is not available")
        else:
            warn("KeePassXC lookup failed: connection, timeout or state I/O.")
    except (
        ConnectorError,
        CryptoError,
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        RuntimeError,
    ):
        warn("KeePassXC lookup failed: invalid protocol or pairing state.")
    finally:
        if connection is not None:
            connection.socket.close()
    return None
