"""Private session records, staging paths, and process ownership."""

import json
import hashlib
import os
import re
import shutil
import signal
import stat
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from kp_sync.paths import atomic_write, ensure_private_directory

SESSION_ID = re.compile(r"[0-9a-f]{8}\Z")
HOST_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
USER_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*\Z")
MODES = {"ro", "rw"}


class SessionError(RuntimeError):
    """Raised when a remote-session operation or record is invalid."""

    pass


def process_start(pid: int) -> int | None:
    """Read a process start time to distinguish reused process IDs.

    Args:
        pid: Process ID to inspect under ``/proc``.

    Returns:
        Start-time ticks, or ``None`` if the process cannot be read.
    """
    try:
        text = Path(f"/proc/{pid}/stat").read_text(
            encoding="ascii", errors="replace"
        )
    except OSError:
        return None
    try:
        return int(text.rsplit(")", 1)[1].split()[19])
    except (IndexError, ValueError):
        return None


def process_alive(pid: int | None, start: int | None) -> bool:
    """Check that a PID still identifies the process recorded earlier.

    Args:
        pid: Recorded process ID.
        start: Recorded ``/proc`` start time in clock ticks.

    Returns:
        ``True`` only if both values match the current process.
    """
    if type(pid) is not int or pid <= 0 or start is None:
        return False
    return process_start(pid) == start


@dataclass(frozen=True)
class SessionRecord:
    """Validated identity and lifecycle metadata for one session.

    Attributes:
        id: Eight-character lowercase hexadecimal session identifier.
        host: Remote host name.
        user: Optional remote account name.
        mode: ``ro`` or ``rw`` access mode.
        owner_pid: PID of the local session owner.
        owner_start: Start-time ticks for the local owner process.
        ssh_pid: Optional PID of the SSH client process.
        ssh_start: Start-time ticks paired with ``ssh_pid``.
        confined: Whether the SFTP service runs inside bubblewrap.
        created: Creation time as Unix seconds.
        expires: Key expiry time as Unix seconds.
        version: Record format version.
    """

    id: str
    host: str
    user: str | None
    mode: str
    owner_pid: int
    owner_start: int
    ssh_pid: int | None
    ssh_start: int | None
    confined: bool
    created: float
    expires: float
    version: int = 1

    @property
    def identity(self) -> str:
        """Return the stable history identity for this session copy."""
        return f"session:{self.host}:{self.id}"

    def owner_alive(self) -> bool:
        """Check whether the original local owner process is alive."""
        return process_alive(self.owner_pid, self.owner_start)

    def ssh_alive(self) -> bool:
        """Return whether the recorded SSH process is still alive."""
        return process_alive(self.ssh_pid, self.ssh_start)

    def validate(self) -> "SessionRecord":
        """Validate all persisted fields and return this record.

        Raises:
            SessionError: If a field violates the record schema.
        """
        if (
            self.version != 1
            or not isinstance(self.id, str)
            or not SESSION_ID.fullmatch(self.id)
            or not isinstance(self.host, str)
            or not HOST_NAME.fullmatch(self.host)
            or (
                self.user is not None
                and (
                    not isinstance(self.user, str)
                    or not USER_NAME.fullmatch(self.user)
                )
            )
            or self.mode not in MODES
            or type(self.owner_pid) is not int
            or type(self.owner_start) is not int
            or (self.ssh_pid is None) != (self.ssh_start is None)
            or (
                self.ssh_pid is not None
                and (
                    type(self.ssh_pid) is not int
                    or type(self.ssh_start) is not int
                )
            )
            or type(self.confined) is not bool
            or type(self.created) not in {int, float}
            or type(self.expires) not in {int, float}
        ):
            raise SessionError("invalid session record")
        return self


class SessionStore:
    """Manage private session records, keys, and staging directories.

    The profile runtime ``sessions/`` directory contains ``<id>.json``,
    ``<id>.key``, and a staged-copy directory named ``<id>/``.
    """

    def __init__(self, run_directory):
        """Bind the store to a profile runtime directory.

        Args:
            run_directory: Private directory containing session state.
        """
        self.run_directory = Path(run_directory)
        self.directory = self.run_directory / "sessions"

    def ensure(self) -> Path:
        """Create and return the private session-state directory."""
        return ensure_private_directory(self.directory)

    def record_path(self, session_id: str) -> Path:
        """Return the record path after validating its session ID."""
        self._check_id(session_id)
        return self.directory / f"{session_id}.json"

    def key_path(self, session_id: str) -> Path:
        """Return the private key path after validating its ID."""
        self._check_id(session_id)
        return self.directory / f"{session_id}.key"

    def staging_path(self, session_id: str) -> Path:
        """Return the staged-copy directory path for a session."""
        self._check_id(session_id)
        return self.directory / session_id

    @staticmethod
    def _check_id(session_id: str) -> None:
        if not isinstance(session_id, str) or not SESSION_ID.fullmatch(
            session_id
        ):
            raise SessionError(f"invalid session id: {session_id!r}")

    def save(self, record: SessionRecord) -> None:
        """Validate and atomically persist a session record.

        Args:
            record: Record to serialize in the private runtime dir.
        """
        record.validate()
        self.ensure()
        atomic_write(
            self.record_path(record.id),
            json.dumps(asdict(record), sort_keys=True) + "\n",
        )

    def load(self, session_id: str) -> SessionRecord:
        """Load and validate a user-owned record.

        Args:
            session_id: Eight-digit lowercase hexadecimal session ID.

        Returns:
            The validated session record.

        Raises:
            SessionError: If the record is missing, unsafe, or invalid.
        """
        path = self.record_path(session_id)
        try:
            if path.is_symlink() or path.stat().st_uid != os.getuid():
                raise SessionError(f"unsafe session record: {path}")
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise SessionError("invalid session record")
            return SessionRecord(**payload).validate()
        except FileNotFoundError:
            raise SessionError(f"unknown session {session_id}") from None
        except (OSError, ValueError, TypeError) as exc:
            raise SessionError(
                f"cannot read session record {path.name}: {exc}"
            ) from exc

    def ids(self) -> list[str]:
        """Return sorted IDs of syntactically valid record filenames."""
        if not self.directory.is_dir():
            return []
        return sorted(
            path.stem
            for path in self.directory.glob("*.json")
            if SESSION_ID.fullmatch(path.stem)
        )

    def records(self, *, on_error=None) -> list[SessionRecord]:
        """Load all records, optionally reporting invalid entries.

        Args:
            on_error: Optional callback receiving ID and exception.

        Returns:
            Valid session records.

        Raises:
            SessionError: If a record is invalid without a callback.
        """
        found = []
        for session_id in self.ids():
            try:
                found.append(self.load(session_id))
            except SessionError as exc:
                if on_error is None:
                    raise
                on_error(session_id, exc)
        return found

    def live_ids(self, *, now: float | None = None) -> set[str]:
        """Return unexpired records whose owner process is still alive.

        Args:
            now: Optional Unix time used for expiry comparison.
        """
        now = time.time() if now is None else now
        return {
            record.id
            for record in self.records(on_error=lambda *_: None)
            if record.owner_alive() and record.expires > now
        }

    def remove_files(self, session_id: str) -> None:
        """Remove staged data and keys, deleting the record last.

        Deleting the record last keeps interrupted cleanup discoverable.

        Args:
            session_id: Session whose runtime files should be removed.

        Raises:
            SessionError: If the staged path is a symbolic link.
        """
        self._check_id(session_id)
        staging = self.staging_path(session_id)
        if staging.is_symlink():
            raise SessionError(
                f"refusing to remove a symbolic link: {staging}"
            )
        if staging.is_dir():
            shutil.rmtree(staging)
        for path in (
            self.key_path(session_id),
            self.directory / f"{session_id}.key.pub",
            self.record_path(session_id),
        ):
            path.unlink(missing_ok=True)
        _fsync_directory(self.directory)

    @property
    def retirement_directory(self) -> Path:
        """Return the private directory holding cleanup progress."""
        return self.run_directory / "session-retirements"

    @staticmethod
    def _validate_retirement_pair(identity: str, owner_id: str) -> None:
        if owner_id == "access":
            if identity != "session:kp-access" and not re.fullmatch(
                r"session:kp-access:[0-9a-f]{32}", identity
            ):
                raise SessionError("invalid access retirement identity")
            return
        if (
            not SESSION_ID.fullmatch(owner_id)
            or identity.rsplit(":", 1)[-1] != owner_id
            or not identity.removeprefix("session:").rsplit(":", 1)[0]
        ):
            raise SessionError(
                "retirement identity does not match its owner ID"
            )

    def prepare_retirement(self, identity: str, owner_id: str) -> Path:
        """Persist retirement progress before removing a copy."""
        if (
            not isinstance(identity, str)
            or not identity.startswith("session:")
            or any(ord(char) < 32 for char in identity)
            or not isinstance(owner_id, str)
            or not owner_id
        ):
            raise SessionError("invalid session retirement progress")
        self._validate_retirement_pair(identity, owner_id)
        ensure_private_directory(self.retirement_directory)
        token = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        path = self.retirement_directory / f"{token}.json"
        atomic_write(
            path,
            json.dumps(
                {"identity": identity, "owner_id": owner_id},
                sort_keys=True,
            )
            + "\n",
        )
        return path

    def pending_retirements(self) -> list[tuple[str, str, Path]]:
        """Load validated crash-recovery retirement progress."""
        if not os.path.lexists(self.retirement_directory):
            return []
        directory_metadata = self.retirement_directory.lstat()
        if (
            not stat.S_ISDIR(directory_metadata.st_mode)
            or directory_metadata.st_uid != os.getuid()
            or stat.S_IMODE(directory_metadata.st_mode) & 0o077
        ):
            raise SessionError("unsafe session retirement directory")
        found = []
        for path in sorted(self.retirement_directory.glob("*.json")):
            try:
                descriptor = os.open(
                    path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                )
                with os.fdopen(descriptor, encoding="utf-8") as source:
                    metadata = os.fstat(source.fileno())
                    if (
                        not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_uid != os.getuid()
                    ):
                        raise SessionError(
                            f"unsafe retirement progress: {path.name}"
                        )
                    if stat.S_IMODE(metadata.st_mode) & 0o077:
                        raise SessionError(
                            f"non-private retirement progress: {path.name}"
                        )
                    payload = json.load(source)
                identity = payload["identity"]
                owner_id = payload["owner_id"]
                if (
                    not isinstance(identity, str)
                    or not identity.startswith("session:")
                    or any(ord(char) < 32 for char in identity)
                    or not isinstance(owner_id, str)
                    or not owner_id
                ):
                    raise SessionError(
                        f"invalid retirement progress: {path.name}"
                    )
                self._validate_retirement_pair(identity, owner_id)
                expected = hashlib.sha256(identity.encode("utf-8")).hexdigest()
                if path.name != f"{expected}.json":
                    raise SessionError(
                        f"invalid retirement progress: {path.name}"
                    )
            except (
                OSError,
                UnicodeError,
                json.JSONDecodeError,
                KeyError,
                TypeError,
            ) as exc:
                raise SessionError(
                    f"cannot read retirement progress {path.name}: {exc}"
                ) from exc
            found.append((identity, owner_id, path))
        return found

    def is_retiring(self, session_id: str) -> bool:
        """Return whether cleanup progress reserves a session ID."""
        self._check_id(session_id)
        return any(
            owner_id == session_id
            for _, owner_id, _ in self.pending_retirements()
        )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def stop_ssh(
    record: SessionRecord, *, grace: float = 3.0, sleep=time.sleep
) -> bool:
    """Stop the recorded SSH process without signaling a reused PID.

    Args:
        record: Session record containing PID and start-time identity.
        grace: Seconds to wait after ``SIGTERM`` before ``SIGKILL``.
        sleep: Injectable wait function.

    Returns:
        ``True`` if the recorded process was signaled, else ``False``.
    """
    if record.ssh_pid is None or not record.ssh_alive():
        return False
    try:
        os.kill(record.ssh_pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not record.ssh_alive():
            return True
        sleep(0.05)
    if record.ssh_alive():
        try:
            os.kill(record.ssh_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return True
