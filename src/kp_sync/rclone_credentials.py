"""Private per-profile copy of an externally provided rclone config.

The external file belongs to its provider and is only ever read. kp-sync
passes rclone its own copy under the profile run directory, imports
changes only between rclone processes, and drains the copy once the
external file disappears. A per-profile lock, shared by rclone runs and
exclusive for import, drain and discard, enforces that across
processes. See "Rclone credentials and ``drive-sync``" in the endpoints
design chapter.
"""

import hashlib
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from kp_sync.endpoints.base import EndpointUnavailable
from kp_sync.lock import LockTimeout, file_lock
from kp_sync.log import error, info
from kp_sync.paths import atomic_write, ensure_private_directory

MAX_CONFIG_BYTES = 1024 * 1024
# Bounded wait for rclone runs (shared) or an import/drain (exclusive).
LOCK_TIMEOUT = 30.0
LOCK_NAME = "rclone-credentials.lock"


class CredentialsUnavailable(EndpointUnavailable):
    """No rclone credentials are available for an endpoint."""

    pass


class RcloneCredentials:
    """Manage the private copy of an external rclone config."""

    def __init__(
        self,
        name: str,
        external: str | os.PathLike[str],
        directory: Path,
        *,
        lock_timeout: float = LOCK_TIMEOUT,
    ) -> None:
        """Bind an endpoint to its external file and run directory.

        Args:
            name: Endpoint name, used for the private file names.
            external: Provider-owned rclone config; never written.
            directory: Profile run directory holding ``rclone/``.
            lock_timeout: Seconds to wait for the profile's
                credentials lock.
        """
        self.name = name
        self.external = Path(external)
        self.directory = Path(directory) / "rclone"
        self.path = self.directory / f"{name}.conf"
        self._source = self.directory / f"{name}.source"
        # Outside ``rclone/``, which a discard removes.
        self.lock_path = Path(directory) / LOCK_NAME
        self.lock_timeout = lock_timeout

    def _lock(self, *, shared: bool = False, wait: bool = True):
        return file_lock(
            self.lock_path,
            self.lock_timeout if wait else 0,
            shared=shared,
        )

    def _external_digest(self) -> str | None:
        """Hash the external file, or ``None`` if absent or unusable."""
        try:
            metadata = self.external.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(metadata.st_mode):
            info(
                f"{self.name}: external rclone config is not a regular "
                "file; ignored"
            )
            return None
        if metadata.st_size > MAX_CONFIG_BYTES:
            info(f"{self.name}: external rclone config is too large; ignored")
            return None
        data = self._read_external()
        return None if data is None else hashlib.sha256(data).hexdigest()

    def _read_external(self) -> bytes | None:
        try:
            descriptor = os.open(self.external, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return None
        with os.fdopen(descriptor, "rb") as source:
            return source.read(MAX_CONFIG_BYTES + 1)

    def external_present(self) -> bool:
        """Return whether the provider publishes a usable file."""
        return self._external_digest() is not None

    def copy_exists(self) -> bool:
        """Return whether a private copy exists."""
        return os.path.lexists(self.path)

    def refresh(self, *, wait: bool = True) -> bool:
        """Import a new or changed external file.

        Holds the credentials lock exclusive, so no rclone process of
        this profile runs and none can write an older refreshed token
        over the new copy. A missing external file leaves any copy;
        :meth:`finish` drains it. Returns ``False`` when the lock stayed
        busy; the current copy is then kept and a later run imports.
        """
        try:
            with self._lock(wait=wait):
                self._refresh_locked()
        except LockTimeout:
            info(f"{self.name}: rclone config in use; import deferred")
            return False
        return True

    def _refresh_locked(self) -> None:
        data = self._read_external() if self.external_present() else None
        if data is None:
            return
        digest = hashlib.sha256(data).hexdigest()
        ensure_private_directory(self.directory)
        for path in (self.path, self._source):
            if path.is_symlink():
                raise RuntimeError(f"refusing to use a symbolic link: {path}")
        recorded = (
            self._source.read_text().strip() if self._source.exists() else ""
        )
        if self.copy_exists() and recorded == digest:
            return
        atomic_write(self.path, data)
        atomic_write(self._source, digest + "\n")
        info(f"{self.name}: imported the external rclone config")

    def require(self) -> str:
        """Return the private copy path or raise when unavailable.

        An absent copy is imported on demand: with no copy there is no
        older token for a running process to write back. Callers that
        run rclone with the copy must use :meth:`in_use` instead.
        """
        if not self.copy_exists():
            self.refresh()
        if not self.copy_exists():
            raise CredentialsUnavailable(
                f"{self.name}: rclone credentials unavailable"
            )
        return str(self.path)

    @contextmanager
    def in_use(self) -> Iterator[str]:
        """Yield the copy's path while holding the lock shared.

        Run rclone with the copy only inside this context. A missing
        copy is imported first; the copy is re-checked under the shared
        lock, since a drain may have removed it in between.
        """
        if not self.copy_exists():
            self.refresh()
        try:
            with self._lock(shared=True):
                if not self.copy_exists():
                    raise CredentialsUnavailable(
                        f"{self.name}: rclone credentials unavailable"
                    )
                yield str(self.path)
        except LockTimeout as exc:
            raise CredentialsUnavailable(
                f"{self.name}: rclone credentials are being updated"
            ) from exc

    def finish(self, *, wait: bool = True) -> bool:
        """Delete the copy when the external file has disappeared.

        Waits for running rclone processes of this profile through the
        exclusive lock. Returns whether a copy was removed; a busy lock
        defers the drain to a later call.
        """
        if self.external_present() or not self.copy_exists():
            return False
        try:
            with self._lock(wait=wait):
                # Re-check: the file may have returned while waiting.
                if self.external_present() or not self.copy_exists():
                    return False
                self._discard_locked()
        except LockTimeout:
            info(f"{self.name}: rclone config in use; drain deferred")
            return False
        info(f"{self.name}: external rclone config gone; copy removed")
        return True

    def discard(self, *, wait: bool = True) -> bool:
        """Delete the private copy once no rclone process uses it.

        Returns ``False`` when the lock stayed busy and the copy was
        kept; the next daemon start or logout removes it.
        """
        try:
            with self._lock(wait=wait):
                self._discard_locked()
        except LockTimeout:
            error(
                f"{self.name}: rclone config still in use; private copy "
                "kept until the next daemon start or logout"
            )
            return False
        return True

    def _discard_locked(self) -> None:
        for path in (self.path, self._source):
            if path.is_symlink():
                raise RuntimeError(
                    f"refusing to clean a symbolic link: {path}"
                )
            path.unlink(missing_ok=True)
        try:
            self.directory.rmdir()
        except OSError:
            pass


def credentials_for(config, run_directory) -> list[RcloneCredentials]:
    """Return handles for enabled rclone endpoints with a config."""
    return [
        RcloneCredentials(item.name, item.rclone_config, run_directory)
        for item in config.endpoints
        if item.type == "rclone"
        and item.enabled
        and item.rclone_config is not None
    ]
