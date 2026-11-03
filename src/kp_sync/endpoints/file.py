"""Filesystem endpoint with guarded, durable copy promotion."""

import ctypes
import errno
import hashlib
import os
import re
import secrets
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Callable, Iterable

from kp_sync.endpoints.base import (
    Endpoint,
    EndpointChanged,
    EndpointFingerprint,
    EndpointUnavailable,
)


class _RenameUnsupported(EndpointUnavailable):
    pass


class FileEndpoint(Endpoint):
    """Database endpoint backed by one regular local file.

    Promotions stage beside the target and replace it atomically when
    supported. Conditional promotion preserves raced copies.
    """

    def __init__(
        self, name: str, path: str | os.PathLike[str], **kwargs
    ) -> None:
        """Create a file endpoint.

        Args:
            name: Endpoint name used in messages.
            path: Database file path.
            **kwargs: Policy options accepted by ``Endpoint``.
        """
        self.path = Path(path).expanduser().resolve()
        super().__init__(
            name, kwargs.pop("identity", f"file:{name}"), **kwargs
        )

    def fetch(self, destination: str | os.PathLike[str]) -> None:
        """Copy the current file to a private destination.

        Args:
            destination: Local path to receive the endpoint bytes.

        Raises:
            EndpointUnavailable: If the target is not a readable file.
        """
        target = Path(destination)
        try:
            self._validate_target()
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with self.path.open("rb") as source, target.open("wb") as output:
                os.fchmod(output.fileno(), 0o600)
                shutil.copyfileobj(source, output)
                output.flush()
                os.fsync(output.fileno())
        except OSError as exc:
            raise EndpointUnavailable(
                f"{self.name}: cannot fetch {self.path}: {exc}"
            ) from exc

    def store(self, source: str | os.PathLike[str]) -> None:
        """Publish ``source`` by replacing the endpoint path.

        Args:
            source: Candidate file to publish.

        Raises:
            PermissionError: If the endpoint is read-only.
            EndpointUnavailable: If the atomic file operation fails.
        """
        self._store(source)

    def store_if_unchanged(
        self,
        source: str | os.PathLike[str],
        expected_sha256: str | None,
        *,
        before_replace: Callable[[], None] | None = None,
        atomic_exchange: bool = True,
    ) -> None:
        """Publish only while the target matches its observed hash.

        Args:
            source: Candidate file to install.
            expected_sha256: Target hash observed before the write;
                ``None`` means the target must remain absent.
            before_replace: Optional callback revalidating shared state.
            atomic_exchange: Verify the displaced file; use guarded
                displace/install if exchange is unsupported.

        Raises:
            EndpointChanged: If a concurrent writer changes the target.
            EndpointUnavailable: If publication or verification fails.
            PermissionError: If the endpoint is read-only.

        Notes:
            Stages beside the target; raced copies remain for recovery.
        """
        self._store(
            source,
            expected_sha256=expected_sha256,
            conditional=True,
            before_replace=before_replace,
            atomic_exchange=atomic_exchange,
        )

    def recover_displaced(
        self,
        expected_sha256: str | None,
        *,
        before_action: Callable[[], None] | None = None,
        discard_sha256s: Iterable[str] = (),
    ) -> list[Path]:
        """Resolve recognized files left by an interrupted promotion.

        Args:
            expected_sha256: Previously recorded target hash.
            before_action: Optional callback before filesystem changes.
            discard_sha256s: Hashes preserved and resolved elsewhere.

        Returns:
            Unresolved files to treat as unknown copies.

        Notes:
            Restores a sole displaced file when the target is absent, or
            removes a duplicate of known content after rechecking it.
        """
        discard = set(discard_sha256s)
        pattern = re.compile(
            rf"\.{re.escape(self.path.name)}\.kp-sync-displaced-"
            rf"[0-9a-f]{{32}}\Z"
        )
        try:
            self._validate_target()
            leftovers = []
            with os.scandir(self.path.parent) as entries:
                for entry in entries:
                    if not pattern.fullmatch(entry.name):
                        continue
                    try:
                        metadata = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if stat.S_ISREG(metadata.st_mode):
                        leftovers.append(Path(entry.path))
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise EndpointUnavailable(
                f"{self.name}: cannot inspect displaced files beside "
                f"{self.path}: {exc}"
            ) from exc

        if not leftovers:
            return []
        leftovers.sort()
        target_exists = os.path.lexists(self.path)
        if not target_exists and len(leftovers) == 1:
            displaced = leftovers[0]
            try:
                if before_action is not None:
                    before_action()
                _rename_noreplace(displaced, self.path)
                _fsync_directory(self.path.parent)
                return []
            except EndpointChanged:
                # Keep both copies if a writer recreated the target; the
                # caller preserves the displaced copy before probing.
                target_exists = os.path.lexists(self.path)
            except OSError as exc:
                raise EndpointUnavailable(
                    f"{self.name}: cannot restore displaced target "
                    f"{displaced}: {exc}"
                ) from exc

        unresolved = []
        for displaced in leftovers:
            try:
                metadata = displaced.lstat()
                if not stat.S_ISREG(metadata.st_mode):
                    continue
                displaced_sha = _sha256_regular_file(displaced)
                if target_exists and (
                    (
                        expected_sha256 is not None
                        and displaced_sha == expected_sha256
                    )
                    or displaced_sha in discard
                ):
                    if before_action is not None:
                        before_action()
                    try:
                        target_metadata = self.path.lstat()
                    except FileNotFoundError:
                        unresolved.append(displaced)
                        continue
                    if not stat.S_ISREG(target_metadata.st_mode):
                        unresolved.append(displaced)
                        continue
                    current = displaced.lstat()
                    if (current.st_dev, current.st_ino) != (
                        metadata.st_dev,
                        metadata.st_ino,
                    ):
                        unresolved.append(displaced)
                        continue
                    displaced.unlink()
                    _fsync_directory(self.path.parent)
                else:
                    unresolved.append(displaced)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise EndpointUnavailable(
                    f"{self.name}: cannot recover displaced target "
                    f"{displaced}: {exc}"
                ) from exc
        return unresolved

    def _store(
        self,
        source: str | os.PathLike[str],
        *,
        expected_sha256: str | None = None,
        conditional: bool = False,
        before_replace: Callable[[], None] | None = None,
        atomic_exchange: bool = False,
    ) -> None:
        if self.readonly:
            raise PermissionError(f"endpoint {self.name} is read-only")
        temp_name = None
        try:
            self._validate_target()
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.", dir=self.path.parent
            )
            with os.fdopen(fd, "wb") as output, open(source, "rb") as incoming:
                os.fchmod(output.fileno(), 0o600)
                shutil.copyfileobj(incoming, output)
                output.flush()
                os.fsync(output.fileno())
            if before_replace is not None:
                before_replace()
            if conditional:
                try:
                    self._validate_target()
                    current_sha256 = EndpointFingerprint.from_file(
                        self.path
                    ).sha256
                except FileNotFoundError:
                    current_sha256 = None
                except OSError as exc:
                    raise EndpointUnavailable(
                        f"{self.name}: cannot re-check {self.path}: {exc}"
                    ) from exc
                if current_sha256 != expected_sha256:
                    raise EndpointChanged(
                        f"{self.name}: target changed before atomic "
                        "replacement"
                    )
            if atomic_exchange:
                if expected_sha256 is None:
                    _rename_noreplace(temp_name, self.path)
                    temp_name = None
                else:
                    try:
                        _rename_exchange(temp_name, self.path)
                    except _RenameUnsupported:
                        self._displace_then_install(
                            temp_name,
                            expected_sha256,
                            before_replace=before_replace,
                        )
                        temp_name = None
                    try:
                        if temp_name is not None:
                            _fsync_directory(self.path.parent)
                    except OSError as exc:
                        preserved_path = Path(temp_name)
                        temp_name = None
                        raise EndpointChanged(
                            f"{self.name}: atomic exchange could not be made "
                            "durable; "
                            "preserving the displaced version",
                            preserved_path=preserved_path,
                        ) from exc
                    if temp_name is not None:
                        try:
                            displaced_sha256 = _sha256_regular_file(temp_name)
                        except OSError as exc:
                            preserved_path = Path(temp_name)
                            temp_name = None
                            raise EndpointChanged(
                                f"{self.name}: displaced target could not be "
                                "verified; "
                                "preserving it for recovery",
                                preserved_path=preserved_path,
                            ) from exc
                        if displaced_sha256 != expected_sha256:
                            preserved_path = Path(temp_name)
                            temp_name = None
                            raise EndpointChanged(
                                f"{self.name}: target changed during atomic "
                                "exchange; "
                                "preserving the displaced version",
                                preserved_path=preserved_path,
                                observed_sha256=displaced_sha256,
                            )
                        os.unlink(temp_name)
            else:
                os.replace(temp_name, self.path)
            directory_fd = os.open(
                self.path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise EndpointUnavailable(
                f"{self.name}: cannot store {self.path}: {exc}"
            ) from exc
        finally:
            if temp_name is not None:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass

    def _displace_then_install(
        self,
        staged: str | os.PathLike[str],
        expected_sha256: str,
        *,
        before_replace: Callable[[], None] | None = None,
    ) -> None:
        if before_replace is not None:
            before_replace()
        _preflight_noreplace(self.path.parent)
        if before_replace is not None:
            before_replace()
        try:
            self._validate_target()
            current_sha256 = _sha256_regular_file(self.path)
        except FileNotFoundError as exc:
            raise EndpointChanged(
                "target disappeared before displace/install"
            ) from exc
        if current_sha256 != expected_sha256:
            raise EndpointChanged("target changed before displace/install")

        displaced = self.path.with_name(
            f".{self.path.name}.kp-sync-displaced-{secrets.token_hex(16)}"
        )
        try:
            os.rename(self.path, displaced)
        except FileNotFoundError as exc:
            raise EndpointChanged(
                "target disappeared before displace/install"
            ) from exc
        _fsync_directory(self.path.parent)
        if before_replace is not None:
            before_replace()
        _rename_noreplace(staged, self.path)
        _fsync_directory(self.path.parent)
        try:
            displaced_sha256 = _sha256_regular_file(displaced)
        except OSError as exc:
            raise EndpointChanged(
                f"{self.name}: displaced target could not be verified; "
                "preserving it for recovery",
                preserved_path=displaced,
            ) from exc
        if displaced_sha256 != expected_sha256:
            raise EndpointChanged(
                f"{self.name}: target changed during displace/install; "
                "preserving the displaced version",
                preserved_path=displaced,
                observed_sha256=displaced_sha256,
            )
        if before_replace is not None:
            before_replace()
        displaced.unlink()
        _fsync_directory(self.path.parent)

    def probe(self) -> EndpointFingerprint:
        """Hash and inspect the endpoint file.

        Returns:
            A fingerprint containing the file's SHA-256 and size.

        Raises:
            EndpointUnavailable: If missing or non-regular.
        """
        try:
            self._validate_target()
            return EndpointFingerprint.from_file(self.path)
        except FileNotFoundError as exc:
            raise EndpointUnavailable(
                f"{self.name}: file is missing: {self.path}"
            ) from exc
        except OSError as exc:
            raise EndpointUnavailable(
                f"{self.name}: cannot probe {self.path}: {exc}"
            ) from exc

    def _validate_target(self) -> None:
        try:
            metadata = self.path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(metadata.st_mode):
            raise EndpointUnavailable(
                f"{self.name}: endpoint path is not a regular file"
            )


def _rename_exchange(
    first: str | os.PathLike[str], second: str | os.PathLike[str]
) -> None:
    _renameat2(first, second, 2, "atomic file replacement")


def _rename_noreplace(
    first: str | os.PathLike[str], second: str | os.PathLike[str]
) -> None:
    _renameat2(first, second, 1, "no-replace file installation")


def _renameat2(
    first: str | os.PathLike[str],
    second: str | os.PathLike[str],
    flags: int,
    operation: str,
) -> None:
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:
        raise _RenameUnsupported(
            f"{operation} requires Linux renameat2"
        ) from exc
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(first),
        -100,
        os.fsencode(second),
        flags,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
        raise _RenameUnsupported(f"filesystem does not support {operation}")
    if error == errno.EEXIST and flags == 1:
        raise EndpointChanged("target appeared before no-replace installation")
    if error == errno.ENOENT:
        raise EndpointChanged(
            "target disappeared before atomic file operation"
        )
    raise OSError(error, os.strerror(error), os.fspath(second))


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _preflight_noreplace(directory: Path) -> None:
    first_fd, first_name = tempfile.mkstemp(
        prefix=".kp-sync-noreplace-", dir=directory
    )
    os.close(first_fd)
    first = Path(first_name)
    second = directory / f".kp-sync-noreplace-{secrets.token_hex(16)}"
    try:
        _rename_noreplace(first, second)
        second.unlink()
        _fsync_directory(directory)
    finally:
        for path in (first, second):
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def _sha256_regular_file(path: str | os.PathLike[str]) -> str:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    digest = hashlib.sha256()
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(errno.EINVAL, "not a regular file", os.fspath(path))
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()
