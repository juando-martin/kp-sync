"""Coordinate sync, verification, merges, and safe promotion."""

import json
import hashlib
import os
import shutil
import tempfile
import fcntl
import itertools
import stat
import re
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from kp_sync.backups import backup_file
from kp_sync.baseline import (
    BaselineChanged,
    MissingBases,
    BaselineManager,
    stores_from_endpoints,
)
from kp_sync.classify import (
    Classification,
    CopyState,
    PersistentHubMissing,
    classify_all,
)
from kp_sync.config import Config, EndpointConfig, validate_hub_encryption
from kp_sync import cloud_contact
from kp_sync.content import content_hash
from kp_sync.credentials import CredentialError, CredentialProvider
from kp_sync.drives import DriveEndpoint, DriveError, list_drives
from kp_sync.endpoints import (
    CommandEndpoint,
    Endpoint,
    EndpointAbsent,
    EndpointChanged,
    EndpointFingerprint,
    EndpointUnavailable,
    FileEndpoint,
)
from kp_sync.endpoints.file import _RenameUnsupported
from kp_sync.history import (
    CopyRecord,
    Version,
    copy_record_order,
    current_version,
    is_ancestor,
    merge_base,
    sha256_file,
)
from kp_sync.kdbx import KdbxError, inspect_kdbx
from kp_sync.lock import acquire_lock, file_lock
from kp_sync.log import debug, debug_enabled, error
from kp_sync.merge import (
    MergeCredentialError,
    MergeError,
    check_foreign_descent,
    filter_foreign_stale_cache,
    merge_databases,
    merge_restore,
    merge_unknown_copy,
)
from kp_sync.paths import (
    atomic_write,
    ensure_private_directory,
    ensure_run_dir,
)
from kp_sync.pending import PendingStore
from kp_sync.state import (
    DRIVE_DECOMMISSIONED,
    DRIVE_ENROLLED,
    HistoryState,
    StateError,
)
from kp_sync.ui.base import UI, UIUnavailable
from kp_sync.ui.factory import create_ui
from kp_sync.verify import (
    Verification,
    VerificationCredentialError,
    VerificationError,
    verify_candidate,
)


@dataclass(frozen=True)
class Observation:
    """Snapshot of an endpoint and fetched content."""

    identity: str
    endpoint: Endpoint
    fingerprint: EndpointFingerprint | None
    sha256: str | None
    size: int | None
    local_path: Path | None


@dataclass(frozen=True)
class RestoreSelection:
    """Validated request to restore from a version, backup, or file."""

    source_kind: str
    source_value: str
    mode: str


@dataclass(frozen=True)
class PreparedRestore:
    """Restore source and credentials prepared for a sync attempt."""

    source: Observation | None
    parents: tuple[str, ...]
    password: str | None
    key_file: Path | None
    reason: str | None = None
    cancelled: bool = False


@dataclass(frozen=True)
class InFlightCandidate:
    """Verified provisional version kept while writes are pending."""

    version: Version
    path: Path
    targets: tuple[str, ...]


@dataclass(frozen=True)
class SyncResult:
    """Summary of a synchronization attempt and committed copy state.

    ``current`` is the committed version. ``pending`` describes deferred
    work; ``consistent`` marks completion.
    """

    current: str | None
    action: str
    classifications: dict[str, str]
    changed: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    pending: str | None = None
    restarted: int = 0
    copies: dict[str, dict[str, object]] = field(default_factory=dict)
    decisions: dict[str, object] = field(default_factory=dict)
    sessions: dict[str, dict[str, object]] = field(default_factory=dict)
    summary: dict[str, int] = field(default_factory=dict)
    # Per cloud identity: when this machine last reached it, and when
    # its copy was last synchronized (status only; see cloud_contact).
    cloud: dict[str, dict[str, object]] = field(default_factory=dict)

    @property
    def consistent(self) -> bool:
        """Whether the result represents a completed consistent run."""
        return self.pending is None and self.action in {
            "unchanged",
            "committed",
            "dry-run",
        }

    def to_dict(self) -> dict[str, object]:
        """Serialize this result for the daemon and runtime cache."""
        return asdict(self) | {"consistent": self.consistent}


class SyncError(RuntimeError):
    """Raised when synchronization cannot complete safely."""

    pass


# Pre-write checks may reuse a remote listing about one round trip old.
# This does not widen the unavoidable
# check-then-write window of remotes without compare-and-swap.
RECHECK_WINDOW = 5.0


def _drive_copy_identity(identity: str) -> str:
    """Return the validated ``drive:SERIAL:FS_UUID`` identity."""
    from kp_sync.state import valid_drive_copy_identity

    if isinstance(identity, str) and not identity.startswith("drive:"):
        identity = f"drive:{identity}"
    if not valid_drive_copy_identity(identity):
        raise SyncError(f"invalid drive identity: {identity!r}")
    return identity


RECOVERED_PREFIX = "recovered:"
RESOLVED_DISPLACED_LIMIT = 64


class SyncEngine:
    """Synchronize one hub with configured and discovered copies.

    The engine owns sync orchestration, not daemon lifecycle. It uses
    runtime state, endpoints, credentials, and UI adapters.
    """

    def __init__(
        self,
        config: Config,
        endpoints: Iterable[Endpoint] = (),
        *,
        run_directory: str | os.PathLike[str] | None = None,
        ui: UI | None = None,
        credentials: CredentialProvider | None = None,
        baseline_stores=None,
    ) -> None:
        """Create an engine for ``config`` and the supplied endpoints.

        Args:
            config: Validated profile configuration.
            endpoints: Non-hub endpoints; the hub is added first.
            run_directory: Private runtime directory, defaulting to the
                selected profile's directory under ``XDG_RUNTIME_DIR``.
            ui: Optional decision and notification adapter.
            credentials: Optional credential provider.
            baseline_stores: Optional explicit baseline store adapters.

        Raises:
            ValueError: If endpoint identities are not unique.
        """
        self.config = config
        self.run_directory = (
            Path(run_directory)
            if run_directory is not None
            else ensure_run_dir(config.profile)
        )
        self.hub_identity = f"hub:{config.host}"
        self.ui = ui or create_ui(
            config.ui["mode"],
            timeout=config.ui["dialog_timeout"],
            gui=config.ui["gui"],
        )
        self.credentials = credentials or CredentialProvider(config)
        self._verification_reference_credentials: (
            tuple[str | None, Path | None] | None
        ) = None
        self._known_credentials: list[tuple[str, Path | None]] = []
        self._path_identities: dict[Path, str] = {}
        self._copy_slots: set[str] = set()
        self._browser_attempted = False
        self._browser_credentials: tuple[str, Path] | None = None
        self._transient_passwords: set[str] = set()
        self._fetch_counter = itertools.count()
        self.hub = FileEndpoint(
            "hub",
            config.hub,
            identity=self.hub_identity,
        )
        self.endpoints = (self.hub, *tuple(endpoints))
        # Attached drives that history says are decommissioned: never
        # observed, written or used as history stores (see
        # "Decommissioned key drives"). Seeded from runtime history; a
        # run adds those it learns about from shared history.
        self.decommissioned_drives: dict[str, Endpoint] = {}
        self.baseline_stores = (
            tuple(baseline_stores)
            if baseline_stores is not None
            else stores_from_endpoints(
                self.endpoints,
                config.baseline["stores"],
                config.baseline["dir"],
            )
        )
        try:
            cached = HistoryState.load(self.run_directory / "state.json")
        except StateError:
            cached = None
        if cached is not None:
            self._exclude_decommissioned(cached.decommissioned())
        identities = [endpoint.identity for endpoint in self.endpoints]
        if len(set(identities)) != len(identities):
            raise ValueError(
                "endpoint identities, including the hub, must be unique"
            )

    def sync(
        self,
        *,
        dry_run: bool = False,
        restore: RestoreSelection | None = None,
    ) -> SyncResult:
        """Sync reachable copies, retrying concurrent changes.

        This probes endpoints and may fetch copies.
        ``dry_run`` suppresses endpoint writes, not observations.
        ``restore`` selects recovery work. The lock releases
        for questions; copies are revalidated before writes.

        Raises:
            SyncError: If copies keep changing past the retry limit or a
                safety check prevents completion.
        """
        validate_hub_encryption(self.config)
        state_path = self.run_directory / "state.json"
        lock_path = self.run_directory / "lock"
        waiting = time.monotonic()
        with file_lock(lock_path, self.config.sync["lock_timeout"]) as lock_fd:
            debug(
                f"sync lock acquired after {time.monotonic() - waiting:.3f}s"
                + (" (dry run)" if dry_run else "")
            )
            self._browser_attempted = False
            self._browser_credentials = None
            self._transient_passwords: set[str] = set()
            original_ui = self.ui
            self.ui = _UnlockedUI(
                original_ui,
                lock_fd,
                self.config.sync["lock_timeout"],
                ask_first=self.config.ui["ask_first"],
                on_pause=lambda: self._credentials_hook("end_run"),
            )
            self._credentials_hook("begin_run")
            try:
                for attempt in range(self.config.sync["max_restarts"] + 1):
                    try:
                        result = self._sync_once(
                            state_path,
                            dry_run=dry_run,
                            restarted=attempt,
                            restore=restore,
                        )
                        if not dry_run:
                            stored = HistoryState.load(state_path)
                            newest = current_version(
                                stored.versions, stored.copies
                            )
                            result = replace(
                                result,
                                current=newest,
                                copies=_copy_status(stored, newest),
                                decisions=PendingStore(
                                    self.run_directory
                                ).load()["decisions"],
                            )
                            atomic_write(
                                self.run_directory / "last-sync.json",
                                json.dumps(result.to_dict(), sort_keys=True)
                                + "\n",
                            )
                        from kp_sync.endpoints.rclone import RcloneEndpoint

                        cloud_contact.record(
                            self.run_directory,
                            cloud_contact.reached_in(
                                result,
                                [
                                    endpoint.identity
                                    for endpoint in self.endpoints
                                    if isinstance(endpoint, RcloneEndpoint)
                                ],
                            ),
                        )
                        debug(
                            f"sync finished: {result.action}, current "
                            f"{_short(result.current)}"
                            + (
                                f", pending: {result.pending}"
                                if result.pending
                                else ""
                            )
                        )
                        return result
                    except (_RestartSync, BaselineChanged) as exc:
                        limit = self.config.sync["max_restarts"]
                        debug(
                            (
                                f"shared history changed during the run: {exc}"
                                if isinstance(exc, BaselineChanged)
                                else "a copy changed during the run"
                            )
                            + (
                                f"; restart {attempt + 1} of {limit}"
                                if attempt < limit
                                else f"; restart limit of {limit} reached"
                            )
                        )
                        if attempt >= limit:
                            raise SyncError(
                                "copies kept changing during sync; "
                                "restart limit reached"
                            )
                raise AssertionError("unreachable")
            finally:
                self._credentials_hook("end_run")
                self.ui = original_ui
                self._browser_credentials = None
                self._transient_passwords.clear()
                self._known_credentials.clear()
                self._verification_reference_credentials = None

    def _credentials_hook(self, name: str) -> None:
        """Run an endpoint credential hook; no rclone child runs."""
        for endpoint in self.endpoints:
            getattr(endpoint, name, lambda: None)()

    def restore(
        self,
        source_kind: str,
        source_value: str,
        *,
        mode: str = "merge",
        dry_run: bool = False,
    ) -> SyncResult:
        """Restore an old version, backup, or file as a new candidate.

        ``mode="merge"`` preserves data; ``mode="replace"`` requests
        replacement. Verification and promotion checks still apply.

        Raises:
            ValueError: If the source or restore mode is invalid.
        """
        if source_kind not in {"version", "backup", "file"}:
            raise ValueError(
                "restore source must be a version, backup or file"
            )
        if not isinstance(source_value, str) or not source_value:
            raise ValueError("restore source value is required")
        if mode not in {"merge", "replace"}:
            raise ValueError("restore mode must be merge or replace")
        return self.sync(
            dry_run=dry_run,
            restore=RestoreSelection(source_kind, source_value, mode),
        )

    def status(self) -> SyncResult:
        """Read cached last-run status without contacting endpoints."""
        state = HistoryState.load(self.run_directory / "state.json")
        last_path = self.run_directory / "last-sync.json"
        try:
            if os.path.lexists(last_path):
                descriptor = os.open(
                    last_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                )
                with os.fdopen(descriptor) as source:
                    metadata = os.fstat(source.fileno())
                    if (
                        not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_uid != os.getuid()
                        or stat.S_IMODE(metadata.st_mode) & 0o077
                    ):
                        raise ValueError(
                            "cached status must be a private user-owned file"
                        )
                    last = json.load(source)
            else:
                last = {}
            if (
                not isinstance(last, dict)
                or not isinstance(last.get("classifications", {}), dict)
                or any(
                    not isinstance(item, str)
                    for item in last.get("missing", [])
                )
                or not isinstance(last.get("action", ""), str)
            ):
                raise ValueError("invalid cached sync status")
        except (OSError, ValueError) as exc:
            raise SyncError(f"cannot read cached sync status: {exc}") from exc
        current = current_version(state.versions, state.copies)
        decisions = PendingStore(self.run_directory).load()["decisions"]
        copies, sessions, summary = _status_details(
            self.config, self.run_directory, state, current, decisions
        )
        # Local only: a hub saved since the copy record was written has
        # changes not yet synchronized (or a plain re-save).
        hub = copies.get(self.hub_identity)
        if hub is not None and hub.get("version"):
            try:
                digest, _ = sha256_file(str(self.config.hub))
            except OSError:
                digest = None
            if digest is not None and digest != hub["version"]:
                hub["state"] = "saved since the last sync"
                summary["hub_unsynced"] = 1
        action = last.get("action", "would probe endpoints")
        if action == "committed":
            action = "unchanged"
            classifications = {
                identity: "unchanged" if status != "missing" else status
                for identity, status in last.get("classifications", {}).items()
            }
        else:
            classifications = last.get("classifications", {})
        if decisions:
            action = "pending"
        cloud = cloud_contact.last_contact(
            self.run_directory,
            cloud_contact.cloud_identities(self.config),
            state.copies,
        )
        for contact in cloud.values():
            contact["description"] = cloud_contact.describe(contact)
        return SyncResult(
            current=current,
            action=action,
            classifications=classifications,
            missing=tuple(last.get("missing", ())),
            pending=last.get("pending") if decisions else None,
            copies=copies,
            decisions=decisions,
            sessions=sessions,
            summary=summary,
            cloud=cloud,
        )

    def _exclude_decommissioned(self, identities: set[str]) -> None:
        """Drop attached decommissioned drives from this run."""
        found = {
            endpoint.identity: endpoint
            for endpoint in self.endpoints
            if isinstance(endpoint, DriveEndpoint)
            and endpoint.identity in identities
        }
        if not found:
            return
        self.decommissioned_drives.update(found)
        self.endpoints = tuple(
            endpoint
            for endpoint in self.endpoints
            if endpoint.identity not in found
        )

        def keep(store) -> bool:
            endpoint = getattr(store, "endpoint", None)
            return getattr(endpoint, "identity", None) not in found

        removed = [
            store.name for store in self.baseline_stores if not keep(store)
        ]
        self.baseline_stores = tuple(
            store for store in self.baseline_stores if keep(store)
        )
        baseline = getattr(self, "_baseline", None)
        if baseline is not None:
            baseline.stores = tuple(
                store for store in baseline.stores if keep(store)
            )
            baseline.available = [
                store for store in baseline.available if keep(store)
            ]
            for name in removed:
                baseline.states.pop(name, None)
                baseline.snapshots.pop(name, None)
                baseline.publication_outcomes.pop(name, None)

    def _notify_decommissioned(self, *, dry_run: bool) -> None:
        """Tell the user once per attachment why a drive is skipped."""
        from kp_sync.log import detail

        if not self.decommissioned_drives:
            return
        path = self.run_directory / "decommissioned-notices.json"
        try:
            notified = json.loads(path.read_text())
            if not isinstance(notified, dict):
                notified = {}
        except (OSError, ValueError):
            notified = {}
        changed = False
        for identity, endpoint in sorted(self.decommissioned_drives.items()):
            drive_id = identity.removeprefix("drive:")
            message = (
                f"Key drive {drive_id} is decommissioned in the shared "
                "history, so it was left untouched. Run kp-sync drive-setup "
                "to use it again, or remove it from udevmon."
            )
            try:
                attached = json.loads(
                    (
                        self.run_directory / "drives" / f"{drive_id}.json"
                    ).read_text()
                ).get("attached_at")
            except (OSError, ValueError, AttributeError):
                attached = None
            key = attached or "configured"
            if notified.get(identity) == key:
                detail(message)
                continue
            self.ui.notify(message)
            if not dry_run:
                notified[identity] = key
                changed = True
        if changed:
            atomic_write(path, json.dumps(notified, sort_keys=True))

    def _drive_decommission_blockers(
        self,
        state: HistoryState,
        identity: str,
        *,
        last_history_drive: bool,
    ) -> list[str]:
        """Return why ``identity`` cannot be decommissioned now."""
        from kp_sync.drives import pending_attach_path

        drive_id = identity.removeprefix("drive:")
        reasons = []
        if os.path.lexists(self.run_directory / "drives" / f"{drive_id}.json"):
            reasons.append(
                "the drive is attached on this machine; unplug it (or run "
                f"kp-sync detach --drive-id {drive_id}) first"
            )
        if os.path.lexists(pending_attach_path(self.config.profile, drive_id)):
            reasons.append("an attach of this drive is still pending")
        for name, what in (
            ("pending.json", "a pending decision"),
            ("inflight.json", "an unfinished write"),
            ("write-race.json", "an unresolved concurrent write"),
        ):
            try:
                text = (self.run_directory / name).read_text(errors="replace")
            except OSError:
                continue
            if drive_id in text:
                reasons.append(f"{what} refers to this drive ({name})")
        record = state.copies.get(identity)
        if record is not None:
            held = {
                other.version
                for key, other in state.copies.items()
                if key != identity
            }
            if not any(
                version == record.version
                or is_ancestor(record.version, version, state.versions)
                for version in held
            ):
                reasons.append(
                    f"its recorded version {record.version[:12]} is not "
                    "held or merged by any other copy; sync it once with "
                    "another copy first"
                )
            others = [
                key
                for key in state.copies
                if key.startswith("drive:") and key != identity
            ]
            if not others and not last_history_drive:
                reasons.append(
                    "it is the last key drive in the history, needed to "
                    "start without network; add another drive first or "
                    "pass --last-history-drive"
                )
        return reasons

    def drive_decommission_preview(self, identity: str) -> dict[str, object]:
        """Report what decommissioning ``identity`` would do."""
        identity = _drive_copy_identity(identity)
        with tempfile.TemporaryDirectory(
            dir=self.run_directory, prefix="decommission-preview-"
        ) as temporary_name:
            baseline = BaselineManager(
                self.run_directory, self.baseline_stores
            )
            state = baseline.load(Path(temporary_name), read_only=True)
        return self._drive_report(
            state, identity, baseline, last_history_drive=False
        ) | {"shared_stores": dict(baseline.publication_outcomes)}

    def _drive_report(
        self, state, identity, baseline, *, last_history_drive
    ) -> dict[str, object]:
        record = state.copies.get(identity)
        marker = state.drive_markers.get(identity)
        return {
            "identity": identity,
            "record": (
                None
                if record is None
                else {
                    "version": record.version,
                    "synced_at": record.synced_at,
                    "host": record.host,
                }
            ),
            "marker": (
                None
                if marker is None
                else {
                    "state": marker.state,
                    "at": marker.at,
                    "host": marker.host,
                    "name": marker.name,
                }
            ),
            "first_drive_marker": not state.drive_markers,
            "blockers": (
                []
                if marker is not None and marker.state == DRIVE_DECOMMISSIONED
                else self._drive_decommission_blockers(
                    state, identity, last_history_drive=last_history_drive
                )
            ),
        }

    def decommission_drive(
        self,
        identity: str,
        *,
        name: str | None = None,
        last_history_drive: bool = False,
    ) -> dict[str, object]:
        """Decommission a key drive in history; the drive is not needed.

        Revalidates every blocker under the access-operation and engine
        locks, then commits the marker to runtime history and publishes
        it to the available shared stores.

        Raises:
            SyncError: If the identity is invalid or a blocker remains.
        """
        return self._mark_drive(
            identity,
            DRIVE_DECOMMISSIONED,
            name=name,
            last_history_drive=last_history_drive,
        )

    def enrol_drive(self, identity: str) -> dict[str, object]:
        """Lift a decommissioning marker so the drive syncs again."""
        return self._mark_drive(identity, DRIVE_ENROLLED)

    def _mark_drive(
        self,
        identity: str,
        marker_state: str,
        *,
        name: str | None = None,
        last_history_drive: bool = False,
    ) -> dict[str, object]:
        identity = _drive_copy_identity(identity)
        validate_hub_encryption(self.config)
        self._ensure_runtime_dirs()
        with file_lock(
            self.run_directory / "access-op.lock",
            self.config.sync["lock_timeout"],
        ):
            with file_lock(
                self.run_directory / "lock", self.config.sync["lock_timeout"]
            ):
                with tempfile.TemporaryDirectory(
                    dir=self.run_directory / "tmp", prefix="drive-marker-"
                ) as temporary_name:
                    temporary = Path(temporary_name)
                    baseline = BaselineManager(
                        self.run_directory, self.baseline_stores
                    )
                    state = baseline.load(temporary)
                    # Never publish to an attached decommissioned drive.
                    self._baseline = baseline
                    self._exclude_decommissioned(state.decommissioned())
                    report = self._drive_report(
                        state,
                        identity,
                        baseline,
                        last_history_drive=last_history_drive,
                    )
                    if (
                        marker_state == DRIVE_DECOMMISSIONED
                        and report["blockers"]
                    ):
                        raise SyncError(
                            "cannot decommission "
                            + identity.removeprefix("drive:")
                            + ": "
                            + "; ".join(report["blockers"])
                        )
                    changed = state.mark_drive(
                        identity, marker_state, self.config.host, name=name
                    )
                    publication_error = None
                    if changed:
                        state.prune(
                            keep_days=self.config.baseline["keep_days"]
                        )
                        state.save(self.run_directory / "state.json")
                        try:
                            baseline.save(state, temporary)
                        except Exception as exc:
                            publication_error = f"{type(exc).__name__}: {exc}"
                    return report | {
                        "changed": changed,
                        "runtime_history": (
                            "committed" if changed else "unchanged"
                        ),
                        "shared_stores": dict(baseline.publication_outcomes),
                        "publication_error": publication_error,
                    }

    def retire_session(
        self, identity: str, *, owner_confirmation: bool = False
    ) -> dict[str, object]:
        """Retire a clean copy and publish its terminal marker.

        Require cleanup evidence or exact identity confirmation. Active
        records and remaining staging always block retirement.
        """
        from kp_sync.sessions.records import SessionStore

        if (
            not isinstance(identity, str)
            or not identity.startswith("session:")
            or any(ord(char) < 32 for char in identity)
        ):
            raise SyncError("invalid session identity")
        validate_hub_encryption(self.config)
        state_path = self.run_directory / "state.json"
        lock_path = self.run_directory / "lock"
        self._ensure_runtime_dirs()
        store = SessionStore(self.run_directory)
        with file_lock(
            self.run_directory / "access-op.lock",
            self.config.sync["lock_timeout"],
        ):
            with file_lock(lock_path, self.config.sync["lock_timeout"]):
                self._validate_retirement_candidate(
                    identity, store, owner_confirmation
                )
                with tempfile.TemporaryDirectory(
                    dir=self.run_directory / "tmp", prefix="retire-"
                ) as temporary_name:
                    temporary = Path(temporary_name)
                    baseline = BaselineManager(
                        self.run_directory, self.baseline_stores
                    )
                    state = baseline.load(temporary)
                    if (
                        owner_confirmation
                        and identity not in state.copies
                        and identity not in state.retirements
                    ):
                        raise SyncError(
                            f"session identity {identity} is not in history"
                        )
                    changed = state.retire(identity, self.config.host)
                    state.prune(keep_days=self.config.baseline["keep_days"])
                    state.save(state_path)
                    publication_error = None
                    try:
                        baseline.save(state, temporary)
                    except Exception as exc:
                        publication_error = f"{type(exc).__name__}: {exc}"
                    return {
                        "identity": identity,
                        "retired": changed,
                        "runtime_history": "committed",
                        "shared_stores": dict(
                            getattr(baseline, "publication_outcomes", {})
                        ),
                        "publication_error": publication_error,
                    }

    def _validate_retirement_candidate(
        self, identity: str, store, owner_confirmation: bool
    ) -> None:
        """Revalidate cleanup and runtime evidence under both locks."""
        from kp_sync.access_ops import access_identity

        if any(record.identity == identity for record in store.records()):
            raise SyncError(f"session {identity} is still recorded")
        progress = {
            entry_identity: owner_id
            for entry_identity, owner_id, _ in store.pending_retirements()
        }.get(identity)
        session_id = identity.rsplit(":", 1)[-1]
        if re.fullmatch(r"[0-9a-f]{8}", session_id):
            if (
                os.path.lexists(store.record_path(session_id))
                or os.path.lexists(store.key_path(session_id))
                or os.path.lexists(store.staging_path(session_id))
            ):
                raise SyncError(f"session {identity} still has runtime files")
        if identity == "session:kp-access" or identity.startswith(
            "session:kp-access:"
        ):
            staging = _access_staging_path(self.config, self.run_directory)
            current_lifecycle = access_identity(
                self.run_directory, create=False
            )
            if current_lifecycle == identity or (
                current_lifecycle is None and os.path.lexists(staging)
            ):
                raise SyncError(
                    f"access copy {identity} still has runtime files"
                )
        if not owner_confirmation and progress is None:
            raise SyncError(
                f"session {identity} lacks successful-cleanup evidence"
            )
        if progress == "access":
            current_lifecycle = access_identity(
                self.run_directory, create=False
            )
            if current_lifecycle == identity:
                raise SyncError("kp-access lifecycle is still active")
            staging = _access_staging_path(self.config, self.run_directory)
            if current_lifecycle is None and os.path.lexists(staging):
                raise SyncError("kp-access staging was not removed")

    def session_retirement_preview(
        self, identity: str | None = None
    ) -> dict[str, object]:
        """List legacy session records without changing state."""
        from kp_sync.access_ops import access_identity
        from kp_sync.sessions.records import SessionStore

        if identity is not None and (
            not isinstance(identity, str)
            or not identity.startswith("session:")
            or any(ord(char) < 32 for char in identity)
        ):
            raise SyncError("invalid session identity")
        with tempfile.TemporaryDirectory(
            dir=self.run_directory, prefix="retirement-preview-"
        ) as temporary_name:
            baseline = BaselineManager(
                self.run_directory, self.baseline_stores
            )
            state = baseline.load(Path(temporary_name), read_only=True)

        store = SessionStore(self.run_directory)
        records = {record.identity: record for record in store.records()}
        progress = {
            entry_identity: owner_id
            for entry_identity, owner_id, _ in store.pending_retirements()
        }
        live_access = access_identity(self.run_directory, create=False)
        candidates = []
        for candidate in sorted(state.copies):
            if not candidate.startswith("session:"):
                continue
            if identity is not None and candidate != identity:
                continue
            reasons = []
            if candidate in records:
                reasons.append("a runtime session record still exists")
            owner_id = candidate.rsplit(":", 1)[-1]
            if re.fullmatch(r"[0-9a-f]{8}", owner_id):
                if (
                    os.path.lexists(store.record_path(owner_id))
                    or os.path.lexists(store.key_path(owner_id))
                    or os.path.lexists(store.staging_path(owner_id))
                ):
                    reasons.append("session runtime files still exist")
            if candidate == live_access or (
                candidate.startswith("session:kp-access")
                and os.path.lexists(
                    _access_staging_path(self.config, self.run_directory)
                )
            ):
                reasons.append("kp-access lifecycle or staging still exists")
            if candidate in progress:
                reasons.append("successful cleanup retirement is in progress")
            candidates.append(
                {
                    "identity": candidate,
                    "eligible": not reasons,
                    "evidence": (
                        "no runtime session or staging evidence found"
                        if not reasons
                        else "runtime evidence blocks retirement"
                    ),
                    "reason": (
                        "; ".join(reasons)
                        if reasons
                        else "requires explicit owner confirmation for this "
                        "exact identity"
                    ),
                }
            )
        return {
            "candidates": candidates,
            "shared_stores": dict(baseline.publication_outcomes),
        }

    def unresolved_write_race(self) -> tuple[str, Path] | None:
        """Return the validated recovery marker or fail closed."""
        return self._load_write_race()

    def _sync_once(
        self,
        state_path: Path,
        *,
        dry_run: bool,
        restarted: int,
        restore: RestoreSelection | None = None,
    ) -> SyncResult:
        self._ensure_runtime_dirs()
        # Drive copies this run could not write, by identity.
        self._copy_failures: dict[str, str] = {}
        self._absent: set[str] = set()
        # Versions whose lost base stopped this run publishing history.
        self._unpublished_lost: set[str] = set()
        self._verification_reference_credentials = None
        self._known_credentials = []
        self._credential_probes: tuple[Path, ...] = ()
        self._path_identities: dict[Path, str] = {}
        self._copy_slots: set[str] = set()
        self._recovered: tuple[FileEndpoint, ...] = ()
        self._resolved_race: tuple[str, tuple[str, ...]] | None = None
        self._fetch_counter = itertools.count()
        for endpoint in self.endpoints:
            endpoint.forget_observations()
        missing: list[str] = []
        with tempfile.TemporaryDirectory(
            dir=self.run_directory / "tmp", prefix="sync-"
        ) as temp_name:
            temporary = Path(temp_name)
            self._temporary = temporary
            self._check_drive_stores(dry_run=dry_run)
            self._baseline = BaselineManager(
                self.run_directory, self.baseline_stores
            )
            state = self._baseline.load(temporary)
            self._exclude_decommissioned(state.decommissioned())
            self._notify_decommissioned(dry_run=dry_run)
            self._inflight_candidates = self._load_inflight_candidates(state)
            if not dry_run:
                state.save(state_path)
            state_snapshot = (
                state_path.read_bytes() if state_path.exists() else None
            )
            write_race = self._load_write_race()
            if write_race is not None and not dry_run:
                write_race = self._stage_write_race(state)
            if write_race is not None:
                identity, backup = write_race
                reason = (
                    f"unresolved concurrent write to {identity} is "
                    "preserved at "
                    f"{backup}; sync remains pending until it is resolved"
                )
                return SyncResult(
                    current=current_version(state.versions, state.copies),
                    action="pending",
                    classifications={},
                    changed=(),
                    missing=(),
                    pending=reason,
                    restarted=restarted,
                )
            if not dry_run:
                recovery = self._recover_displaced_endpoints(state)
                if recovery is not None:
                    return SyncResult(
                        current=current_version(state.versions, state.copies),
                        action="pending",
                        classifications={},
                        changed=(),
                        missing=(),
                        pending=recovery,
                        restarted=restarted,
                    )
            observations = self._observe_all(temporary, missing, state)
            self._credential_probes = tuple(
                item.local_path
                for item in observations
                if item.local_path is not None
            )
            self._path_identities = {
                item.local_path: item.identity
                for item in observations
                if item.local_path is not None
            }
            fingerprints = {
                item.identity: item.sha256 for item in observations
            }
            fingerprints = self._apply_content_hash_classification(
                fingerprints, observations, state
            )

            if fingerprints.get(self.hub_identity) is None:
                if self.config.hub_persistent:
                    raise PersistentHubMissing(
                        f"persistent hub is missing: {self.config.hub}"
                    )
                if (
                    state.copies.pop(self.hub_identity, None) is not None
                    and not dry_run
                ):
                    state.save(state_path)
                    state_snapshot = state_path.read_bytes()

            self._apply_inflight_candidates(
                state,
                observations,
                fingerprints,
                dry_run=dry_run,
            )
            current, classified = classify_all(
                fingerprints,
                state.copies,
                state.versions,
                hub_identity=self.hub_identity,
                hub_persistent=self.config.hub_persistent,
            )
            observations_by_id = {item.identity: item for item in observations}
            classified = self._demote_never_sources(
                classified, observations_by_id
            )
            if debug_enabled():
                _debug_classification(
                    classified, observations_by_id, fingerprints
                )

            if restore is not None and any(
                item.state
                in {CopyState.CHANGED, CopyState.DIVERGED, CopyState.NEW}
                for item in classified.values()
            ):
                return SyncResult(
                    current=current,
                    action="pending",
                    classifications={
                        key: value.state.value
                        for key, value in classified.items()
                    },
                    missing=tuple(sorted(missing)),
                    pending=(
                        "synchronize changed or unknown copies "
                        "before restoring"
                    ),
                    restarted=restarted,
                )

            restore_plan = (
                self._prepare_restore(
                    restore,
                    temporary,
                    state,
                    current,
                    observations_by_id,
                    dry_run=dry_run,
                )
                if restore is not None
                else None
            )
            if restore_plan is not None and restore_plan.cancelled:
                return SyncResult(
                    current=current,
                    action="cancelled",
                    classifications={
                        key: value.state.value
                        for key, value in classified.items()
                    },
                    missing=tuple(sorted(missing)),
                    restarted=restarted,
                )
            if restore_plan is not None and restore_plan.reason is not None:
                return SyncResult(
                    current=current,
                    action="pending",
                    classifications={
                        key: value.state.value
                        for key, value in classified.items()
                    },
                    missing=tuple(sorted(missing)),
                    pending=restore_plan.reason,
                    restarted=restarted,
                )

            if dry_run:
                if restore_plan is not None:
                    restore_sha = (
                        restore_plan.source.sha256
                        if restore_plan.source is not None
                        else current
                    )
                    return SyncResult(
                        current=current,
                        action=(
                            "would restore"
                            if restore_sha != current
                            else "unchanged"
                        ),
                        classifications={
                            key: value.state.value
                            for key, value in classified.items()
                        },
                        changed=tuple(
                            sorted(
                                item.identity
                                for item in observations
                                if item.sha256 != restore_sha
                                and not item.endpoint.readonly
                            )
                        ),
                        missing=tuple(sorted(missing)),
                        restarted=restarted,
                        copies=_copy_status(state, current),
                        decisions=PendingStore(self.run_directory).load()[
                            "decisions"
                        ],
                    )
                return SyncResult(
                    current=current,
                    action=self._plan_action(
                        classified, current, observations_by_id
                    ),
                    classifications={
                        key: value.state.value
                        for key, value in classified.items()
                    },
                    changed=tuple(
                        sorted(
                            key
                            for key, value in classified.items()
                            if value.state
                            in {
                                CopyState.CHANGED,
                                CopyState.DIVERGED,
                                CopyState.NEW,
                            }
                        )
                    ),
                    missing=tuple(sorted(missing)),
                    restarted=restarted,
                    copies=_copy_status(state, current),
                    decisions=PendingStore(self.run_directory).load()[
                        "decisions"
                    ],
                )

            if restore_plan is not None:
                source = restore_plan.source
                parents = restore_plan.parents
                reason = None
            else:
                source, parents, reason = self._select_candidate(
                    classified,
                    observations_by_id,
                    state,
                    current,
                )
            merge_decision: str | None = None
            merge_password = (
                restore_plan.password if restore_plan is not None else None
            )
            merge_key_file = (
                restore_plan.key_file if restore_plan is not None else None
            )
            merged_candidate = restore_plan is not None
            unknown_resolved = False
            if restore_plan is None and reason in {
                "would merge multiple changed or diverged copies",
                "changed copy diverged from an older version; would merge",
            }:
                (
                    source,
                    parents,
                    reason,
                    merge_decision,
                    merge_password,
                    merge_key_file,
                ) = (
                    self._resolve_foreign_changes
                    if any(
                        item.state == CopyState.CHANGED
                        and observations_by_id[
                            item.identity
                        ].endpoint.foreign_writers
                        for item in classified.values()
                    )
                    else self._merge_changes
                )(
                    temporary,
                    classified,
                    observations_by_id,
                    state,
                    current,
                )
                merged_candidate = source is not None and reason is None
            elif (
                restore_plan is None
                and reason == "new copy requires unknown-copy comparison"
            ):
                unknown_resolved = True
                (
                    source,
                    parents,
                    reason,
                    merge_decision,
                    merge_password,
                    merge_key_file,
                ) = self._resolve_unknown_copies(
                    temporary,
                    classified,
                    observations_by_id,
                    state,
                    current,
                )
                merged_candidate = (
                    source is not None
                    and reason is None
                    and source.identity.startswith("merge:")
                )
            elif (
                restore_plan is None
                and reason
                == "foreign change check requires password verification"
            ):
                (
                    source,
                    parents,
                    reason,
                    merge_decision,
                    merge_password,
                    merge_key_file,
                ) = self._resolve_foreign_changes(
                    temporary,
                    classified,
                    observations_by_id,
                    state,
                    current,
                )
                merged_candidate = source is not None and reason is None
            if (
                restore_plan is None
                and not unknown_resolved
                and reason is None
                and source is not None
                and current is not None
                and any(
                    item.state == CopyState.NEW for item in classified.values()
                )
            ):
                previous_parents = parents
                (
                    source,
                    unknown_parents,
                    reason,
                    merge_decision,
                    merge_password,
                    merge_key_file,
                ) = self._resolve_unknown_copies(
                    temporary,
                    classified,
                    observations_by_id,
                    state,
                    current,
                    source_override=source,
                )
                parents = tuple(
                    sorted(set(previous_parents) | set(unknown_parents))
                )
                merged_candidate = merged_candidate or (
                    source is not None and source.identity.startswith("merge:")
                )
            if reason is not None:
                unavailable = not any(
                    item.sha256 is not None for item in observations
                )
                if unavailable:
                    from kp_sync.log import info

                    info("no database copy reachable; waiting for an endpoint")
                else:
                    self._write_pending(
                        reason,
                        classified,
                        decision_key=merge_decision or "sync",
                    )
                return SyncResult(
                    current=current,
                    action="unavailable" if unavailable else "pending",
                    classifications={
                        key: value.state.value
                        for key, value in classified.items()
                    },
                    changed=tuple(
                        sorted(
                            key
                            for key, value in classified.items()
                            if value.state
                            in {
                                CopyState.CHANGED,
                                CopyState.DIVERGED,
                                CopyState.NEW,
                            }
                        )
                    ),
                    missing=tuple(sorted(missing)),
                    pending=None if unavailable else reason,
                    restarted=restarted,
                )
            if source is None or source.local_path is None:
                return SyncResult(
                    current=current,
                    action="unchanged",
                    classifications={
                        key: value.state.value
                        for key, value in classified.items()
                    },
                    missing=tuple(sorted(missing)),
                    restarted=restarted,
                )

            candidate_sha = source.sha256
            target_observations = [
                item
                for item in observations
                if item.sha256 != candidate_sha
                and not item.endpoint.readonly
                and (
                    item.sha256 is not None
                    or item.identity == self.hub_identity
                    or item.identity in self._absent
                )
            ]
            if (
                candidate_sha == current
                and not target_observations
                and not any(
                    item.state
                    in {CopyState.CHANGED, CopyState.DIVERGED, CopyState.NEW}
                    for item in classified.values()
                )
            ):
                copy_keys = [
                    item.identity
                    for item in observations
                    if item.sha256 == candidate_sha
                    and not item.endpoint.readonly
                    and not item.identity.startswith(RECOVERED_PREFIX)
                ]
                if copy_keys:
                    # Refresh liveness without publishing on every poll.
                    now = datetime.now(timezone.utc)
                    refresh_after = min(
                        86400, self.config.baseline["keep_days"] * 43200
                    )
                    copy_keys = [
                        identity
                        for identity in copy_keys
                        if (record := state.copies.get(identity)) is None
                        or record.version != candidate_sha
                        or record.sha256 != candidate_sha
                        or not 0
                        <= now.timestamp() - copy_record_order(record)[1]
                        < refresh_after
                    ]
                    previous_records = dict(state.copies)
                    if copy_keys:
                        state.record_copies(
                            candidate_sha,
                            copy_keys,
                            self.config.host,
                            synced_at=now.isoformat(),
                        )
                    for identity in copy_keys:
                        previous = previous_records.get(identity)
                        if previous is not None:
                            state.copies[identity] = replace(
                                state.copies[identity],
                                remote_hashes=previous.remote_hashes,
                            )
                    state.save(state_path)
                    self._publish(state, temporary, observations)
                    self._finish_inflight_candidates(
                        self._completed_inflight_candidates(
                            candidate_sha,
                            {
                                item.identity: item.sha256
                                for item in observations
                            },
                        )
                    )
                self._clear_pending()
                self._finish_write_race()
                self._confirm_replica_failures(
                    {
                        item.identity
                        for item in observations
                        if item.sha256 == candidate_sha
                    }
                )
                return SyncResult(
                    current=current,
                    action="unchanged",
                    classifications={
                        key: value.state.value
                        for key, value in classified.items()
                    },
                    missing=tuple(sorted(missing)),
                    restarted=restarted,
                )

            reference_path = self._version_path(
                current if merged_candidate else next(iter(parents), None),
                observations_by_id,
                state,
            )
            password = merge_password
            key_file = merge_key_file
            reference_password, reference_key_file = (
                self._verification_reference_credentials or (None, None)
            )
            if password is None:
                password = self._cached_password()
                if password is not None and self.config.key_file.is_file():
                    key_file = self.config.key_file
            if password is not None:
                self._known_credentials.append((password, key_file))

            def verify_with_credentials():
                nonlocal password, key_file, reference_password
                nonlocal reference_key_file
                for _ in range(3):
                    try:
                        return verify_candidate(
                            source.local_path,
                            reference_size=self._reference_size(
                                parents, state
                            ),
                            reference_path=(
                                reference_path
                                if password is not None
                                else None
                            ),
                            password=password,
                            key_file=key_file,
                            reference_password=reference_password,
                            reference_key_file=reference_key_file,
                            max_entry_drop=self.config.verify[
                                "max_entry_drop"
                            ],
                            min_entry_drop=self.config.verify[
                                "min_entry_drop"
                            ],
                            ui=self.ui,
                            require_keepassxc=merged_candidate,
                        )
                    except VerificationCredentialError as exc:
                        alternate, alternate_key = (
                            self._resolve_alternate_credentials(exc.path)
                        )
                        if alternate is None:
                            return Verification(
                                inspect_kdbx(source.local_path),
                                (
                                    "copy requires separate credentials; "
                                    "verification deferred",
                                ),
                            )
                        if exc.path == source.local_path:
                            password, key_file = alternate, alternate_key
                        else:
                            reference_password, reference_key_file = (
                                alternate,
                                alternate_key,
                            )
                raise VerificationError(
                    "copies repeatedly rejected the supplied credentials"
                )

            try:
                verification = verify_with_credentials()
                if verification.pending_reasons and password is None:
                    password, key_file = self._request_credentials()
                    if password is not None:
                        verification = verify_with_credentials()
            except VerificationError as exc:
                raise SyncError(str(exc)) from exc
            if verification.pending_reasons:
                reason = "; ".join(verification.pending_reasons)
                self._write_pending(
                    reason,
                    classified,
                    decision_key=merge_decision or "verify",
                )
                return SyncResult(
                    current=current,
                    action="pending",
                    classifications={
                        key: value.state.value
                        for key, value in classified.items()
                    },
                    changed=tuple(
                        sorted(
                            key
                            for key, value in classified.items()
                            if value.state
                            in {
                                CopyState.CHANGED,
                                CopyState.DIVERGED,
                                CopyState.NEW,
                            }
                        )
                    ),
                    missing=tuple(sorted(missing)),
                    pending=reason,
                    restarted=restarted,
                )
            if password is not None:
                self._cache_selected_password(password)

            candidate_sha = verification.info.sha256
            candidate_size = verification.info.size

            if (
                state_path.read_bytes() if state_path.exists() else None
            ) != state_snapshot:
                raise _RestartSync
            self._baseline.check(temporary, max_age=RECHECK_WINDOW)
            # The baseline check listed remotes; reuse it.
            for observation in observations:
                self._check_unchanged(observation, temporary, cached=True)

            if candidate_sha not in state.versions:
                target_ids = {item.identity for item in target_observations}
                inflight_targets = tuple(
                    sorted(
                        {
                            item.identity
                            for item in observations
                            if not item.endpoint.readonly
                            and not item.identity.startswith(RECOVERED_PREFIX)
                            and (
                                item.sha256 == candidate_sha
                                or item.identity in target_ids
                            )
                        }
                    )
                )
                self._record_inflight_candidate(
                    state,
                    source.local_path,
                    candidate_sha,
                    parents,
                    candidate_size,
                    inflight_targets,
                )

            # Post-write observations, which proved the written bytes.
            verified = {}
            for target in target_observations:
                fresh_target = self._check_unchanged(
                    target, temporary, cached=True
                )
                if (
                    fresh_target.local_path is not None
                    and fresh_target.sha256 is not None
                ):
                    backup_file(
                        fresh_target.local_path,
                        self.run_directory / "backups",
                        identity=target.identity,
                        keep=self.config.backups["keep"],
                    )
                # Re-check immediately before the write.
                self._check_unchanged(
                    fresh_target, temporary, max_age=RECHECK_WINDOW
                )
                try:
                    if isinstance(target.endpoint, FileEndpoint):
                        target.endpoint.store_if_unchanged(
                            source.local_path,
                            fresh_target.sha256,
                            atomic_exchange=True,
                        )
                    else:
                        target.endpoint.store(source.local_path)
                except EndpointChanged as exc:
                    if exc.preserved_path is not None:
                        backup = backup_file(
                            exc.preserved_path,
                            self.run_directory / "backups",
                            identity=target.identity,
                            keep=self.config.backups["keep"],
                        )
                        backup_sha = EndpointFingerprint.from_file(
                            backup
                        ).sha256
                        if (
                            exc.observed_sha256 is None
                            or backup_sha != exc.observed_sha256
                        ):
                            raise SyncError(
                                "target changed during atomic exchange; "
                                "displaced data remains at "
                                f"{exc.preserved_path} and recovery copy "
                                f"{backup}"
                            ) from exc
                        atomic_write(
                            self.run_directory / "write-race.json",
                            json.dumps(
                                {
                                    "backup": backup.name,
                                    "identity": target.identity,
                                    "sha256": backup_sha,
                                },
                                sort_keys=True,
                                indent=2,
                            )
                            + "\n",
                        )
                        exc.preserved_path.unlink()
                        _fsync_directory(exc.preserved_path.parent)
                        reason = (
                            f"{target.identity} changed during atomic "
                            "exchange; displaced version was preserved at "
                            f"{backup.name}; sync is pending"
                        )
                        self._write_pending(reason, classified)
                        return SyncResult(
                            current=current,
                            action="pending",
                            classifications={
                                key: value.state.value
                                for key, value in classified.items()
                            },
                            changed=tuple(
                                sorted(
                                    key
                                    for key, value in classified.items()
                                    if value.state
                                    in {
                                        CopyState.CHANGED,
                                        CopyState.DIVERGED,
                                        CopyState.NEW,
                                    }
                                )
                            ),
                            missing=tuple(sorted(missing)),
                            pending=reason,
                            restarted=restarted,
                        )
                    raise _RestartSync
                except EndpointUnavailable as exc:
                    if not self._unwritten_drive_copy(fresh_target):
                        raise SyncError(str(exc)) from exc
                    from kp_sync.log import error

                    # Like an unreachable endpoint: retried next run.
                    self._copy_failures[target.identity] = str(exc)
                    error(
                        f"cannot update {target.identity}; continuing "
                        f"without it: {exc}"
                    )
                    continue
                verified[target.identity] = self._check_candidate(
                    target.endpoint,
                    candidate_sha,
                    source.local_path,
                    temporary,
                )

            target_ids = {
                target.identity
                for target in target_observations
                if target.identity not in self._copy_failures
            }
            final_observations = {}
            for item in observations:
                if item.sha256 is None:
                    continue
                # A skipped drive must still hold what was read.
                readonly = (
                    item.endpoint.readonly
                    or item.identity in self._copy_failures
                )
                # The written target was verified by a fresh probe.
                # Each copy's last verified observation (post-write for
                # targets) may show it unchanged by an equal token.
                last = verified.get(item.identity, item)
                fingerprint = last.fingerprint
                final_observations[item.identity] = self._confirm(
                    item.endpoint,
                    item.sha256 if readonly else candidate_sha,
                    item.local_path if readonly else source.local_path,
                    temporary,
                    cached=item.identity in target_ids,
                    token=(
                        fingerprint.token
                        if fingerprint is not None
                        and fingerprint.token_is_content
                        and last.sha256
                        == (item.sha256 if readonly else candidate_sha)
                        else None
                    ),
                )

            self._save_base(source.local_path, candidate_sha)
            copy_keys = [
                item.identity
                for item in observations
                if (
                    item.sha256 == candidate_sha or item.identity in target_ids
                )
                and not item.identity.startswith(RECOVERED_PREFIX)
            ]
            if candidate_sha in state.versions:
                state.record_copies(candidate_sha, copy_keys, self.config.host)
            else:
                state.commit(
                    candidate_sha,
                    parents,
                    self.config.host,
                    copy_keys,
                    size=candidate_size,
                    content_hash=(
                        content_hash(
                            source.local_path,
                            password=password,
                            key_file=key_file,
                        )
                        if password is not None
                        else None
                    ),
                )
            resolved_inflight = self._completed_inflight_candidates(
                candidate_sha,
                {
                    identity: item.sha256
                    for identity, item in final_observations.items()
                },
                excused=self._copy_failures.keys(),
            )
            if (
                len(resolved_inflight) == len(self._inflight_candidates)
                and restore_plan is None
            ):
                state.prune(keep_days=self.config.baseline["keep_days"])
            for identity in copy_keys:
                fresh = final_observations.get(identity)
                if fresh is not None and fresh.fingerprint is not None:
                    fingerprint = fresh.fingerprint
                    state.copies[identity] = replace(
                        state.copies[identity],
                        remote_hashes=(
                            {"probe": fingerprint.token}
                            if fingerprint.token_is_content
                            and fingerprint.token
                            else {}
                        ),
                    )
            state.save(state_path)
            self._publish(state, temporary, observations)
            self._finish_inflight_candidates(resolved_inflight)
            self._prune_cached_bases(state)
            self._clear_pending()
            self._finish_write_race()
            self._forget_copy_passwords()
            self._confirm_replica_failures(set(copy_keys))
            return SyncResult(
                current=current,
                action="committed",
                classifications={
                    key: value.state.value for key, value in classified.items()
                },
                changed=tuple(
                    sorted(
                        key
                        for key, value in classified.items()
                        if value.state
                        in {
                            CopyState.CHANGED,
                            CopyState.DIVERGED,
                            CopyState.NEW,
                        }
                    )
                ),
                missing=tuple(sorted(missing)),
                restarted=restarted,
            )

    def _check_drive_stores(self, *, dry_run: bool) -> None:
        """Check each attached drive's history once per attachment."""
        from kp_sync.baseline import DriveStore, check_drive_history

        stores = [
            store
            for store in self.baseline_stores
            if isinstance(store, DriveStore)
        ]
        if not stores:
            return
        record_path = self.run_directory / "drive-checks.json"
        try:
            checked = json.loads(record_path.read_text())
            if not isinstance(checked, dict):
                checked = {}
        except (OSError, ValueError):
            checked = {}
        changed = False
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for store in stores:
            identity = store.endpoint.identity
            try:
                attached = json.loads(
                    (
                        self.run_directory / "drives" / f"{identity}.json"
                    ).read_text()
                ).get("attached_at")
            except (OSError, ValueError, AttributeError):
                attached = None
            key = attached or "configured"
            if checked.get(identity) == key:
                continue
            try:
                damaged = check_drive_history(
                    store, quarantine=not dry_run, stamp=stamp
                )
            except (OSError, EndpointUnavailable, StateError) as exc:
                debug(f"drive history check for {identity} skipped: {exc}")
                continue
            if damaged:
                names = ", ".join(name[:12] for name in damaged)
                message = (
                    f"Key drive {store.endpoint.name} ({identity}) has "
                    f"{len(damaged)} damaged history file(s) ({names})"
                    + (
                        "; they were moved to its damaged/ folder and will "
                        "be replaced from verified copies. If this repeats, "
                        "the drive may be failing."
                        if not dry_run
                        else "; a real sync moves them aside."
                    )
                )
                error(message)
                self.ui.notify(message)
            if not dry_run:
                checked[identity] = key
                changed = True
        if changed:
            atomic_write(record_path, json.dumps(checked, sort_keys=True))

    def _observe_all(
        self,
        temporary: Path,
        missing: list[str],
        state: HistoryState,
    ) -> list[Observation]:
        observations = []
        # Writable copies proven empty (an attached drive without a
        # database): install targets, not unreachable endpoints.
        self._absent: set[str] = set()
        for index, endpoint in enumerate((*self.endpoints, *self._recovered)):
            try:
                observation = self._observe(
                    endpoint,
                    temporary,
                    index,
                    state=state,
                    cached=True,
                )
            except EndpointAbsent as exc:
                if endpoint.readonly:
                    raise SyncError(str(exc)) from exc
                from kp_sync.log import info

                info(f"{exc}; installing the current version")
                self._absent.add(endpoint.identity)
                observations.append(
                    Observation(
                        endpoint.identity, endpoint, None, None, None, None
                    )
                )
                continue
            except EndpointUnavailable as exc:
                if endpoint.on_unavailable == "fail":
                    raise SyncError(str(exc)) from exc
                is_hub = endpoint.identity == self.hub_identity
                if (
                    endpoint.on_unavailable == "warn"
                    and not endpoint.optional
                    and not is_hub
                ):
                    from kp_sync.log import info

                    info(
                        f"skipping unavailable endpoint {endpoint.name}: {exc}"
                    )
                if not endpoint.optional and not is_hub:
                    missing.append(endpoint.identity)
                observations.append(
                    Observation(
                        endpoint.identity, endpoint, None, None, None, None
                    )
                )
                continue
            observations.append(observation)
        return observations

    def _observe(
        self,
        endpoint: Endpoint,
        temporary: Path,
        index: int,
        *,
        state: HistoryState | None = None,
        cached: bool = False,
    ) -> Observation:
        fingerprint = endpoint.probe_cached() if cached else endpoint.probe()
        if state is not None and fingerprint is not None:
            record = state.copies.get(endpoint.identity)
            if (
                record is not None
                and record.sha256 == record.version
                and (
                    fingerprint.sha256 == record.version
                    or (
                        # Only a token covering a content hash may
                        # stand for the bytes; size and mtime can
                        # survive edits.
                        fingerprint.token_is_content
                        and fingerprint.token is not None
                        and record.remote_hashes.get("probe")
                        == fingerprint.token
                    )
                )
            ):
                base = self.run_directory / "bases" / f"{record.version}.kdbx"
                if base.is_file() and not base.is_symlink():
                    info = inspect_kdbx(base)
                    if info.sha256 != record.version:
                        raise SyncError(
                            f"cached probe base has the wrong hash: {base}"
                        )
                    return Observation(
                        endpoint.identity,
                        endpoint,
                        fingerprint,
                        record.version,
                        info.size,
                        base,
                    )
        # A counter prevents overwriting live copies on re-observation.
        local_path = (
            temporary / f"copy-{index}-{next(self._fetch_counter)}.kdbx"
        )
        try:
            endpoint.fetch(local_path)
        except EndpointUnavailable:
            raise
        try:
            info = inspect_kdbx(local_path)
        except (OSError, KdbxError) as exc:
            raise SyncError(
                f"{endpoint.name}: invalid KDBX copy: {exc}"
            ) from exc
        if (
            fingerprint is not None
            and fingerprint.matches(info.sha256, local_path) is False
        ):
            # The copy changed between probe and download.
            raise _RestartSync
        return Observation(
            endpoint.identity,
            endpoint,
            fingerprint,
            info.sha256,
            info.size,
            local_path,
        )

    def _select_candidate(
        self,
        classified: dict[str, Classification],
        observations: dict[str, Observation],
        state: HistoryState,
        current: str | None,
    ) -> tuple[Observation | None, tuple[str, ...], str | None]:
        changed = [
            item
            for item in classified.values()
            if item.state == CopyState.CHANGED
        ]
        diverged = [
            item
            for item in classified.values()
            if item.state == CopyState.DIVERGED
        ]
        new = [
            item for item in classified.values() if item.state == CopyState.NEW
        ]
        readonly_changes = [
            item
            for item in classified.values()
            if item.state
            in {CopyState.CHANGED, CopyState.DIVERGED, CopyState.NEW}
            and observations[item.identity].endpoint.readonly
        ]
        if readonly_changes:
            return None, (), "read-only endpoint has changed content"
        changed_hashes = {item.sha256 for item in changed}
        if diverged or len(changed_hashes) > 1:
            return None, (), "would merge multiple changed or diverged copies"
        if new and not changed:
            if (
                current is None
                and len(
                    available := [
                        item
                        for item in observations.values()
                        if item.sha256 is not None
                    ]
                )
                > 1
                and len({item.sha256 for item in available}) == 1
            ):
                return available[0], (), None
            return None, (), "new copy requires unknown-copy comparison"
        if changed:
            if any(
                item.record is None or item.record.version != current
                for item in changed
            ):
                return (
                    None,
                    (),
                    "changed copy diverged from an older version; would merge",
                )
            if self.config.verify["foreign_check"] == "always" and any(
                observations[item.identity].endpoint.foreign_writers
                for item in changed
            ):
                return (
                    None,
                    (),
                    "foreign change check requires password verification",
                )
            item = changed[0]
            source = observations[item.identity]
            return source, (item.record.version,), None
        if current is None:
            available = [
                item
                for item in observations.values()
                if item.sha256 is not None
            ]
            if (
                len(available) > 1
                and len({item.sha256 for item in available}) == 1
            ):
                return available[0], (), None
            return (
                None,
                (),
                "no known version; unknown copies are not promoted",
            )
        source = next(
            (item for item in observations.values() if item.sha256 == current),
            None,
        )
        if source is None:
            base_path = self._version_path(current, observations, state)
            if base_path is not None:
                try:
                    info = inspect_kdbx(base_path)
                except (OSError, KdbxError) as exc:
                    raise SyncError(
                        f"cached current base is invalid: {exc}"
                    ) from exc
                if info.sha256 != current:
                    raise SyncError(
                        f"cached current base has the wrong hash: {base_path}"
                    )
                source = Observation(
                    f"base:{current}",
                    FileEndpoint("cached-base", base_path, readonly=True),
                    EndpointFingerprint(current, info.size),
                    current,
                    info.size,
                    base_path,
                )
        if source is None:
            return (
                None,
                (),
                "current version is not available from a reachable copy",
            )
        return source, (current,), None

    @staticmethod
    def _demote_never_sources(classified, observations):
        # Read-only session copies are targets, never promoted.
        result = {}
        for identity, item in classified.items():
            observation = observations.get(identity)
            if (
                observation is not None
                and observation.endpoint.never_source
                and item.state
                in {CopyState.CHANGED, CopyState.DIVERGED, CopyState.NEW}
            ):
                from kp_sync.log import info

                info(f"ignoring changes to {identity}; it is never a source")
                item = replace(item, state=CopyState.STALE)
            result[identity] = item
        return result

    def _prepare_restore(
        self,
        selection: RestoreSelection,
        temporary: Path,
        state: HistoryState,
        current: str | None,
        observations: dict[str, Observation],
        *,
        dry_run: bool,
    ) -> PreparedRestore:
        if current is None:
            return PreparedRestore(
                None,
                (),
                None,
                None,
                "there is no current version to restore against",
            )
        current_path = self._version_path(current, observations, state)
        if current_path is None:
            return PreparedRestore(
                None, (), None, None, "the current version is unavailable"
            )

        if selection.source_kind == "version":
            version = selection.source_value
            if version not in state.versions:
                raise SyncError(
                    f"version is not retained in history: {version}"
                )
            source_path = self._baseline.ensure_base(version, temporary)
            if source_path is None:
                raise SyncError(
                    f"retained version bytes are unavailable: {version}"
                )
        elif selection.source_kind == "backup":
            backup_name = selection.source_value
            if Path(backup_name).name != backup_name:
                raise SyncError("backup name must be a .kdbx file name")
            if not backup_name.endswith(".kdbx"):
                raise SyncError("backup name must be a .kdbx file name")
            source_path = (
                self.run_directory / "backups" / selection.source_value
            )
            try:
                metadata = source_path.lstat()
            except OSError as exc:
                raise SyncError(
                    f"cannot read restore backup "
                    f"{selection.source_value}: {exc}"
                ) from exc
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise SyncError(
                    "restore backup is not a private regular user-owned file"
                )
        else:
            source_path = Path(selection.source_value).expanduser()
            try:
                metadata = source_path.lstat()
            except OSError as exc:
                raise SyncError(f"cannot read restore file: {exc}") from exc
            if not stat.S_ISREG(metadata.st_mode) or source_path.is_symlink():
                raise SyncError(
                    "restore file must be a regular non-symlink file"
                )
            staged_source = temporary / "restore-source.kdbx"
            descriptor = os.open(
                source_path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            with (
                os.fdopen(descriptor, "rb") as incoming,
                staged_source.open("xb") as outgoing,
            ):
                os.fchmod(outgoing.fileno(), 0o600)
                shutil.copyfileobj(incoming, outgoing)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            source_path = staged_source

        try:
            source_info = inspect_kdbx(source_path)
        except (OSError, KdbxError) as exc:
            raise SyncError(
                f"restore source is not a valid KDBX database: {exc}"
            ) from exc
        if (
            selection.source_kind == "version"
            and source_info.sha256 != selection.source_value
        ):
            raise SyncError(
                "retained restore version does not match its history hash"
            )
        restored_version = (
            source_info.sha256
            if source_info.sha256 in state.versions
            else None
        )
        password, key_file = self._request_credentials()
        if password is None or key_file is None:
            return PreparedRestore(
                None,
                (),
                None,
                None,
                "restore needs a database password and key file",
            )
        current_password, current_key_file = password, key_file
        self._verification_reference_credentials = (password, key_file)

        candidate_path = temporary / "restore-candidate.kdbx"
        if selection.mode == "merge":
            outcome = merge_restore(
                current_path,
                source_path,
                candidate_path,
                password=password,
                key_file=key_file,
                ui=self.ui,
                credential_resolver=self._resolve_alternate_credentials,
            )
            if outcome.unresolved:
                return PreparedRestore(
                    None,
                    (),
                    outcome.password,
                    outcome.key_file,
                    "restore conflicts need review; no copies were changed",
                )
            if outcome.candidate is None:
                return PreparedRestore(None, (current,), password, key_file)
            password = outcome.password or password
            key_file = (
                outcome.key_file if outcome.password is not None else key_file
            )
        else:
            from kp_sync.merge import (
                _entity_maps,
                _open_database,
                _save_database,
                _touch_entity,
                _uuid,
            )

            restored, credentials = _open_database(
                source_path,
                password,
                key_file,
                self._resolve_alternate_credentials,
            )
            maps = _entity_maps(restored)
            restored_ids = set(maps["groups"]) | set(maps["entries"])
            for entity in (
                *maps["groups"].values(),
                *maps["entries"].values(),
            ):
                _touch_entity(entity, restored)
            deleted = restored.tree.find("./Root/DeletedObjects")
            if deleted is not None:
                for record in tuple(deleted.findall("DeletedObject")):
                    if _uuid(record) in restored_ids:
                        deleted.remove(record)
            _save_database(restored, candidate_path)
            password, key_file = credentials

        try:
            candidate_info = inspect_kdbx(candidate_path)
        except (OSError, KdbxError) as exc:
            raise SyncError(f"restore candidate is invalid: {exc}") from exc
        parents = tuple(
            sorted(
                {
                    current,
                    *(
                        (restored_version,)
                        if restored_version is not None
                        else ()
                    ),
                }
            )
        )
        if candidate_info.sha256 == current:
            return PreparedRestore(None, parents, password, key_file)

        from pykeepass import PyKeePass
        from kp_sync.merge import _entity_equal, _entity_maps

        current_database = PyKeePass(
            str(current_path),
            current_password,
            keyfile=str(current_key_file),
        )
        candidate_database = PyKeePass(
            str(candidate_path),
            password,
            keyfile=str(key_file),
        )
        current_maps = _entity_maps(current_database)
        candidate_maps = _entity_maps(candidate_database)
        additions = removals = changes = 0
        for kind in ("groups", "entries"):
            before, after = current_maps[kind], candidate_maps[kind]
            additions += len(after.keys() - before.keys())
            removals += len(before.keys() - after.keys())
            changes += sum(
                not _entity_equal(before[identifier], after[identifier])
                for identifier in before.keys() & after.keys()
            )
        if (
            not dry_run
            and self.ui.confirm(
                "Restore will add "
                f"{additions} entries/groups, change {changes}, "
                f"and remove {removals}. "
                "Promote this as a new synchronized version?",
                default=False,
            )
            is not True
        ):
            return PreparedRestore(None, (), None, None, cancelled=True)
        endpoint = FileEndpoint(
            "restore candidate", candidate_path, readonly=True
        )
        source = Observation(
            f"restore:{candidate_info.sha256}",
            endpoint,
            EndpointFingerprint(candidate_info.sha256, candidate_info.size),
            candidate_info.sha256,
            candidate_info.size,
            candidate_path,
        )
        return PreparedRestore(source, parents, password, key_file)

    def _plan_action(
        self,
        classified: dict[str, Classification],
        current: str | None,
        observations: dict[str, Observation],
    ) -> str:
        statuses = {item.state for item in classified.values()}
        if statuses & {CopyState.CHANGED, CopyState.DIVERGED, CopyState.NEW}:
            return "would merge or resolve unknown copy"
        if (
            any(
                item.state == CopyState.STALE
                and not observations[identity].endpoint.readonly
                for identity, item in classified.items()
            )
            or observations[self.hub_identity].sha256 is None
            or self._absent
        ):
            return "would promote current version"
        if (
            current is None
            and len(
                {
                    item.sha256
                    for item in observations.values()
                    if item.sha256 is not None
                }
            )
            > 1
        ):
            return "would resolve unknown copies"
        return "unchanged"

    def _confirm(
        self,
        endpoint: Endpoint,
        sha256: str | None,
        local_path: Path | None,
        temporary: Path,
        *,
        cached: bool = False,
        max_age: float | None = None,
        reraise: bool = False,
        token: str | None = None,
    ) -> Observation:
        """Observe an endpoint expected to hold ``sha256`` or be absent.

        Server hashes are compared with local bytes. A remote copy is
        downloaded again only when no endpoint hash is available.
        Raises _RestartSync if the copy differs.

        ``token`` is the content token of an earlier observation of the
        same copy holding ``sha256``; an unchanged token (only when it
        covers a content hash) shows the copy unchanged without a
        download. Post-write checks must not pass one: they must prove
        which bytes the copy holds.
        """
        try:
            fingerprint = (
                endpoint.probe_cached()
                if cached
                else (
                    endpoint.probe_recent(max_age)
                    if max_age is not None
                    else endpoint.probe()
                )
            )
        except EndpointUnavailable:
            if reraise:
                raise
            if sha256 is None:
                return Observation(
                    endpoint.identity, endpoint, None, None, None, None
                )
            raise _RestartSync
        if fingerprint is not None:
            match = (
                None
                if sha256 is None
                else fingerprint.matches(sha256, local_path)
            )
            if match is False:
                raise _RestartSync
            if match or (
                match is None
                and token is not None
                and sha256 is not None
                and fingerprint.token_is_content
                and fingerprint.token == token
            ):
                return Observation(
                    endpoint.identity,
                    endpoint,
                    fingerprint,
                    sha256,
                    fingerprint.size,
                    local_path,
                )
        try:
            fresh = self._observe(
                endpoint,
                temporary,
                sum(1 for _ in temporary.iterdir()) + 10,
                cached=True,
            )
        except EndpointUnavailable:
            if reraise:
                raise
            if sha256 is None:
                return Observation(
                    endpoint.identity, endpoint, None, None, None, None
                )
            raise _RestartSync
        if fresh.sha256 != sha256:
            raise _RestartSync
        return fresh

    def _check_unchanged(
        self,
        expected: Observation,
        temporary: Path,
        *,
        cached: bool = False,
        max_age: float | None = None,
    ) -> Observation:
        fingerprint = expected.fingerprint
        return self._confirm(
            expected.endpoint,
            expected.sha256,
            expected.local_path,
            temporary,
            cached=cached,
            max_age=max_age,
            # "Unchanged since observed" holds for any content hash,
            # also one kp-sync cannot compute.
            token=(
                fingerprint.token
                if fingerprint is not None and fingerprint.token_is_content
                else None
            ),
        )

    def _check_candidate(
        self,
        endpoint: Endpoint,
        candidate_sha: str,
        candidate_path: Path,
        temporary: Path,
    ) -> Observation:
        try:
            return self._confirm(
                endpoint,
                candidate_sha,
                candidate_path,
                temporary,
                reraise=True,
            )
        except EndpointUnavailable as exc:
            raise SyncError(
                f"{endpoint.name}: cannot verify write: {exc}"
            ) from exc

    def _reference_size(
        self, parents: tuple[str, ...], state: HistoryState
    ) -> int | None:
        if not parents:
            return None
        return max(
            (state.versions[parent].size for parent in parents), default=0
        )

    def _apply_content_hash_classification(
        self,
        fingerprints: dict[str, str | None],
        observations: list[Observation],
        state: HistoryState,
    ) -> dict[str, str | None]:
        password = self._cached_password()
        if password is None:
            return fingerprints
        key_file = (
            self.config.key_file if self.config.key_file.is_file() else None
        )
        result = dict(fingerprints)
        from pykeepass.exceptions import CredentialsError

        for observation in observations:
            if observation.sha256 is None:
                continue
            record = state.copies.get(observation.identity)
            version = (
                state.versions.get(record.version)
                if record is not None
                else None
            )
            if (
                version is None
                or version.content_hash is None
                or observation.local_path is None
                or observation.sha256 in state.versions
            ):
                continue
            try:
                digest = content_hash(
                    observation.local_path,
                    password=password,
                    key_file=key_file,
                )
            except CredentialsError:
                continue
            if digest == version.content_hash:
                result[observation.identity] = record.version
        return result

    def _cached_password(self) -> str | None:
        # Optional hashing must neither prompt nor mask the configured
        # provider with a manually cached credential.
        if self.config.credentials.get("keepassxc"):
            return None
        try:
            return self.credentials.cached_password()
        except CredentialError as exc:
            from kp_sync.log import info

            info(
                "cached password is unavailable for optional content "
                f"hashing: {exc}"
            )
            return None

    def _request_credentials(self) -> tuple[str | None, Path | None]:
        if self.config.credentials.get("keepassxc"):
            if self._browser_credentials is not None:
                return self._browser_credentials
            if not self._browser_attempted:
                self._browser_attempted = True
                from kp_sync.keepassxc import retrieve

                messages = []
                supplied = self.ui.credential_lookup(
                    lambda: retrieve(self.config, messages.append)
                )
                for message in messages:
                    self.ui.notify(message)
                    self.ui.choose(
                        message, ("Continue with the password fallback",)
                    )
                if supplied is not None:
                    key_file = self.credentials.key_file(self.ui)
                    if key_file is None:
                        return None, None
                    if self._opens_an_observed_copy(
                        supplied, key_file, strict=True
                    ):
                        self._transient_passwords.add(supplied)
                        self._browser_credentials = (supplied, key_file)
                        self._known_credentials.append(
                            self._browser_credentials
                        )
                        return self._browser_credentials
                    self.ui.notify(
                        "The KeePassXC password does not open an "
                        "observed database copy; using the fallback."
                    )
        try:
            for _ in range(3):
                previous = self.credentials.cached_password()
                password = self.credentials.password(self.ui)
                if password is None:
                    return None, None
                key_file = self.credentials.key_file(self.ui)
                if key_file is None:
                    return None, None
                if password != previous and not self._opens_an_observed_copy(
                    password, key_file
                ):
                    # A mistyped password must not stay cached for the
                    # The keyring times out; copies may differ after a
                    # password change, so any accepting copy suffices.
                    forget = getattr(self.credentials, "forget", None)
                    if forget is not None:
                        forget()
                    self.ui.notify(
                        "The password does not open any copy of the database."
                    )
                    continue
                if (password, key_file) not in self._known_credentials:
                    self._known_credentials.append((password, key_file))
                return password, key_file
        except CredentialError as exc:
            raise SyncError(str(exc)) from exc
        raise SyncError("the supplied password was rejected three times")

    def _cache_selected_password(self, password: str) -> None:
        if password not in self._transient_passwords:
            self.credentials.cache_password(password)

    def _opens_an_observed_copy(
        self, password: str, key_file: Path, *, strict: bool = False
    ) -> bool:
        from pykeepass import PyKeePass
        from pykeepass.exceptions import CredentialsError

        paths = [path for path in self._credential_probes if path.is_file()]
        if not paths:
            return not strict
        for path in paths:
            try:
                PyKeePass(str(path), password=password, keyfile=str(key_file))
            except CredentialsError:
                continue
            except Exception:
                # Unreadable or damaged copies are judged elsewhere.
                if strict:
                    continue
                return True
            return True
        return False

    def _resolve_alternate_credentials(
        self,
        database_path: Path,
    ) -> tuple[str | None, Path | None]:
        from pykeepass import PyKeePass
        from pykeepass.exceptions import CredentialsError

        for password, key_file in self._known_credentials:
            try:
                PyKeePass(
                    str(database_path), password=password, keyfile=key_file
                )
            except CredentialsError:
                continue
            return password, key_file
        slot = self._copy_slot(database_path)
        cached = self._copy_credential("cached_copy_password", slot)
        if cached is not None:
            for key_file in dict.fromkeys((self.config.key_file, None)):
                if key_file is not None and not key_file.is_file():
                    continue
                try:
                    PyKeePass(
                        str(database_path), password=cached, keyfile=key_file
                    )
                except CredentialsError:
                    continue
                self._copy_slots.add(slot)
                self._known_credentials.append((cached, key_file))
                return cached, key_file
            self._copy_credential("forget_copy_password", slot)
        password = self.ui.password(
            f"Password for KeePass copy {database_path.name}",
            slot=slot,
        )
        if password is None:
            return None, None
        key_choice = self.ui.choose(
            f"Key file for KeePass copy {database_path.name}",
            ("configured key file", "choose another key file", "no key file"),
        )
        if key_choice is None:
            return None, None
        if key_choice == "choose another key file":
            key_file = self.ui.file_path("Select this copy's key file")
            if key_file is None:
                return None, None
        elif key_choice == "configured key file":
            key_file = self.config.key_file
        else:
            key_file = None
        if key_file is not None and not key_file.is_file():
            raise SyncError(f"key file is not readable: {key_file}")
        self._known_credentials.append((password, key_file))
        # A wrong password is caught when opening; the next lookup then
        # forgets it.
        self._copy_credential("cache_copy_password", slot, password)
        self._copy_slots.add(slot)
        return password, key_file

    def _copy_slot(self, database_path: Path) -> str:
        from kp_sync.credentials import copy_slot

        identity = self._path_identities.get(database_path)
        if identity is None:
            identity = (
                "sha256:" + EndpointFingerprint.from_file(database_path).sha256
            )
        return copy_slot(f"{identity}")

    def _copy_credential(self, method: str, *args):
        # Copy passwords live in their own keyring slots so they never
        # replace the main password. Providers without slots skip it.
        handler = getattr(self.credentials, method, None)
        if handler is None:
            return None
        try:
            return handler(*args)
        except CredentialError as exc:
            from kp_sync.log import info

            info(f"copy password cache unavailable: {exc}")
            return None

    def _forget_copy_passwords(self) -> None:
        # The merged copies were rewritten with the current master key.
        for slot in sorted(self._copy_slots):
            self._copy_credential("forget_copy_password", slot)
        self._copy_slots = set()

    def _version_path(
        self,
        version: str | None,
        observations: dict[str, Observation],
        state: HistoryState,
    ) -> Path | None:
        if version is None:
            return None
        for observation in observations.values():
            if (
                observation.sha256 == version
                and observation.local_path is not None
            ):
                return observation.local_path
        base_path = self.run_directory / "bases" / f"{version}.kdbx"
        if not base_path.is_file():
            return self._baseline.ensure_base(version, self._temporary)
        try:
            info = inspect_kdbx(base_path)
        except (OSError, KdbxError) as exc:
            raise SyncError(f"cached base is invalid: {exc}") from exc
        if info.sha256 != version:
            raise SyncError(f"cached base has the wrong hash: {base_path}")
        return base_path

    def _merge_changes(
        self,
        temporary: Path,
        classified: dict[str, Classification],
        observations: dict[str, Observation],
        state: HistoryState,
        current: str | None,
    ) -> tuple[
        Observation | None,
        tuple[str, ...],
        str | None,
        str,
        str | None,
        Path | None,
    ]:
        contributors = [
            item
            for item in classified.values()
            if item.state in {CopyState.CHANGED, CopyState.DIVERGED}
        ]
        if any(
            observations[item.identity].endpoint.readonly
            for item in contributors
        ):
            return (
                None,
                (),
                "read-only endpoint has changed content",
                "sync",
                None,
                None,
            )
        if current is None:
            return (
                None,
                (),
                "no current version is available as a merge base",
                "sync",
                None,
                None,
            )

        inputs: list[tuple[Classification, Observation, str]] = []
        for item in contributors:
            observation = observations[item.identity]
            branch = (
                item.sha256
                if item.state == CopyState.DIVERGED
                else item.record.version if item.record is not None else None
            )
            if branch is None or branch not in state.versions:
                return (
                    None,
                    (),
                    "copy has no known merge ancestor",
                    "sync",
                    None,
                    None,
                )
            if item.sha256 == current or any(
                existing[0].sha256 == item.sha256 for existing in inputs
            ):
                continue
            inputs.append((item, observation, branch))

        if not inputs:
            return (
                None,
                (),
                "no distinct copy is available to merge",
                "sync",
                None,
                None,
            )
        input_key = _merge_input_key(
            current,
            [
                (item.identity, item.sha256, branch)
                for item, observation, branch in inputs
            ],
        )
        decision_key = f"merge:{input_key}"
        current_path = self._version_path(current, observations, state)
        if current_path is None:
            return (
                None,
                (),
                "current version needed for merge is unavailable",
                decision_key,
                None,
                None,
            )
        password, key_file = self._request_credentials()
        if password is None or key_file is None:
            return (
                None,
                (),
                "merge needs a password and readable key file; "
                "copies were left unchanged",
                decision_key,
                None,
                None,
            )

        pending = PendingStore(self.run_directory)
        saved_choices: dict[str, str] = {}
        parent_versions = {current}
        if self._verification_reference_credentials is None:
            self._verification_reference_credentials = (password, key_file)
        candidate_path = current_path
        review_policy = self.config.merge["review"]
        merge_ui = self.ui if review_policy == "interactive" else None
        output_password, output_key_file = password, key_file
        for index, (classification, incoming, branch) in enumerate(inputs):
            ancestor = (
                current
                if branch == current
                else merge_base(current, branch, state.versions)
            )
            if ancestor is None:
                return (
                    None,
                    (),
                    f"{incoming.identity} has no common ancestor with "
                    "the current version",
                    decision_key,
                    None,
                    None,
                )
            base_path = self._version_path(ancestor, observations, state)
            if base_path is None:
                return (
                    None,
                    (),
                    f"merge ancestor {ancestor} is not available",
                    decision_key,
                    None,
                    None,
                )
            parent_versions.add(branch)
            pair_key = _merge_input_key(
                ancestor,
                [
                    ("hub", inspect_kdbx(candidate_path).sha256, ancestor),
                    ("other", incoming.sha256, branch),
                ],
            )
            saved_choices = pending.choices().get(pair_key, {})
            merged_path = temporary / f"merge-{index}.kdbx"
            try:
                outcome = merge_databases(
                    base_path,
                    candidate_path,
                    incoming.local_path,
                    merged_path,
                    password=password,
                    key_file=key_file,
                    ui=merge_ui,
                    choices=saved_choices,
                    credential_resolver=self._resolve_alternate_credentials,
                    metadata_path=(
                        observations[self.hub_identity].local_path
                        or current_path
                    ),
                )
            except (MergeCredentialError, MergeError) as exc:
                return (
                    None,
                    (),
                    f"could not merge {incoming.identity}: {exc}",
                    decision_key,
                    None,
                    None,
                )
            choices = dict(outcome.choices or saved_choices)
            if outcome.unresolved and review_policy in {
                "auto-hub",
                "auto-other",
            }:
                side = "hub" if review_policy == "auto-hub" else "other"
                choices.update(
                    {identifier: side for identifier in outcome.unresolved}
                )
                outcome = merge_databases(
                    base_path,
                    candidate_path,
                    incoming.local_path,
                    merged_path,
                    password=password,
                    key_file=key_file,
                    choices=choices,
                    credential_resolver=self._resolve_alternate_credentials,
                    metadata_path=(
                        observations[self.hub_identity].local_path
                        or current_path
                    ),
                )
            saved_choices.update(outcome.choices or {})
            if saved_choices:
                pending.save_choices(pair_key, saved_choices)
            if outcome.password is not None:
                output_password = outcome.password
                output_key_file = outcome.key_file
                password, key_file = outcome.password, outcome.key_file
            if outcome.unresolved:
                return (
                    None,
                    (),
                    f"merge conflicts need review for {incoming.identity}",
                    decision_key,
                    None,
                    None,
                )
            if not outcome.merged:
                return (
                    None,
                    (),
                    f"merge was deferred for {incoming.identity}",
                    decision_key,
                    None,
                    None,
                )
            candidate_path = merged_path

        try:
            self._cache_selected_password(output_password)
        except CredentialError as exc:
            raise SyncError(
                f"could not cache the selected master-key password: {exc}"
            ) from exc
        try:
            info = inspect_kdbx(candidate_path)
        except (OSError, KdbxError) as exc:
            raise SyncError(f"merge output is invalid: {exc}") from exc
        source = Observation(
            f"merge:{input_key}",
            FileEndpoint(
                "merge-result",
                candidate_path,
                identity=f"merge:{input_key}",
                readonly=True,
            ),
            EndpointFingerprint(info.sha256, info.size),
            info.sha256,
            info.size,
            candidate_path,
        )
        return (
            source,
            tuple(sorted(parent_versions)),
            None,
            decision_key,
            output_password,
            output_key_file,
        )

    def _resolve_unknown_copies(
        self,
        temporary: Path,
        classified: dict[str, Classification],
        observations: dict[str, Observation],
        state: HistoryState,
        current: str | None,
        *,
        source_override: Observation | None = None,
    ) -> tuple[
        Observation | None,
        tuple[str, ...],
        str | None,
        str,
        str | None,
        Path | None,
    ]:
        unknown = [
            item for item in classified.values() if item.state == CopyState.NEW
        ]
        first_run = current is None
        if first_run:
            available = [
                item
                for item in observations.values()
                if item.local_path is not None
            ]
            source_observation = observations.get(self.hub_identity)
            if (
                source_observation is None
                or source_observation.local_path is None
            ):
                source_observation = available[0] if available else None
            source_version = (
                source_observation.sha256
                if source_observation is not None
                else None
            )
            source_path = (
                source_observation.local_path
                if source_observation is not None
                else None
            )
        else:
            source_version = (
                source_override.sha256
                if source_override is not None
                else current
            )
            source_observation = source_override
            source_path = (
                source_override.local_path
                if source_override is not None
                else self._version_path(current, observations, state)
            )
        if source_path is None:
            return (
                None,
                (),
                "current version needed for unknown-copy comparison "
                "is unavailable",
                "sync",
                None,
                None,
            )
        distinct = [
            (item, observations[item.identity])
            for item in unknown
            if observations[item.identity].sha256 is not None
            and observations[item.identity].sha256 != source_version
            and (
                not first_run
                or source_observation is None
                or item.identity != source_observation.identity
            )
        ]
        if first_run:
            distinct = [
                (item, observation)
                for item, observation in distinct
                if observation.local_path is not None
            ]
        unique_inputs = {
            item.sha256: (item, observation) for item, observation in distinct
        }
        if not unique_inputs:
            return (
                source_override,
                (current,) if current else (),
                (
                    None
                    if source_override is not None
                    else "no distinct unknown copy is available"
                ),
                "sync",
                None,
                None,
            )
        input_key = _merge_input_key(
            source_version or "initial",
            [
                (
                    item.identity,
                    observation.sha256,
                    source_version or "initial",
                )
                for item, observation in unique_inputs.values()
            ],
        )
        decision_key = f"unknown:{input_key}"
        password, key_file = self._request_credentials()
        if password is None or key_file is None:
            return (
                None,
                (),
                "unknown copy needs a password and readable key file; "
                "copies were left unchanged",
                decision_key,
                None,
                None,
            )
        pending = PendingStore(self.run_directory)
        saved_choices: dict[str, str] = {}
        self._verification_reference_credentials = (password, key_file)
        review_policy = self.config.merge["review"]
        review_ui = self.ui if review_policy == "interactive" else None
        candidate_path = source_path
        imported = False
        for index, (item, incoming) in enumerate(unique_inputs.values()):
            if incoming.local_path is None:
                continue
            output = temporary / f"unknown-{index}.kdbx"
            pair_key = _merge_input_key(
                "unknown",
                [
                    (
                        "hub",
                        inspect_kdbx(candidate_path).sha256,
                        source_version or "initial",
                    ),
                    ("other", incoming.sha256, "unknown"),
                ],
            )
            saved_choices = pending.choices().get(pair_key, {})
            try:
                outcome = merge_unknown_copy(
                    candidate_path,
                    incoming.local_path,
                    output,
                    password=password,
                    key_file=key_file,
                    ui=review_ui,
                    choices=saved_choices,
                    credential_resolver=self._resolve_alternate_credentials,
                    force_review=first_run,
                )
            except (MergeCredentialError, MergeError) as exc:
                return (
                    None,
                    (),
                    f"could not inspect unknown copy "
                    f"{incoming.identity}: {exc}",
                    decision_key,
                    None,
                    None,
                )
            choices = dict(outcome.choices or saved_choices)
            if outcome.unresolved and review_policy in {
                "auto-hub",
                "auto-other",
            }:
                side = "hub" if review_policy == "auto-hub" else "other"
                choices.update(
                    {identifier: side for identifier in outcome.unresolved}
                )
                outcome = merge_unknown_copy(
                    candidate_path,
                    incoming.local_path,
                    output,
                    password=password,
                    key_file=key_file,
                    choices=choices,
                    credential_resolver=self._resolve_alternate_credentials,
                    force_review=first_run,
                )
            saved_choices.update(outcome.choices or {})
            if saved_choices:
                pending.save_choices(pair_key, saved_choices)
            if outcome.password is not None:
                password, key_file = outcome.password, outcome.key_file
            if outcome.unresolved:
                return (
                    None,
                    (),
                    f"unknown copy {incoming.identity} has entries "
                    "awaiting review",
                    decision_key,
                    None,
                    None,
                )
            if outcome.candidate is not None:
                candidate_path = outcome.candidate
                imported = True
        try:
            self._cache_selected_password(password)
        except CredentialError as exc:
            raise SyncError(
                f"could not cache the selected master-key password: {exc}"
            ) from exc
        try:
            info = inspect_kdbx(candidate_path)
        except (OSError, KdbxError) as exc:
            raise SyncError(
                f"unknown-copy merge output is invalid: {exc}"
            ) from exc
        source_identity = (
            f"merge:{input_key}" if imported else f"base:{source_version}"
        )
        source = Observation(
            source_identity,
            FileEndpoint(
                "unknown-copy-result",
                candidate_path,
                identity=source_identity,
                readonly=True,
            ),
            EndpointFingerprint(info.sha256, info.size),
            info.sha256,
            info.size,
            candidate_path,
        )
        return (
            source,
            (current,) if current is not None else (),
            None,
            decision_key,
            password,
            key_file,
        )

    def _resolve_foreign_changes(
        self,
        temporary: Path,
        classified: dict[str, Classification],
        observations: dict[str, Observation],
        state: HistoryState,
        current: str | None,
    ) -> tuple[
        Observation | None,
        tuple[str, ...],
        str | None,
        str,
        str | None,
        Path | None,
    ]:
        foreign = [
            item
            for item in classified.values()
            if item.state == CopyState.CHANGED
            and observations[item.identity].endpoint.foreign_writers
        ]
        decision_key = "sync"
        if current is None:
            return (
                None,
                (),
                "foreign change has no known current version",
                decision_key,
                None,
                None,
            )
        input_items = [
            (
                item.identity,
                item.sha256,
                item.record.version if item.record is not None else "",
            )
            for item in foreign
        ]
        input_key = _merge_input_key(current, input_items)
        decision_key = f"foreign:{input_key}"
        password, key_file = self._request_credentials()
        if password is None or key_file is None:
            return (
                None,
                (),
                "foreign change check needs a password and readable key file",
                decision_key,
                None,
                None,
            )
        for foreign_index, item in enumerate(foreign):
            if item.record is None:
                return (
                    None,
                    (),
                    f"foreign copy {item.identity} has no recorded version",
                    decision_key,
                    None,
                    None,
                )
            base_path = self._version_path(
                item.record.version, observations, state
            )
            incoming = observations[item.identity]
            if base_path is None or incoming.local_path is None:
                return (
                    None,
                    (),
                    f"foreign copy {item.identity} cannot be compared "
                    "with its recorded version",
                    decision_key,
                    None,
                    None,
                )
            try:
                check = check_foreign_descent(
                    base_path,
                    incoming.local_path,
                    password=password,
                    key_file=key_file,
                    recorded_at=state.versions[item.record.version].time,
                    credential_resolver=self._resolve_alternate_credentials,
                )
            except MergeError as exc:
                return (
                    None,
                    (),
                    f"foreign copy {item.identity} could not be checked: "
                    f"{exc}",
                    decision_key,
                    None,
                    None,
                )
            trust_clock = False
            if check.suspicious:
                trust_clock = (
                    self.ui.confirm(
                        f"Foreign copy {item.identity} has "
                        f"{len(check.suspicious)} entry or group timestamps "
                        "outside the expected range. "
                        "Trust these timestamps for this run?"
                    )
                    is True
                )
            if check.suspicious and not trust_clock:
                return (
                    None,
                    (),
                    f"foreign copy {item.identity} has entry or group "
                    "timestamps requiring review",
                    decision_key,
                    None,
                    None,
                )
            if not check.descends:
                filtered_path, suspicious = filter_foreign_stale_cache(
                    base_path,
                    incoming.local_path,
                    temporary / f"foreign-filtered-{foreign_index}.kdbx",
                    password=password,
                    key_file=key_file,
                    recorded_at=state.versions[item.record.version].time,
                    credential_resolver=self._resolve_alternate_credentials,
                    allow_clock_skew=trust_clock,
                )
                if suspicious:
                    return (
                        None,
                        (),
                        f"foreign copy {item.identity} has entry or group "
                        "timestamps requiring review",
                        decision_key,
                        None,
                        None,
                    )
                try:
                    info = inspect_kdbx(filtered_path)
                except (OSError, KdbxError) as exc:
                    raise SyncError(
                        f"filtered foreign copy is invalid: {exc}"
                    ) from exc
                observations[item.identity] = replace(
                    incoming,
                    fingerprint=EndpointFingerprint(info.sha256, info.size),
                    sha256=info.sha256,
                    size=info.size,
                    local_path=filtered_path,
                )
                self.ui.notify(
                    f"{item.identity} held a stale-cache save; only "
                    "post-baseline changes were used"
                )
        self._verification_reference_credentials = (password, key_file)
        return self._merge_changes(
            temporary,
            classified,
            observations,
            state,
            current,
        )

    def _load_inflight_candidates(
        self, state: HistoryState
    ) -> list[InFlightCandidate]:
        marker = self.run_directory / "inflight.json"
        if marker.is_symlink():
            raise SyncError(f"in-flight marker is a symbolic link: {marker}")
        if not marker.exists():
            return []
        try:
            descriptor = os.open(
                marker, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            )
            with os.fdopen(descriptor, encoding="utf-8") as source:
                metadata = os.fstat(source.fileno())
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) & 0o077
                ):
                    raise ValueError(
                        "marker must be a private user-owned regular file"
                    )
                payload = json.load(source)
            if (
                not isinstance(payload, dict)
                or isinstance(payload.get("schema_version"), bool)
                or not isinstance(payload.get("schema_version"), int)
                or payload.get("schema_version") != 1
                or not isinstance(payload.get("candidates"), list)
                or not payload["candidates"]
            ):
                raise ValueError("invalid marker schema")
            candidates = []
            known_versions = dict(state.versions)
            seen = set()
            for item in payload["candidates"]:
                if not isinstance(item, dict):
                    raise ValueError("candidate must be an object")
                sha = item.get("sha256")
                parents = item.get("parents")
                host = item.get("host")
                created_at = item.get("created_at")
                size = item.get("size")
                targets = item.get("targets")
                if (
                    not isinstance(sha, str)
                    or not isinstance(parents, list)
                    or any(not isinstance(parent, str) for parent in parents)
                    or len(set(parents)) != len(parents)
                    or not isinstance(host, str)
                    or not host
                    or not isinstance(created_at, str)
                    or isinstance(size, bool)
                    or not isinstance(size, int)
                    or size < 0
                    or not isinstance(targets, list)
                    or any(
                        not isinstance(target, str) or not target
                        for target in targets
                    )
                    or len(set(targets)) != len(targets)
                    or sha in seen
                ):
                    raise ValueError("invalid candidate fields")
                seen.add(sha)
                existing = state.versions.get(sha)
                if existing is not None:
                    if existing.size != size:
                        raise ValueError(
                            "candidate size differs from committed history"
                        )
                    known_versions[sha] = existing
                    continue
                version = Version.create(
                    sha,
                    parents,
                    host,
                    time=created_at,
                    size=size,
                    versions=known_versions,
                )
                known_versions[sha] = version
                base = self.run_directory / "bases" / f"{sha}.kdbx"
                if base.is_symlink() or not base.is_file():
                    raise ValueError(
                        f"candidate base is missing or unsafe: {base}"
                    )
                base_info = inspect_kdbx(base)
                if base_info.sha256 != sha or base_info.size != size:
                    raise ValueError(
                        f"candidate base failed verification: {base}"
                    )
                candidates.append(
                    InFlightCandidate(version, base, tuple(targets))
                )
            if len(candidates) != len(payload["candidates"]):
                self._write_inflight_candidates(candidates)
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
            KdbxError,
        ) as exc:
            raise SyncError(
                f"invalid in-flight candidate marker {marker}: {exc}"
            ) from exc
        return candidates

    def _apply_inflight_candidates(
        self,
        state: HistoryState,
        observations: list[Observation],
        fingerprints: dict[str, str | None],
        *,
        dry_run: bool,
    ) -> None:
        observations_by_id = {item.identity: item for item in observations}
        candidates_by_sha = {
            item.version.sha: item for item in self._inflight_candidates
        }
        matching = {
            candidate.version.sha
            for candidate in self._inflight_candidates
            if any(
                observation.sha256 == candidate.version.sha
                and not observation.endpoint.readonly
                and not observation.identity.startswith(RECOVERED_PREFIX)
                for observation in observations
            )
        }
        active = set(matching)

        def include_ancestors(sha):
            candidate = candidates_by_sha.get(sha)
            if candidate is None:
                return
            for parent in candidate.version.parents:
                if parent in candidates_by_sha and parent not in active:
                    active.add(parent)
                    include_ancestors(parent)

        for sha in tuple(matching):
            include_ancestors(sha)

        def add_version(sha):
            if sha in state.versions:
                return
            candidate = candidates_by_sha[sha]
            for parent in candidate.version.parents:
                if (
                    parent in candidates_by_sha
                    and parent not in state.versions
                ):
                    add_version(parent)
            state.versions[sha] = candidate.version

        for sha in active:
            add_version(sha)

        remaining = []
        obsolete_bases = []
        for candidate in self._inflight_candidates:
            matches = [
                item
                for item in observations
                if item.sha256 == candidate.version.sha
                and not item.endpoint.readonly
                and not item.identity.startswith(RECOVERED_PREFIX)
            ]
            if candidate.version.sha in active:
                if matches:
                    existing = state.versions.get(candidate.version.sha)
                    if existing is None or set(existing.parents) != set(
                        candidate.version.parents
                    ):
                        raise SyncError(
                            f"in-flight candidate {candidate.version.sha} "
                            "conflicts with history"
                        )
                    state.generation += 1
                    synced_at = datetime.now(timezone.utc).isoformat()
                    for item in matches:
                        fingerprints[item.identity] = candidate.version.sha
                        state.copies[item.identity] = CopyRecord(
                            version=candidate.version.sha,
                            generation=state.generation,
                            synced_at=synced_at,
                            host=candidate.version.host,
                            sha256=candidate.version.sha,
                        )
                remaining.append(candidate)
                continue
            all_targets_observed = all(
                identity in observations_by_id
                and observations_by_id[identity].sha256 is not None
                for identity in candidate.targets
            )
            if not all_targets_observed:
                remaining.append(candidate)
                continue
            if dry_run:
                remaining.append(candidate)
                continue
            obsolete_bases.append(candidate.path)
        if len(remaining) != len(self._inflight_candidates):
            self._write_inflight_candidates(remaining)
        self._inflight_candidates = remaining
        for path in obsolete_bases:
            path.unlink(missing_ok=True)

    def _record_inflight_candidate(
        self,
        state: HistoryState,
        source: Path,
        sha: str,
        parents: tuple[str, ...],
        size: int,
        targets: tuple[str, ...],
    ) -> None:
        if any(item.version.sha == sha for item in self._inflight_candidates):
            return
        self._save_base(source, sha)
        version = Version.create(
            sha,
            parents,
            self.config.host,
            time=datetime.now(timezone.utc).isoformat(),
            size=size,
            versions=state.versions,
        )
        self._inflight_candidates.append(
            InFlightCandidate(
                version,
                self.run_directory / "bases" / f"{sha}.kdbx",
                targets,
            )
        )
        self._write_inflight_candidates(self._inflight_candidates)

    def _write_inflight_candidates(
        self, candidates: list[InFlightCandidate]
    ) -> None:
        marker = self.run_directory / "inflight.json"
        if not candidates:
            marker.unlink(missing_ok=True)
            if self.run_directory.exists():
                directory_fd = os.open(
                    self.run_directory,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            return
        payload = {
            "schema_version": 1,
            "candidates": [
                {
                    "sha256": item.version.sha,
                    "parents": list(item.version.parents),
                    "host": item.version.host,
                    "created_at": item.version.time,
                    "size": item.version.size,
                    "targets": list(item.targets),
                }
                for item in candidates
            ],
        }
        atomic_write(
            marker, json.dumps(payload, sort_keys=True, indent=2) + "\n"
        )

    def _completed_inflight_candidates(
        self,
        committed_sha: str,
        confirmed_copies: dict[str, str | None],
        *,
        excused=(),
    ) -> set[str]:
        # Excused drives were verified unchanged after a failed write,
        # so they hold no candidate bytes needing recognition later.
        return {
            candidate.version.sha
            for candidate in self._inflight_candidates
            if all(
                identity in excused
                or confirmed_copies.get(identity) == committed_sha
                for identity in candidate.targets
            )
        }

    def _unwritten_drive_copy(self, observation: Observation) -> bool:
        """Return whether a failed drive write left the copy unchanged.

        Only a pendrive copy whose bytes still match the pre-write
        observation, or an empty drive that still has no database after
        a failed install, may be skipped; anything else (other endpoint
        types, a changed, missing or unreadable copy, a replaced drive)
        keeps failing the run.
        """
        endpoint = observation.endpoint
        if not isinstance(endpoint, DriveEndpoint):
            return False
        if observation.sha256 is None:
            # An install into an empty drive is a no-replace rename:
            # nothing was written while no database file exists.
            if observation.identity not in self._absent:
                return False
            try:
                return not os.path.lexists(endpoint.path)
            except OSError:
                return False
        try:
            endpoint._check_mount()
            current = EndpointFingerprint.from_file(endpoint.path).sha256
        except Exception:
            # Not proven unchanged: keep failing the run.
            return False
        return current == observation.sha256

    def _finish_inflight_candidates(self, resolved: set[str]) -> None:
        remaining = [
            item
            for item in self._inflight_candidates
            if item.version.sha not in resolved
        ]
        if len(remaining) != len(self._inflight_candidates):
            self._write_inflight_candidates(remaining)
            self._inflight_candidates = remaining

    def _save_base(self, source: Path, sha: str) -> None:
        directory = self.run_directory / "bases"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        base_path = directory / f"{sha}.kdbx"
        if base_path.is_symlink():
            raise SyncError(
                f"cached base path is a symbolic link: {base_path}"
            )
        if base_path.exists():
            info = inspect_kdbx(base_path)
            if info.sha256 != sha:
                raise SyncError(
                    f"cached base has invalid content: {base_path}"
                )
            return
        fd, temp_name = tempfile.mkstemp(prefix=f".{sha}.", dir=directory)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as output, source.open("rb") as incoming:
                os.fchmod(output.fileno(), 0o600)
                shutil.copyfileobj(incoming, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp_path, base_path)
        except BaseException:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
            raise
        directory_fd = os.open(
            directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _prune_cached_bases(self, state: HistoryState) -> None:
        directory = self.run_directory / "bases"
        retained = {
            f"{sha}.kdbx"
            for sha in (
                set(state.versions)
                | {item.version.sha for item in self._inflight_candidates}
            )
        }
        removed = False
        for path in directory.glob("*.kdbx"):
            if path.name not in retained:
                path.unlink()
                removed = True
        if removed:
            directory_fd = os.open(
                directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

    def _write_pending(
        self,
        reason: str,
        classifications: dict[str, Classification],
        *,
        decision_key: str = "sync",
    ) -> None:
        PendingStore(self.run_directory).save(
            decision_key,
            reason,
            {
                "copies": {
                    key: value.state.value
                    for key, value in sorted(classifications.items())
                }
            },
        )

    def _recover_displaced_endpoints(self, state: HistoryState) -> str | None:
        for endpoint in self.endpoints:
            if not isinstance(endpoint, FileEndpoint):
                continue
            record = state.copies.get(endpoint.identity)
            expected_sha256 = record.version if record is not None else None
            before_action = (
                endpoint._check_mount
                if isinstance(endpoint, DriveEndpoint)
                else None
            )
            try:
                if before_action is not None:
                    before_action()
                leftovers = endpoint.recover_displaced(
                    expected_sha256,
                    before_action=before_action,
                    discard_sha256s=self._resolved_displaced(),
                )
            except _RenameUnsupported as exc:
                raise SyncError(str(exc)) from exc
            except EndpointUnavailable:
                continue
            if not leftovers:
                continue

            backups = []
            for leftover in leftovers:
                if before_action is not None:
                    before_action()
                try:
                    metadata = leftover.lstat()
                    if not stat.S_ISREG(metadata.st_mode):
                        continue
                    if (
                        self._resolved_race is not None
                        and EndpointFingerprint.from_file(leftover).sha256
                        in self._resolved_race[1]
                    ):
                        # Already preserved and being resolved now.
                        continue
                    backup = backup_file(
                        leftover,
                        self.run_directory / "backups",
                        identity=(
                            f"{endpoint.identity}-displaced-"
                            f"{leftover.name[-32:]}"
                        ),
                        keep=self.config.backups["keep"],
                    )
                    backup_sha256 = EndpointFingerprint.from_file(
                        backup
                    ).sha256
                    current = leftover.lstat()
                except OSError as exc:
                    raise SyncError(
                        f"cannot preserve displaced file {leftover}: {exc}"
                    ) from exc
                if (current.st_dev, current.st_ino) != (
                    metadata.st_dev,
                    metadata.st_ino,
                ) or EndpointFingerprint.from_file(
                    leftover
                ).sha256 != backup_sha256:
                    raise SyncError(
                        "displaced file changed while being backed up: "
                        f"{leftover}"
                    )
                backups.append((backup, backup_sha256))

            if not backups:
                continue
            marker_backups = [
                {"backup": backup.name, "sha256": digest}
                for backup, digest in backups
            ]
            atomic_write(
                self.run_directory / "write-race.json",
                json.dumps(
                    {
                        "backups": marker_backups,
                        "backup": marker_backups[0]["backup"],
                        "identity": endpoint.identity,
                        "sha256": marker_backups[0]["sha256"],
                    },
                    sort_keys=True,
                    indent=2,
                )
                + "\n",
            )
            reason = (
                f"unresolved displaced content for {endpoint.identity} "
                "was preserved "
                f"in {', '.join(item['backup'] for item in marker_backups)}; "
                "sync remains pending until it is resolved"
            )
            self._write_pending(reason, {})
            return reason
        return None

    def _publish(self, state, temporary: Path, observations) -> None:
        """Publish history; a lost base refuses it without failing.

        Runtime history is already committed. A base that exists nowhere
        keeps the shared history unpublished (retried every run) and is
        confirmed with the user after the run's local work.
        """
        try:
            self._baseline.save(
                state,
                temporary,
                sources=_observed_sources(observations, state),
            )
        except MissingBases as exc:
            from kp_sync.log import error

            error(str(exc))
            self._unpublished_lost = set(exc.missing)

    def _confirm_replica_failures(self, current_copies=()) -> None:
        """Ask whether to continue without updating some pendrives.

        Covers drive history left outdated (``drive-history:``) and
        drive database copies that could not be written
        (``drive-copy:``). Runs after the hub and required history store
        are committed, so nothing is rolled back. Acceptance is kept
        privately until that drive is next current; a refusal or a
        missing UI leaves a pending decision, which the next run
        re-evaluates. The sync result itself stays consistent.

        Args:
            current_copies: Identities holding the committed version
                after this run; their accepted copy warnings clear.
        """
        from kp_sync.log import info

        outcomes = getattr(self._baseline, "publication_outcomes", {})
        failures = {}
        for name, reason in getattr(
            self._baseline, "replica_failures", {}
        ).items():
            failures[f"drive-history:{name}"] = (name, "history", reason)
        for identity, reason in getattr(self, "_copy_failures", {}).items():
            failures[f"drive-copy:{identity}"] = (identity, "copy", reason)
        lost = sorted(
            set(getattr(self._baseline, "lost_bases", ()))
            | getattr(self, "_unpublished_lost", set())
        )
        if lost:
            names = ", ".join(sha[:12] for sha in lost)
            failures["history-bases:lost"] = ("", "bases", names)

        def still_outdated(key: str) -> bool:
            kind, _, name = key.partition(":")
            if key in failures:
                return True
            if kind == "history-bases":
                return False
            if kind == "drive-copy":
                return name not in current_copies
            outcome = outcomes.get(name, "")
            return not (
                outcome == "already current" or outcome.startswith("published")
            )

        path = self.run_directory / "replica-acknowledgements.json"
        accepted = _read_replica_acknowledgements(path)
        current = {
            key: reason
            for key, reason in accepted.items()
            if still_outdated(key)
        }
        pending = PendingStore(self.run_directory)
        for key, (name, kind, reason) in sorted(failures.items()):
            if key in current:
                continue
            label = _copy_display(name, self.config, {})[1] if name else ""
            if kind == "bases":
                question = (
                    f"The saved copy of history version(s) {reason} is "
                    "missing from the runtime cache, every history store "
                    "and every reachable database copy. The database "
                    "copies are still synchronized, but kp-sync will not "
                    "publish new history until you run kp-sync "
                    "repair-history. Continue for now?"
                )
                summary = "history version(s) lost their saved copy"
            elif kind == "history":
                question = (
                    f"History on pendrive {label} could not be updated "
                    f"({reason}). The database and cloud history are "
                    "synchronized, but this pendrive keeps older history, "
                    "so it may not be recognized when starting offline. "
                    "Continue without updating it?"
                )
                summary = f"history on pendrive {label} could not be updated"
            else:
                question = (
                    f"The database on pendrive {label} could not be "
                    f"updated ({reason}). The other copies are "
                    "synchronized, but this pendrive keeps an older "
                    "version until a later sync can write it. "
                    "Continue without updating it?"
                )
                summary = f"database on pendrive {label} could not be updated"
            if self.ui.confirm(question, default=False) is True:
                info(
                    f"continuing with lost history bases: {reason}"
                    if kind == "bases"
                    else f"continuing without updating {kind} on {label}"
                )
                current[key] = reason
                continue
            pending.save(key, f"{summary}: {reason}", {"store": name})
        if current != accepted:
            if current:
                atomic_write(
                    path,
                    json.dumps(
                        {"schema_version": 1, "accepted": current},
                        sort_keys=True,
                    )
                    + "\n",
                )
            elif os.path.lexists(path):
                path.unlink()

    def _clear_pending(self) -> None:
        pending = PendingStore(self.run_directory)
        for key in tuple(pending.load()["decisions"]):
            pending.resolve(key)

    def _stage_write_race(
        self, state: HistoryState
    ) -> tuple[str, Path] | None:
        """Feed preserved race backups into this run as unknown copies.

        Backups in history need no processing. Others are observed as
        ``recovered:<sha>`` without copy records and use unknown-copy
        safeguards. The marker clears only when a run completes.
        """
        identity, records = self._load_write_race_records()
        recovered = []
        directory = self.run_directory / "recovered"
        ensure_private_directory(directory)
        self._recovered = ()
        for backup, digest in records:
            if digest in state.versions:
                continue
            try:
                inspect_kdbx(backup)
            except (OSError, KdbxError):
                # Not mergeable: retain the marker for manual handling.
                return identity, backup
            target = directory / f"{digest}.kdbx"
            if not (
                target.is_file()
                and not target.is_symlink()
                and EndpointFingerprint.from_file(target).sha256 == digest
            ):
                atomic_write(target, backup.read_bytes())
            recovered.append(
                FileEndpoint(
                    "recovered",
                    target,
                    identity=f"{RECOVERED_PREFIX}{digest[:16]}",
                )
            )
        self._recovered = tuple(recovered)
        self._resolved_race = (
            identity,
            tuple(digest for _, digest in records),
        )
        if not recovered:
            self._finish_write_race()
        return None

    def _finish_write_race(self) -> None:
        resolved = getattr(self, "_resolved_race", None)
        if resolved is None:
            return
        identity, digests = resolved
        path = self.run_directory / "resolved-displaced.json"
        known = self._resolved_displaced()
        known.update(digests)
        atomic_write(
            path, json.dumps(sorted(known)[-RESOLVED_DISPLACED_LIMIT:]) + "\n"
        )
        (self.run_directory / "write-race.json").unlink(missing_ok=True)
        for endpoint in self._recovered:
            endpoint.path.unlink(missing_ok=True)
        self._resolved_race = None
        from kp_sync.log import info

        info(f"resolved preserved concurrent write to {identity}")

    def _resolved_displaced(self) -> set[str]:
        path = self.run_directory / "resolved-displaced.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return set()
        except (OSError, ValueError) as exc:
            raise SyncError(
                f"invalid resolved displaced-file list {path}: {exc}"
            ) from exc
        if not isinstance(payload, list) or any(
            not isinstance(item, str) or len(item) != 64 for item in payload
        ):
            raise SyncError(f"invalid resolved displaced-file list {path}")
        return set(payload)

    def _load_write_race(self) -> tuple[str, Path] | None:
        loaded = self._load_write_race_records()
        if loaded is None:
            return None
        identity, records = loaded
        return identity, records[0][0]

    def _load_write_race_records(
        self,
    ) -> tuple[str, list[tuple[Path, str]]] | None:
        marker = self.run_directory / "write-race.json"
        if marker.is_symlink():
            raise SyncError(
                f"unresolved write-race marker is a symbolic link: {marker}"
            )
        if not marker.exists():
            return None
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            backup_name = payload["backup"]
            identity = payload["identity"]
            expected_sha = payload["sha256"]
            records = payload.get(
                "backups",
                [
                    {
                        "backup": backup_name,
                        "sha256": expected_sha,
                    }
                ],
            )
            if (
                not isinstance(records, list)
                or not records
                or not isinstance(identity, str)
                or not identity
                or not isinstance(expected_sha, str)
                or len(expected_sha) != 64
            ):
                raise ValueError("invalid identity, backup name, or SHA-256")
            if not isinstance(records[0], dict):
                raise ValueError("invalid primary backup record")
            if (
                records[0].get("backup") != backup_name
                or records[0].get("sha256") != expected_sha
            ):
                raise ValueError(
                    "primary backup fields do not match backup records"
                )
            validated = []
            for item in records:
                name = item["backup"]
                digest = item["sha256"]
                if (
                    not isinstance(name, str)
                    or Path(name).name != name
                    or not name.endswith(".kdbx")
                    or not isinstance(digest, str)
                    or len(digest) != 64
                ):
                    raise ValueError("invalid backup name or SHA-256")
                backup = self.run_directory / "backups" / name
                if backup.is_symlink() or not backup.is_file():
                    raise ValueError("recovery backup is not a regular file")
                actual_sha = EndpointFingerprint.from_file(backup).sha256
                if actual_sha != digest:
                    raise ValueError(
                        "recovery backup failed hash verification"
                    )
                validated.append((backup, digest))
            backup = validated[0][0]
        except (
            OSError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise SyncError(
                f"invalid unresolved write-race marker {marker}: {exc}"
            ) from exc
        if backup.name != backup_name:
            raise SyncError(
                "unresolved write-race marker has inconsistent backup "
                f"records: {marker}"
            )
        return identity, validated

    def _ensure_runtime_dirs(self) -> None:
        ensure_private_directory(self.run_directory)
        for name in ("tmp", "bases", "backups"):
            ensure_private_directory(self.run_directory / name)


def _short(value: str | None) -> str:
    return value[:12] if value else "none"


def _debug_classification(classified, observations, fingerprints) -> None:
    """Explain each copy's state; identities and hashes only."""
    for identity, item in sorted(classified.items()):
        observation = observations.get(identity)
        observed = observation.sha256 if observation else None
        if observation is None or (
            observed is None and observation.fingerprint is None
        ):
            how = "not observed"
        elif observation.local_path is not None:
            how = "content read"
        else:
            how = "identified without download"
        effective = fingerprints.get(identity)
        same_content = (
            f", same content as {_short(effective)}"
            if effective and observed and effective != observed
            else ""
        )
        record = item.record.version if item.record else None
        debug(
            f"copy {identity}: {item.state.value} (observed "
            f"{_short(observed)}{same_content}, recorded {_short(record)}, "
            f"{how})"
        )


class _RestartSync(Exception):
    """Signal that endpoint observations must be refreshed."""

    pass


class _UnlockedUI(UI):
    """Ask the UI without holding the synchronization flock."""

    def __init__(
        self,
        backend: UI,
        lock_fd: int,
        timeout: float,
        *,
        ask_first=False,
        on_pause=None,
    ) -> None:
        self.backend = backend
        self.on_pause = on_pause
        self.lock_fd = lock_fd
        self.timeout = timeout
        self.ask_first = ask_first
        self._approved: bool | None = None

    def _approve(self) -> bool:
        if not self.ask_first:
            return True
        if self._approved is None:
            self._approved = (
                self.confirm(
                    "KeePass copies need verification or merge; review now?"
                )
                is True
            )
        return self._approved

    def _call(self, method, *args, **kwargs):
        if self.on_pause is not None:
            self.on_pause()
        fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
        try:
            try:
                return method(*args, **kwargs)
            except UIUnavailable as exc:
                from kp_sync.log import info

                info(f"user decision deferred: {exc}")
                return None
        finally:
            acquire_lock(self.lock_fd, self.timeout)

    def confirm(self, prompt: str, *, default: bool = False) -> bool | None:
        """Forward confirmation outside the synchronization lock."""
        return self._call(self.backend.confirm, prompt, default=default)

    def choose(self, prompt: str, options: Iterable[str]) -> str | None:
        """Forward choice prompts outside the synchronization lock."""
        return self._call(self.backend.choose, prompt, options)

    def password(self, prompt: str, *, slot: str | None = None) -> str | None:
        """Request a password only after the first-question gate."""
        if not self._approve():
            return None
        if slot is None:
            return self._call(self.backend.password, prompt)
        return self._call(self.backend.password, prompt, slot=slot)

    def credential_lookup(self, lookup):
        """Request external credentials outside the flock and gate."""
        if not self._approve():
            return None
        return self._call(self.backend.credential_lookup, lookup)

    def file_path(self, prompt: str) -> Path | None:
        """Forward key-file selection without holding the sync lock."""
        return self._call(self.backend.file_path, prompt)

    def review(
        self, conflicts: list[dict[str, object]]
    ) -> dict[str, str] | None:
        """Review conflicts outside the lock after required approval."""
        if not self._approve():
            return None
        return self._call(self.backend.review, conflicts)

    def notify(self, message: str) -> None:
        """Notify without holding the synchronization lock."""
        self._call(self.backend.notify, message)


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _access_staging_path(config: Config, run_directory: Path) -> Path:
    """Resolve the access staging directory without creating it."""
    configured = config.access.get("staging_dir")
    staging = Path(configured).expanduser() if configured else Path("staging")
    return staging if staging.is_absolute() else run_directory / staging


def _copy_status(
    state: HistoryState, current: str | None
) -> dict[str, dict[str, object]]:
    distances = {}
    frontier = [current] if current in state.versions else []
    distance = 0
    while frontier:
        following = []
        for sha in frontier:
            if sha in distances:
                continue
            distances[sha] = distance
            following.extend(state.versions[sha].parents)
        frontier = following
        distance += 1
    return {
        identity: {
            "version": record.version,
            "versions_behind": distances.get(record.version),
            "state": (
                "unchanged"
                if record.version == current
                else "stale" if record.version in distances else "diverged"
            ),
            "host": record.host,
            "synced_at": record.synced_at,
        }
        for identity, record in sorted(state.copies.items())
    }


def _status_details(
    config: Config,
    run_directory: Path,
    state: HistoryState,
    current: str | None,
    decisions: dict[str, object],
) -> tuple[
    dict[str, dict[str, object]],
    dict[str, dict[str, object]],
    dict[str, int],
]:
    """Build cached lifecycle details without probing endpoints."""
    from kp_sync.access_ops import access_identity
    from kp_sync.sessions.records import SessionStore

    copies = _copy_status(state, current)
    endpoints = {}
    for endpoint in config.endpoints:
        if endpoint.type == "rclone" and endpoint.remote:
            endpoints[f"cloud:{endpoint.remote}"] = (
                "cloud",
                f"{endpoint.name} ({endpoint.remote})",
            )
        elif endpoint.type == "file":
            endpoints[endpoint.identity or f"file:{endpoint.name}"] = (
                "file",
                endpoint.name,
            )
        else:
            endpoints[f"command:{endpoint.name}"] = (
                "command",
                endpoint.name,
            )

    sessions: dict[str, dict[str, object]] = {}
    session_store = SessionStore(run_directory)
    records = session_store.records()
    by_identity = {record.identity: record for record in records}
    for identity, details in copies.items():
        if identity.startswith("session:"):
            record = by_identity.get(identity)
            lifecycle = (
                "live"
                if record
                and record.owner_alive()
                and record.expires > time.time()
                else "unfinished" if record else "historical_unknown"
            )
            sessions[identity] = {
                "identity": identity,
                "name": _session_display_name(identity, record),
                "lifecycle": lifecycle,
                "state": details["state"],
                "version": details["version"],
                "last_synchronized_at": details["synced_at"],
                "synchronized_age": _readable_age(details["synced_at"]),
            }
            details.update(
                {"kind": "session", "name": sessions[identity]["name"]}
            )
        else:
            kind, name = _copy_display(identity, config, endpoints)
            details.update({"kind": kind, "name": name})
        details["identity"] = identity
        details["last_synchronized_at"] = details["synced_at"]
        details["synchronized_age"] = _readable_age(details["synced_at"])

    for record in records:
        if record.identity in state.retirements:
            continue
        if record.identity in sessions:
            continue
        sessions[record.identity] = {
            "identity": record.identity,
            "name": _session_display_name(record.identity, record),
            "lifecycle": (
                "live"
                if record.owner_alive() and record.expires > time.time()
                else "unfinished"
            ),
            "mode": record.mode,
            "state": "not yet synchronized",
        }

    lifecycle = access_identity(run_directory, create=False)
    if (
        lifecycle
        and lifecycle not in state.retirements
        and lifecycle not in sessions
    ):
        sessions[lifecycle] = {
            "identity": lifecycle,
            "name": "kp-access",
            "lifecycle": "unfinished",
            "state": (
                "recorded" if lifecycle in copies else "not yet synchronized"
            ),
        }

    summary = {
        "recorded_copies": sum(
            not identity.startswith("session:") for identity in copies
        ),
        "unfinished_sessions": sum(
            item["lifecycle"] in {"live", "unfinished"}
            for item in sessions.values()
        ),
        "pending_decisions": len(decisions),
    }
    return copies, sessions, summary


def _observed_sources(observations, state: HistoryState) -> dict:
    """Map retained versions to this run's local copies of their bytes.

    The baseline manager verifies each file's hash before using it to
    restore a missing base.
    """
    return {
        item.sha256: item.local_path
        for item in observations
        if item.sha256 in state.versions and item.local_path is not None
    }


def _read_replica_acknowledgements(path: Path) -> dict[str, str]:
    """Load accepted outdated-replica warnings; ignore invalid data."""
    from kp_sync.log import info

    if not os.path.lexists(path):
        return {}
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("not a regular file")
        payload = json.loads(path.read_text())
        stores = payload["accepted"]
        if payload.get("schema_version") != 1 or not all(
            isinstance(key, str)
            and key.startswith(
                ("drive-history:", "drive-copy:", "history-bases:")
            )
            and isinstance(value, str)
            for key, value in stores.items()
        ):
            raise ValueError("unsupported content")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        # Losing an acknowledgement only means asking again.
        info(f"ignoring invalid replica acknowledgements: {exc}")
        return {}
    return dict(stores)


def _copy_display(identity: str, config: Config, endpoints):
    if identity.startswith("hub:"):
        return "hub", identity.removeprefix("hub:")
    if identity.startswith("drive:"):
        technical = identity.removeprefix("drive:")
        aliases = config.drives.get("aliases", {})
        return "drive", aliases.get(technical, technical)
    if identity in endpoints:
        return endpoints[identity]
    return "copy", identity


def _session_display_name(identity: str, record) -> str:
    if record is not None:
        return f"{record.host} ({record.id})"
    if identity.startswith("session:kp-access"):
        return "kp-access"
    suffix = identity.removeprefix("session:")
    host, separator, session_id = suffix.rpartition(":")
    if separator and re.fullmatch(r"[0-9a-f]{8}", session_id):
        return f"{host} ({session_id})"
    return suffix


def _readable_age(value: object) -> str:
    if not isinstance(value, str):
        return "unknown"
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
    except ValueError:
        return "unknown"
    seconds = max(
        0, int((datetime.now(timezone.utc) - timestamp).total_seconds())
    )
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _merge_input_key(
    current: str,
    inputs: list[tuple[str, str | None, str]],
) -> str:
    encoded = json.dumps(
        [current, sorted(inputs)],
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def endpoints_from_config(
    config: Config,
    run_directory: str | os.PathLike[str],
    *,
    rclone_daemons: dict | None = None,
) -> tuple[Endpoint, ...]:
    """Create endpoint adapters from configuration and attached drives.

    Runtime-discovered sessions are added by ``session_endpoints``. This
    constructs adapters without probing remote endpoints.

    ``rclone_daemons`` is the daemon's registry of owned ``rclone rcd``
    transports for endpoints with ``rclone_transport = "auto"``, kept
    across runs. Without it, such endpoints only use an rcd the daemon
    already runs.
    """
    endpoints: list[Endpoint] = []
    for item in config.endpoints:
        if not item.enabled:
            continue
        if item.type == "file":
            if item.source == "runtime":
                continue
            endpoints.append(_file_endpoint(item))
        elif item.type == "command":
            endpoints.append(_command_endpoint(item))
        elif item.type == "rclone":
            from kp_sync.endpoints.rclone import RcloneEndpoint
            from kp_sync.rclone_credentials import RcloneCredentials

            credentials = (
                RcloneCredentials(item.name, item.rclone_config, run_directory)
                if item.rclone_config is not None
                else None
            )
            endpoints.append(
                RcloneEndpoint(
                    item.name,
                    item.remote,
                    database_name=config.db_name,
                    credentials=credentials,
                    transport=_rclone_transport(
                        item, credentials, run_directory, rclone_daemons
                    ),
                    download=item.download,
                    upload=item.upload,
                    probe=item.probe,
                    reach=_reach_check(item),
                    identity=item.identity,
                    readonly=item.readonly,
                    optional=item.optional,
                    on_unavailable=item.on_unavailable,
                    timeout=item.timeout,
                    foreign_writers=item.foreign_writers,
                )
            )

    endpoints.extend(session_endpoints(config, run_directory))
    try:
        drives = list_drives(run_directory)
    except DriveError as exc:
        raise SyncError(f"cannot inspect attached drives: {exc}") from exc
    for drive in drives:
        runtime_configs = [
            item
            for item in config.endpoints
            if item.enabled
            and item.type == "file"
            and item.source == "runtime"
        ]
        for item in runtime_configs:
            endpoints.append(
                DriveEndpoint(
                    item.name,
                    drive.mountpoint,
                    str(Path(config.drives["directory"]) / config.db_name),
                    drive.identity,
                    readonly=item.readonly,
                    optional=item.optional,
                    on_unavailable=item.on_unavailable,
                    timeout=item.timeout,
                    foreign_writers=item.foreign_writers,
                )
            )
    return tuple(endpoints)


def _reach_check(item):
    """The endpoint's cloud reach check, or None with ``false``."""
    if item.reach_check is False:
        return None
    from kp_sync.reach import ReachCheck

    return ReachCheck(item.reach_check, item.reach_timeout, item.remote)


def _rclone_transport(item, credentials, run_directory, registry):
    """Return the rcd transport for an ``auto`` endpoint, if any."""
    if item.rclone_transport != "auto":
        return None
    from kp_sync.rclone_rcd import RcloneDaemon

    if registry is None:
        return RcloneDaemon(
            item.name, run_directory, owner=False, credentials=credentials
        )
    transport = registry.get(item.name)
    if transport is None:
        transport = registry[item.name] = RcloneDaemon(
            item.name, run_directory, owner=True, credentials=credentials
        )
    return transport


def session_endpoints(config: Config, run_directory) -> list[Endpoint]:
    """Discover sessions from the profile's private runtime state."""
    from kp_sync.endpoints.session import SessionEndpoint
    from kp_sync.log import error
    from kp_sync.sessions.records import SessionStore

    store = SessionStore(run_directory)
    records = store.records(
        on_error=lambda session_id, exc: error(
            f"ignoring session {session_id}: {exc}"
        ),
    )
    return [
        SessionEndpoint(record, store, config.db_name) for record in records
    ]


def _file_endpoint(item: EndpointConfig) -> FileEndpoint:
    assert item.path is not None
    return FileEndpoint(
        item.name,
        item.path,
        identity=item.identity or f"file:{item.name}",
        readonly=item.readonly,
        optional=item.optional,
        on_unavailable=item.on_unavailable,
        timeout=item.timeout,
        foreign_writers=item.foreign_writers,
    )


def _command_endpoint(item: EndpointConfig) -> CommandEndpoint:
    assert item.fetch is not None and item.store is not None
    return CommandEndpoint(
        item.name,
        item.fetch,
        item.store,
        probe=item.probe,
        probe_is_content_hash=item.probe_is_content_hash,
        identity=item.identity or f"command:{item.name}",
        readonly=item.readonly,
        optional=item.optional,
        on_unavailable=item.on_unavailable,
        timeout=item.timeout,
        foreign_writers=item.foreign_writers,
    )
