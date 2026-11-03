"""Model and query the immutable version DAG shared by copies."""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable


class HistoryError(ValueError):
    """Raised when a version record or history graph is invalid."""

    pass


def _timestamp_key(value: str) -> float:
    if not isinstance(value, str):
        raise HistoryError(f"invalid timestamp: {value!r}")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise HistoryError(f"invalid timestamp: {value!r}") from exc
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.timestamp()


@dataclass(frozen=True)
class Version:
    """Immutable database version and its ancestry metadata.

    The version identifier is the file's SHA-256. ``generation`` orders
    commits across parent branches, while ``content_hash`` can identify
    equivalent database content after a plain re-save.
    """

    sha: str
    parents: tuple[str, ...]
    generation: int
    host: str
    time: str
    content_hash: str | None = None
    size: int = 0

    def __post_init__(self) -> None:
        if not _is_sha256(self.sha):
            raise HistoryError(f"invalid SHA-256 version id: {self.sha!r}")
        if not isinstance(self.parents, tuple) or any(
            not _is_sha256(item) for item in self.parents
        ):
            raise HistoryError(f"invalid parent version id for {self.sha}")
        if self.sha in self.parents:
            raise HistoryError("a version cannot be its own parent")
        if len(set(self.parents)) != len(self.parents):
            raise HistoryError(f"duplicate parents for version {self.sha}")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or isinstance(self.size, bool)
            or not isinstance(self.size, int)
            or self.generation < 0
            or self.size < 0
        ):
            raise HistoryError(
                "version generation and size must not be negative"
            )
        if not isinstance(self.host, str) or not self.host:
            raise HistoryError("version host must be a non-empty string")
        if self.content_hash is not None and not _is_sha256(self.content_hash):
            raise HistoryError("content hash must be a SHA-256 hex string")
        _timestamp_key(self.time)

    @classmethod
    def create(
        cls,
        sha: str,
        parents: Iterable[str],
        host: str,
        *,
        time: str | None = None,
        content_hash: str | None = None,
        size: int = 0,
        versions: dict[str, "Version"] | None = None,
    ) -> "Version":
        """Create a version with generation derived from known parents.

        Raises:
            HistoryError: If a parent or record is invalid.
        """
        graph = versions or {}
        parent_tuple = tuple(sorted(set(parents)))
        missing = set(parent_tuple) - graph.keys()
        if missing:
            raise HistoryError(
                f"unknown parent version(s): {', '.join(sorted(missing))}"
            )
        generation = 1 + max(
            (graph[parent].generation for parent in parent_tuple), default=0
        )
        return cls(
            sha=sha,
            parents=parent_tuple,
            generation=generation,
            host=host,
            time=time or datetime.now(timezone.utc).isoformat(),
            content_hash=content_hash,
            size=size,
        )


@dataclass(frozen=True)
class CopyRecord:
    """Last synchronized version and observation data for one copy."""

    version: str
    generation: int
    synced_at: str
    host: str
    sha256: str | None = None
    remote_hashes: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _is_sha256(self.version):
            raise HistoryError("copy record version must be a SHA-256 hash")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 0
        ):
            raise HistoryError("copy record generation must not be negative")
        if not isinstance(self.host, str) or not self.host:
            raise HistoryError("copy record host must be a non-empty string")
        if self.sha256 is not None and not _is_sha256(self.sha256):
            raise HistoryError(
                "copy record file hash must be a SHA-256 hex string"
            )
        if not isinstance(self.remote_hashes, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.remote_hashes.items()
        ):
            raise HistoryError(
                "copy record remote hashes must be string pairs"
            )
        _timestamp_key(self.synced_at)


def validate_graph(versions: dict[str, Version]) -> None:
    """Validate keys, parent links, generation order, and acyclicity."""
    for sha, version in versions.items():
        if sha != version.sha:
            raise HistoryError(
                f"version map key does not match its SHA: {sha}"
            )
        for parent in version.parents:
            if parent not in versions:
                raise HistoryError(
                    f"version {sha} has unknown parent {parent}"
                )
            if versions[parent].generation >= version.generation:
                raise HistoryError(
                    f"version {sha} has a parent with a newer generation"
                )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(sha: str) -> None:
        """Visit ancestors and detect cycles in the active traversal."""
        if sha in visiting:
            raise HistoryError(f"cycle in version history at {sha}")
        if sha in visited:
            return
        visiting.add(sha)
        for parent in versions[sha].parents:
            visit(parent)
        visiting.remove(sha)
        visited.add(sha)

    for sha in versions:
        visit(sha)


def ancestors(
    version: str, versions: dict[str, Version], *, include_self: bool = True
) -> set[str]:
    """Return known ancestors, optionally including ``version``."""
    if version not in versions:
        return set()
    found: set[str] = {version} if include_self else set()
    pending = list(versions[version].parents)
    while pending:
        parent = pending.pop()
        if parent not in found:
            found.add(parent)
            pending.extend(versions[parent].parents)
    return found


def is_ancestor(
    ancestor: str, descendant: str, versions: dict[str, Version]
) -> bool:
    """Return whether ``ancestor`` is in ``descendant``'s lineage."""
    return ancestor in ancestors(descendant, versions)


def merge_base(
    left: str, right: str, versions: dict[str, Version]
) -> str | None:
    """Return the newest shared ancestor of two versions, if any."""
    common = ancestors(left, versions) & ancestors(right, versions)
    if not common:
        return None
    return max(common, key=lambda sha: _version_order(versions[sha]))


def _version_order(version: Version) -> tuple[int, float, str, str]:
    return (
        version.generation,
        _timestamp_key(version.time),
        version.host,
        version.sha,
    )


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def heads(versions: dict[str, Version], candidates: Iterable[str]) -> set[str]:
    """Return candidates that are not ancestors of another candidate."""
    known = {sha for sha in candidates if sha in versions}
    return {
        sha
        for sha in known
        if not any(
            sha != other and is_ancestor(sha, other, versions)
            for other in known
        )
    }


def current_version(
    versions: dict[str, Version], copies: dict[str, CopyRecord]
) -> str | None:
    """Choose the deterministic current head among recorded copies."""
    candidates = {
        record.version
        for record in copies.values()
        if record.version in versions
    }
    current_heads = heads(versions, candidates)
    if not current_heads:
        return None
    return max(current_heads, key=lambda sha: _version_order(versions[sha]))


def copy_record_order(record: CopyRecord) -> tuple[int, float, str]:
    """Return the ordering used to reconcile records for one copy."""
    return record.generation, _timestamp_key(record.synced_at), record.host


def prune_history(
    versions: dict[str, Version],
    copies: dict[str, CopyRecord],
    *,
    now: datetime | None = None,
    keep_days: int = 180,
) -> tuple[dict[str, Version], dict[str, CopyRecord]]:
    """Drop expired copies and contract unneeded history.

    Copy-held versions and merge bases remain pinned. Input mappings are
    unchanged.
    """
    if keep_days < 0:
        raise ValueError("keep_days must not be negative")
    moment = now or datetime.now(timezone.utc)
    cutoff = moment.timestamp() - keep_days * 86400
    retained_copies = {
        key: record
        for key, record in copies.items()
        if _timestamp_key(record.synced_at) >= cutoff
        and record.version in versions
    }
    copy_versions = {record.version for record in retained_copies.values()}
    pinned = set(copy_versions)
    for left in copy_versions:
        for right in copy_versions:
            base = merge_base(left, right, versions)
            if base is not None:
                pinned.add(base)

    # Contract each removed edge into its children.
    contracted = dict(versions)
    removed = set(contracted) - pinned
    while removed:
        for sha in removed:
            node = contracted.get(sha)
            if node is None:
                continue
            for child_sha, child in tuple(contracted.items()):
                if sha in child.parents:
                    parents = (set(child.parents) - {sha}) | set(node.parents)
                    contracted[child_sha] = Version(
                        sha=child.sha,
                        parents=tuple(sorted(parents)),
                        generation=child.generation,
                        host=child.host,
                        time=child.time,
                        content_hash=child.content_hash,
                        size=child.size,
                    )
            contracted.pop(sha, None)
        removed = set(contracted) - pinned

    # Contracted parents may include pruned nodes if a retained node had
    # Multiple paths through pruned ancestors are resolved transitively.
    for sha, version in tuple(contracted.items()):
        parents = set(version.parents)
        pending = list(parents - contracted.keys())
        parents -= set(pending)
        while pending:
            removed_parent = pending.pop()
            old = versions.get(removed_parent)
            if old is None:
                raise HistoryError(
                    f"cannot contract missing version {removed_parent}"
                )
            for parent in old.parents:
                if parent in contracted:
                    parents.add(parent)
                elif parent not in parents:
                    pending.append(parent)
        contracted[sha] = Version(
            sha=version.sha,
            parents=tuple(sorted(parents)),
            generation=version.generation,
            host=version.host,
            time=version.time,
            content_hash=version.content_hash,
            size=version.size,
        )

    validate_graph(contracted)
    return contracted, retained_copies


def sha256_file(
    path: str, *, chunk_size: int = 1024 * 1024
) -> tuple[str, int]:
    """Return a file's SHA-256 digest and size using bounded reads."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size
