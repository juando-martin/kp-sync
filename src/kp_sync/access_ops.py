"""Validate and apply kp-access operations on the staged database."""

import json
import os
import re
import secrets
import shutil
import stat
from pathlib import Path, PurePosixPath

from kp_sync.backups import backup_file
from kp_sync.endpoints.base import EndpointFingerprint
from kp_sync.kdbx import KdbxError, inspect_kdbx
from kp_sync.lock import file_lock
from kp_sync.paths import atomic_write, ensure_private_directory

ACCESS_IDENTITY = "session:kp-access"
ACCESS_IDENTITY_PREFIX = "session:kp-access:"
ACCESS_LEASE = "access:kp-access"
ACCESS_TTL_SECONDS = 60
_UPLOAD_ID = re.compile(r"[0-9a-f]{32}\Z")
_OPERATIONS = {"GET", "PUT", "DELETE", "MOVE", "BEGIN", "END"}


class AccessOperationError(RuntimeError):
    """Raised when an access operation violates its staging contract."""

    pass


def access_paths(
    config, run_directory: str | os.PathLike[str]
) -> tuple[Path, Path, str]:
    """Resolve and secure runtime staging and database paths.

    Args:
        config: Loaded kp-sync configuration with an ``[access]`` table.
        run_directory: Private runtime directory for this profile.

    Returns:
        Staging directory, local database path, and virtual path.

    Raises:
        AccessOperationError: If paths are missing, unsafe, or outside
            the profile runtime directory.
    """
    run_dir = Path(run_directory).resolve()
    database_path = config.access.get("db_path")
    if not isinstance(database_path, str):
        raise AccessOperationError("[access].db_path must be configured")
    virtual = _virtual_path(database_path)
    if len(virtual.parts) < 2:
        raise AccessOperationError("[access].db_path must include a directory")

    configured_staging = config.access.get("staging_dir")
    staging = (
        Path(configured_staging).expanduser()
        if configured_staging
        else Path("staging")
    )
    if not staging.is_absolute():
        staging = run_dir / staging
    try:
        relative_staging = staging.relative_to(run_dir)
    except ValueError as exc:
        raise AccessOperationError(
            "[access].staging_dir must stay inside the "
            "profile runtime directory"
        ) from exc
    current = run_dir
    for part in relative_staging.parts:
        if part in {"", ".", ".."}:
            raise AccessOperationError(
                "[access].staging_dir must be a normalized runtime path"
            )
        current = current / part
        if current.is_symlink():
            raise AccessOperationError(
                "[access].staging_dir must not contain symbolic links"
            )
    staging = current
    if staging == run_dir or run_dir not in staging.parents:
        raise AccessOperationError(
            "[access].staging_dir must stay inside the "
            "profile runtime directory"
        )
    ensure_private_directory(run_dir)
    ensure_private_directory(staging)
    return staging, staging / virtual.name, database_path


def access_identity(
    run_directory: str | os.PathLike[str], *, create: bool = True
) -> str | None:
    """Return this lifecycle's unique history identity."""
    run_dir = Path(run_directory)
    path = run_dir / "access-lifecycle.json"
    if os.path.lexists(path):
        try:
            metadata = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise AccessOperationError(
                    "access lifecycle metadata is not private"
                )
            payload = json.loads(path.read_text(encoding="utf-8"))
            identity = payload["identity"]
            if not isinstance(identity, str) or not re.fullmatch(
                r"session:kp-access:[0-9a-f]{32}", identity
            ):
                raise AccessOperationError(
                    "access lifecycle metadata is invalid"
                )
            return identity
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
        ) as exc:
            raise AccessOperationError(
                f"cannot read access lifecycle metadata: {exc}"
            ) from exc

    if not create:
        return None
    from kp_sync.sessions.records import SessionStore
    from kp_sync.state import HistoryState

    retired = set(HistoryState.load(run_dir / "state.json").retirements)
    pending = {
        entry_identity
        for entry_identity, _, _ in SessionStore(run_dir).pending_retirements()
    }
    identity = ACCESS_IDENTITY_PREFIX + secrets.token_hex(16)
    while identity in retired or identity in pending:
        identity = ACCESS_IDENTITY_PREFIX + secrets.token_hex(16)
    payload = json.dumps({"identity": identity}, sort_keys=True) + "\n"
    temporary = run_dir / f".access-lifecycle-{secrets.token_hex(8)}.tmp"
    try:
        atomic_write(temporary, payload)
        os.link(temporary, path)
    except FileExistsError:
        return access_identity(run_dir, create=False)
    finally:
        temporary.unlink(missing_ok=True)
    _fsync_directory(run_dir)
    return identity


def validate_file_path(
    config,
    run_directory: str | os.PathLike[str],
    value: str,
    *,
    database_only: bool = False,
) -> Path:
    """Validate a virtual path and map it into the staging directory.

    Only the database and temporary siblings are allowed. When
    ``database_only`` is true, only the database itself is accepted.
    """
    staging, database, configured_db = access_paths(config, run_directory)
    path = _virtual_path(value)
    root = _virtual_path(configured_db)
    if path.parent != root.parent:
        raise AccessOperationError(
            "file operation path is outside the configured database directory"
        )
    if path.name != root.name and not path.name.startswith(root.name + "."):
        raise AccessOperationError(
            "file operation path is not the database or one of its "
            "temporary names"
        )
    if database_only and path != root:
        raise AccessOperationError(
            "operation is only valid for the database path"
        )
    local = staging / path.name
    _check_stage_file(local, must_exist=False)
    return database if path == root else local


def validate_operation(
    config,
    run_directory: str | os.PathLike[str],
    operation: str,
    file_path: str = "",
    destination: str = "",
) -> tuple[Path | None, Path | None]:
    """Validate an operation's source and optional destination paths.

    BEGIN and END accept no paths; GET accepts only the database path.
    """
    if operation not in _OPERATIONS:
        raise AccessOperationError("unsupported kp-access operation")
    if operation in {"BEGIN", "END"}:
        if file_path or destination:
            raise AccessOperationError(
                f"{operation} must not include file paths"
            )
        return None, None
    if operation == "GET":
        return (
            validate_file_path(
                config,
                run_directory,
                file_path,
                database_only=True,
            ),
            None,
        )
    source = validate_file_path(config, run_directory, file_path)
    target = None
    if operation == "MOVE":
        if not destination:
            raise AccessOperationError("MOVE requires a destination")
        target = validate_file_path(config, run_directory, destination)
    elif destination:
        raise AccessOperationError(
            f"{operation} must not include a destination"
        )
    return source, target


def create_upload(config, run_directory, payload: bytes) -> str:
    """Write a private, fsynced upload and return its ID."""
    with file_lock(
        Path(run_directory) / "access-op.lock",
        config.sync["lock_timeout"],
    ):
        return _create_upload(config, run_directory, payload)


def _create_upload(config, run_directory, payload: bytes) -> str:
    staging, _, _ = access_paths(config, run_directory)
    identifier = secrets.token_hex(16)
    path = staging / f".kp-sync-upload-{identifier}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return identifier


def discard_upload(config, run_directory, identifier: str) -> None:
    """Remove a pending upload when its identifier is valid."""
    if not isinstance(identifier, str) or not _UPLOAD_ID.fullmatch(identifier):
        return
    staging, _, _ = access_paths(config, run_directory)
    (staging / f".kp-sync-upload-{identifier}").unlink(missing_ok=True)


def apply_file_operation(
    config,
    run_directory,
    *,
    operation: str,
    file_path: str,
    destination: str = "",
    upload_id: str | None = None,
) -> tuple[bool, Path | None]:
    """Apply one validated file operation to the staged database.

    The Boolean indicates whether the database changed. Replacing or
    deleting files first creates backups. Uploads pass KDBX checks.
    """
    source, target = validate_operation(
        config,
        run_directory,
        operation,
        file_path,
        destination,
    )
    staging, database, _ = access_paths(config, run_directory)
    if operation == "GET":
        return False, source
    if operation == "PUT":
        if upload_id is None or not _UPLOAD_ID.fullmatch(upload_id):
            raise AccessOperationError("PUT requires a valid upload id")
        upload = staging / f".kp-sync-upload-{upload_id}"
        _check_stage_file(upload, must_exist=True)
        assert source is not None
        if source == database:
            _inspect_database(upload)
        _backup_existing(source, run_directory, config)
        os.replace(upload, source)
        _fsync_directory(staging)
        return source == database, source
    if operation == "DELETE":
        assert source is not None
        _check_stage_file(source, must_exist=True)
        _backup_existing(source, run_directory, config)
        source.unlink()
        _fsync_directory(staging)
        return False, source if source == database else None
    if operation == "MOVE":
        assert source is not None and target is not None
        _check_stage_file(source, must_exist=True)
        _check_stage_file(target, must_exist=False)
        if target == database:
            _inspect_database(source)
        _backup_existing(target, run_directory, config)
        os.replace(source, target)
        _fsync_directory(staging)
        return target == database, target
    raise AccessOperationError("unsupported file operation")


def transit_path(run_directory: str | os.PathLike[str]) -> Path:
    """Return the runtime marker path for an incomplete remote save."""
    return Path(run_directory) / "access-in-transit.json"


def mark_transit(run_directory, *, now: float) -> None:
    """Record when a remote save enters its delete/move window."""
    atomic_write(
        transit_path(run_directory),
        json.dumps({"version": 1, "since": now}, sort_keys=True) + "\n",
    )


def clear_transit(run_directory) -> None:
    """Remove the remote-save marker, rejecting a symlink."""
    path = transit_path(run_directory)
    if path.is_symlink():
        raise AccessOperationError(
            "refusing to remove an unsafe in-transit marker"
        )
    path.unlink(missing_ok=True)


def in_transit(run_directory, *, now: float) -> bool:
    """Check whether a transit marker remains valid."""
    path = transit_path(run_directory)
    if not os.path.lexists(path):
        return False
    if path.is_symlink():
        raise AccessOperationError("unsafe in-transit marker")
    metadata = path.stat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise AccessOperationError("unsafe in-transit marker")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AccessOperationError(
            f"cannot read in-transit marker: {exc}"
        ) from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"version", "since"}
        or payload["version"] != 1
        or type(payload["since"]) not in {int, float}
        or payload["since"] > now
    ):
        raise AccessOperationError("invalid in-transit marker")
    if now - payload["since"] < ACCESS_TTL_SECONDS:
        return True
    error_message = (
        "kp-access database upload did not complete within one minute; "
        "the staged copy is treated as missing"
    )
    from kp_sync.log import error

    error(error_message)
    clear_transit(run_directory)
    return False


def _virtual_path(value: str) -> PurePosixPath:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or "\0" in value
        or "\\" in value
        or "//" in value
    ):
        raise AccessOperationError("invalid kp-access file path")
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts[1:]):
        raise AccessOperationError("invalid kp-access file path")
    return path


def _check_stage_file(path: Path, *, must_exist: bool) -> None:
    if path.parent.is_symlink():
        raise AccessOperationError(
            "staging directory must not be a symbolic link"
        )
    if not path.parent.is_dir():
        raise AccessOperationError("staging directory is unavailable")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if must_exist:
            raise AccessOperationError(
                f"staged file does not exist: {path.name}"
            ) from None
        return
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise AccessOperationError(
            f"staged path is not a regular user-owned file: {path.name}"
        )


def _inspect_database(path: Path) -> None:
    try:
        inspect_kdbx(path)
    except (OSError, KdbxError) as exc:
        raise AccessOperationError(
            f"uploaded database failed KDBX verification: {exc}"
        ) from exc


def _backup_existing(path: Path, run_directory, config) -> None:
    _check_stage_file(path, must_exist=False)
    if path.exists():
        backup_file(
            path,
            Path(run_directory) / "backups",
            identity=ACCESS_IDENTITY,
            keep=config.backups["keep"],
        )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_copy(source: Path, target: Path) -> None:
    """Atomically copy a regular file with private mode and fsync."""
    if source.is_symlink() or not source.is_file():
        raise AccessOperationError("source database is not a regular file")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.stage-{secrets.token_hex(8)}"
    try:
        with source.open("rb") as incoming, temporary.open("xb") as outgoing:
            os.fchmod(outgoing.fileno(), 0o600)
            shutil.copyfileobj(incoming, outgoing)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def database_hash(path: Path) -> str:
    """Return the staged database's SHA-256 fingerprint.

    Raises:
        AccessOperationError: If the staged database cannot be read.
    """
    try:
        return EndpointFingerprint.from_file(path).sha256 or ""
    except OSError as exc:
        raise AccessOperationError(
            f"cannot read staged database: {exc}"
        ) from exc
