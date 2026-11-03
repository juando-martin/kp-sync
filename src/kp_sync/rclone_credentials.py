"""Private per-profile copy of an externally provided rclone config.

The external file belongs to its provider and is only ever read. kp-sync
passes rclone its own copy under the profile run directory, imports
changes only between rclone processes, and drains the copy once the
external file disappears. See "Rclone credentials and ``drive-sync``"
in the endpoints design chapter.
"""

import hashlib
import os
import stat
from pathlib import Path

from kp_sync.endpoints.base import EndpointUnavailable
from kp_sync.log import info
from kp_sync.paths import atomic_write, ensure_private_directory

MAX_CONFIG_BYTES = 1024 * 1024


class CredentialsUnavailable(EndpointUnavailable):
    """No rclone credentials are available for an endpoint."""

    pass


class RcloneCredentials:
    """Manage the private copy of an external rclone config."""

    def __init__(
        self, name: str, external: str | os.PathLike[str], directory: Path
    ) -> None:
        """Bind an endpoint to its external file and run directory.

        Args:
            name: Endpoint name, used for the private file names.
            external: Provider-owned rclone config; never written.
            directory: Profile run directory holding ``rclone/``.
        """
        self.name = name
        self.external = Path(external)
        self.directory = Path(directory) / "rclone"
        self.path = self.directory / f"{name}.conf"
        self._source = self.directory / f"{name}.source"

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

    def refresh(self) -> None:
        """Import a new or changed external file.

        Call only while no rclone process of this profile runs, so it
        cannot write an older refreshed token over the new copy. A
        missing external file leaves any copy; :meth:`finish` drains it.
        """
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
        older token for a running process to write back.
        """
        if not self.copy_exists():
            self.refresh()
        if not self.copy_exists():
            raise CredentialsUnavailable(
                f"{self.name}: rclone credentials unavailable"
            )
        return str(self.path)

    def finish(self) -> bool:
        """Delete the copy when the external file has disappeared.

        Never call while an rclone process of this profile is running.
        Returns whether a copy was removed.
        """
        if self.external_present() or not self.copy_exists():
            return False
        self.discard()
        info(f"{self.name}: external rclone config gone; copy removed")
        return True

    def discard(self) -> None:
        """Delete the private copy unconditionally."""
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
