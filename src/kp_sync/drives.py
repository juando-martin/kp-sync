"""Register, identify, and access attached runtime-discovered drives."""

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from kp_sync.endpoints.base import (
    EndpointChanged,
    EndpointFingerprint,
    EndpointUnavailable,
)
from kp_sync.endpoints.file import FileEndpoint
from kp_sync.paths import atomic_write, ensure_private_directory, run_dir


class DriveError(ValueError):
    """Raised when drive registration or identity validation fails."""

    pass


class DriveNotMounted(DriveError):
    """No mounted filesystem matches the requested mount point."""

    pass


class DriveInspectionError(DriveError):
    """Raised when the system cannot inspect block-device metadata."""

    pass


_IDENTITY = re.compile(r"[^:/\\\x00]+:[A-Za-z0-9-]+\Z")


def pending_attach_path(profile: str | None, identity: str) -> Path:
    """Return the shared runtime marker path for a pending attach."""
    token = hashlib.sha256(
        f"{profile or ''}\0{identity}".encode("utf-8")
    ).hexdigest()
    return run_dir() / f"keydisk-up-{token}.pending"


@dataclass(frozen=True)
class Drive:
    """Registered drive identity, mount point, and attach time."""

    identity: str
    mountpoint: Path
    attached_at: str


def identify_drive(
    mountpoint: str | os.PathLike[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    """Identify a mounted filesystem by serial and filesystem UUID.

    Args:
        mountpoint: Path of the mounted drive.
        runner: Injectable ``lsblk`` command runner.

    Raises:
        DriveInspectionError: If block-device metadata cannot be read.
        DriveNotMounted: If no matching mounted filesystem is found.
    """
    mount = Path(mountpoint).expanduser().resolve()
    try:
        result = runner(
            [
                "lsblk",
                "--json",
                "--tree",
                "--output",
                "SERIAL,UUID,MOUNTPOINTS",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise DriveInspectionError(
            f"cannot identify drive at {mount}: {exc}"
        ) from exc
    for device in _walk_devices(data):
        mountpoints = device.get("mountpoints") or []
        serial = device.get("serial")
        fs_uuid = device.get("uuid")
        if not serial or not fs_uuid:
            continue
        if any(
            point and Path(point).resolve() == mount for point in mountpoints
        ):
            return _validate_identity(f"{serial.strip()}:{fs_uuid.strip()}")
    raise DriveNotMounted(
        f"no filesystem identity found for mount point {mount}"
    )


def register_drive(
    run_directory: str | os.PathLike[str],
    mountpoint: str | os.PathLike[str],
    identity: str,
    *,
    attached_at: str | None = None,
) -> Drive:
    """Register a mounted drive in private profile runtime state."""
    mount = Path(mountpoint).expanduser().resolve()
    if not mount.is_dir():
        raise DriveError(
            f"drive mount point is not an accessible directory: {mount}"
        )
    identity = _validate_identity(identity)
    drive = Drive(
        identity=identity,
        mountpoint=mount,
        attached_at=attached_at or datetime.now(timezone.utc).isoformat(),
    )
    ensure_private_directory(run_directory)
    directory = ensure_private_directory(Path(run_directory) / "drives")
    atomic_write(directory / f"{identity}.json", _encode_drive(drive))
    return drive


def list_drives(
    run_directory: str | os.PathLike[str],
    *,
    remove_stale: bool = True,
    identity_resolver: Callable[
        [str | os.PathLike[str]], str
    ] = identify_drive,
) -> list[Drive]:
    """Read attached-drive records and optionally remove stale ones.

    The resolver rejects a different drive at a recorded path.
    """
    directory = Path(run_directory) / "drives"
    if not os.path.lexists(directory):
        return []
    ensure_private_directory(directory)
    drives: list[Drive] = []
    removed = False
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            drive = Drive(
                identity=_validate_identity(payload["identity"]),
                mountpoint=Path(payload["mountpoint"]),
                attached_at=payload["attached_at"],
            )
        except (
            OSError,
            KeyError,
            TypeError,
            json.JSONDecodeError,
            DriveError,
        ) as exc:
            raise DriveError(f"invalid drive record {path}: {exc}") from exc
        if not drive.mountpoint.is_dir():
            if remove_stale:
                path.unlink()
                removed = True
            continue
        try:
            actual_identity = identity_resolver(drive.mountpoint)
        except DriveNotMounted:
            if remove_stale:
                path.unlink()
                removed = True
            continue
        if actual_identity != drive.identity:
            if remove_stale:
                path.unlink()
                removed = True
            continue
        drives.append(drive)
    if removed:
        _fsync_directory(directory)
    return drives


def detach_drive(
    run_directory: str | os.PathLike[str],
    identity: str,
) -> bool:
    """Remove a drive registration and report whether it existed."""
    identity = _validate_identity(identity)
    path = Path(run_directory) / "drives" / f"{identity}.json"
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    _fsync_directory(path.parent)
    return True


def _validate_identity(identity: str) -> str:
    if not isinstance(identity, str) or not _IDENTITY.fullmatch(identity):
        raise DriveError(f"invalid drive identity: {identity!r}")
    return identity


def _encode_drive(drive: Drive) -> str:
    return (
        json.dumps(
            {
                "identity": drive.identity,
                "mountpoint": str(drive.mountpoint),
                "attached_at": drive.attached_at,
            },
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )


def _walk_devices(data, parent_serial=None):
    if not isinstance(data, dict):
        return
    for device in data.get("blockdevices", data.get("children", [])):
        if isinstance(device, dict):
            serial = device.get("serial") or parent_serial
            yield {**device, "serial": serial}
            yield from _walk_devices(device, serial)


class DriveEndpoint(FileEndpoint):
    """File endpoint that revalidates drive identity on each access."""

    def __init__(
        self,
        name: str,
        mountpoint: str | os.PathLike[str],
        database_name: str,
        drive_identity: str,
        *,
        identity_resolver: Callable[
            [str | os.PathLike[str]], str
        ] = identify_drive,
        **kwargs,
    ) -> None:
        self.mountpoint = Path(mountpoint)
        self.drive_identity = _validate_identity(drive_identity)
        self.identity_resolver = identity_resolver
        super().__init__(
            name,
            self.mountpoint / database_name,
            identity=f"drive:{self.drive_identity}",
            **kwargs,
        )
        self._validate_drive_path()

    def probe(self) -> EndpointFingerprint:
        """Fingerprint the database only while its drive is mounted."""
        self._check_mount()
        return super().probe()

    def fetch(self, destination: str | os.PathLike[str]) -> None:
        """Fetch the database after confirming the drive identity."""
        self._check_mount()
        super().fetch(destination)

    def store(self, source: str | os.PathLike[str]) -> None:
        """Store the database after confirming the drive identity."""
        self._check_mount()
        super().store(source)

    def store_if_unchanged(
        self,
        source: str | os.PathLike[str],
        expected_sha256: str | None,
        *,
        before_replace: Callable[[], None] | None = None,
        atomic_exchange: bool = True,
    ) -> None:
        """Promote only if the expected database hash still matches.

        ``before_replace`` runs before replacement. The drive ID is also
        checked around the write.
        """
        self._check_mount()

        def check_live_mount() -> None:
            """Run the caller check and reject a changed mount."""
            if before_replace is not None:
                before_replace()
            try:
                self._check_mount()
            except EndpointUnavailable as exc:
                raise EndpointChanged(str(exc)) from exc

        super().store_if_unchanged(
            source,
            expected_sha256,
            before_replace=check_live_mount,
            atomic_exchange=atomic_exchange,
        )

    def _check_mount(self) -> None:
        try:
            identity = self.identity_resolver(self.mountpoint)
        except DriveError as exc:
            raise EndpointUnavailable(
                f"{self.name}: drive mount is no longer available: {exc}"
            ) from exc
        if identity != self.drive_identity:
            raise EndpointUnavailable(
                f"{self.name}: drive identity changed at {self.mountpoint}"
            )
        self._validate_drive_path()

    def _validate_drive_path(self) -> None:
        try:
            self.path.resolve().relative_to(self.mountpoint.resolve())
        except (OSError, ValueError) as exc:
            raise DriveError(
                f"database path escapes its drive mount: {self.path}"
            ) from exc


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
