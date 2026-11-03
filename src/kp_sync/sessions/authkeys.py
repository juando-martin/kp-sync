"""Safely render and update kp-sync-owned authorized_keys entries."""

import os
import re
import stat
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from kp_sync.lock import file_lock

TAG_PREFIX = "kp-sync-session-"
DEFAULT_PROFILE_TAG = "_"
_KEY = re.compile(r"ssh-ed25519 [A-Za-z0-9+/]+={0,2}\Z")
_LINE = re.compile(
    rb"(?P<options>[^\n]*?)\s*ssh-ed25519 [A-Za-z0-9+/]+={0,2} "
    rb"kp-sync-session-(?P<profile>_|[A-Za-z0-9][A-Za-z0-9_-]*)-"
    rb"(?P<id>[0-9a-f]{8})[ \t]*\r?\n?\Z"
)
_EXPIRY = re.compile(rb'(?:^|,)expiry-time="(\d{12})"')
_SPLIT = re.compile(rb"[^\n]*\n|[^\n]+")
_MAX_ATTEMPTS = 8


class AuthorizedKeysError(RuntimeError):
    """Raised when authorized_keys cannot be safely read or updated."""

    pass


def profile_tag(profile: str | None) -> str:
    """Return the tag component for the default or named profile."""
    return DEFAULT_PROFILE_TAG if profile is None else profile


def format_expiry(timestamp: float) -> str:
    """Format Unix expiry as an OpenSSH expiry-time value."""
    return datetime.fromtimestamp(timestamp).strftime("%Y%m%d%H%M")


def parse_expiry(text: str) -> float:
    """Parse an OpenSSH expiry-time value into Unix seconds."""
    return datetime.strptime(text, "%Y%m%d%H%M").timestamp()


@dataclass(frozen=True)
class SessionLine:
    """Fields required to render one forced-command public-key line."""

    session_id: str
    public_key: str
    command: str
    expires: float


def render_line(profile: str | None, line: SessionLine) -> bytes:
    """Render a restricted, loopback-only session key entry.

    Args:
        profile: Profile whose session owns the key.
        line: Public key, forced command, session ID, and expiry.

    Returns:
        One newline-terminated authorized_keys line as UTF-8 bytes.

    Raises:
        AuthorizedKeysError: If the key, ID, or command is unsafe.
    """
    key = line.public_key.strip()
    if not _KEY.fullmatch(key):
        raise AuthorizedKeysError(
            "session key must be a single ssh-ed25519 public key"
        )
    if not re.fullmatch(r"[0-9a-f]{8}", line.session_id):
        raise AuthorizedKeysError("invalid session id")
    command = line.command.replace("\\", "\\\\").replace('"', '\\"')
    if "\n" in command or "\r" in command:
        raise AuthorizedKeysError("forced command must be a single line")
    options = (
        'restrict,from="127.0.0.1,::1",'
        f'expiry-time="{format_expiry(line.expires)}",command="{command}"'
    )
    return (
        f"{options} {key} {TAG_PREFIX}"
        f"{profile_tag(profile)}-{line.session_id}\n"
    ).encode("utf-8")


def forced_command(
    executable: str,
    profile: str | None,
    session_id: str,
    *,
    diagnostic: bool = False,
) -> str:
    """Build the absolute forced command embedded in a session key.

    Args:
        executable: Absolute path to the kp-sync executable.
        profile: Optional named profile.
        session_id: Session identifier passed to ``sftp-serve``.
        diagnostic: Select the isolated diagnostic record directory.

    Returns:
        A command string for the authorized_keys ``command`` option.

    Raises:
        AuthorizedKeysError: If the executable path is unsafe.
    """
    if not os.path.isabs(executable):
        raise AuthorizedKeysError(
            "the forced command needs the absolute path of kp-sync"
        )
    if any(ch.isspace() or ch in "\"\\'$`" for ch in executable):
        raise AuthorizedKeysError(
            f"unsupported character in kp-sync path: {executable!r}"
        )
    parts = [executable]
    if profile is not None:
        parts += ["--profile", profile]
    return " ".join(
        [
            *parts,
            "sftp-serve",
            *(["--diagnostic"] if diagnostic else []),
            session_id,
        ]
    )


def _our_line(raw: bytes):
    if raw.lstrip().startswith(b"#"):
        return None
    return _LINE.fullmatch(raw)


def _split(data: bytes) -> list[bytes]:
    return _SPLIT.findall(data)


class AuthorizedKeys:
    """Edit only this profile's kp-sync key lines under a shared lock.

    Other entries are preserved byte for byte. Writes verify ownership,
    permissions, file identity, and original contents.
    """

    def __init__(
        self,
        path=None,
        *,
        lock_path=None,
        runtime_dir=None,
        clock=time.time,
        lock_timeout: float = 30,
    ):
        """Configure the authorized_keys path and lock.

        Args:
            path: File to edit; defaults to ``~/.ssh/authorized_keys``.
            lock_path: Shared lock path, defaulting to runtime.
            runtime_dir: Runtime dir used if ``lock_path`` is omitted.
            clock: Wall-clock source for key expiration checks.
            lock_timeout: Maximum seconds to wait for the shared lock.

        Raises:
            AuthorizedKeysError: If no runtime directory is available.
        """
        self.path = (
            Path(path)
            if path is not None
            else Path.home() / ".ssh" / "authorized_keys"
        )
        if lock_path is None:
            runtime = (
                runtime_dir
                if runtime_dir is not None
                else os.environ.get("XDG_RUNTIME_DIR")
            )
            if not runtime:
                raise AuthorizedKeysError("XDG_RUNTIME_DIR is not set")
            lock_path = Path(runtime) / "kp-sync-authorized_keys.lock"
        self.lock_path = Path(lock_path)
        self.clock = clock
        self.lock_timeout = lock_timeout

    def check(self) -> None:
        """Verify that the file and parent directory are safe to edit.

        A missing file is allowed but is not created by this check.

        Raises:
            AuthorizedKeysError: If file type or permissions are unsafe.
        """
        self._read()

    def _read(self) -> tuple[bytes, os.stat_result | None]:
        try:
            before = self.path.lstat()
        except FileNotFoundError:
            self._check_directory()
            return b"", None
        if not stat.S_ISREG(before.st_mode):
            raise AuthorizedKeysError(
                f"{self.path} is a symbolic link or not a regular file"
            )
        if before.st_uid != os.getuid():
            raise AuthorizedKeysError(
                f"{self.path} is not owned by the current user"
            )
        if before.st_mode & 0o022:
            raise AuthorizedKeysError(
                f"{self.path} is group or other writable"
            )
        self._check_directory()
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (
                before.st_dev,
                before.st_ino,
            ):
                raise AuthorizedKeysError(
                    f"{self.path} changed while being read"
                )
            with os.fdopen(fd, "rb", closefd=False) as source:
                data = source.read()
        finally:
            os.close(fd)
        return data, opened

    def _check_directory(self) -> None:
        directory = self.path.parent
        if directory.is_symlink():
            raise AuthorizedKeysError(f"{directory} is a symbolic link")
        if directory.exists():
            metadata = directory.stat()
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.getuid()
            ):
                raise AuthorizedKeysError(
                    f"{directory} is not a directory owned by the user"
                )
            if metadata.st_mode & 0o022:
                raise AuthorizedKeysError(
                    f"{directory} is group or other writable"
                )

    def read_lines(
        self, profile: str | None
    ) -> list[tuple[str, float | None]]:
        """List this profile's session IDs and parsed expiry times.

        Args:
            profile: Profile whose tagged lines should be returned.

        Returns:
            ``(session_id, expiry)`` pairs; invalid expiry is ``None``.
            This method does not modify the file.
        """
        data, _ = self._read()
        tag = profile_tag(profile).encode()
        found = []
        for raw in _split(data):
            match = _our_line(raw)
            if match is not None and match.group("profile") == tag:
                found.append((match.group("id").decode(), _line_expiry(match)))
        return found

    def update(
        self,
        profile: str | None,
        *,
        add: tuple[SessionLine, ...] = (),
        remove: frozenset[str] | set[str] = frozenset(),
        keep_only: set[str] | frozenset[str] | None = None,
    ) -> list[str]:
        """Add, remove, and prune only this profile's session entries.

        Args:
            profile: Profile whose tagged lines may be changed.
            add: New entries; matching session IDs are replaced.
            remove: Session IDs to remove.
            keep_only: Optional allowlist; other lines are pruned.

        Returns:
            IDs of profile entries dropped during the update.

        Raises:
            AuthorizedKeysError: If unsafe or repeatedly changed.

        Notes:
            Atomically replaces the file under lock, preserving others.
        """
        tag = profile_tag(profile).encode()
        replaced = {line.session_id for line in add}
        remove = set(remove)
        for _ in range(_MAX_ATTEMPTS):
            with file_lock(self.lock_path, self.lock_timeout):
                done = self._attempt(
                    profile, tag, add, replaced, remove, keep_only
                )
            if done is not None:
                return done
        raise AuthorizedKeysError(f"{self.path} kept changing; giving up")

    def _attempt(self, profile, tag, add, replaced, remove, keep_only):
        data, original = self._read()
        now = self.clock()
        kept: list[bytes] = []
        dropped: list[str] = []
        for raw in _split(data):
            match = _our_line(raw)
            if match is None or match.group("profile") != tag:
                kept.append(raw)
                continue
            session_id = match.group("id").decode()
            expiry = _line_expiry(match)
            if (
                session_id in remove
                or session_id in replaced
                or (
                    keep_only is not None
                    and (
                        session_id not in keep_only
                        or (expiry is not None and expiry <= now)
                    )
                )
            ):
                dropped.append(session_id)
            else:
                kept.append(raw)
        added = [render_line(profile, line) for line in add]
        result_lines = list(kept)
        if added and result_lines and not result_lines[-1].endswith(b"\n"):
            result_lines[-1] += b"\n"
        result_lines.extend(added)
        result = b"".join(result_lines)
        if result == data:
            return [] if not dropped else dropped
        self._verify_only_ours_changed(data, result)
        directory = self.path.parent
        if not directory.exists():
            directory.mkdir(mode=0o700, parents=True)
        temporary = directory / ".authorized_keys.kp-sync.tmp"
        temporary.unlink(missing_ok=True)
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        try:
            with os.fdopen(fd, "wb") as output:
                if original is not None:
                    os.fchmod(output.fileno(), stat.S_IMODE(original.st_mode))
                output.write(result)
                output.flush()
                os.fsync(output.fileno())
            if not self._unchanged(original):
                temporary.unlink(missing_ok=True)
                return None
            if original is None:
                try:
                    os.link(temporary, self.path)
                except FileExistsError:
                    return None
                finally:
                    temporary.unlink(missing_ok=True)
            else:
                os.replace(temporary, self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        _fsync_directory(directory)
        return dropped

    def _unchanged(self, original) -> bool:
        try:
            now = self.path.lstat()
        except FileNotFoundError:
            return original is None
        if original is None:
            return False
        return (now.st_ino, now.st_size, now.st_mtime_ns, now.st_dev) == (
            original.st_ino,
            original.st_size,
            original.st_mtime_ns,
            original.st_dev,
        )

    @staticmethod
    def _verify_only_ours_changed(before: bytes, after: bytes) -> None:
        def others(data):
            return [
                raw.rstrip(b"\n")
                for raw in _split(data)
                if _our_line(raw) is None
            ]

        if others(before) != others(after):
            raise AuthorizedKeysError(
                "refusing to write: a line other than ours would change"
            )


def _line_expiry(match) -> float | None:
    found = _EXPIRY.search(match.group("options"))
    if found is None:
        return None
    try:
        return parse_expiry(found.group(1).decode())
    except ValueError:
        return None


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
