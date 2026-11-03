"""Reconcile shared history and publish content-addressed bases."""

import hashlib
import os
import re
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Iterable

from kp_sync.drives import DriveEndpoint
from kp_sync.endpoints.base import EndpointUnavailable
from kp_sync.endpoints.file import FileEndpoint
from kp_sync.kdbx import KdbxError, inspect_kdbx
from kp_sync.log import error, info
from kp_sync.paths import atomic_write, ensure_private_directory
from kp_sync.state import HistoryState, StateError, merge_states


class BaselineChanged(RuntimeError):
    """Raised when shared history changes during synchronization."""

    pass


def _relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or ":" in value
        or "\x00" in value
        or str(path) == "."
    ):
        raise ValueError(f"unsafe baseline path: {value!r}")
    return str(path)


class DriveStore:
    """Baseline store rooted under an attached drive directory."""

    def __init__(self, endpoint: DriveEndpoint, directory: str) -> None:
        self.endpoint = endpoint
        self.root = endpoint.mountpoint / _relative_path(directory)
        self.name = endpoint.identity
        self.cloud = False

    def _path(self, relative: str) -> Path:
        self.endpoint._check_mount()
        target = self.root / _relative_path(relative)
        # Reject symlinks, including links elsewhere on the drive.
        current = self.root
        if not self.root.resolve().is_relative_to(
            self.endpoint.mountpoint.resolve()
        ):
            raise StateError(f"baseline path escapes drive: {self.root}")
        ancestor = self.root
        while ancestor != self.endpoint.mountpoint:
            if ancestor.is_symlink():
                raise StateError(
                    f"baseline path is a symbolic link: {ancestor}"
                )
            ancestor = ancestor.parent
        for part in (".", *PurePosixPath(relative).parts):
            current = current if part == "." else current / part
            if current.is_symlink():
                raise StateError(
                    f"baseline path is a symbolic link: {current}"
                )
        return target

    def fetch(
        self, relative: str, destination: Path, *, max_age: float | None = None
    ) -> bool:
        """Copy an existing store file to ``destination`` if present."""
        path = self._path(relative)
        if not path.exists():
            return False
        FileEndpoint(self.name, path).fetch(destination)
        return True

    def put(self, relative: str, source: Path) -> None:
        """Store a file after rechecking the attached drive mount."""
        path = self._path(relative)
        # Do not write to a vanished mount or replacement drive.
        self.endpoint._check_mount()
        FileEndpoint(self.name, path)._store(
            source,
            before_replace=self.endpoint._check_mount,
        )

    def bases(self, *, cached: bool = False) -> list[str]:
        """List regular content-base filenames on this drive."""
        directory = self._path("bases")
        if not directory.exists():
            return []
        return [path.name for path in directory.iterdir() if path.is_file()]

    def delete_base(self, name: str) -> None:
        """Delete one named base from this store."""
        self._path(f"bases/{name}").unlink(missing_ok=True)

    def base_sizes(self) -> dict[str, int]:
        """List regular, non-symlink bases and their sizes."""
        directory = self._path("bases")
        if not directory.exists():
            return {}
        return {
            path.name: path.stat().st_size
            for path in directory.iterdir()
            if not path.is_symlink() and path.is_file()
        }

    def remove(self) -> None:
        """Remove recognized bases and state from this store."""
        # Remove known files only; never recursively remove a mount.
        for name in self.bases():
            if re.fullmatch(r"[0-9a-f]{64}\.kdbx", name):
                self.delete_base(name)
        self._path("state.json").unlink(missing_ok=True)
        bases = self._path("bases")
        if bases.exists() and not any(bases.iterdir()):
            bases.rmdir()
        self.endpoint._check_mount()
        if self.root.exists() and not any(self.root.iterdir()):
            self.root.rmdir()


class CloudStore:
    """Baseline store backed by one configured rclone endpoint."""

    def __init__(self, endpoint, directory: str) -> None:
        self.endpoint = endpoint
        self.directory = _relative_path(directory)
        self.name = endpoint.identity
        self.cloud = True
        self._state_cache: bytes | None = None
        # Cache state privately so future runs skip downloads after
        # checksum matches.
        self.cache_path: Path | None = None
        track = getattr(endpoint, "track_directory", None)
        if track is not None:
            track(self.directory)

    def _remote(self, relative: str) -> str:
        return self.endpoint.remote_path(
            f"{self.directory}/{_relative_path(relative)}"
        )

    def _cached_state(self) -> bytes | None:
        if self._state_cache is not None or self.cache_path is None:
            return self._state_cache
        try:
            descriptor = os.open(self.cache_path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return None
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                return None
            return source.read()

    def _remember_state(self, payload: bytes | None) -> None:
        self._state_cache = payload
        if self.cache_path is None:
            return
        if payload is None:
            self.cache_path.unlink(missing_ok=True)
        else:
            ensure_private_directory(self.cache_path.parent)
            atomic_write(self.cache_path, payload)

    def fetch(
        self, relative: str, destination: Path, *, max_age: float | None = None
    ) -> bool:
        """Download a file, reusing state only after checksum checks."""
        if relative != "state.json":
            # Bases are content-addressed and verified after download.
            return self.endpoint.download_file(
                self._remote(relative),
                destination,
                check_metadata=False,
            )
        found = self.endpoint.download_file(
            self._remote(relative),
            destination,
            cached=self._cached_state(),
            max_age=max_age,
        )
        self._remember_state(destination.read_bytes() if found else None)
        return found

    def fetch_read_only(self, relative: str, destination: Path) -> bool:
        """Fetch state without changing the persistent history cache."""
        if relative != "state.json":
            return self.endpoint.download_file(
                self._remote(relative),
                destination,
                check_metadata=False,
            )
        return self.endpoint.download_file(
            self._remote(relative),
            destination,
            cached=None,
            max_age=None,
        )

    def put(self, relative: str, source: Path) -> None:
        """Upload a file and refresh the private cache when needed."""
        self.endpoint.upload_file(source, self._remote(relative))
        if relative == "state.json":
            self._remember_state(Path(source).read_bytes())

    def bases(self, *, cached: bool = False) -> list[str]:
        """List remote base names, optionally using a recent listing."""
        return [
            item["Name"]
            for item in self.endpoint.list_files(
                self._remote("bases"), cached=cached
            )
            if not item.get("IsDir", False)
        ]

    def delete_base(self, name: str) -> None:
        """Delete one content-addressed base from the remote store."""
        self.endpoint.delete_file(self._remote(f"bases/{name}"))

    def base_sizes(self) -> dict[str, int]:
        """List base sizes without downloading database contents."""
        result = {}
        for item in self.endpoint.list_files(
            self._remote("bases"), cached=True
        ):
            if item.get("IsDir", False):
                continue
            size = item.get("Size")
            if type(size) is not int or size < 0:
                raise StateError("baseline listing has no valid file size")
            result[item["Name"]] = size
        return result


Store = DriveStore | CloudStore


class BaselineManager:
    """Load, reconcile, verify, cache, and publish baseline history.

    Merge runtime state with available stores. Publish to the primary
    available cloud and attached writable drives for offline use.
    """

    # Stale cloud bases are deleted in batches. Each pass costs a state
    # re-check and one remote call per base; leftovers cost storage.
    PRUNE_BATCH = 8

    def __init__(self, runtime: Path, stores: Iterable[Store] = ()) -> None:
        self.runtime = runtime
        self.prune_batch = self.PRUNE_BATCH
        self.stores = tuple(stores)
        for store in self.stores:
            if store.cloud and store.cache_path is None:
                key = hashlib.sha256(
                    f"{store.name}\0{store.directory}".encode()
                ).hexdigest()
                store.cache_path = (
                    runtime / "history-cache" / f"{key[:32]}.json"
                )
        self.snapshots: dict[str, dict | None] = {}
        self.states: dict[str, HistoryState] = {}
        self.available: list[Store] = []
        self.publication_outcomes: dict[str, str] = {}
        # Best-effort replicas the last save left with outdated history.
        self.replica_failures: dict[str, str] = {}

    def _read(
        self,
        store: Store,
        temporary: Path,
        max_age: float | None = None,
        *,
        read_only: bool = False,
    ) -> HistoryState | None:
        with tempfile.TemporaryDirectory(
            prefix="baseline-", dir=temporary
        ) as name:
            path = Path(name) / "state.json"
            fetch_read_only = getattr(store, "fetch_read_only", None)
            found = (
                fetch_read_only("state.json", path)
                if read_only and fetch_read_only is not None
                else store.fetch("state.json", path, max_age=max_age)
            )
            if not found:
                return None
            return HistoryState.load(path)

    def load(
        self, temporary: Path, *, read_only: bool = False
    ) -> HistoryState:
        """Load and merge cached and available shared state.

        ``temporary`` is the private download directory. Unavailable
        stores are skipped; snapshots support checks.
        """
        cache = HistoryState.load(self.runtime / "state.json")
        self.available = []
        self.states = {}
        self.snapshots = {}
        self.publication_outcomes = {}
        self.replica_failures = {}
        for store in self.stores:
            try:
                state = self._read(store, temporary, read_only=read_only)
            except EndpointUnavailable as exc:
                info(f"baseline {store.name} is unavailable: {exc}")
                self.publication_outcomes[store.name] = "unavailable"
                continue
            self.available.append(store)
            self.publication_outcomes[store.name] = "awaiting publication"
            self.snapshots[store.name] = (
                state.to_dict() if state is not None else None
            )
            self.states[store.name] = state or HistoryState()
        return merge_states([cache, *self.states.values()])

    def check(
        self,
        temporary: Path,
        *,
        max_age: float | None = None,
        read_only: bool = False,
        skip: frozenset[str] = frozenset(),
    ) -> None:
        """Fail if shared history changed.

        ``max_age`` permits reuse of a very recent listing. Stores named
        in ``skip`` (best-effort replicas that already failed) are not
        read.
        """
        for store in self.available:
            if store.name in skip:
                continue
            try:
                state = self._read(
                    store, temporary, max_age, read_only=read_only
                )
            except EndpointUnavailable as exc:
                raise BaselineChanged(
                    f"baseline {store.name} became unavailable"
                ) from exc
            if (
                state.to_dict() if state is not None else None
            ) != self.snapshots[store.name]:
                raise BaselineChanged(
                    f"baseline {store.name} changed during sync"
                )

    def ensure_base(self, sha: str, temporary: Path) -> Path | None:
        """Find a verified base in runtime cache or available stores.

        Downloads are checked against the content-addressed SHA-256 and
        cached privately. Returns ``None`` when no store has the base.
        """
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise StateError("invalid baseline hash")
        cache = self.runtime / "bases" / f"{sha}.kdbx"
        if cache.is_symlink():
            raise StateError(f"cached base is a symbolic link: {cache}")
        if cache.exists():
            if inspect_kdbx(cache).sha256 != sha:
                raise StateError(f"cached base has the wrong hash: {cache}")
            return cache
        for store in self.available:
            with tempfile.TemporaryDirectory(
                prefix="base-", dir=temporary
            ) as name:
                downloaded = Path(name) / f"{sha}.kdbx"
                try:
                    found = store.fetch(f"bases/{sha}.kdbx", downloaded)
                except EndpointUnavailable as exc:
                    info(f"base {sha} unavailable from {store.name}: {exc}")
                    continue
                if not found:
                    continue
                try:
                    digest = inspect_kdbx(downloaded).sha256
                except (OSError, KdbxError) as exc:
                    raise StateError(
                        f"invalid downloaded base {sha} from {store.name}"
                    ) from exc
                if digest != sha:
                    raise StateError(
                        f"base {sha} failed hash verification from "
                        f"{store.name}"
                    )
                ensure_private_directory(cache.parent)
                FileEndpoint("baseline-cache", cache).store(downloaded)
                return cache
        return None

    def save(self, state: HistoryState, temporary: Path) -> None:
        """Publish required bases before state and prune stale data.

        Shared state is checked before publication. Without a reachable
        store, the runtime cache remains authoritative. The first store
        (the cloud, else the first drive) is required; later drive
        stores are best-effort replicas whose failures are logged and
        recorded as outcomes; those left with outdated history are
        listed in ``replica_failures``. ``BaselineChanged`` always
        propagates.
        """
        cloud = [store for store in self.available if store.cloud]
        targets = cloud[:1] + [
            store for store in self.available if not store.cloud
        ]
        required = targets[0].name if targets else None
        self.replica_failures = {}
        if not targets:
            info(
                "no baseline store is available; history remains "
                "in runtime cache only"
            )
            return
        payload_dict = state.to_dict()
        targets = [
            store
            for store in targets
            if self.snapshots[store.name] != payload_dict
        ]
        for store in self.available:
            if self.snapshots[store.name] == payload_dict:
                self.publication_outcomes[store.name] = "already current"
        if not targets:
            return
        # Retained bases must be durable before publishing the graph.
        paths = {}
        for sha in state.versions:
            paths[sha] = self.ensure_base(sha, temporary)
            if paths[sha] is None:
                raise StateError(
                    f"cannot publish history: base {sha} is unavailable"
                )
        payload = temporary / "publish-state.json"
        state.save(payload)
        failed: set[str] = set()
        for store in targets:
            published = False
            try:
                # Upload content-addressed bases before the check.
                known = self.states[store.name].versions
                for sha, path in paths.items():
                    if sha not in known:
                        store.put(f"bases/{sha}.kdbx", path)
                self.check(temporary, skip=frozenset(failed))
                # The check's listing identifies bases left to prune.
                stale = self._stale_bases(store, state, cached=True)
                store.put("state.json", payload)
                published = True
                self.snapshots[store.name] = state.to_dict()
                self.states[store.name] = state
                self.publication_outcomes[store.name] = "published"
                if stale and (
                    not store.cloud or len(stale) >= self.prune_batch
                ):
                    # Re-check deletion against a fresh listing.
                    self.check(temporary, skip=frozenset(failed))
                    for name in self._stale_bases(store, state, cached=True):
                        store.delete_base(name)
            except BaselineChanged:
                self.publication_outcomes[store.name] = (
                    "published; pruning interrupted by a concurrent change"
                    if published
                    else "failed: shared history changed"
                )
                raise
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                self.publication_outcomes[store.name] = (
                    f"published; pruning failed: {reason}"
                    if published
                    else f"failed: {reason}"
                )
                if store.cloud or store.name == required:
                    raise
                # The required store already holds this state.
                failed.add(store.name)
                if not published:
                    self.replica_failures[store.name] = reason
                error(
                    f"history publication to {store.name} failed; "
                    f"continuing without it: {reason}"
                )

    @staticmethod
    def _stale_bases(
        store: Store, state: HistoryState, *, cached: bool
    ) -> list[str]:
        return [
            name
            for name in store.bases(cached=cached)
            if re.fullmatch(r"[0-9a-f]{64}\.kdbx", name)
            and name[:-5] not in state.versions
        ]


def stores_from_endpoints(
    endpoints, names: list[str], directory: str
) -> tuple[Store, ...]:
    """Build adapters for eligible configured baseline endpoints."""
    from kp_sync.endpoints.rclone import RcloneEndpoint

    _relative_path(directory)
    result = []
    for name in names:
        for endpoint in endpoints:
            if endpoint.name != name or endpoint.readonly:
                continue
            if isinstance(endpoint, RcloneEndpoint):
                result.append(CloudStore(endpoint, directory))
            elif isinstance(endpoint, DriveEndpoint):
                result.append(DriveStore(endpoint, directory))
    return tuple(result)
