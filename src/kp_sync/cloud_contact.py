"""When this machine last reached its cloud endpoints.

The engine records, after every run, the time each ``rclone`` endpoint
was observed (reachable, whatever it held) in ``cloud-contact.json`` in
the profile's runtime directory. Like all runtime state it is lost at
reboot; the shared history's record of the cloud copy (when any machine
last synchronized it) is then the best remaining clue. Nothing here
contacts an endpoint.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from kp_sync.paths import atomic_write

FILENAME = "cloud-contact.json"
WARNED = "cloud-warning.json"


def cloud_identities(config) -> list[str]:
    """Return the copy identities of the configured cloud endpoints."""
    return [
        endpoint.identity or f"cloud:{endpoint.remote}"
        for endpoint in config.endpoints
        if endpoint.type == "rclone" and endpoint.remote
    ]


def load(run_directory: Path) -> dict[str, str]:
    """Return ``{identity: ISO time}``; empty if absent or unusable."""
    path = Path(run_directory) / FILENAME
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return {}
    try:
        with os.fdopen(descriptor) as source:
            metadata = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
            ):
                return {}
            data = json.load(source)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        key: value
        for key, value in data.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def record(
    run_directory: Path,
    reached: Iterable[str],
    *,
    now: datetime | None = None,
) -> None:
    """Record that the ``reached`` cloud identities answered now."""
    reached = list(reached)
    if not reached:
        return
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    data = load(run_directory)
    data.update({identity: stamp for identity in reached})
    atomic_write(
        Path(run_directory) / FILENAME,
        json.dumps(data, sort_keys=True) + "\n",
    )


def reached_in(result, identities: Iterable[str]) -> list[str]:
    """Cloud identities a sync result observed (not unreachable)."""
    missing = set(result.missing)
    return [
        identity
        for identity in identities
        if identity in result.classifications and identity not in missing
    ]


def last_contact(
    run_directory: Path,
    identities: Iterable[str],
    copies: Mapping[str, object] | None = None,
) -> dict[str, dict[str, str | None]]:
    """Return when each cloud was last reached here, and last synced.

    ``copies`` maps identities to history copy records (objects with a
    ``synced_at``) or status dictionaries (with ``synced_at``).
    """
    contacts = load(run_directory)
    result = {}
    for identity in identities:
        record_ = (copies or {}).get(identity)
        synced = (
            record_.get("synced_at")
            if isinstance(record_, Mapping)
            else getattr(record_, "synced_at", None)
        )
        result[identity] = {
            "reached_at": contacts.get(identity),
            "synced_at": synced if isinstance(synced, str) else None,
        }
    return result


def seconds_since(value: str | None, *, now: datetime | None = None):
    """Seconds since an ISO time, or None when unknown."""
    if not value:
        return None
    try:
        then = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(
        0.0, ((now or datetime.now(timezone.utc)) - then).total_seconds()
    )


def describe(contact: Mapping[str, str | None]) -> str:
    """One phrase for messages: when the cloud was last reached."""
    from kp_sync.engine import _readable_age

    if contact.get("reached_at"):
        return f"last reached {_readable_age(contact['reached_at'])}"
    if contact.get("synced_at"):
        return (
            "not reached since this machine started; its copy was last "
            f"synchronized {_readable_age(contact['synced_at'])}"
        )
    return "not reached since this machine started"


def warning_due(
    run_directory: Path, interval: float, *, now: datetime | None = None
) -> bool:
    """Whether the last staleness warning is ``interval`` old."""
    previous = load_warning(run_directory)
    seconds = seconds_since(previous, now=now)
    return seconds is None or seconds >= interval


def load_warning(run_directory: Path) -> str | None:
    """When the last staleness warning was given, if known."""
    try:
        data = json.loads((Path(run_directory) / WARNED).read_text())
    except (OSError, ValueError):
        return None
    value = data.get("warned_at") if isinstance(data, dict) else None
    return value if isinstance(value, str) else None


def record_warning(
    run_directory: Path, *, now: datetime | None = None
) -> None:
    """Remember that a staleness warning was given now."""
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    atomic_write(
        Path(run_directory) / WARNED,
        json.dumps({"warned_at": stamp}) + "\n",
    )
