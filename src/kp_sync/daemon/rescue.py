# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Save an unsynced hub to a verified removable-drive rescue copy."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Any, Mapping

from kp_sync.drives import Drive, DriveEndpoint
from kp_sync.kdbx import KdbxError, inspect_kdbx
from kp_sync.state import HistoryState, StateError
from kp_sync.pending import PendingError, PendingStore
from kp_sync.drives import _validate_identity
from kp_sync.log import logged


class RescueError(RuntimeError):
    """Raised when an unsynced hub cannot be safely preserved."""

    pass


@dataclass(frozen=True)
class RescueResult:
    """Verified rescue-file location and the copy it preserves."""

    path: Path
    sha256: str
    drive_identity: str


class RescueManager:
    """Create verified rescue files on eligible removable drives.

    This API intentionally has no delete operation: rescue copies are
    additional recovery data and are never removed by kp-sync.
    """

    def __init__(
        self,
        *,
        identity_resolver: Callable | None = None,
        verifier: Callable = inspect_kdbx,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Configure identity checks, verification, and timestamps.

        Args:
            identity_resolver: Optional mounted-drive identity check.
            verifier: KDBX validation function.
            clock: UTC-aware timestamp source for rescue filenames.
        """
        self.identity_resolver = identity_resolver
        self.verifier = verifier
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def save(
        self,
        hub: str | os.PathLike[str],
        drive: Drive,
        *,
        host: str,
        expected_sha256: str | None = None,
    ) -> RescueResult:
        """Install and verify a timestamped copy on a removable drive.

        Args:
            hub: Runtime hub file to preserve.
            drive: Eligible removable drive descriptor.
            host: Safe hostname used in the rescue filename.
            expected_sha256: Optional hub digest for race detection.

        Returns:
            Verified path, SHA-256 digest, and drive identity.

        Raises:
            RescueError: If source or target is unsafe or invalid.

        Notes:
            Writes a new ``pass.rescue-*.kdbx`` file without moving or
            deleting the runtime hub.
        """
        source = Path(hub)
        try:
            source_mode = source.lstat().st_mode
        except OSError as exc:
            raise RescueError(f"cannot inspect hub for rescue: {exc}") from exc
        if not stat.S_ISREG(source_mode):
            raise RescueError(
                "rescue source must be a regular, non-symlink hub file"
            )
        if source_mode & 0o077:
            raise RescueError("rescue source permissions are not private")
        digest = _sha256(source)
        if expected_sha256 is not None and digest != expected_sha256:
            raise RescueError(
                "hub changed before rescue; retry after a fresh sync check"
            )
        self._verify(source, digest)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", host):
            raise RescueError("host name is not safe for a rescue filename")
        timestamp = (
            self.clock().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        )
        rescue_name = (
            f"pass.rescue-{host}-{timestamp}-{uuid.uuid4().hex[:8]}.kdbx"
        )
        try:
            endpoint = DriveEndpoint(
                "rescue",
                drive.mountpoint,
                rescue_name,
                drive.identity,
                **(
                    {"identity_resolver": self.identity_resolver}
                    if self.identity_resolver is not None
                    else {}
                ),
            )
            # Install with RENAME_NOREPLACE, not check-then-rename-over.
            # Never move the regular hub.
            endpoint.store_if_unchanged(
                source,
                None,
                atomic_exchange=True,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            if isinstance(exc, RescueError):
                raise
            raise RescueError(
                f"cannot install rescue copy on {drive.identity}: {exc}"
            ) from exc

        target = drive.mountpoint / rescue_name
        try:
            target_mode = target.lstat().st_mode
            if not stat.S_ISREG(target_mode):
                raise RescueError("rescue target is not a regular file")
            target_digest = _sha256(target)
            if target_digest != digest:
                raise RescueError(
                    "installed rescue copy failed its SHA-256 verification"
                )
            self._verify(target, target_digest)
        except OSError as exc:
            raise RescueError(
                f"cannot verify installed rescue copy: {exc}"
            ) from exc
        return RescueResult(target, digest, drive.identity)

    def _verify(self, path: Path, digest: str) -> None:
        """Validate a KDBX file and ensure its hash stayed unchanged."""
        try:
            self.verifier(path)
        except (KdbxError, OSError, ValueError) as exc:
            raise RescueError(
                f"hub is not a verified KDBX file: {exc}"
            ) from exc
        if _sha256(path) != digest:
            raise RescueError("hub changed while it was being verified")


class RescueCoordinator:
    """Detect unsynced hubs and offer removable-drive rescue."""

    def __init__(
        self,
        config: Any,
        runtime: str | os.PathLike[str] | Any,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        mounts_provider: Callable[[], list[Drive]] | None = None,
        notifier: (
            Callable[[str, Callable[[], tuple[RescueResult, ...]]], Any] | None
        ) = None,
        manager: RescueManager | None = None,
        runner: Callable = subprocess.run,
        popen: Callable = subprocess.Popen,
        environ: Mapping[str, str] | None = None,
        prompt: Callable[[str, float], bool | None] | None = None,
    ) -> None:
        """Configure rescue discovery, notification, and prompts.

        Args:
            config: Loaded profile configuration.
            runtime: Private runtime directory or adapter exposing one.
            clock: Monotonic time source for shutdown deadlines.
            sleep: Injectable bounded wait function.
            mounts_provider: Function returning removable-drive records.
            notifier: Nonblocking rescue notification callback.
            manager: Rescue file manager.
            runner: Injectable runner for notify and discovery commands.
            popen: Injectable process launcher for action notifications.
            environ: Desktop environment used to detect a display.
            prompt: Optional callback for bounded shutdown confirmation.
        """
        self.config = config
        self.runtime = _runtime_directory(runtime)
        self.clock = clock
        self.sleep = sleep
        self.runner = runner
        self.popen = popen
        self.environ = dict(os.environ if environ is None else environ)
        self.mounts_provider = mounts_provider or (
            lambda: removable_mounts(runner=self.runner)
        )
        self.notifier = notifier or self._desktop_notify
        self.manager = manager or RescueManager()
        self.prompt = prompt or self._desktop_prompt
        self._needed = False
        self._hub_digest: str | None = None
        self._notified_digests: set[str] = set()
        self._rescued: dict[str, set[str]] = {}
        self._notification_process: Any | None = None
        self.discovery_errors: list[str] = []

    @property
    def rescue_needed(self) -> bool:
        """Whether the runtime hub lacks safe commit evidence."""
        return self._needed

    @property
    def rescued(self) -> bool:
        """Whether this digest has been copied to an eligible drive."""
        return bool(self._hub_digest and self._rescued.get(self._hub_digest))

    def update(self) -> bool:
        """Check commit evidence and attempt rescue on eligible drives.

        Returns:
            Whether the hub currently needs a rescue copy.

        Notes:
            May notify and save a verified copy to a removable drive.
        """
        self._poll_notification()
        hub = Path(self.config.hub)
        if self.config.hub_persistent or hub.is_symlink() or not hub.is_file():
            self._needed = False
            self._hub_digest = None
            return False
        digest = _sha256(hub)
        unsafe = not hub_is_safe_to_remove(self.config, self.runtime)
        self._needed = unsafe
        self._hub_digest = digest if unsafe else None
        if not unsafe:
            return False
        if digest not in self._notified_digests:
            self._notified_digests.add(digest)
            message = (
                f"{hub.name} is not synced to another copy. Insert a writable "
                "removable drive to save a rescue copy."
            )
            self.notifier(message, self.save_to_pendrives)
        self._save_to_available_drives(digest)
        return True

    def save_to_pendrives(self) -> tuple[RescueResult, ...]:
        """Save the current unsynced hub to eligible unused drives.

        Returns:
            Successfully verified rescue copies.
        """
        if not self._needed or self._hub_digest is None:
            return ()
        return self._save_to_available_drives(self._hub_digest)

    def _save_to_available_drives(
        self, digest: str
    ) -> tuple[RescueResult, ...]:
        saved_identities = self._rescued.setdefault(digest, set())
        attached = _attached_drive_identities(self.runtime)
        results: list[RescueResult] = []
        self.discovery_errors.clear()
        for drive in self.mounts_provider():
            try:
                identity = _validate_identity(drive.identity)
                if identity in attached or identity in saved_identities:
                    continue
                result = self.manager.save(
                    self.config.hub,
                    drive,
                    host=self.config.host,
                    expected_sha256=digest,
                )
            except RescueError as exc:
                self.discovery_errors.append(str(exc))
                continue
            saved_identities.add(identity)
            results.append(result)
        return tuple(results)

    def stop(self, timeout: float | None = None) -> bool:
        """Offer bounded shutdown rescue when a display is available.

        Args:
            timeout: Optional outer shutdown budget in seconds.

        Returns:
            Whether the current hub version has been rescued.

        Notes:
            May prompt and wait for a drive within the shutdown budget.
        """
        self._poll_notification()
        self.update()
        if not self._needed or self.rescued or not self._has_display():
            return self.rescued
        configured = max(0, int(self.config.daemon.get("rescue_wait", 60)))
        budget = float(
            configured if timeout is None else min(configured, max(0, timeout))
        )
        if budget <= 0:
            return False
        deadline = self.clock() + budget
        answer = self.prompt(
            f"Save unsynced {Path(self.config.hub).name} "
            "to a removable drive? "
            "Insert a writable drive to receive an encrypted rescue copy.",
            max(0.0, deadline - self.clock()),
        )
        if answer is not True:
            return False
        while self.clock() < deadline:
            self.update()
            if self.rescued:
                return True
            remaining = deadline - self.clock()
            if remaining <= 0:
                break
            self.sleep(min(0.25, remaining))
        return self.rescued

    def _has_display(self) -> bool:
        return bool(
            self.environ.get("DISPLAY") or self.environ.get("WAYLAND_DISPLAY")
        )

    def _desktop_notify(
        self,
        message: str,
        action: Callable[[], tuple[RescueResult, ...]],
    ) -> None:
        if not self._has_display():
            return
        if self._notification_process is not None:
            if self._notification_process.poll() is None:
                return
            self._poll_notification()
        try:
            self._notification_process = logged(self.popen, hide_text=True)(
                [
                    "notify-send",
                    "--app-name=kp-sync",
                    "--action=save=Save to pendrive",
                    "--wait",
                    message,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.environ,
            )
        except OSError as exc:
            raise RescueError(
                f"cannot show rescue notification: {exc}"
            ) from exc
        if self._notification_process.stdout is not None:
            os.set_blocking(self._notification_process.stdout.fileno(), False)
        self._notification_action = action

    def _poll_notification(self) -> None:
        process = self._notification_process
        if process is None or process.poll() is None:
            return
        self._notification_process = None
        output = error = ""
        try:
            output = (
                process.stdout.read().strip()
                if process.stdout is not None
                else ""
            )
            error = (
                process.stderr.read().strip()
                if process.stderr is not None
                else ""
            )
        finally:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        action = getattr(self, "_notification_action", None)
        self._notification_action = None
        if process.returncode:
            raise RescueError(
                f"rescue notification failed: {error or process.returncode}"
            )
        if output == "save" and action is not None:
            action()

    def _desktop_prompt(self, message: str, timeout: float) -> bool | None:
        if not self._has_display() or timeout <= 0:
            return None
        try:
            result = logged(subprocess.run, hide_text=True)(
                [
                    "zenity",
                    "--question",
                    "--timeout",
                    str(max(1, int(timeout))),
                    "--text",
                    message,
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=self.environ,
            )
        except subprocess.TimeoutExpired:
            return None
        except OSError as exc:
            raise RescueError(
                f"cannot show shutdown rescue dialog: {exc}"
            ) from exc
        if result.returncode == 0:
            return True
        if result.returncode in {1, 5}:
            return False
        raise RescueError(
            "shutdown rescue dialog failed: "
            f"{(result.stderr or '').strip() or result.returncode}"
        )

    def close(self) -> None:
        """Stop the notification process and close output streams."""
        process, self._notification_process = self._notification_process, None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def removable_mounts(
    *,
    runner: Callable = subprocess.run,
    identity_resolver: Callable[[str | os.PathLike[str]], str] | None = None,
) -> list[Drive]:
    """Discover removable writable mounts using read-only queries.

    Args:
        runner: Injectable runner for ``lsblk`` and ``findmnt``.
        identity_resolver: Optional function checking mount identity.

    Returns:
        Writable removable drives with validated filesystem identities.

    Raises:
        RescueError: If system queries fail or return malformed data.
    """
    run = runner
    try:
        result = run(
            [
                "lsblk",
                "--json",
                "--bytes",
                "--output",
                "TYPE,RM,RO,SERIAL,UUID,MOUNTPOINTS",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RescueError(
            f"cannot discover removable filesystems with lsblk: {exc}"
        ) from exc
    if result.returncode:
        raise RescueError(
            f"lsblk removable-filesystem discovery failed: "
            f"{(result.stderr or '').strip() or result.returncode}"
        )
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        raise RescueError(
            "lsblk returned malformed JSON while "
            "discovering removable drives"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(
        payload.get("blockdevices"), list
    ):
        raise RescueError("lsblk returned an invalid block-device tree")

    candidates: dict[tuple[str, str], Drive] = {}

    def walk(
        device: dict[str, Any],
        *,
        parent_removable: bool,
        parent_serial: str | None,
    ) -> None:
        if not isinstance(device, dict):
            raise RescueError("lsblk block-device tree contains a non-object")
        removable_value = device.get("rm", False)
        readonly_value = device.get("ro", False)
        if (
            type(removable_value) is not bool
            or type(readonly_value) is not bool
        ):
            raise RescueError(
                "lsblk returned invalid removable/read-only flags"
            )
        removable = removable_value or parent_removable
        serial_value = device.get("serial")
        serial = (
            serial_value.strip()
            if isinstance(serial_value, str) and serial_value.strip()
            else parent_serial
        )
        fs_uuid = device.get("uuid")
        mountpoints = device.get("mountpoints") or []
        if isinstance(mountpoints, str):
            mountpoints = [mountpoints]
        if not isinstance(mountpoints, list) or any(
            point is not None and not isinstance(point, str)
            for point in mountpoints
        ):
            raise RescueError("lsblk returned invalid mountpoint data")
        if (
            removable
            and not readonly_value
            and serial
            and isinstance(fs_uuid, str)
            and fs_uuid
        ):
            identity = _validate_identity(f"{serial}:{fs_uuid}")
            for point in mountpoints:
                if not point:
                    continue
                mount = Path(point)
                try:
                    metadata = mount.lstat()
                except OSError:
                    continue
                if not stat.S_ISDIR(metadata.st_mode) or mount.is_symlink():
                    continue
                if not os.access(mount, os.W_OK | os.X_OK):
                    continue
                try:
                    mount_result = run(
                        [
                            "findmnt",
                            "--noheadings",
                            "--output",
                            "OPTIONS",
                            "--target",
                            str(mount),
                        ],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=False,
                    )
                except (OSError, subprocess.SubprocessError) as exc:
                    raise RescueError(
                        f"cannot inspect mount options for {mount}: {exc}"
                    ) from exc
                if mount_result.returncode:
                    detail = (
                        mount_result.stderr or ""
                    ).strip() or mount_result.returncode
                    raise RescueError(
                        f"cannot determine whether {mount} is read-only: "
                        f"{detail}"
                    )
                options = mount_result.stdout.strip().split(",")
                if not options or "ro" in options:
                    continue
                if "rw" not in options:
                    raise RescueError(
                        f"mount options for {mount} do not establish "
                        "writability"
                    )
                actual_identity = (
                    identity_resolver(mount)
                    if identity_resolver is not None
                    else identity
                )
                if actual_identity != identity:
                    raise RescueError(
                        f"removable-drive identity changed at {mount}: "
                        f"expected {identity}"
                    )
                candidates[(identity, str(mount))] = Drive(
                    identity,
                    mount,
                    datetime.now(timezone.utc).isoformat(),
                )
        children = device.get("children", [])
        if children is None:
            children = []
        if not isinstance(children, list):
            raise RescueError("lsblk returned invalid child block devices")
        for child in children:
            walk(child, parent_removable=removable, parent_serial=serial)

    for device in payload["blockdevices"]:
        walk(device, parent_removable=False, parent_serial=None)
    return list(candidates.values())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RescueError(f"rescue file is not regular: {path}")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return digest.hexdigest()


def hub_is_safe_to_remove(config: Any, runtime: Any) -> bool:
    """Check that the unchanged hub is known committed elsewhere.

    Args:
        config: Loaded profile configuration.
        runtime: Runtime directory or object exposing its path.

    Returns:
        ``True`` if this version exists elsewhere; no pending work.

    Persistent hubs are never eligible. Unknown or changed hubs stay.
    """
    if config.hub_persistent:
        return False
    try:
        runtime_dir = _runtime_directory(runtime)
        hub = Path(config.hub)
        if hub.is_symlink() or not hub.is_file():
            return False
        current_sha = _sha256(hub)
        pending = PendingStore(runtime_dir).load()
        if pending["decisions"]:
            return False
        state = HistoryState.load(runtime_dir / "state.json")
        hub_record = state.copies.get(f"hub:{config.host}")
        if hub_record is None or hub_record.version not in state.versions:
            return False
        if hub_record.sha256 is not None and hub_record.sha256 != current_sha:
            return False
        if hub_record.version != current_sha:
            return False
        for identity, record in state.copies.items():
            if (
                identity == f"hub:{config.host}"
                or record.version != hub_record.version
            ):
                continue
            if record.sha256 is None or record.sha256 == current_sha:
                return True
        return False
    except (OSError, PendingError, StateError, TypeError, ValueError):
        return False


def _runtime_directory(runtime: Any) -> Path:
    if isinstance(runtime, (str, os.PathLike)):
        return Path(runtime)
    for attribute in ("run_directory", "runtime_dir", "path"):
        value = getattr(runtime, attribute, None)
        if value is not None:
            return Path(value)
    raise ValueError("runtime must be a path or expose its run directory")


def _attached_drive_identities(runtime_dir: Path) -> set[str]:
    directory = runtime_dir / "drives"
    try:
        metadata = directory.lstat()
    except FileNotFoundError:
        return set()
    if not stat.S_ISDIR(metadata.st_mode) or directory.is_symlink():
        raise RescueError(
            f"runtime drives path is not a real directory: {directory}"
        )
    identities: set[str] = set()
    for path in directory.glob("*.json"):
        try:
            descriptor = os.open(
                path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            )
        except OSError as exc:
            raise RescueError(
                f"cannot read attached drive record {path}: {exc}"
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise RescueError(f"attached drive record is unsafe: {path}")
            with os.fdopen(descriptor, encoding="utf-8") as source:
                descriptor = -1
                record = json.load(source)
            identity = _validate_identity(record["identity"])
        except (
            OSError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            if isinstance(exc, RescueError):
                raise
            raise RescueError(
                f"invalid attached drive record {path}: {exc}"
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        identities.add(identity)
    return identities
