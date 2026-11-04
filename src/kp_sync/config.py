"""Parse, normalize, and validate kp-sync TOML configuration."""

import os
import re
import socket
import subprocess
import tomllib
import unicodedata
import math
import posixpath
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


class ConfigError(ValueError):
    """Raised when a configuration file or its values are invalid."""

    pass


_TOP_LEVEL = {
    "hub",
    "hub_persistent",
    "require_encrypted",
    "host",
    "db_name",
    "key_file",
    "endpoints",
    "sessions",
    "access",
    "credentials",
    "ui",
    "baseline",
    "drives",
    "backups",
    "verify",
    "merge",
    "daemon",
    "sync",
    "launch",
    "apps",
    "ssh_sessions",
    "access_owners",
}
_TABLE_KEYS = {
    "sessions": {
        "enabled",
        "mount_dir",
        "tunnel_port",
        "default_mode",
        "session_key_ttl",
    },
    "access": {"staging_dir", "db_path"},
    "credentials": {"cache_timeout", "keepassxc"},
    "ui": {
        "mode",
        "gui",
        "ask_first",
        "dialog_timeout",
        "notify",
        "progress",
    },
    "baseline": {"stores", "dir", "keep_days"},
    "drives": {"notify_stale", "directory", "aliases"},
    "backups": {"keep"},
    "verify": {"max_entry_drop", "min_entry_drop", "foreign_check"},
    "merge": {"engine", "review"},
    "daemon": {
        "debounce",
        "poll_interval",
        "idle_exit",
        "prestart_timeout",
        "access_lease_ttl",
        "hub_path_unit",
        "idle_sync",
        "idle_sync_interval",
        "rescue_wait",
    },
    "sync": {"lock_timeout", "max_restarts"},
    "launch": {"default_app", "confirm_without_cloud", "quit_on_last_drive"},
}
_ENDPOINT_KEYS = {
    "name",
    "type",
    "enabled",
    "readonly",
    "optional",
    "on_unavailable",
    "timeout",
    "foreign_writers",
    "path",
    "source",
    "identity",
    "remote",
    "rclone_config",
    "download",
    "upload",
    "probe",
    "probe_is_content_hash",
    "rclone_transport",
    "fetch",
    "store",
}
_PROFILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
_DRIVE_IDENTITY = re.compile(r"[^:/\\\x00-\x1f\x7f-\x9f]+:[A-Za-z0-9-]+\Z")


@dataclass(frozen=True)
class EndpointConfig:
    """Normalized settings for one configured synchronization endpoint.

    Command fields are immutable argument tuples; filesystem paths use
    ``Path`` objects. Runtime endpoints are built separately.
    """

    name: str
    type: str
    enabled: bool = True
    readonly: bool = False
    optional: bool = False
    on_unavailable: str = "warn"
    timeout: float = 30
    foreign_writers: bool = False
    path: Path | None = None
    source: str | None = None
    identity: str | None = None
    remote: str | None = None
    rclone_config: Path | None = None
    download: tuple[str, ...] | None = None
    upload: tuple[str, ...] | None = None
    probe: tuple[str, ...] | None = None
    probe_is_content_hash: bool = False
    rclone_transport: str = "cli"
    fetch: tuple[str, ...] | None = None
    store: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Config:
    """Frozen, normalized settings used by kp-sync components.

    The section mappings contain validated configuration options, while
    ``endpoints`` contains the configured endpoint records.
    """

    source: Path | None
    profile: str | None
    hub: Path
    hub_persistent: bool
    require_encrypted: bool
    host: str
    db_name: str
    key_file: Path
    endpoints: tuple[EndpointConfig, ...] = ()
    sessions: dict[str, Any] = field(default_factory=dict)
    access: dict[str, Any] = field(default_factory=dict)
    credentials: dict[str, Any] = field(default_factory=dict)
    ui: dict[str, Any] = field(default_factory=dict)
    baseline: dict[str, Any] = field(default_factory=dict)
    drives: dict[str, Any] = field(default_factory=dict)
    backups: dict[str, Any] = field(default_factory=dict)
    verify: dict[str, Any] = field(default_factory=dict)
    merge: dict[str, Any] = field(default_factory=dict)
    daemon: dict[str, Any] = field(default_factory=dict)
    sync: dict[str, Any] = field(default_factory=dict)
    launch: dict[str, Any] = field(default_factory=dict)
    apps: dict[str, dict[str, Any]] = field(default_factory=dict)
    ssh_sessions: dict[str, dict[str, Any]] = field(default_factory=dict)
    access_owners: dict[str, dict[str, Any]] = field(default_factory=dict)


def baseline_store_problems(
    endpoints: tuple[EndpointConfig, ...],
    names: list[str],
) -> list[str]:
    """Describe configured baseline stores that cannot be used.

    Returns one human-readable diagnostic for each missing, disabled,
    read-only, or unsupported endpoint named in ``names``.
    """
    by_name = {endpoint.name: endpoint for endpoint in endpoints}
    problems = []
    for name in names:
        endpoint = by_name.get(name)
        if endpoint is None:
            problems.append(
                f"baseline store {name!r} does not match a configured endpoint"
            )
        elif not endpoint.enabled:
            problems.append(f"baseline store {name!r} is disabled")
        elif endpoint.readonly:
            problems.append(f"baseline store {name!r} is read-only")
        elif not (
            endpoint.type == "rclone"
            or (endpoint.type == "file" and endpoint.source == "runtime")
        ):
            problems.append(
                f"baseline store {name!r} is not a cloud or "
                "runtime-drive endpoint"
            )
    return problems


def _table(value: Any, name: str, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    unknown = set(value) - allowed
    if unknown:
        raise ConfigError(
            f"unknown key(s) in [{name}]: {', '.join(sorted(unknown))}"
        )
    return value


def _validate_types(
    value: dict[str, Any], name: str, expected: dict[str, type]
) -> None:
    for key, expected_type in expected.items():
        if key not in value:
            continue
        item = value[key]
        if expected_type is int and isinstance(item, bool):
            valid = False
        elif expected_type is float:
            valid = (
                isinstance(item, (int, float))
                and not isinstance(item, bool)
                and math.isfinite(item)
            )
        else:
            valid = isinstance(item, expected_type)
        if not valid:
            raise ConfigError(f"{name}.{key} must be {expected_type.__name__}")


def _expand_path(value: Any, name: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{name} must be a non-empty path")
    return Path(value).expanduser()


def _command(
    value: Any, name: str, *, optional: bool = False
) -> tuple[str, ...] | None:
    if value is None and optional:
        return None
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(part, str) or not part for part in value)
    ):
        raise ConfigError(f"{name} must be a non-empty array of strings")
    return tuple(value)


def _parse_endpoint(value: Any, index: int) -> EndpointConfig:
    if not isinstance(value, dict):
        raise ConfigError(f"endpoints[{index}] must be a table")
    unknown = set(value) - _ENDPOINT_KEYS
    if unknown:
        raise ConfigError(
            f"unknown key(s) in endpoints[{index}]: "
            f"{', '.join(sorted(unknown))}"
        )
    _validate_types(
        value,
        f"endpoints[{index}]",
        {
            "name": str,
            "type": str,
            "enabled": bool,
            "readonly": bool,
            "optional": bool,
            "timeout": float,
            "foreign_writers": bool,
            "probe_is_content_hash": bool,
            "rclone_transport": str,
            "source": str,
            "identity": str,
            "remote": str,
        },
    )
    name = value.get("name")
    endpoint_type = value.get("type")
    if not name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
        raise ConfigError(
            f"endpoints[{index}].name must be a non-empty identifier"
        )
    if endpoint_type not in {"file", "command", "rclone"}:
        raise ConfigError(
            f"endpoints[{index}].type must be file, command, or rclone"
        )
    common_keys = {
        "name",
        "type",
        "enabled",
        "readonly",
        "optional",
        "on_unavailable",
        "timeout",
        "foreign_writers",
        "identity",
    }
    type_keys = {
        "file": {"path", "source"},
        "command": {"fetch", "store", "probe", "probe_is_content_hash"},
        "rclone": {
            "remote",
            "rclone_config",
            "download",
            "upload",
            "probe",
            "rclone_transport",
        },
    }
    invalid = set(value) - common_keys - type_keys[endpoint_type]
    if invalid:
        raise ConfigError(
            f"invalid key(s) for {endpoint_type} endpoint {name}: "
            f"{', '.join(sorted(invalid))}"
        )
    unavailable = value.get("on_unavailable", "warn")
    if unavailable not in {"skip", "warn", "fail"}:
        raise ConfigError(
            f"endpoints[{index}].on_unavailable must be skip, warn, or fail"
        )
    timeout = value.get("timeout", 30)
    if timeout <= 0:
        raise ConfigError(
            f"endpoints[{index}].timeout must be greater than zero"
        )

    path = None
    source = value.get("source")
    if endpoint_type == "file":
        if source == "runtime":
            if "path" in value:
                raise ConfigError(
                    f"endpoints[{index}] runtime file cannot set path"
                )
        elif source is not None:
            raise ConfigError(f"endpoints[{index}].source must be 'runtime'")
        else:
            path = _expand_path(value.get("path"), f"endpoints[{index}].path")
    elif endpoint_type == "command":
        fetch = _command(value.get("fetch"), f"endpoints[{index}].fetch")
        store = _command(value.get("store"), f"endpoints[{index}].store")
        if not any("{file}" in arg for arg in fetch):
            raise ConfigError(
                f"endpoints[{index}].fetch must contain a {{file}} placeholder"
            )
        if not any("{file}" in arg for arg in store):
            raise ConfigError(
                f"endpoints[{index}].store must contain a {{file}} placeholder"
            )
        if value.get("probe_is_content_hash") and "probe" not in value:
            raise ConfigError(
                f"endpoints[{index}].probe_is_content_hash requires a probe"
            )
    elif endpoint_type == "rclone":
        if not isinstance(value.get("remote"), str) or not value["remote"]:
            raise ConfigError(
                f"endpoints[{index}].remote is required for rclone"
            )
        fetch = None
        store = None
        if value.get("rclone_transport", "cli") not in {"cli", "auto"}:
            raise ConfigError(
                f"endpoints[{index}].rclone_transport must be cli or auto"
            )

    return EndpointConfig(
        name=name,
        type=endpoint_type,
        enabled=value.get("enabled", True),
        readonly=value.get("readonly", False),
        optional=value.get("optional", False),
        on_unavailable=unavailable,
        timeout=timeout,
        foreign_writers=value.get(
            "foreign_writers",
            endpoint_type == "rclone",
        ),
        path=path,
        source=source,
        identity=value.get("identity"),
        remote=value.get("remote"),
        rclone_config=(
            _expand_path(
                value["rclone_config"], f"endpoints[{index}].rclone_config"
            )
            if "rclone_config" in value
            else None
        ),
        download=(
            _command(value["download"], f"endpoints[{index}].download")
            if "download" in value
            else None
        ),
        upload=(
            _command(value["upload"], f"endpoints[{index}].upload")
            if "upload" in value
            else None
        ),
        probe=(
            _command(
                value["probe"], f"endpoints[{index}].probe", optional=True
            )
            if "probe" in value
            else None
        ),
        probe_is_content_hash=value.get("probe_is_content_hash", False),
        rclone_transport=value.get("rclone_transport", "cli"),
        fetch=(
            _command(value["fetch"], f"endpoints[{index}].fetch")
            if endpoint_type == "command"
            else None
        ),
        store=(
            _command(value["store"], f"endpoints[{index}].store")
            if endpoint_type == "command"
            else None
        ),
    )


def load_config(
    config_path: str | os.PathLike[str] | None = None,
    *,
    profile: str | None = None,
    environ: dict[str, str] | None = None,
    home: str | os.PathLike[str] | None = None,
    hostname: str | None = None,
) -> Config:
    """Load configuration from a file, profile, or defaults.

    Args:
        config_path: TOML path; mutually exclusive with ``profile``.
        profile: Named profile in ``~/.config/kp-sync``.
        environ: Environment mapping, including HOME.
        home: Optional home directory override for lookup.
        hostname: Optional override for the configured host name.

    Returns:
        A frozen ``Config`` with expanded paths and normalized sections.

    Raises:
        ConfigError: If the source or any setting is invalid.

    Configuration is read locally; endpoints are not contacted.
    """
    env = os.environ if environ is None else environ
    home_path = Path(home or env.get("HOME", "~")).expanduser()
    if config_path is not None and profile is not None:
        raise ConfigError("--config and --profile are mutually exclusive")
    if profile is not None:
        if not _PROFILE_RE.fullmatch(profile):
            raise ConfigError(f"invalid profile name: {profile!r}")
        source = home_path / ".config" / "kp-sync" / f"{profile}.toml"
        if not source.is_file():
            raise ConfigError(
                f"profile configuration does not exist: {source}"
            )
    elif config_path is not None:
        source = Path(config_path).expanduser()
        if not source.is_file():
            raise ConfigError(f"configuration file does not exist: {source}")
    else:
        default_source = home_path / ".config" / "kp-sync" / "config.toml"
        source = default_source if default_source.is_file() else None

    try:
        raw = (
            tomllib.loads(source.read_text(encoding="utf-8")) if source else {}
        )
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read configuration: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("configuration root must be a table")
    unknown = set(raw) - _TOP_LEVEL
    if unknown:
        raise ConfigError(
            f"unknown top-level key(s): {', '.join(sorted(unknown))}"
        )

    _validate_types(
        raw,
        "configuration",
        {
            "hub": str,
            "hub_persistent": bool,
            "require_encrypted": bool,
            "host": str,
            "db_name": str,
            "key_file": str,
            "endpoints": list,
            "apps": dict,
        },
    )

    sections: dict[str, dict[str, Any]] = {}
    for name, keys in _TABLE_KEYS.items():
        section = _table(raw.get(name, {}), name, keys)
        sections[name] = section

    if "keepassxc" in sections["credentials"]:
        connector = _table(
            sections["credentials"]["keepassxc"],
            "credentials.keepassxc",
            {"url"},
        )
        url = connector.get("url")
        if (
            not isinstance(url, str)
            or not url
            or any(char.isspace() or ord(char) < 32 for char in url)
        ):
            raise ConfigError("credentials.keepassxc.url must be a URL")
        try:
            parsed_url = urlsplit(url)
            valid_url = (
                bool(parsed_url.scheme and parsed_url.hostname)
                and parsed_url.username is None
                and parsed_url.password is None
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ConfigError("credentials.keepassxc.url must be a URL")

    apps = raw.get("apps", {})
    for name, app in apps.items():
        if not isinstance(name, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]*", name
        ):
            raise ConfigError(f"invalid app name: {name!r}")
        if not isinstance(app, dict):
            raise ConfigError(f"[apps.{name}] must be a table")
        unknown_app = set(app) - {
            "command",
            "process",
            "reloads",
            "single_instance",
        }
        if unknown_app:
            raise ConfigError(
                f"unknown key(s) in [apps.{name}]: "
                f"{', '.join(sorted(unknown_app))}"
            )
        if "command" in app:
            app["command"] = list(
                _command(app["command"], f"apps.{name}.command") or ()
            )
        if "process" in app and (
            not isinstance(app["process"], list)
            or any(not isinstance(item, str) for item in app["process"])
        ):
            raise ConfigError(
                f"apps.{name}.process must be an array of strings"
            )

    ssh_sessions = _parse_named_services(
        raw.get("ssh_sessions", {}), "ssh_sessions"
    )
    for name, session in ssh_sessions.items():
        unknown_session = set(session) - {
            "target",
            "mode",
            "mount_dir",
            "unconfined",
        }
        if unknown_session:
            raise ConfigError(
                f"unknown key(s) in [ssh_sessions.{name}]: "
                f"{', '.join(sorted(unknown_session))}"
            )
        if (
            "target" not in session
            or not isinstance(session["target"], str)
            or not session["target"]
        ):
            raise ConfigError(
                f"ssh_sessions.{name}.target must be a non-empty string"
            )
        if any(char in session["target"] for char in ("\x00", "\n", "\r")):
            raise ConfigError(
                f"ssh_sessions.{name}.target contains an invalid character"
            )
        if "mode" in session and (
            not isinstance(session["mode"], str)
            or session["mode"] not in {"ro", "rw"}
        ):
            raise ConfigError(f"ssh_sessions.{name}.mode must be ro or rw")
        if "mount_dir" in session and (
            not isinstance(session["mount_dir"], str)
            or not session["mount_dir"]
            or any(
                char in session["mount_dir"] for char in ("\x00", "\n", "\r")
            )
        ):
            raise ConfigError(
                f"ssh_sessions.{name}.mount_dir must be a non-empty path"
            )
        if "unconfined" in session and not isinstance(
            session["unconfined"], bool
        ):
            raise ConfigError(
                f"ssh_sessions.{name}.unconfined must be a boolean"
            )

    access_owners = _parse_named_services(
        raw.get("access_owners", {}), "access_owners"
    )
    for name, owner in access_owners.items():
        unknown_owner = set(owner) - {"command"}
        if unknown_owner:
            raise ConfigError(
                f"unknown key(s) in [access_owners.{name}]: "
                f"{', '.join(sorted(unknown_owner))}"
            )
        command = _command(
            owner.get("command"), f"access_owners.{name}.command"
        )
        if command is None:
            raise ConfigError(
                f"access_owners.{name}.command must be a non-empty argv array"
            )
        owner["command"] = list(command)

    endpoint_values = raw.get("endpoints", [])
    endpoints = tuple(
        _parse_endpoint(item, index)
        for index, item in enumerate(endpoint_values)
    )
    names = [item.name for item in endpoints]
    if len(names) != len(set(names)):
        raise ConfigError("endpoint names must be unique")
    if (
        sum(
            item.enabled and item.type == "file" and item.source == "runtime"
            for item in endpoints
        )
        > 1
    ):
        raise ConfigError(
            "only one enabled file endpoint may use source = 'runtime'"
        )

    validations: dict[str, dict[str, type]] = {
        "sessions": {
            "enabled": bool,
            "mount_dir": str,
            "tunnel_port": int,
            "default_mode": str,
            "session_key_ttl": int,
        },
        "access": {"staging_dir": str, "db_path": str},
        "credentials": {"cache_timeout": int},
        "ui": {
            "mode": str,
            "ask_first": bool,
            "dialog_timeout": int,
            "notify": bool,
            "progress": bool,
        },
        "baseline": {"stores": list, "dir": str, "keep_days": int},
        "drives": {
            "notify_stale": bool,
            "directory": str,
            "aliases": dict,
        },
        "backups": {"keep": int},
        "verify": {
            "max_entry_drop": float,
            "min_entry_drop": int,
            "foreign_check": str,
        },
        "merge": {"engine": str, "review": str},
        "daemon": {
            "debounce": float,
            "poll_interval": int,
            "idle_exit": int,
            "prestart_timeout": int,
            "access_lease_ttl": int,
            "hub_path_unit": bool,
            "idle_sync": bool,
            "idle_sync_interval": int,
            "rescue_wait": int,
        },
        "sync": {"lock_timeout": int, "max_restarts": int},
        "launch": {
            "default_app": str,
            "confirm_without_cloud": bool,
            "quit_on_last_drive": bool,
        },
    }
    defaults: dict[str, dict[str, Any]] = {
        "sessions": {
            "enabled": False,
            "mount_dir": "~/kp",
            "tunnel_port": 2222,
            "default_mode": "ro",
            "session_key_ttl": 86400,
        },
        "access": {},
        "credentials": {"cache_timeout": 900},
        "ui": {
            "mode": "auto",
            "gui": "zenity",
            "ask_first": True,
            "dialog_timeout": 600,
            "notify": True,
            "progress": True,
        },
        "baseline": {
            "stores": ["cloud", "pendrive"],
            "dir": "kp-sync",
            "keep_days": 180,
        },
        "drives": {"notify_stale": True, "directory": ".", "aliases": {}},
        "backups": {"keep": 20},
        "verify": {
            "max_entry_drop": 0.10,
            "min_entry_drop": 5,
            "foreign_check": "always",
        },
        "merge": {"engine": "kp-sync", "review": "interactive"},
        "daemon": {
            "debounce": 2,
            "poll_interval": 900,
            "idle_exit": 120,
            "prestart_timeout": 20,
            "access_lease_ttl": 3600,
            "hub_path_unit": True,
            "idle_sync": None,
            "idle_sync_interval": 3600,
            "rescue_wait": 60,
        },
        "sync": {"lock_timeout": 60, "max_restarts": 3},
        "launch": {
            "default_app": "keepassxc",
            "confirm_without_cloud": False,
            "quit_on_last_drive": True,
        },
    }
    for name, values in sections.items():
        _validate_types(values, name, validations[name])
        defaults[name].update(values)
        if defaults[name].get("idle_sync") is None:
            defaults[name]["idle_sync"] = bool(
                raw.get("hub_persistent", False)
            )
    _validate_values(sections, defaults)
    if "stores" in sections["baseline"]:
        problems = baseline_store_problems(
            endpoints, defaults["baseline"]["stores"]
        )
        if not defaults["baseline"]["stores"] or len(problems) == len(
            defaults["baseline"]["stores"]
        ):
            raise ConfigError(
                "explicit baseline.stores has no usable configured stores: "
                + ("; ".join(problems) or "the list is empty")
            )
        if problems:
            from kp_sync.log import info

            for problem in problems:
                info(f"WARNING: {problem}")

    runtime_dir = env.get("XDG_RUNTIME_DIR")
    if "hub" in raw:
        hub = _expand_path(raw["hub"], "hub")
    elif runtime_dir:
        hub = Path(runtime_dir) / (
            f"{profile}/pass.kdbx" if profile else "pass.kdbx"
        )
    else:
        raise ConfigError(
            "XDG_RUNTIME_DIR is not set and no hub path was configured"
        )

    persistent = raw.get("hub_persistent", False)
    require_encrypted = raw.get("require_encrypted", persistent)
    key_file = _expand_path(
        raw.get("key_file", "~/data/Personal/pass.keyx"), "key_file"
    )
    host = raw.get("host", hostname or socket.gethostname())
    db_name = raw.get("db_name", "pass.kdbx")
    if not host:
        raise ConfigError("host must not be empty")
    if (
        db_name in {"", ".", ".."}
        or Path(db_name).name != db_name
        or "/" in db_name
        or "\x00" in db_name
    ):
        raise ConfigError("db_name must be a single file name")

    return Config(
        source=source,
        profile=profile,
        hub=hub,
        hub_persistent=persistent,
        require_encrypted=require_encrypted,
        host=host,
        db_name=db_name,
        key_file=key_file,
        endpoints=endpoints,
        **defaults,
        apps=apps,
        ssh_sessions=ssh_sessions,
        access_owners=access_owners,
    )


def _parse_named_services(
    value: Any, section: str
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise ConfigError(f"[{section}] must contain named tables")
    services = {}
    for name, options in value.items():
        if not isinstance(name, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}", name
        ):
            raise ConfigError(f"invalid {section} name: {name!r}")
        if not isinstance(options, dict):
            raise ConfigError(f"[{section}.{name}] must be a table")
        services[name] = dict(options)
    return services


def _validate_values(
    sections: dict[str, dict[str, Any]],
    values: dict[str, dict[str, Any]],
) -> None:
    for section, key, allowed in (
        ("ui", "mode", {"auto", "tty", "gui", "none"}),
        ("verify", "foreign_check", {"always", "merge-only"}),
        ("merge", "engine", {"kp-sync", "keepassxc"}),
        (
            "merge",
            "review",
            {"interactive", "auto-hub", "auto-other", "defer"},
        ),
        ("sessions", "default_mode", {"ro", "rw"}),
    ):
        if values[section][key] not in allowed:
            raise ConfigError(
                f"{section}.{key} must be one of {', '.join(sorted(allowed))}"
            )
    for identity, alias in values["drives"]["aliases"].items():
        if not isinstance(identity, str) or not _DRIVE_IDENTITY.fullmatch(
            identity
        ):
            raise ConfigError(f"invalid drives.aliases identity: {identity!r}")
        if (
            not isinstance(alias, str)
            or not alias.strip()
            or any(unicodedata.category(char) == "Cc" for char in alias)
        ):
            raise ConfigError(
                f"drives.aliases.{identity} must be a non-empty display "
                "name without control characters"
            )
    access_db_path = values["access"].get("db_path")
    if access_db_path is not None:
        if (
            not access_db_path.startswith("/")
            or "\x00" in access_db_path
            or "\\" in access_db_path
            or posixpath.normpath(access_db_path) != access_db_path
            or len(Path(access_db_path).parts) < 2
        ):
            raise ConfigError(
                "access.db_path must be a normalized absolute file path"
            )
    staging_dir = values["access"].get("staging_dir")
    if staging_dir is not None and not staging_dir:
        raise ConfigError("access.staging_dir must not be empty")
    gui = values["ui"]["gui"]
    if isinstance(gui, str):
        if not gui:
            raise ConfigError(
                "ui.gui must be a backend name or non-empty command"
            )
    elif (
        not isinstance(gui, list)
        or not gui
        or any(not isinstance(part, str) or not part for part in gui)
    ):
        raise ConfigError(
            "ui.gui must be a backend name or a non-empty array of strings"
        )
    for section, key in (
        ("backups", "keep"),
        ("verify", "min_entry_drop"),
        ("baseline", "keep_days"),
        ("sync", "max_restarts"),
        ("credentials", "cache_timeout"),
        ("ui", "dialog_timeout"),
        ("daemon", "idle_exit"),
        ("daemon", "prestart_timeout"),
        ("sync", "lock_timeout"),
        ("daemon", "poll_interval"),
        ("daemon", "access_lease_ttl"),
        ("daemon", "idle_sync_interval"),
        ("daemon", "rescue_wait"),
        ("sessions", "session_key_ttl"),
        ("daemon", "debounce"),
    ):
        if values[section][key] < 0:
            raise ConfigError(f"{section}.{key} must not be negative")
    if values["backups"]["keep"] < 1:
        raise ConfigError("backups.keep must be at least 1")
    if not 1 <= values["sessions"]["tunnel_port"] <= 65535:
        raise ConfigError("sessions.tunnel_port must be between 1 and 65535")
    ratio = values["verify"]["max_entry_drop"]
    if not 0 <= ratio <= 1:
        raise ConfigError("verify.max_entry_drop must be between 0 and 1")
    stores = values["baseline"]["stores"]
    if any(
        not isinstance(item, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", item)
        for item in stores
    ) or len(set(stores)) != len(stores):
        raise ConfigError("baseline.stores must be an array of endpoint names")
    directory = values["baseline"]["dir"]
    if (
        not directory
        or directory in {".", ".."}
        or Path(directory).is_absolute()
        or ".." in Path(directory).parts
        or any(char in directory for char in ("\\", ":", "\x00"))
    ):
        raise ConfigError(
            "baseline.dir must be a relative directory inside its store"
        )
    directory = values["drives"]["directory"]
    if (
        not directory
        or Path(directory).is_absolute()
        or ".." in Path(directory).parts
        or any(char in directory for char in ("\\", ":", "\x00"))
    ):
        raise ConfigError(
            "drives.directory must be a relative directory inside its drive"
        )


def require_encrypted_hub(
    hub: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    """Require ``hub`` to reside on a dm-crypt/LUKS-backed filesystem.

    Use ``findmnt`` and ``lsblk`` via ``runner``.
    Raise ``ConfigError`` if encryption cannot be confirmed.
    """
    try:
        mounted = runner(
            [
                "findmnt",
                "--noheadings",
                "--output",
                "SOURCE",
                "--target",
                str(hub),
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        source = mounted.split("[", 1)[0]
        result = runner(
            ["lsblk", "--json", "--inverse", "--output", "TYPE", source],
            capture_output=True,
            text=True,
            check=True,
        )
        import json

        devices = json.loads(result.stdout)
        if any(
            device.get("type") == "crypt" for device in _walk_lsblk(devices)
        ):
            return
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise ConfigError(
            f"cannot verify encrypted filesystem for hub {hub}: {exc}"
        ) from exc
    raise ConfigError(
        f"persistent hub is not on a dm-crypt/LUKS device: {hub}"
    )


def _walk_lsblk(data: Any):
    if not isinstance(data, dict):
        return
    for device in data.get("blockdevices", []):
        if isinstance(device, dict):
            yield device
            yield from _walk_lsblk(device)


def validate_hub_encryption(config: Config) -> None:
    """Enforce encrypted storage when a persistent hub requires it."""
    if config.hub_persistent and config.require_encrypted:
        require_encrypted_hub(config.hub)
