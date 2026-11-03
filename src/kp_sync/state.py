"""Validate, merge, serialize, and persist version-history state."""

import json
import os
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from kp_sync.history import (
    CopyRecord,
    HistoryError,
    Version,
    copy_record_order,
    prune_history,
    validate_graph,
)
from kp_sync.paths import atomic_write


SCHEMA_VERSION = 2


class StateError(ValueError):
    """Raised when persisted synchronization history is invalid."""

    pass


@dataclass(frozen=True)
class RetirementRecord:
    """Terminal retirement metadata for a disposable session."""

    generation: int
    retired_at: str
    host: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 0
        ):
            raise StateError("retirement generation must not be negative")
        if not isinstance(self.host, str) or not self.host:
            raise StateError("retirement host must be a non-empty string")
        try:
            datetime.fromisoformat(self.retired_at.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise StateError("invalid retirement timestamp") from exc


@dataclass
class HistoryState:
    """Version DAG, copy records, retirement markers, and generation."""

    generation: int = 0
    versions: dict[str, Version] = field(default_factory=dict)
    copies: dict[str, CopyRecord] = field(default_factory=dict)
    retirements: dict[str, RetirementRecord] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.generation, bool) or not isinstance(
            self.generation, int
        ):
            raise StateError("state generation must be an integer")
        if self.generation < 0:
            raise StateError("state generation must not be negative")
        if any(
            not isinstance(identity, str)
            or not identity.startswith("session:")
            or len(identity) <= len("session:")
            or any(ord(char) < 32 for char in identity)
            or not isinstance(record, RetirementRecord)
            for identity, record in self.retirements.items()
        ):
            raise StateError("retirement identities must be session copies")
        self.copies = {
            identity: record
            for identity, record in self.copies.items()
            if identity not in self.retirements
        }
        try:
            validate_graph(self.versions)
            if any(
                record.version not in self.versions
                for record in self.copies.values()
            ):
                raise HistoryError("copy record references an unknown version")
        except HistoryError as exc:
            raise StateError(str(exc)) from exc

    @classmethod
    def from_dict(cls, payload: Any) -> "HistoryState":
        """Parse and validate a JSON-compatible state object.

        Raises:
            StateError: If the schema, records, or graph are invalid.
        """
        if not isinstance(payload, dict):
            raise StateError("state must be a JSON object")
        schema_version = payload.get("schema_version")
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version not in {1, SCHEMA_VERSION}
        ):
            raise StateError(
                f"unsupported state schema version: {schema_version!r}"
            )
        generation = payload.get("generation")
        raw_versions = payload.get("versions")
        raw_copies = payload.get("copies")
        raw_retirements = payload.get("retirements", {})
        if isinstance(generation, bool) or not isinstance(generation, int):
            raise StateError("state generation must be an integer")
        if (
            not isinstance(raw_versions, dict)
            or not isinstance(raw_copies, dict)
            or not isinstance(raw_retirements, dict)
        ):
            raise StateError(
                "state versions, copies, and retirements must be objects"
            )

        versions: dict[str, Version] = {}
        for sha, raw in raw_versions.items():
            if not isinstance(sha, str) or not isinstance(raw, dict):
                raise StateError(f"version {sha!r} must be an object")
            parents = raw.get("parents")
            if (
                not isinstance(parents, list)
                or any(not isinstance(parent, str) for parent in parents)
                or isinstance(raw.get("generation"), bool)
                or not isinstance(raw.get("generation"), int)
                or not isinstance(raw.get("host"), str)
                or not isinstance(raw.get("time"), str)
                or (
                    raw.get("content_hash") is not None
                    and not isinstance(raw.get("content_hash"), str)
                )
                or isinstance(raw.get("size", 0), bool)
                or not isinstance(raw.get("size", 0), int)
            ):
                raise StateError(f"invalid version {sha!r}: malformed fields")
            try:
                versions[sha] = Version(
                    sha=sha,
                    parents=tuple(parents),
                    generation=raw["generation"],
                    host=raw["host"],
                    time=raw["time"],
                    content_hash=raw.get("content_hash"),
                    size=raw.get("size", 0),
                )
            except (KeyError, TypeError, HistoryError) as exc:
                raise StateError(f"invalid version {sha!r}: {exc}") from exc

        copies: dict[str, CopyRecord] = {}
        for key, raw in raw_copies.items():
            if not isinstance(raw, dict):
                raise StateError(f"copy record {key!r} must be an object")
            if (
                isinstance(raw.get("generation"), bool)
                or not isinstance(raw.get("generation"), int)
                or not isinstance(raw.get("host"), str)
                or not isinstance(raw.get("synced_at"), str)
                or not isinstance(raw.get("version"), str)
                or (
                    raw.get("sha256") is not None
                    and not isinstance(raw.get("sha256"), str)
                )
                or not isinstance(raw.get("remote_hashes", {}), dict)
            ):
                raise StateError(
                    f"invalid copy record {key!r}: malformed fields"
                )
            try:
                record = CopyRecord(
                    version=raw["version"],
                    generation=raw["generation"],
                    synced_at=raw["synced_at"],
                    host=raw["host"],
                    sha256=raw.get("sha256"),
                    remote_hashes=raw.get("remote_hashes", {}),
                )
            except (KeyError, TypeError, HistoryError) as exc:
                raise StateError(
                    f"invalid copy record {key!r}: {exc}"
                ) from exc
            copies[key] = record
        retirements: dict[str, RetirementRecord] = {}
        for identity, raw in raw_retirements.items():
            if not isinstance(identity, str) or not isinstance(raw, dict):
                raise StateError(f"retirement {identity!r} must be an object")
            try:
                retirements[identity] = RetirementRecord(
                    generation=raw["generation"],
                    retired_at=raw["retired_at"],
                    host=raw["host"],
                )
            except (KeyError, TypeError, StateError) as exc:
                raise StateError(
                    f"invalid retirement {identity!r}: {exc}"
                ) from exc
        try:
            return cls(generation, versions, copies, retirements)
        except (HistoryError, StateError) as exc:
            raise StateError(f"invalid state: {exc}") from exc

    @classmethod
    def load(cls, path: str | Path) -> "HistoryState":
        """Load a private state file or return an empty state."""
        state_path = Path(path)
        if not os.path.lexists(state_path):
            return cls()
        try:
            fd = os.open(
                state_path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            with os.fdopen(fd, "r", encoding="utf-8") as state_file:
                metadata = os.fstat(state_file.fileno())
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                ):
                    raise StateError(
                        f"state path is not a regular file: {state_path}"
                    )
                if stat.S_IMODE(metadata.st_mode) & 0o077:
                    raise StateError(
                        f"state file permissions are not private: {state_path}"
                    )
                payload = json.load(state_file)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise StateError(
                f"cannot read state file {state_path}: {exc}"
            ) from exc
        return cls.from_dict(payload)

    def to_dict(self) -> dict[str, Any]:
        """Return this state as deterministic JSON data."""
        return {
            "schema_version": SCHEMA_VERSION,
            "generation": self.generation,
            "versions": {
                sha: {
                    "parents": list(version.parents),
                    "generation": version.generation,
                    "host": version.host,
                    "time": version.time,
                    "content_hash": version.content_hash,
                    "size": version.size,
                }
                for sha, version in sorted(self.versions.items())
            },
            "copies": {
                key: {
                    "version": record.version,
                    "generation": record.generation,
                    "synced_at": record.synced_at,
                    "host": record.host,
                    "sha256": record.sha256,
                    "remote_hashes": dict(record.remote_hashes),
                }
                for key, record in sorted(self.copies.items())
            },
            "retirements": {
                identity: {
                    "generation": record.generation,
                    "retired_at": record.retired_at,
                    "host": record.host,
                }
                for identity, record in sorted(self.retirements.items())
            },
        }

    def save(self, path: str | Path) -> None:
        """Persist state atomically with private file permissions."""
        payload = json.dumps(self.to_dict(), sort_keys=True, indent=2) + "\n"
        atomic_write(path, payload)

    def commit(
        self,
        sha: str,
        parents: Iterable[str],
        host: str,
        copy_keys: Iterable[str],
        *,
        size: int,
        content_hash: str | None = None,
        synced_at: str | None = None,
    ) -> Version:
        """Record a committed version and update synchronized copies.

        Existing identifiers must have the same parents. The generation
        counter advances once for the grouped copy update.
        """
        timestamp = synced_at or datetime.now(timezone.utc).isoformat()
        keys = tuple(copy_keys)
        retired = set(keys) & self.retirements.keys()
        if retired:
            raise StateError(
                "cannot record retired session identity: "
                + ", ".join(sorted(retired))
            )
        version = self.versions.get(sha)
        parent_tuple = tuple(sorted(set(parents)))
        if version is None:
            version = Version.create(
                sha,
                parent_tuple,
                host,
                time=timestamp,
                content_hash=content_hash,
                size=size,
                versions=self.versions,
            )
            self.versions[sha] = version
        elif set(version.parents) != set(parent_tuple):
            raise StateError(
                f"version {sha} already exists with different parents"
            )

        self.generation += 1
        for key in keys:
            self.copies[key] = CopyRecord(
                version=sha,
                generation=self.generation,
                synced_at=timestamp,
                host=host,
                sha256=sha,
            )
        return version

    def record_copies(
        self,
        sha: str,
        copy_keys: Iterable[str],
        host: str,
        *,
        synced_at: str | None = None,
    ) -> None:
        """Refresh copy records without creating a version."""
        if sha not in self.versions:
            raise StateError(f"cannot record copies for unknown version {sha}")
        keys = tuple(copy_keys)
        retired = set(keys) & self.retirements.keys()
        if retired:
            raise StateError(
                "cannot record retired session identity: "
                + ", ".join(sorted(retired))
            )
        timestamp = synced_at or datetime.now(timezone.utc).isoformat()
        self.generation += 1
        for key in keys:
            self.copies[key] = CopyRecord(
                version=sha,
                generation=self.generation,
                synced_at=timestamp,
                host=host,
                sha256=sha,
            )

    def retire(
        self,
        identity: str,
        host: str,
        *,
        retired_at: str | None = None,
    ) -> bool:
        """Retire a disposable session; return whether it was new."""
        if (
            not isinstance(identity, str)
            or not identity.startswith("session:")
            or len(identity) <= len("session:")
            or any(ord(char) < 32 for char in identity)
        ):
            raise StateError("only a session copy identity can be retired")
        if identity in self.retirements:
            self.copies.pop(identity, None)
            return False
        record = RetirementRecord(
            generation=self.generation + 1,
            retired_at=retired_at or datetime.now(timezone.utc).isoformat(),
            host=host,
        )
        self.generation += 1
        self.copies.pop(identity, None)
        self.retirements[identity] = record
        return True

    def prune(
        self, *, now: datetime | None = None, keep_days: int = 180
    ) -> None:
        """Prune expired copy records and contract unused history."""
        self.versions, self.copies = prune_history(
            self.versions,
            self.copies,
            now=now,
            keep_days=keep_days,
        )


def load_and_merge(paths: Iterable[str | Path]) -> HistoryState:
    """Load state files and merge their records deterministically."""
    states = [HistoryState.load(path) for path in paths]
    return merge_states(states)


def merge_states(states: Iterable[HistoryState]) -> HistoryState:
    """Merge version DAGs and choose the newest record for each copy.

    Versions and compatible metadata are merged. Conflicts or invalid
    history raise ``StateError``.
    """
    generation = 0
    versions: dict[str, Version] = {}
    copies: dict[str, CopyRecord] = {}
    retirements: dict[str, RetirementRecord] = {}
    for state in states:
        generation = max(generation, state.generation)
        for sha, version in state.versions.items():
            current = versions.get(sha)
            if current is None:
                versions[sha] = version
                continue
            # The ID is the file SHA-256. Keep the earliest metadata and
            # union parents when hosts commit identical bytes.
            if (
                current.size and version.size and current.size != version.size
            ) or (
                current.content_hash
                and version.content_hash
                and current.content_hash != version.content_hash
            ):
                raise StateError(
                    f"conflicting immutable metadata for version {sha}"
                )
            first = min(
                current,
                version,
                key=lambda item: (item.generation, item.time, item.host),
            )
            versions[sha] = Version(
                sha=sha,
                parents=tuple(
                    sorted(set(current.parents) | set(version.parents))
                ),
                generation=first.generation,
                host=first.host,
                time=first.time,
                content_hash=current.content_hash or version.content_hash,
                size=current.size or version.size,
            )
        for key, record in state.copies.items():
            current = copies.get(key)
            if current is None or copy_record_order(
                record
            ) > copy_record_order(current):
                copies[key] = record
            elif (
                copy_record_order(record) == copy_record_order(current)
                and record != current
            ):
                raise StateError(
                    "conflicting copy records with identical tie-break "
                    f"for {key}"
                )
        for identity, record in state.retirements.items():
            current = retirements.get(identity)
            if current is None or (
                record.generation,
                record.retired_at,
                record.host,
            ) > (
                current.generation,
                current.retired_at,
                current.host,
            ):
                retirements[identity] = record

    for identity in retirements:
        copies.pop(identity, None)

    try:
        validate_graph(versions)
        if any(record.version not in versions for record in copies.values()):
            raise HistoryError("copy record references an unknown version")
    except HistoryError as exc:
        raise StateError(f"merged state is invalid: {exc}") from exc
    return HistoryState(generation, versions, copies, retirements)
