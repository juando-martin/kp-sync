# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Quick check that a cloud endpoint's server can be reached.

Offline, rclone retries until the endpoint timeout, longer than the
launch's ``prestart_timeout``. A check of a few seconds before a run's
first rclone call settles the clearly offline case early: a failure
skips the endpoint for that run as unreachable, a success decides
nothing. See "Cloud reach check" in the endpoints design chapter.
"""

from __future__ import annotations

import configparser
import errno
import os
import selectors
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from kp_sync.log import detail, logged

# Remote types whose server is fixed.
FIXED_SERVERS = {
    "drive": "www.googleapis.com",
    "onedrive": "graph.microsoft.com",
    "dropbox": "api.dropboxapi.com",
    "box": "api.box.com",
}
# Remote types that wrap one other remote named by their ``remote`` key.
WRAPPERS = {"crypt", "alias", "chunker", "compress", "hasher"}
MAX_WRAPPER_DEPTH = 4
# Only these keys are read from the rclone config; never tokens.
_KEYS = ("type", "remote", "provider", "region", "endpoint", "url")
_KEYS += ("host", "port")
_ENCRYPTED = "# Encrypted rclone configuration File"
_DEFAULT_PORTS = {"https": 443, "http": 80, "socks5": 1080, "socks5h": 1080}


@dataclass(frozen=True)
class Target:
    """Where to connect, and whether rclone would reach it over HTTP(S).

    ``scheme`` is ``https``/``http`` for HTTP APIs (a proxy applies) and
    ``tcp`` otherwise (SFTP, FTP, an explicit ``HOST:PORT``).
    """

    host: str
    port: int
    scheme: str = "tcp"

    def __str__(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"


@dataclass(frozen=True)
class Verdict:
    """Result of one check: ``ok`` False means "skip as unreachable"."""

    ok: bool
    reason: str = ""


def parse_target(text: str) -> Target:
    """Parse ``HOST:PORT`` (IPv6 as ``[ADDR]:PORT``).

    Raises:
        ValueError: If the text is not a host and a port 1-65535.
    """
    if not isinstance(text, str) or not text:
        raise ValueError("expected HOST:PORT")
    if text.startswith("["):
        host, separator, rest = text[1:].partition("]")
        if not separator or not rest.startswith(":"):
            raise ValueError("expected [ADDRESS]:PORT")
        port_text = rest[1:]
    else:
        host, separator, port_text = text.rpartition(":")
        if not separator or ":" in host:
            raise ValueError("expected HOST:PORT")
    if not host or any(c.isspace() or c in "/@" for c in host):
        raise ValueError("invalid host")
    if not port_text.isdigit() or not 0 < int(port_text) < 65536:
        raise ValueError("port must be 1-65535")
    return Target(host, int(port_text))


def _url_target(text: str) -> Target | None:
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if not parts.hostname or scheme not in {"https", "http"}:
        return None
    return Target(parts.hostname, port or _DEFAULT_PORTS[scheme], scheme)


def rclone_config_path(
    explicit: str | os.PathLike[str] | None,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    """The config file rclone reads, in rclone's own lookup order."""
    if explicit is not None:
        return Path(explicit)
    env = os.environ if environ is None else environ
    if env.get("RCLONE_CONFIG"):
        return Path(env["RCLONE_CONFIG"]).expanduser()
    home = Path(env.get("HOME", "~")).expanduser()
    config_home = env.get("XDG_CONFIG_HOME") or str(home / ".config")
    for candidate in (
        Path(config_home) / "rclone" / "rclone.conf",
        home / ".rclone.conf",
    ):
        if candidate.is_file():
            return candidate
    return None


def _remote_settings(
    name: str, parser: configparser.ConfigParser | None, environ
) -> dict[str, str]:
    settings = {}
    if parser is not None and parser.has_section(name):
        for key in _KEYS:
            value = parser.get(name, key, fallback=None)
            if value is not None:
                settings[key] = value.strip()
    # rclone lets RCLONE_CONFIG_NAME_KEY override the file.
    prefix = f"RCLONE_CONFIG_{name.upper()}_"
    for key in _KEYS:
        value = environ.get(prefix + key.upper())
        if value:
            settings[key] = value
    return settings


def remote_target(
    remote: str,
    config_path: str | os.PathLike[str] | None,
    environ: Mapping[str, str] | None = None,
) -> tuple[Target | None, str]:
    """Derive the server of ``remote`` (``NAME:path``) from its config.

    Returns:
        ``(target, "")``, or ``(None, why)`` when no server can be
        derived; the check is then skipped.
    """
    env = os.environ if environ is None else environ
    parser = None
    if config_path is not None:
        try:
            text = Path(config_path).read_text(errors="replace")
        except OSError:
            text = None
        if text is not None:
            if text.startswith(_ENCRYPTED):
                return None, "the rclone config is encrypted"
            parser = configparser.ConfigParser(
                interpolation=None, strict=False
            )
            parser.optionxform = str.lower
            try:
                parser.read_string(text)
            except configparser.Error:
                return None, "the rclone config cannot be parsed"
    name = remote.split(":", 1)[0]
    for _ in range(MAX_WRAPPER_DEPTH + 1):
        if not name or "," in name:
            return None, f"remote {remote!r} is not a named remote"
        settings = _remote_settings(name, parser, env)
        kind = settings.get("type", "")
        if not kind:
            return None, f"remote {name!r} has no type in the rclone config"
        if kind in FIXED_SERVERS:
            if kind == "onedrive" and settings.get("region", "global") not in {
                "",
                "global",
            }:
                return None, f"onedrive region {settings['region']!r}"
            return Target(FIXED_SERVERS[kind], 443, "https"), ""
        if kind in {"webdav"} and settings.get("url"):
            target = _url_target(settings["url"])
            return (target, "") if target else (None, "invalid webdav url")
        if kind == "s3":
            if settings.get("endpoint"):
                target = _url_target(settings["endpoint"])
                return (target, "") if target else (None, "invalid endpoint")
            if settings.get("provider", "AWS") in {"", "AWS"}:
                region = settings.get("region") or "us-east-1"
                return Target(f"s3.{region}.amazonaws.com", 443, "https"), ""
            return (
                None,
                f"s3 provider {settings['provider']!r} has no endpoint",
            )
        if kind in {"sftp", "ftp"} and settings.get("host"):
            default = 22 if kind == "sftp" else 21
            port = settings.get("port") or str(default)
            if not port.isdigit():
                return None, f"invalid {kind} port"
            return Target(settings["host"], int(port)), ""
        if kind in WRAPPERS:
            wrapped = settings.get("remote", "")
            if ":" not in wrapped or wrapped.startswith(("/", ".")):
                return None, f"{kind} remote {name!r} wraps a local path"
            name = wrapped.split(":", 1)[0]
            continue
        return None, f"remote type {kind!r} has no known server"
    return None, "too many nested wrapper remotes"


def _no_proxy(host: str, environ) -> bool:
    value = environ.get("NO_PROXY") or environ.get("no_proxy") or ""
    for entry in (part.strip().lower() for part in value.split(",")):
        if not entry:
            continue
        if entry == "*":
            return True
        entry = entry.split(":", 1)[0] if entry.count(":") == 1 else entry
        bare = entry.lstrip(".")
        if host.lower() == bare or host.lower().endswith("." + bare):
            return True
    return False


def through_proxy(
    target: Target, environ: Mapping[str, str] | None = None
) -> Target:
    """The proxy rclone would use for ``target``, or ``target`` itself.

    rclone, like any Go program, honours ``HTTPS_PROXY``/``HTTP_PROXY``
    (upper or lower case) and ``NO_PROXY`` for HTTP requests only.
    """
    env = os.environ if environ is None else environ
    if target.scheme not in {"https", "http"}:
        return target
    names = (
        ("HTTPS_PROXY", "https_proxy")
        if target.scheme == "https"
        else ("HTTP_PROXY", "http_proxy")
    )
    proxy = next((env[name] for name in names if env.get(name)), "")
    if not proxy or _no_proxy(target.host, env):
        return target
    if "://" not in proxy:
        proxy = "http://" + proxy
    try:
        parts = urlsplit(proxy)
        port = parts.port
    except ValueError:
        return target
    scheme = parts.scheme.lower()
    if not parts.hostname or scheme not in _DEFAULT_PORTS:
        return target
    return Target(parts.hostname, port or _DEFAULT_PORTS[scheme])


def nm_connectivity(
    timeout: float,
    *,
    runner: Callable = subprocess.run,
) -> str:
    """NetworkManager's last connectivity verdict, without a new check.

    Returns ``none``, ``portal``, ``limited``, ``full`` or ``unknown``
    (also when NetworkManager or ``busctl`` is missing).
    """
    names = {"0": "unknown", "1": "none", "2": "portal"}
    names |= {"3": "limited", "4": "full"}
    try:
        result = logged(runner)(
            [
                "busctl",
                "--system",
                f"--timeout={max(timeout, 0.1):g}",
                "get-property",
                "org.freedesktop.NetworkManager",
                "/org/freedesktop/NetworkManager",
                "org.freedesktop.NetworkManager",
                "Connectivity",
            ],
            capture_output=True,
            text=True,
            timeout=max(timeout, 0.1) + 0.5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    words = (result.stdout or "").split()
    if result.returncode or len(words) != 2 or words[0] != "u":
        return "unknown"
    return names.get(words[1], "unknown")


def connect_any(
    host: str,
    port: int,
    timeout: float,
    *,
    resolver: Callable = socket.getaddrinfo,
) -> str | None:
    """Open a TCP connection to any address of ``host`` and close it.

    Every address is tried at once, so a dead IPv6 route cannot use up
    the time an IPv4 one would need. Nothing is sent.

    Returns:
        None on success, otherwise the reason it failed.
    """
    deadline = time.monotonic() + timeout
    found: dict[str, object] = {}

    def resolve() -> None:
        try:
            found["addresses"] = resolver(host, port, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError) as exc:
            found["error"] = exc

    # getaddrinfo cannot be timed out; a daemon thread may outlive us.
    thread = threading.Thread(target=resolve, daemon=True)
    thread.start()
    thread.join(max(deadline - time.monotonic(), 0))
    if thread.is_alive():
        return f"cannot resolve {host} within {timeout:g} s"
    if "error" in found:
        return f"cannot resolve {host}"
    addresses = list(
        dict.fromkeys(
            (family, kind, protocol, address)
            for family, kind, protocol, _, address in found.get("addresses")
            or ()
        )
    )
    if not addresses:
        return f"cannot resolve {host}"
    target = f"{host}:{port}"
    failures: list[int] = []
    selector = selectors.DefaultSelector()
    sockets = []
    try:
        for family, kind, protocol, address in addresses[:6]:
            try:
                sock = socket.socket(family, kind, protocol)
            except OSError as exc:
                failures.append(exc.errno or errno.EIO)
                continue
            sockets.append(sock)
            sock.setblocking(False)
            code = sock.connect_ex(address)
            if code == 0:
                return None
            if code not in (errno.EINPROGRESS, errno.EWOULDBLOCK):
                failures.append(code)
                continue
            selector.register(sock, selectors.EVENT_WRITE)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return f"connect to {target} timed out after {timeout:g} s"
            for key, _ in selector.select(remaining):
                sock = key.fileobj
                code = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if code == 0:
                    return None
                failures.append(code)
                selector.unregister(sock)
    finally:
        selector.close()
        for sock in sockets:
            sock.close()
    return f"connect to {target} failed ({os.strerror(failures[0])})"


@dataclass
class Probes:
    """The two system probes; tests replace them as a whole."""

    connectivity: Callable[[float], str] = nm_connectivity
    connect: Callable[[str, int, float], str | None] = connect_any


PROBES = Probes()


class ReachCheck:
    """One endpoint's check (``reach_check``, ``reach_timeout``)."""

    def __init__(
        self,
        setting: str,
        timeout: float,
        remote: str,
        *,
        environ: Mapping[str, str] | None = None,
        probes: Probes | None = None,
    ) -> None:
        """Configure the check.

        Args:
            setting: ``"auto"`` or ``"HOST:PORT"``.
            timeout: Budget for the whole check, in seconds.
            remote: The endpoint's rclone remote (``NAME:path``).
            environ: Environment rclone runs with (proxies, config).
            probes: Injectable system probes; default ``PROBES``.
        """
        self.explicit = None if setting == "auto" else parse_target(setting)
        self.timeout = timeout
        self.remote = remote
        self.environ = environ
        self.probes = probes

    def target(self, config_path) -> tuple[Target | None, str]:
        """Where the check connects, or ``(None, why)``."""
        if self.explicit is not None:
            return self.explicit, ""
        env = os.environ if self.environ is None else self.environ
        target, why = remote_target(
            self.remote, rclone_config_path(config_path, env), env
        )
        return (through_proxy(target, env), "") if target else (None, why)

    def run(self, config_path=None) -> Verdict:
        """Check now; ``config_path`` is the rclone config in use."""
        probes = self.probes or PROBES
        target, why = self.target(config_path)
        if target is None:
            detail(f"reach check skipped for {self.remote}: {why}")
            return Verdict(True, why)
        started = time.monotonic()
        # Only a captive portal is trusted: NetworkManager says "none"
        # also when it does not manage a working connection, and a
        # truly offline connect fails at once anyway.
        if probes.connectivity(min(1.0, self.timeout / 2)) == "portal":
            return Verdict(False, "captive portal (NetworkManager)")
        remaining = max(self.timeout - (time.monotonic() - started), 0.1)
        reason = probes.connect(target.host, target.port, remaining)
        if reason is not None:
            return Verdict(False, reason)
        return Verdict(True)
