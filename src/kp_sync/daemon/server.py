"""Daemon request handling, synchronization scheduling, and sockets."""

import concurrent.futures
import fcntl
import hashlib
import itertools
import os
import shutil
import signal
import socket
import stat
import threading
import time
import uuid
from pathlib import Path

from kp_sync.access_ops import (
    ACCESS_IDENTITY,
    ACCESS_LEASE,
    ACCESS_TTL_SECONDS,
    AccessOperationError,
    access_paths,
    access_identity,
    apply_file_operation,
    atomic_copy,
    clear_transit,
    database_hash,
    in_transit,
    mark_transit,
    validate_operation,
)
from kp_sync.credentials import CredentialProvider
from kp_sync.daemon.leases import LeaseStore
from kp_sync.daemon.protocol import (
    Connection,
    ProtocolError,
    peer_uid,
    validate_request,
)
from kp_sync.daemon.triggers import HubWatch, Triggers
from kp_sync.rclone_credentials import credentials_for
from kp_sync.drives import (
    detach_drive,
    identify_drive,
    list_drives,
    pending_attach_path,
    register_drive,
)
from kp_sync.engine import SyncEngine, endpoints_from_config
from kp_sync.endpoints.base import EndpointChanged
from kp_sync.endpoints.file import _fsync_directory, _rename_noreplace
from kp_sync.log import debug, error, info, logged, set_log_file
from kp_sync.lock import file_lock
from kp_sync.paths import ensure_private_directory, ensure_run_dir
from kp_sync.pending import PendingError, PendingStore, _read_private_json
from kp_sync.sessions.session import SessionManager, _outcome_clean
from kp_sync.ui.base import UI
from kp_sync.ui.factory import create_ui
from kp_sync.ui.none import NoneUI
from kp_sync.ui.progress import SyncProgress


def _access_staging_directory(config, run_directory):
    configured = config.access.get("staging_dir")
    staging = Path(configured).expanduser() if configured else Path("staging")
    return staging if staging.is_absolute() else Path(run_directory) / staging


def file_hash(path):
    """Return a file's SHA-256 digest, or ``None`` if it is absent."""
    try:
        with Path(path).open("rb") as source:
            return hashlib.file_digest(source, "sha256").hexdigest()
    except FileNotFoundError:
        return None


class RemoteUI(UI):
    """Forward UI questions to a waiting client over the socket.

    The sync gate is released while waiting for answers, and protected
    conflict values are masked before they cross the socket.
    """

    def __init__(self, connection, config, sync_gate):
        """Bind the remote UI to a client connection and sync gate."""
        self.connection = connection
        self.config = config
        self.sync_gate = sync_gate
        self.ids = itertools.count()

    def _ask(self, method, *args):
        identifier = next(self.ids)
        if method == "review":
            args = (
                [
                    {
                        **conflict,
                        **(
                            {
                                side: "[hidden]"
                                for side in ("base", "hub", "other")
                            }
                            if conflict.get("secret")
                            else {}
                        ),
                    }
                    for conflict in args[0]
                ],
            )
        self.sync_gate.release()
        try:
            self.connection.socket.settimeout(self.config.ui["dialog_timeout"])
            self.connection.send(
                {
                    "type": "question",
                    "id": identifier,
                    "method": method,
                    "args": args,
                }
            )
            response = self.connection.receive()
            if (
                set(response) != {"version", "type", "id", "answer"}
                or response.get("type") != "ui-reply"
                or response.get("id") != identifier
            ):
                raise ProtocolError("invalid UI reply")
            answer = response.get("answer")
            if method == "password":
                if answer is None:
                    return None
                if answer != {"cached": True}:
                    raise ProtocolError(
                        "passwords must be supplied through the keyring"
                    )
                provider = CredentialProvider(self.config)
                if len(args) > 1:
                    return provider.cached_copy_password(args[1])
                return provider.cached_password()
            if (
                method == "confirm"
                and answer is not None
                and type(answer) is not bool
            ):
                raise ProtocolError("invalid confirmation reply")
            if (
                method == "choose"
                and answer is not None
                and answer not in args[1]
            ):
                raise ProtocolError("invalid choice reply")
            if method == "file_path" and answer is not None:
                if not isinstance(answer, str):
                    raise ProtocolError("invalid key file path reply")
                return Path(answer)
            if method == "review" and answer is not None:
                ids = {item["id"] for item in args[0]}
                if not isinstance(answer, dict) or any(
                    key not in ids or value not in {"hub", "other", "base"}
                    for key, value in answer.items()
                ):
                    raise ProtocolError("invalid conflict review reply")
            return answer
        except (EOFError, OSError) as exc:
            info(f"terminal decision deferred: {type(exc).__name__}")
            return None
        finally:
            self.connection.socket.settimeout(None)
            self.sync_gate.acquire()

    def confirm(self, prompt, *, default=False):
        """Ask the connected client to confirm an operation."""
        return self._ask("confirm", prompt)

    def credential_lookup(self, lookup):
        """Retrieve locally with the gate released, never over IPC."""
        self.sync_gate.release()
        try:
            return lookup()
        finally:
            self.sync_gate.acquire()

    def choose(self, prompt, options):
        """Ask the client to choose one of the supplied options."""
        return self._ask("choose", prompt, list(options))

    def password(self, prompt, *, slot=None):
        """Ask the client to cache a password, then read it locally."""
        if slot is None:
            return self._ask("password", prompt)
        return self._ask("password", prompt, slot)

    def file_path(self, prompt):
        """Ask the waiting client to choose a file path."""
        return self._ask("file_path", prompt)

    def review(self, conflicts):
        """Request conflict choices after masking protected values."""
        return self._ask("review", conflicts)


class UnlockedUI(UI):
    """Call a local UI backend without holding the sync gate."""

    def __init__(self, backend, gate, progress=None, notifier=None):
        """Wrap a backend and pause progress while asking.

        ``notifier`` (the daemon's log-and-desktop notification) takes
        the engine's ``notify`` messages: no local backend shows them,
        and nobody watches the daemon's log.
        """
        self.backend = backend
        self.gate = gate
        self.progress = progress
        self.notifier = notifier

    def _call(self, method, *args, **kwargs):
        if self.progress is not None:
            with self.progress.paused():
                return self._unlocked_call(method, *args, **kwargs)
        return self._unlocked_call(method, *args, **kwargs)

    def _unlocked_call(self, method, *args, **kwargs):
        self.gate.release()
        try:
            return getattr(self.backend, method)(*args, **kwargs)
        finally:
            self.gate.acquire()

    def confirm(self, prompt, *, default=False):
        """Ask for confirmation without blocking other sync work."""
        return self._call("confirm", prompt, default=default)

    def credential_lookup(self, lookup):
        """Pause progress and release the gate for external approval."""
        return self._call("credential_lookup", lookup)

    def choose(self, prompt, options):
        return self._call("choose", prompt, options)

    def password(self, prompt, *, slot=None):
        if slot is None:
            return self._call("password", prompt)
        return self._call("password", prompt, slot=slot)

    def file_path(self, prompt):
        return self._call("file_path", prompt)

    def review(self, conflicts):
        return self._call("review", conflicts)

    def notify(self, message):
        if self.notifier is not None:
            # Bounded and asks nothing, so the gate stays held.
            return self.notifier(message)
        return self._call("notify", message)


class Controller:
    """Coordinate daemon requests, sync execution, leases, and triggers.

    The controller serializes lifecycle and sync operations.
    ``SyncEngine`` decides copy outcomes; the socket server calls it.
    """

    def __init__(
        self,
        config,
        *,
        engine_factory=None,
        apps=None,
        inhibitor=None,
        clock=time.monotonic,
        wall_clock=time.time,
        drive_resolver=identify_drive,
        rescue=None,
        sessions=None,
    ):
        """Initialize runtime state and injectable daemon adapters.

        Args:
            config: Loaded profile configuration.
            engine_factory: Optional synchronization engine factory.
            apps: Optional application lifecycle adapter.
            inhibitor: Optional shutdown/sleep inhibitor adapter.
            clock: Monotonic time source for scheduling.
            wall_clock: Wall-clock source for persisted lease expiry.
            drive_resolver: Function resolving mount identity.
            rescue: Optional runtime-hub rescue coordinator.
            sessions: Optional session manager.

        Notes:
            Creates runtime state, loads leases, and reconciles drives.
        """
        self.config = config
        self.directory = ensure_run_dir(config.profile)
        self.engine_factory = engine_factory or self._engine
        self.clock = clock
        self.drive_resolver = drive_resolver
        self.apps = apps
        self.inhibitor = inhibitor
        self.rescue = rescue
        self.sessions = sessions or SessionManager(config, self.directory)
        self.session_signatures = {}
        self.closing_sessions = {}
        self.reported_bad_sessions = set()
        self.next_session_scan = 0
        self.next_line_sweep = 0
        self.leases = LeaseStore(
            self.directory,
            access_ttl=config.daemon["access_lease_ttl"],
            clock=wall_clock,
        )
        self.guard = threading.RLock()
        self.sync_gate = threading.Lock()
        self.lifecycle_gate = threading.RLock()
        self.app_gate = threading.RLock()
        self.pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="kp-sync"
        )
        self.requests = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="kp-request"
        )
        self.queued_sync = None
        self.queued_request: dict = {}
        self.inflight = 0
        self.last_written = file_hash(config.hub)
        # Hub hash a failed run left; not re-triggered until it changes.
        self.failed_hub = None
        self.triggers = Triggers(
            debounce=config.daemon["debounce"],
            poll_interval=config.daemon["poll_interval"],
            idle_exit=config.daemon["idle_exit"],
            clock=clock,
        )
        self.watch = HubWatch(config.hub)
        self.credentials = credentials_for(config, self.directory)
        self.credential_state = {}
        # Owned ``rclone rcd`` transports, by endpoint name.
        self.rclone_daemons = {}
        self.stopping = False
        self.last_notification = -float("inf")
        self.pending_error = None
        self.tick_failures = {}
        self.tick_retry_at = {}
        self.tick_last_error = {}
        self.tick_last_log = {}
        self.notified_missing = ()
        self.has_reachable_copy = self.last_written is not None
        self.rescue_needed = False
        self.next_app_scan = 0
        self.next_drive_scan = self.clock()
        self._tick_step("credentials", self._watch_credentials)
        if self._tick_step("drives", self._reconcile_drives)[0]:
            self.next_drive_scan = self.clock() + (
                config.daemon["poll_interval"] or 60
            )

    def _watch_credentials(self):
        """React to provider rclone configs appearing or going.

        A changed file triggers a sync while leases are held; the
        run imports it. A vanished file triggers one draining sync or
        drops the private copy at once when nothing needs it. Another
        process may still run rclone with the copy, so the drain only
        tries the credentials lock and retries on a later tick.
        """
        for handle in self.credentials:
            digest = handle._external_digest()
            previous = self.credential_state.get(handle.name, digest)
            self.credential_state[handle.name] = digest
            busy = self.sync_gate.locked() or self.inflight
            if digest is not None and digest != previous:
                if self.leases.leases:
                    self.triggers.trigger(immediate=True)
            elif digest is None and handle.copy_exists():
                if previous is not None and self.leases.leases:
                    self.triggers.trigger(immediate=True)
                elif not self.leases.leases and not busy:
                    # An rcd holds the copy's lock; it must go first.
                    self._stop_rclone_daemon(handle.name)
                    handle.finish(wait=False)

    def _stop_rclone_daemon(self, name=None):
        """Stop owned rcd transports (all, or one endpoint's)."""
        for key, transport in list(self.rclone_daemons.items()):
            if name is None or key == name:
                try:
                    transport.stop()
                except Exception as exc:
                    error(
                        f"stopping rclone rcd for {key} failed: "
                        f"{type(exc).__name__}: {exc}"
                    )

    def _tick_step(self, name, operation, *, circuit_breaker=True):
        now = self.clock()
        if now < self.tick_retry_at.get(name, 0):
            return False, None
        try:
            result = operation()
        except Exception as exc:
            failures = self.tick_failures.get(name, 0) + 1
            self.tick_failures[name] = failures
            delay = min(
                max(1, self.config.daemon["poll_interval"] or 60),
                2 ** min(failures - 1, 10),
            )
            self.tick_retry_at[name] = now + delay
            signature = f"{type(exc).__name__}: {exc}"
            if (
                self.tick_last_error.get(name) != signature
                or now - self.tick_last_log.get(name, -float("inf")) >= 60
            ):
                error(
                    f"daemon tick {name} failed; retrying in {delay}s: "
                    f"{signature}"
                )
                self.tick_last_error[name] = signature
                self.tick_last_log[name] = now
            if circuit_breaker and failures >= 5:
                error(
                    f"daemon tick {name} failed {failures} consecutive times; "
                    "stopping"
                )
                raise RuntimeError(
                    f"daemon tick {name} failed {failures} consecutive times"
                ) from exc
            return False, None
        self.tick_failures.pop(name, None)
        self.tick_retry_at.pop(name, None)
        self.tick_last_error.pop(name, None)
        self.tick_last_log.pop(name, None)
        return True, result

    @classmethod
    def production(cls, config):
        """Create a controller with the production system adapters."""
        from kp_sync.daemon.apps import AppManager
        from kp_sync.daemon.inhibit import LogindInhibitor
        from kp_sync.daemon.rescue import RescueCoordinator

        directory = ensure_run_dir(config.profile)
        return cls(
            config,
            apps=AppManager(config),
            inhibitor=LogindInhibitor(),
            rescue=RescueCoordinator(config, directory),
        )

    def _reconcile_drives(self):
        drives = list_drives(
            self.directory, identity_resolver=self.drive_resolver
        )
        names = {f"drive:{drive.identity}" for drive in drives}
        for name, lease in list(self.leases.leases.items()):
            if lease.kind == "drive" and name not in names:
                self.leases.release(name)
        for name in names:
            self.leases.take(name, "drive")

    def _engine(self, ui):
        endpoints = list(
            endpoints_from_config(
                self.config,
                self.directory,
                rclone_daemons=self.rclone_daemons,
            )
        )
        with self.guard:
            access_active = ACCESS_LEASE in self.leases.leases
        if access_active:
            from kp_sync.endpoints.access import AccessEndpoint

            _, database, _ = access_paths(self.config, self.directory)
            if database.is_file() and not database.is_symlink():
                identity = access_identity(self.directory, create=False)
                endpoints.append(
                    AccessEndpoint(database, identity or ACCESS_IDENTITY)
                )
        engine = SyncEngine(
            self.config,
            endpoints,
            run_directory=self.directory,
            ui=ui,
        )
        if self.apps is not None:
            with self.app_gate:
                engine.hub.foreign_writers = self.apps.foreign_writers
        return engine

    def _ui(self, request, connection, progress=None):
        mode = request.get("ui", self.config.ui["mode"])
        if (
            mode == "tty"
            and request.get("wait", True)
            and connection is not None
        ):
            return RemoteUI(connection, self.config, self.sync_gate)
        if mode == "tty":
            mode = "none"
        if mode == "auto":
            mode = (
                "gui"
                if os.environ.get("DISPLAY")
                or os.environ.get("WAYLAND_DISPLAY")
                else "none"
            )
        return UnlockedUI(
            create_ui(
                mode,
                timeout=self.config.ui["dialog_timeout"],
                gui=self.config.ui["gui"],
            ),
            self.sync_gate,
            progress,
            notifier=self._notify,
        )

    def sync(self, request=None, connection=None, *, queued=None):
        """Run sync or restore under the serialized sync gate.

        Args:
            request: Validated daemon request, or an empty sync request.
            connection: Waiting client for remote UI questions.
            queued: Future identifying the coalesced run, if any.

        Returns:
            JSON-compatible engine outcome.

        Raises:
            RuntimeError: If pending state is invalid or sync fails.

        Notes:
            Updates hub tracking, session ancestry, and triggers after a
            non-dry-run operation.
        """
        request = request or {}
        try:
            PendingStore(self.directory).load()
        except PendingError as exc:
            self.pending_error = str(exc)
            self.leases.take("pending", "pending")
            raise RuntimeError(
                "sync is disabled because pending decisions are invalid: "
                f"{exc}"
            ) from exc
        self.pending_error = None
        with self.sync_gate:
            with self.guard:
                if queued is not None and self.queued_sync is queued:
                    self.queued_sync = None
                self.inflight += 1
                self.triggers.due = None
            try:
                mode = request.get("ui", self.config.ui["mode"])
                progress = SyncProgress(
                    enabled=bool(
                        self.config.ui["progress"]
                        and self.config.ui["gui"] == "zenity"
                        and mode in {"auto", "gui"}
                        and (
                            os.environ.get("DISPLAY")
                            or os.environ.get("WAYLAND_DISPLAY")
                        )
                    ),
                    database_info=(
                        f"Profile: {self.config.profile or 'default'} | "
                        f"Database: {self.config.hub}"
                    ),
                )
                ui = self._ui(request, connection, progress)
                engine = self.engine_factory(ui)
                before = file_hash(self.config.hub)
                try:
                    with progress:
                        if request.get("command") == "restore":
                            outcome = engine.restore(
                                request["source_kind"],
                                request["source_value"],
                                mode=request.get("restore_mode", "merge"),
                                dry_run=request.get("dry_run", False),
                            )
                        else:
                            outcome = engine.sync(
                                dry_run=request.get("dry_run", False)
                            )
                except Exception:
                    if not request.get("dry_run", False):
                        with self.guard:
                            # Drop this run's own hub events; a new save
                            # or hash still triggers, polls still retry.
                            self.watch.read()
                            self.failed_hub = file_hash(self.config.hub)
                    raise
                after = file_hash(self.config.hub)
                if not request.get("dry_run", False):
                    with self.guard:
                        self.failed_hub = None
                        self.last_written = (
                            outcome.current
                            if outcome.action in {"committed", "unchanged"}
                            else before
                        )
                        self.watch.read()
                        self._snapshot_sessions(outcome)
                        if after != self.last_written:
                            self.triggers.trigger()
                        if before != after and self.apps is not None:
                            with self.app_gate:
                                self.apps.hub_updated()
                        if self.rescue is not None:
                            self.rescue_needed = self.rescue.update()
                        self.watch.read()
                        self.has_reachable_copy = after is not None or any(
                            state != "missing"
                            for state in outcome.classifications.values()
                        )
                    missing = (
                        tuple(sorted(outcome.missing))
                        if self.has_reachable_copy
                        else ()
                    )
                    if missing and missing != self.notified_missing:
                        self._notify(
                            "KeePass endpoints unavailable: "
                            + ", ".join(missing)
                        )
                    self.notified_missing = missing
                    try:
                        self._warn_stale_cloud(outcome)
                    except Exception as exc:
                        error(f"cloud staleness check failed: {exc}")
                return outcome.to_dict()
            finally:
                with self.guard:
                    self.inflight -= 1
                    self.triggers.idle_since = self.clock()

    def queue_sync(self, request=None, connection=None):
        """Queue sync and coalesce ordinary requests into one follow-up.

        Args:
            request: Optional sync, restore, or dry-run request.
            connection: Waiting client for terminal UI questions.

        Returns:
            Future resolving to the engine outcome.
        """
        if request and (
            request.get("dry_run")
            or request.get("ui") == "tty"
            or request.get("command") == "restore"
        ):
            return self.pool.submit(self.sync, request, connection)
        request = dict(request or {})
        with self.guard:
            if self.queued_sync is not None:
                queued = self.queued_request
                # A waiting desktop client must not inherit a background
                # request's "none" UI, or questions would be deferred.
                if queued.get("ui") == "none" and request.get("ui") != "none":
                    queued.pop("ui", None)
                    if "ui" in request:
                        queued["ui"] = request["ui"]
                joined = self.queued_sync
            else:
                joined = None
                future = concurrent.futures.Future()
                self.queued_sync = future
                self.queued_request = request
        if joined is not None:
            debug("sync request joined the already queued run")
            return joined

        def execute():
            try:
                future.set_result(
                    self.sync(request, connection, queued=future)
                )
            except Exception as exc:
                error(f"daemon sync failed: {exc}")
                future.set_exception(exc)

        task = self.pool.submit(execute)
        debug(
            "sync queued"
            + (f" for {request['command']}" if "command" in request else "")
        )

        def cancelled(done):
            if done.cancelled():
                future.cancel()

        task.add_done_callback(cancelled)
        return future

    def disconnect(self, owner):
        """Release connection-owned leases and schedule cleanup."""
        with self.guard:
            needs_sync = any(
                lease.owner == owner and lease.kind != "session"
                for lease in self.leases.leases.values()
            )
            self.leases.disconnect(owner)
        if needs_sync and not self.stopping:
            self.queue_sync({"ui": "none"})

    def status(self):
        """Return cached status, leases, sessions, and rescue flag."""
        result = (
            SyncEngine(
                self.config, (), run_directory=self.directory, ui=NoneUI()
            )
            .status()
            .to_dict()
        )
        with self.guard:
            result["leases"] = self.leases.to_dict()
            access_active = ACCESS_LEASE in self.leases.leases
        if access_active:
            identity = (
                access_identity(self.directory, create=False)
                or ACCESS_IDENTITY
            )
            result["sessions"].setdefault(
                identity,
                {
                    "identity": identity,
                    "name": "kp-access",
                    "state": "not yet synchronized",
                },
            )["lifecycle"] = "live"
        result["rescue_needed"] = self.rescue_needed
        return result

    def _desktop_ui(self):
        return create_ui(
            (
                "gui"
                if os.environ.get("DISPLAY")
                or os.environ.get("WAYLAND_DISPLAY")
                else "none"
            ),
            timeout=self.config.ui["dialog_timeout"],
            gui=self.config.ui["gui"],
        )

    @staticmethod
    def _cloud_launch_message(offline, *, late=False):
        """The launch warning; ``late`` once the application is open."""
        clouds = "; ".join(f"{identity}: {text}" for identity, text in offline)
        if late:
            opening = (
                f"The cloud could not be reached ({clouds}). KeePassXC is "
                "open on this machine's copy"
            )
        else:
            opening = (
                f"The cloud cannot be reached ({clouds}). KeePassXC opens "
                "this machine's copy"
            )
        return (
            opening + ", which may be missing changes made elsewhere; your "
            "changes stay here until the cloud is reached again."
        )

    def _late_cloud_warning(self, future):
        """After a launch that outran its pre-sync: warn if offline.

        Runs in the thread that completed the sync, so the dialog gets
        its own thread and never holds the sync slot.
        """
        try:
            if future.cancelled() or future.exception() is not None:
                return  # failures are notified as usual
            offline = self._offline_clouds(future.result())
        except Exception as exc:
            error(f"late cloud warning failed: {exc}")
            return
        if offline:
            threading.Thread(
                target=self._warn_quietly,
                args=(self._cloud_launch_message(offline, late=True),),
                daemon=True,
            ).start()

    def _warn_quietly(self, message):
        try:
            self._desktop_ui().warn(message)
        except Exception as exc:  # a dialog must never break a launch
            error(f"cloud warning dialog failed: {exc}")
            self._notify(message)

    def _offline_clouds(self, outcome):
        """Unreachable clouds in an outcome, with contact phrases."""
        from kp_sync import cloud_contact
        from kp_sync.state import HistoryState

        missing = set(outcome.get("missing") or ())
        identities = [
            identity
            for identity in cloud_contact.cloud_identities(self.config)
            if identity in missing
        ]
        if not identities:
            return []
        try:
            copies = HistoryState.load(self.directory / "state.json").copies
        except Exception:
            copies = {}
        contacts = cloud_contact.last_contact(
            self.directory, identities, copies
        )
        return [
            (identity, cloud_contact.describe(contacts[identity]))
            for identity in identities
        ]

    def _warn_stale_cloud(self, outcome):
        """Notify when hub changes waited too long for the cloud."""
        from kp_sync import cloud_contact

        hours = self.config.daemon["cloud_warning_hours"]
        offline = self._offline_clouds(outcome.to_dict())
        if not hours or not offline:
            return
        limit = hours * 3600
        status = SyncEngine(
            self.config, (), run_directory=self.directory, ui=NoneUI()
        ).status()
        if not status.summary.get("hub_unsynced"):
            return
        waited = [
            cloud_contact.seconds_since(
                contact.get("reached_at") or contact.get("synced_at")
            )
            for contact in status.cloud.values()
        ]
        if not waited or any(
            seconds is not None and seconds < limit for seconds in waited
        ):
            return
        if not cloud_contact.warning_due(self.directory, limit):
            return
        self._notify(
            "KeePass changes on this machine have not reached the cloud ("
            + "; ".join(f"{identity}: {text}" for identity, text in offline)
            + "). Connect to the network, or plug a key drive to keep a "
            "copy."
        )
        cloud_contact.record_warning(self.directory)

    def _notify(self, message):
        info(message)
        if self.config.ui["notify"]:
            from kp_sync.daemon.apps import NotificationError, notify_user

            try:
                notify_user(message)
            except NotificationError as exc:
                error(f"desktop notification failed: {exc}")

    def handle(self, request, connection=None, owner=None):
        """Route one validated request to lifecycle or sync handling.

        Args:
            request: Validated protocol request.
            connection: Optional waiting client connection.
            owner: Identifier used for connection-bound leases.

        Returns:
            Operation result or sync outcome.

        Raises:
            ProtocolError: If a request cannot be serviced as specified.
        """
        # Field names only: values may name paths or hold user input.
        debug(
            f"request {request['command']} "
            f"({', '.join(sorted(set(request) - {'command'})) or 'no fields'})"
        )
        if request["command"] in {
            "attach",
            "detach",
            "up",
            "down",
            "eject",
            "run",
            "access-op",
            "restore",
        }:
            with self.lifecycle_gate:
                return self._handle(request, connection, owner)
        return self._handle(request, connection, owner)

    def _handle(self, request, connection=None, owner=None):
        command = request["command"]
        if command == "status":
            return self.status()
        if command == "access-op":
            return self._access_operation(request, connection)
        if command in {"sync", "resolve", "restore"}:
            return self.queue_sync(request, connection).result()
        if command in {"lease", "release"}:
            kind = request.get("kind", "client")
            if kind not in {"client", "session", "access"}:
                raise ProtocolError("invalid client lease kind")
            name = request.get("name")
            if not name:
                raise ProtocolError("lease name is required")
            if not name.startswith(f"{kind}:"):
                raise ProtocolError("lease name must use its kind prefix")
            if kind != "access" and not request.get("wait", True):
                raise ProtocolError(
                    "connection-bound leases require a persistent connection"
                )
            with self.guard:
                if command == "lease":
                    self.leases.take(
                        name, kind, owner=None if kind == "access" else owner
                    )
                else:
                    self.leases.release(
                        name, owner=None if kind == "access" else owner
                    )
            if command == "lease" and kind == "session":
                return {"action": "lease"}
            return self.queue_sync(request, connection).result()
        if command in {"up", "down"} and self.config.hub_persistent:
            # Main-mode drive events start and quit applications, which
            # a machine with a persistent hub normally does not want.
            info(
                f"WARNING: {command} on a profile with a persistent hub "
                f"{'starts' if command == 'up' else 'may quit'} "
                "KeePassXC; drive hooks for this machine should use "
                "drive-sync --mode secondary (attach/detach)"
            )
        if command in {"attach", "up"} and request.get("mountpoint"):
            mount = Path(request["mountpoint"]).expanduser().resolve()
            actual = self.drive_resolver(mount)
            identity = request.get("drive_id", actual)
            if identity != actual:
                raise ValueError(
                    "drive identity does not match the mounted filesystem"
                )
            with self.guard:
                drive = register_drive(self.directory, mount, identity)
                self.leases.take(f"drive:{drive.identity}", "drive")
            with file_lock(
                ensure_run_dir() / "keydisk.lock",
                self.config.sync["lock_timeout"],
            ):
                marker = pending_attach_path(self.config.profile, identity)
                marker.unlink(missing_ok=True)
                _fsync_directory(marker.parent)
        elif command == "attach":
            raise ProtocolError("attach requires a mountpoint")
        if command in {"detach", "down", "eject"}:
            self._detach(request, eject=command == "eject")
            if command in {"down", "eject"}:
                return self._down(request, connection)
            return self.queue_sync(request, connection).result()
        if command in {"run", "up"}:
            return self._run(request, connection)
        return self.queue_sync(request, connection).result()

    def _access_operation(self, request, connection):
        operation = request["operation"]
        if request.get("db_path") != self.config.access.get("db_path"):
            raise ProtocolError(
                "DB_PATH does not match the configured access database path"
            )
        if operation in {"PUT", "DELETE", "MOVE"} and not request.get(
            "identified_user"
        ):
            raise ProtocolError(
                "file operation requires an authenticated user"
            )
        file_path = request.get("file_path", "")
        destination = request.get("destination", "")
        validate_operation(
            self.config,
            self.directory,
            operation,
            file_path,
            destination,
        )
        if operation == "BEGIN":
            return self._begin_access(request, connection)
        if operation == "END":
            return self._end_access(request, connection)

        _, database, _ = access_paths(self.config, self.directory)
        if operation == "GET":
            with file_lock(
                self.directory / "access-op.lock",
                self.config.sync["lock_timeout"],
            ):
                if in_transit(self.directory, now=self.leases.clock()):
                    raise AccessOperationError(
                        "staged database upload is still in transit"
                    )
                if not database.is_file() or database.is_symlink():
                    raise AccessOperationError("staged database is missing")
                self._activate_access(
                    request, connection, refill_missing=False
                )
                self._refresh_stale_access_copy(request, connection, database)
            return {"action": "access-get"}

        with file_lock(
            self.directory / "access-op.lock",
            self.config.sync["lock_timeout"],
        ):
            self._activate_access(request, connection, refill_missing=False)
            access_sync, _ = apply_file_operation(
                self.config,
                self.directory,
                operation=operation,
                file_path=file_path,
                destination=destination,
                upload_id=request.get("upload_id"),
            )
            if (
                operation == "DELETE"
                and file_path == self.config.access["db_path"]
            ):
                mark_transit(self.directory, now=self.leases.clock())
            elif (
                operation == "PUT"
                and file_path == self.config.access["db_path"]
            ):
                clear_transit(self.directory)
            elif operation == "MOVE":
                if destination == self.config.access["db_path"]:
                    clear_transit(self.directory)
                elif file_path == self.config.access["db_path"]:
                    mark_transit(self.directory, now=self.leases.clock())
        if access_sync:
            self.queue_sync(request, connection)
        return {"action": f"access-{operation.lower()}"}

    def _begin_access(self, request, connection):
        with file_lock(
            self.directory / "access-op.lock",
            self.config.sync["lock_timeout"],
        ):
            return self._begin_access_locked(request, connection)

    def _begin_access_locked(self, request, connection):
        self._prepare_access_lifecycle()
        _, database, _ = access_paths(self.config, self.directory)
        if database.is_symlink():
            raise AccessOperationError(
                "staged database must not be a symbolic link"
            )
        created = not database.exists()
        if created:
            self.queue_sync(request, connection).result()
            if not self.config.hub.is_file() or self.config.hub.is_symlink():
                raise AccessOperationError(
                    "no database copy is available for access staging"
                )
            atomic_copy(self.config.hub, database)
        with self.guard:
            new_lease = ACCESS_LEASE not in self.leases.leases
            self.leases.take(ACCESS_LEASE, "access")
            self.triggers.leases_changed(True)
        if created or new_lease:
            outcome = self.queue_sync(request, connection).result()
            if (
                outcome.get("pending")
                or outcome.get("missing")
                or outcome.get("action") == "unavailable"
            ):
                with self.guard:
                    self.leases.release(ACCESS_LEASE)
                    self.triggers.leases_changed(bool(self.leases.leases))
                raise AccessOperationError(
                    "kp-access could not start because its pre-sync is "
                    "pending or incomplete"
                )
        return {"action": "access-begin"}

    def _activate_access(self, request, connection, *, refill_missing=True):
        self._prepare_access_lifecycle()
        _, database, _ = access_paths(self.config, self.directory)
        if database.is_symlink():
            raise AccessOperationError(
                "staged database must not be a symbolic link"
            )
        created = not database.exists()
        if created and refill_missing:
            self.queue_sync(request, connection).result()
            if not self.config.hub.is_file() or self.config.hub.is_symlink():
                raise AccessOperationError(
                    "no database copy is available for access staging"
                )
            atomic_copy(self.config.hub, database)
        with self.guard:
            new_lease = ACCESS_LEASE not in self.leases.leases
            self.leases.take(ACCESS_LEASE, "access")
            self.triggers.leases_changed(True)
        if created or new_lease:
            outcome = self.queue_sync(request, connection).result()
            if (
                outcome.get("pending")
                or outcome.get("missing")
                or outcome.get("action") == "unavailable"
            ):
                raise AccessOperationError(
                    "kp-access pre-sync is pending or incomplete; staged data "
                    "was retained"
                )

    def _prepare_access_lifecycle(self):
        identity = access_identity(self.directory, create=False)
        staging = _access_staging_directory(self.config, self.directory)
        for (
            pending_identity,
            owner_id,
            _,
        ) in self.sessions.store.pending_retirements():
            if owner_id != "access":
                continue
            if pending_identity == identity or (
                identity is None and staging.exists()
            ):
                raise AccessOperationError(
                    "kp-access cleanup is pending; retry after daemon recovery"
                )
        return access_identity(self.directory)

    def _end_access(self, request, connection):
        with file_lock(
            self.directory / "access-op.lock",
            self.config.sync["lock_timeout"],
        ):
            return self._finish_access(request, connection)

    def _finish_access(self, request, connection):
        staging_path = _access_staging_directory(self.config, self.directory)
        identity = (
            access_identity(self.directory, create=False) or ACCESS_IDENTITY
        )
        with self.guard:
            access_active = ACCESS_LEASE in self.leases.leases
        if not access_active and not staging_path.exists():
            return {"action": "access-end"}
        pending = {
            entry_identity: owner_id
            for entry_identity, owner_id, _ in (
                self.sessions.store.pending_retirements()
            )
        }
        if pending.get(identity) == "access":
            with file_lock(
                self.directory / "lock",
                self.config.sync["lock_timeout"],
            ):
                self._complete_access_cleanup(identity, staging_path)
            with self.guard:
                self.leases.release(ACCESS_LEASE)
                self.triggers.leases_changed(bool(self.leases.leases))
            return {"action": "access-end"}
        staging, database, _ = access_paths(self.config, self.directory)
        if staging.is_symlink():
            raise AccessOperationError(
                "refusing to remove unsafe staging directory"
            )
        if any(path != database for path in staging.iterdir()):
            raise AccessOperationError(
                "kp-access has unfinished uploads; staged files were retained"
            )
        if in_transit(self.directory, now=self.leases.clock()):
            raise AccessOperationError(
                "kp-access has an unfinished database save; staged files "
                "were retained"
            )
        with self.guard:
            self.leases.take(ACCESS_LEASE, "access")
            self.triggers.leases_changed(True)
        outcome = self.queue_sync(request, connection).result()
        clean = (
            not outcome.get("pending")
            and not outcome.get("missing")
            and outcome.get("action") != "unavailable"
        )
        if clean:
            with file_lock(
                self.directory / "lock",
                self.config.sync["lock_timeout"],
            ):
                self.sessions.store.prepare_retirement(identity, "access")
                self._complete_access_cleanup(identity, staging)
        with self.guard:
            self.leases.release(ACCESS_LEASE)
            self.triggers.leases_changed(bool(self.leases.leases))
        if not clean:
            raise AccessOperationError(
                "kp-access post-sync is pending; staged files were retained"
            )
        return {"action": "access-end"}

    def _complete_access_cleanup(self, identity, staging):
        current = access_identity(self.directory, create=False)
        if current not in {None, identity}:
            return
        if staging.is_symlink():
            raise AccessOperationError(
                "refusing to remove unsafe staging directory"
            )
        if staging.exists():
            shutil.rmtree(staging)
            _fsync_directory(staging.parent)
        lifecycle_path = self.directory / "access-lifecycle.json"
        if current == identity:
            lifecycle_path.unlink(missing_ok=True)
            _fsync_directory(lifecycle_path.parent)
        if current in {None, identity}:
            clear_transit(self.directory)

    def _refresh_stale_access_copy(self, request, connection, database):
        from kp_sync.history import current_version, is_ancestor
        from kp_sync.state import HistoryState

        try:
            state = HistoryState.load(self.directory / "state.json")
        except (OSError, ValueError):
            return
        identity = (
            access_identity(self.directory, create=False) or ACCESS_IDENTITY
        )
        record = state.copies.get(identity)
        if record is None or database_hash(database) != record.sha256:
            return
        current = current_version(state.versions, state.copies)
        if (
            current is None
            or record.version == current
            or not is_ancestor(record.version, current, state.versions)
        ):
            return
        future = self.queue_sync(request, connection)
        try:
            future.result(timeout=3)
        except concurrent.futures.TimeoutError:
            info(
                "kp-access GET serving the current staged copy while refresh "
                "continues"
            )

    def _detach(self, request, *, eject=False):
        drives = list_drives(
            self.directory,
            remove_stale=False,
            identity_resolver=self.drive_resolver,
        )
        identity = request.get("drive_id")
        if identity is None and request.get("mountpoint"):
            mount = Path(request["mountpoint"]).expanduser().resolve()
            identity = next(
                (item.identity for item in drives if item.mountpoint == mount),
                None,
            )
        if (
            identity is None
            and not request.get("mountpoint")
            and len(drives) == 1
        ):
            identity = drives[0].identity
        if identity is None:
            raise ProtocolError(
                "detach/down/eject requires a drive identity or mountpoint"
            )
        if eject:
            outcome = self.queue_sync({"ui": "none"}).result()
            if (
                outcome.get("pending")
                or outcome.get("missing")
                or outcome.get("action") == "unavailable"
            ):
                raise RuntimeError(
                    "eject refused: final sync is pending or endpoints are "
                    "unreachable"
                )
            drive = next(
                (item for item in drives if item.identity == identity), None
            )
            if drive is None:
                raise RuntimeError("cannot eject a drive that is not attached")
            import subprocess

            logged(subprocess.run)(
                ["umount", "--", str(drive.mountpoint)], check=True, timeout=30
            )
        with self.guard:
            detach_drive(self.directory, identity)
            self.leases.release(f"drive:{identity}")

    def _run(self, request, connection):
        if self.apps is None:
            raise RuntimeError("application manager is unavailable")
        name = request.get("app", self.config.launch["default_app"])
        if request["command"] == "run":
            with self.app_gate:
                existing = self.apps.activate_existing(
                    name,
                    request.get("args", []),
                    interactive=request.get("interactive", False),
                    environment=request.get("environment", {}),
                )
            if existing is not None:
                self._refresh_apps(force=True)
                return {"action": "activated existing application"}
        # A bounded prestart cannot leave a terminal question using the
        # reply stream.
        mode = request.get("ui", "auto")
        future = self.queue_sync(
            {**request, "ui": "auto" if mode == "tty" else mode}, None
        )
        try:
            outcome = future.result(
                timeout=self.config.daemon["prestart_timeout"]
            )
        except concurrent.futures.TimeoutError:
            if self.config.hub.is_file():
                outcome = {"action": "pre-sync still running"}
                info(
                    "pre-sync timed out; launching existing hub while "
                    "synchronization continues"
                )
                # Warn late if it ends without the cloud.
                future.add_done_callback(self._late_cloud_warning)
            else:
                info(
                    "pre-sync timed out with no hub; waiting for "
                    "synchronization before launching"
                )
                outcome = future.result()
        if not self.config.hub.is_file():
            raise RuntimeError(
                "no database copy available; insert a pendrive or check "
                "the cloud"
            )
        if outcome.get("missing"):
            info(
                "starting application without all endpoints; hub may be "
                "outdated"
            )
        offline = self._offline_clouds(outcome)
        if offline:
            message = self._cloud_launch_message(offline)
            if self.config.launch["confirm_without_cloud"]:
                if (
                    self._desktop_ui().confirm(
                        message + " Launch the existing database?"
                    )
                    is False
                ):
                    return {**outcome, "action": "launch cancelled"}
            else:
                # Shown beside the application, never delaying it.
                threading.Thread(
                    target=self._warn_quietly,
                    args=(message,),
                    daemon=True,
                ).start()
        with self.app_gate:
            self.apps.start(
                name,
                request.get("args", []),
                interactive=request.get("interactive", False),
                environment=request.get("environment", {}),
            )
        self._refresh_apps(force=True)
        return outcome

    def _down(self, request, connection):
        with self.guard:
            remaining = any(
                lease.kind == "drive" for lease in self.leases.leases.values()
            )
        if remaining or not self.config.launch["quit_on_last_drive"]:
            return self.queue_sync(request, connection).result()
        if self.apps is not None:
            with self.app_gate:
                self.apps.stop_started()
            self._refresh_apps(force=True)
        outcome = self.queue_sync(request, connection).result()
        if self.config.hub_persistent:
            return outcome
        from kp_sync.daemon.rescue import hub_is_safe_to_remove

        with (
            file_lock(
                ensure_run_dir() / "keydisk.lock",
                self.config.sync["lock_timeout"],
            ),
            self.sync_gate,
            file_lock(
                self.directory / "lock",
                self.config.sync["lock_timeout"],
            ),
            self.guard,
        ):
            if any(ensure_run_dir().glob("keydisk-up-*.pending")):
                info(
                    "runtime copies retained while a drive attachment is "
                    "pending"
                )
                return outcome
            if (
                PendingStore(self.directory).load()["decisions"]
                or (self.directory / "write-race.json").exists()
            ):
                info("pending decisions retained with their runtime copies")
                return outcome
            expected_hub = file_hash(self.config.hub)
            if not hub_is_safe_to_remove(self.config, self.directory):
                info("unsynced hub retained: insert a pendrive to save it")
                outcome["rescue_needed"] = True
                return outcome
            if (
                self.leases.leases
                or self.queued_sync is not None
                or self.inflight
            ):
                # A paused sync released its slot and engine flock.
                info(
                    "hub retained while applications, clients or a paused "
                    "sync still need it"
                )
                return outcome
            if not self._remove_hub(expected_hub):
                outcome["rescue_needed"] = True
                return outcome
            self.last_written = None
            self._cleanup_runtime()
        return outcome

    def _remove_hub(self, expected):
        if expected is None:
            return not self.config.hub.exists()
        directory = ensure_private_directory(self.directory / "backups")
        displaced = (
            directory / f"hub-down-{time.time_ns()}-{uuid.uuid4().hex}.kdbx"
        )
        _rename_noreplace(self.config.hub, displaced)
        _fsync_directory(self.config.hub.parent)
        _fsync_directory(directory)
        if displaced.is_symlink() or file_hash(displaced) != expected:
            try:
                _rename_noreplace(displaced, self.config.hub)
                _fsync_directory(self.config.hub.parent)
            except EndpointChanged:
                PendingStore(self.directory).save(
                    "down-race",
                    "hub changed during down; both saves were preserved",
                    {"backup": displaced.name},
                )
            info("hub changed during down; runtime copies retained")
            return False
        displaced.unlink()
        _fsync_directory(directory)
        if self.config.hub.exists():
            info(
                "hub was recreated during down; new save and runtime copies "
                "retained"
            )
            return False
        return True

    def _cleanup_runtime(self):
        for name in (
            "state.json",
            "last-sync.json",
            "pending.json",
            "choices.json",
        ):
            path = self.directory / name
            if path.is_symlink():
                raise RuntimeError(
                    f"refusing to clean a symbolic link: {path}"
                )
            path.unlink(missing_ok=True)
        for name in ("bases", "backups", "tmp"):
            directory = self.directory / name
            if not directory.exists():
                continue
            if directory.is_symlink():
                raise RuntimeError(
                    f"refusing to clean a symbolic link: {directory}"
                )
            for path in directory.iterdir():
                if path.is_file() and not path.is_symlink():
                    path.unlink()
            if not any(directory.iterdir()):
                directory.rmdir()
        self._stop_rclone_daemon()
        for handle in self.credentials:
            handle.discard()

    def _refresh_apps(self, *, force=False):
        if self.apps is None:
            return
        now = self.clock()
        if not force and now < self.next_app_scan:
            return
        self.next_app_scan = now + 2
        with self.app_gate:
            active = set(self.apps.refresh(force=force))
        with self.guard:
            previous = {
                name
                for name, lease in self.leases.leases.items()
                if lease.kind == "app"
            }
            for name in previous - active:
                self.leases.release(name)
            for name in active:
                self.leases.take(name, "app")
        if previous - active:
            self.queue_sync({"ui": "none"})

    def _session_records(self):
        def report(session_id, exc):
            if session_id not in self.reported_bad_sessions:
                self.reported_bad_sessions.add(session_id)
                error(f"ignoring session {session_id}: {exc}")

        return list(self.sessions.store.records(on_error=report))

    def _session_signature(self, record):
        return file_hash(
            self.sessions.store.staging_path(record.id) / self.config.db_name
        )

    def _snapshot_sessions(self, outcome):
        for record in self._session_records():
            if (
                outcome.action in {"committed", "unchanged"}
                and record.identity in outcome.classifications
                and record.identity not in outcome.missing
            ):
                self.session_signatures[record.id] = outcome.current

    def _refresh_sessions(self, *, force=False):
        now = self.clock()
        if not force and now < self.next_session_scan:
            return
        self.next_session_scan = now + 2
        records = self._session_records()
        enabled = self.config.sessions["enabled"] or bool(records)
        if enabled and (force or now >= self.next_line_sweep):
            self.next_line_sweep = now + 60
            try:
                dropped = self.sessions.drop_stale_lines()
            except (OSError, RuntimeError) as exc:
                error(f"cannot clean up session keys: {exc}")
            else:
                if dropped:
                    info(
                        "dropped stale session key lines: "
                        f"{', '.join(dropped)}"
                    )
        with self.guard:
            names = {f"session-proc:{record.id}" for record in records}
            for name, lease in list(self.leases.leases.items()):
                if name.startswith("session-proc:") and name not in names:
                    self.leases.release(name)
            for name in names:
                self.leases.take(name, "session")
            current = {record.id for record in records}
            for session_id in set(self.session_signatures) - current:
                del self.session_signatures[session_id]
            for record in records:
                if not record.owner_alive() or self.sync_gate.locked():
                    continue
                signature = self._session_signature(record)
                previous = self.session_signatures.setdefault(
                    record.id, signature
                )
                if signature != previous:
                    self.session_signatures[record.id] = signature
                    self.triggers.trigger()
        for record in records:
            if record.owner_alive() or self.stopping:
                continue
            retry = self.closing_sessions.get(record.id)
            if retry is not None and (retry is True or now < retry):
                continue
            self.closing_sessions[record.id] = True
            self.requests.submit(self._close_dead_session, record)
        for session_id in set(self.closing_sessions) - {
            record.id for record in records
        }:
            del self.closing_sessions[session_id]
        self._process_session_retirements()

    def _process_session_retirements(self):
        """Finish cleanup after staged data was safely removed."""
        from kp_sync.state import HistoryState

        store = self.sessions.store
        try:
            pending = store.pending_retirements()
        except Exception as exc:
            error(f"cannot read session retirement progress: {exc}")
            return
        for identity, owner_id, progress_path in pending:
            if owner_id == "access":
                try:
                    with file_lock(
                        self.directory / "access-op.lock",
                        self.config.sync["lock_timeout"],
                    ):
                        with file_lock(
                            self.directory / "lock",
                            self.config.sync["lock_timeout"],
                        ):
                            self._complete_access_cleanup(
                                identity,
                                _access_staging_directory(
                                    self.config, self.directory
                                ),
                            )
                except Exception as exc:
                    error(
                        f"access cleanup for {identity} is pending: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    continue
            else:
                try:
                    session_id = owner_id
                    if store.record_path(session_id).exists():
                        record = store.load(session_id)
                        if record.identity != identity:
                            raise RuntimeError(
                                "cleanup progress no longer matches session"
                            )
                    store.remove_files(session_id)
                except Exception as exc:
                    error(f"cleanup of {identity} is pending: {exc}")
                    continue
            try:
                result = self._engine(None).retire_session(identity)
                state = HistoryState.load(self.directory / "state.json")
                if identity not in state.retirements:
                    raise RuntimeError(
                        "runtime history did not retain the retirement"
                    )
                progress_path.unlink()
                _fsync_directory(progress_path.parent)
            except Exception as exc:
                error(
                    f"retirement of {identity} is pending: "
                    f"{type(exc).__name__}: {exc}"
                )
                continue
            stores = result["shared_stores"]
            if result["publication_error"]:
                error(
                    f"{identity} retired in runtime history; shared "
                    f"publication failed: {result['publication_error']}"
                )
            elif not stores or all(
                value == "unavailable" for value in stores.values()
            ):
                info(
                    f"{identity} retired in runtime history; shared "
                    "publication is awaiting an available store"
                )
            else:
                info(
                    f"{identity} retired in runtime history; shared "
                    f"outcomes: {stores}"
                )

    def _close_dead_session(self, record):
        retry_at = self.clock() + 60
        try:
            info(f"closing session {record.id} whose client is gone")
            self.sessions.revoke(record)
            outcome = self.queue_sync({"ui": "none"}).result()
            if _outcome_clean(outcome, record):
                self.sessions.discard(record)
                retry_at = None
            else:
                error(
                    f"session {record.id} kept: its post-sync did not "
                    "complete; will retry"
                )
        except Exception as exc:
            error(
                f"closing session {record.id} failed: "
                f"{type(exc).__name__}: {exc}"
            )
        finally:
            with self.guard:
                if retry_at is None:
                    self.closing_sessions.pop(record.id, None)
                else:
                    self.closing_sessions[record.id] = retry_at

    def tick(self):
        """Refresh daemon state and due work; report whether to exit.

        Returns:
            ``True`` when no leases or work remain past idle timeout.

        Notes:
            Polls applications, sessions, drives, pending state, and hub
            events. Queues sync when a trigger becomes ready.
        """
        self._tick_step("apps", self._refresh_apps)
        self._tick_step("sessions", self._refresh_sessions)
        if self.clock() >= self.next_drive_scan:

            def reconcile():
                with self.guard:
                    previous_drives = {
                        name
                        for name, lease in self.leases.leases.items()
                        if lease.kind == "drive"
                    }
                    self._reconcile_drives()
                    current_drives = {
                        name
                        for name, lease in self.leases.leases.items()
                        if lease.kind == "drive"
                    }
                return previous_drives, current_drives

            succeeded, drive_sets = self._tick_step("drives", reconcile)
            if succeeded:
                previous_drives, current_drives = drive_sets
                self.next_drive_scan = self.clock() + (
                    self.config.daemon["poll_interval"] or 60
                )
                if previous_drives != current_drives:
                    self.triggers.trigger(immediate=True)

        def watch_hub():
            with self.guard:
                saved, opened = self.watch.read()
                current_hub = file_hash(self.config.hub)
                if (
                    not self.sync_gate.locked()
                    and current_hub != self.last_written
                    and (
                        saved
                        or (
                            self.triggers.due is None
                            and current_hub != self.failed_hub
                        )
                    )
                ):
                    self.triggers.trigger()
                self.watch.read()
                if opened and not self.inflight and self.apps is not None:
                    with self.app_gate:
                        self.apps.hub_opened()

        self._tick_step("hub-watch", watch_hub)
        self._tick_step("credentials", self._watch_credentials)
        pending = None

        def load_pending():
            try:
                return PendingStore(self.directory).load()["decisions"]
            except PendingError as exc:
                self.pending_error = str(exc)
                raise

        pending_ok, pending = self._tick_step(
            "pending",
            load_pending,
            circuit_breaker=False,
        )
        if pending_ok:
            self.pending_error = None
        # A corrupt pending file is user state: serve clients, but
        # never proceed with synchronization or overwrite that file.
        with self.guard:
            expired = self.leases.expire()
            display = bool(
                os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
            )
            if self.pending_error:
                self.leases.take("pending", "pending")
            elif pending and display:
                self.leases.take("pending", "pending")
            elif "pending" in self.leases.leases:
                self.leases.release("pending")
            self.triggers.leases_changed(bool(self.leases.leases))
            if expired:
                self.triggers.trigger(immediate=True)
            if (
                self.has_reachable_copy
                and (pending and display or self.pending_error)
            ) and self.clock() - self.last_notification >= 3600:
                message = (
                    "Invalid pending decisions file; run kp-sync doctor "
                    "to inspect it"
                    if self.pending_error
                    else "Pending KeePass decisions; use kp-sync resolve"
                )
                self._notify(message)
                self.last_notification = self.clock()
            ready = self.triggers.ready() and self.pending_error is None
            silent = self.triggers.start() if ready else False
        if self.inhibitor is not None:
            self._tick_step(
                "inhibitor",
                lambda: self.inhibitor.update(bool(self.leases.leases)),
            )
        if ready:
            future = self.queue_sync({"ui": "none"} if silent else None)

            def finished(done):
                with self.guard:
                    self.triggers.finish()

            future.add_done_callback(finished)
        with self.guard:
            return (
                self.triggers.should_exit()
                and self.inflight == 0
                and self.queued_sync is None
            )

    def close(self):
        """Run final shutdown sync and release daemon-owned resources.

        Notes:
            Attempts a bounded final synchronization, offers rescue when
            configured, and closes adapters and worker pools.
        """
        self.stopping = True
        deadline = self.clock() + 85
        reserve = (
            min(
                self.config.daemon["rescue_wait"],
                60,
            )
            if self.rescue is not None
            and not self.config.hub_persistent
            and (
                os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
            )
            else 0
        )
        final = self.queue_sync({"ui": "none"})
        try:
            final.result(timeout=max(0, deadline - self.clock() - reserve))
        except concurrent.futures.TimeoutError:
            error(
                "final synchronization exceeded shutdown timeout; runtime "
                "copies retained"
            )
        except concurrent.futures.CancelledError as exc:
            error(
                f"final synchronization failed; runtime copies retained: {exc}"
            )
        except Exception as exc:
            error(
                f"final synchronization failed; runtime copies retained: {exc}"
            )
        finally:
            if self.rescue is not None:
                try:
                    self.rescue.stop(
                        timeout=min(
                            self.config.daemon["rescue_wait"],
                            max(0, deadline - self.clock()),
                        )
                    )
                except (OSError, RuntimeError, ValueError) as exc:
                    error(
                        "shutdown rescue failed; runtime copies retained: "
                        f"{exc}"
                    )
            if self.inhibitor is not None:
                self.inhibitor.close()
            if self.apps is not None:
                with self.app_gate:
                    self.apps.close()
            self._stop_rclone_daemon()
            if final.done():
                for handle in self.credentials:
                    handle.discard()
            self.watch.close()
            self.pool.shutdown(wait=False, cancel_futures=True)
            self.requests.shutdown(wait=False, cancel_futures=True)


class Server:
    """Serve bounded client connections over the profile Unix socket.

    Check peer IDs; bound slots; release leases on disconnect.
    """

    def __init__(self, config, *, controller=None, listener=None):
        """Create the socket service around a controller.

        Args:
            config: Loaded profile configuration.
            controller: Optional injected request controller.
            listener: Optional pre-bound or systemd-activated socket.
        """
        self.config = config
        self.controller = controller or Controller(config)
        self.directory = self.controller.directory
        self.path = self.directory / "control.sock"
        self.listener = listener
        self.own_socket = False
        self.stop_event = threading.Event()
        self.clients = set()
        self.clients_lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(32)

    def listen(self):
        """Acquire the daemon lock and prepare the profile socket.

        Raises:
            RuntimeError: If a daemon runs or the socket is unsafe.
        """
        lock_fd = os.open(
            self.directory / "daemon.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        self.lock_fd = lock_fd
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(lock_fd)
            raise RuntimeError("a daemon is already running for this profile")
        if self.listener is None and os.environ.get("LISTEN_PID") == str(
            os.getpid()
        ):
            if os.environ.get("LISTEN_FDS") != "1":
                raise RuntimeError("expected exactly one activation socket")
            self.listener = socket.socket(fileno=os.dup(3))
            if (
                self.listener.family != socket.AF_UNIX
                or self.listener.type != socket.SOCK_STREAM
                or Path(self.listener.getsockname()) != self.path
            ):
                raise RuntimeError("invalid activation socket")
        if self.listener is None:
            if os.path.lexists(self.path):
                metadata = self.path.lstat()
                if (
                    not stat.S_ISSOCK(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                ):
                    raise RuntimeError(
                        "refusing to remove an unsafe socket path"
                    )
                self.path.unlink()
            self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.listener.bind(str(self.path))
            os.chmod(self.path, 0o600)
            self.listener.listen(16)
            self.own_socket = True
        self.listener.settimeout(0.2)

    def serve(self, *, startup_sync=True):
        """Accept and dispatch client connections until daemon shutdown.

        Args:
            startup_sync: Queue a sync after app and session discovery.

        Notes:
            Owns the listener, threads, controller, and socket cleanup.
        """
        self.listen()
        self.controller._tick_step(
            "apps",
            lambda: self.controller._refresh_apps(force=True),
        )
        self.controller._tick_step(
            "sessions",
            lambda: self.controller._refresh_sessions(force=True),
        )
        if startup_sync:
            self.controller.queue_sync()
        try:
            while not self.stop_event.is_set():
                if self.controller.tick():
                    break
                try:
                    connection, _ = self.listener.accept()
                except socket.timeout:
                    continue
                if peer_uid(
                    connection
                ) != os.getuid() or not self.slots.acquire(blocking=False):
                    connection.close()
                    continue
                connection.settimeout(10)
                thread = threading.Thread(
                    target=self._client, args=(connection,), daemon=True
                )
                thread.start()
        finally:
            self.listener.close()
            with self.clients_lock:
                for connection in self.clients:
                    try:
                        connection.shutdown(socket.SHUT_RDWR)
                    except OSError as exc:
                        info(
                            "client already closed during stop: "
                            f"{type(exc).__name__}"
                        )
            try:
                self.controller.close()
            finally:
                if self.own_socket:
                    self.path.unlink(missing_ok=True)
                os.close(self.lock_fd)

    def _client(self, sock):
        connection = Connection(sock)
        owner = str(id(connection))
        with self.clients_lock:
            self.clients.add(sock)
        try:
            while not self.stop_event.is_set():
                request = validate_request(connection.receive())
                for key, value in request.get("environment", {}).items():
                    os.environ[key] = value
                sock.settimeout(None)
                if not request.get("wait", True):
                    if request.get("ui") == "tty":
                        raise ProtocolError(
                            "terminal UI requires a waiting client"
                        )
                    future = self.controller.requests.submit(
                        self.controller.handle, request, None, owner
                    )
                    future.add_done_callback(self._report_background_error)
                    connection.send(
                        {"type": "accepted", "result": {"action": "accepted"}}
                    )
                    break
                result = self.controller.handle(request, connection, owner)
                connection.send({"type": "result", "result": result})
        except EOFError:
            pass
        except (
            OSError,
            RuntimeError,
            ValueError,
            concurrent.futures.CancelledError,
        ) as exc:
            error(f"daemon request failed: {exc}")
            try:
                connection.send({"type": "error", "message": str(exc)})
            except OSError:
                pass
        except Exception as exc:
            # Unexpected failures reach the log and waiting client.
            error(
                f"daemon request failed unexpectedly: {type(exc).__name__}: "
                f"{exc}"
            )
            try:
                connection.send(
                    {
                        "type": "error",
                        "message": (
                            f"internal daemon error: {type(exc).__name__}"
                        ),
                    }
                )
            except OSError:
                pass
        finally:
            self.controller.disconnect(owner)
            with self.clients_lock:
                self.clients.discard(sock)
            connection.close()
            self.slots.release()

    @staticmethod
    def _report_background_error(future):
        try:
            future.result()
        except concurrent.futures.CancelledError:
            info("background request cancelled during daemon stop")
        except Exception as exc:
            error(f"background request failed: {type(exc).__name__}: {exc}")


def run(config):
    """Run the production daemon and restore signal handlers on exit."""
    server = Server(config, controller=Controller.production(config))
    set_log_file(server.directory / "log")
    old = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        old[sig] = signal.signal(sig, lambda *_: server.stop_event.set())
    try:
        server.serve()
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)
        set_log_file(None)
