"""Google Drive and other cloud endpoints accessed through rclone."""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Mapping, Sequence

from kp_sync.endpoints.base import (
    Endpoint,
    EndpointError,
    EndpointFingerprint,
    EndpointUnavailable,
)
from kp_sync.log import info
from kp_sync.paths import atomic_write


class EndpointMissing(EndpointUnavailable):
    """The remote is reachable, but a requested path is absent."""


_GLOB_SPECIAL = set("*?[]{}\\")
_CONTENT_HASHES = frozenset({"sha256", "sha1", "md5"})


def _hash_pairs(hashes: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            (key.lower().replace("-", ""), value.lower())
            for key, value in hashes.items()
            if value
        )
    )


class RcloneEndpoint(Endpoint):
    """Database and history endpoint implemented with rclone commands.

    Remote observations use a filtered listing when possible.
    Transfers use ``copyto`` so existing cloud file history is retained.
    """

    def __init__(
        self,
        name: str,
        remote: str,
        *,
        database_name: str = "pass.kdbx",
        rclone_config: str | Path | None = None,
        download: Sequence[str] | None = None,
        upload: Sequence[str] | None = None,
        probe: Sequence[str] | None = None,
        identity: str | None = None,
        runner: Callable | None = None,
        credentials=None,
        transport=None,
        **kwargs,
    ) -> None:
        """Configure the remote root, commands, and listing cache.

        Args:
            name: Configuration-facing endpoint name.
            remote: Rclone name, optionally with a directory suffix.
            database_name: Relative database path below the remote root.
            rclone_config: Optional private rclone configuration file.
            download: Optional command template with ``{remote}`` and
                ``{file}`` placeholders.
            upload: Optional upload command template.
            probe: Optional metadata command without ``{file}``.
            identity: Stable physical-copy identity.
            runner: Injectable subprocess-compatible command runner.
            credentials: Optional ``RcloneCredentials``; rclone then
                uses its private copy, and the endpoint is unavailable
                without one.
            transport: Optional ``kp_sync.rclone_rcd.RcloneDaemon``.
                Generated commands then go through its ``rclone rcd``
                while it is ready; custom and batch commands, and every
                call while it is not ready, run as commands.
            **kwargs: Policy options accepted by ``Endpoint``.

        Raises:
            ValueError: If the remote or command templates are invalid.
        """
        if not isinstance(remote, str) or not remote or remote.startswith(":"):
            raise ValueError("remote must name an rclone remote")
        self.remote = remote
        self.root = remote if ":" in remote else f"{remote}:"
        self.root = self.root.rstrip("/")
        self.rclone_config = (
            str(rclone_config) if rclone_config is not None else None
        )
        self.credentials = credentials
        self.transport = transport
        self._runner = runner if runner is not None else subprocess.run
        self.database_remote = self.remote_path(database_name)
        self.database_relative = str(PurePosixPath(database_name))
        # One listing serves the database and tracked history.
        self._listing_enabled = probe is None and not _GLOB_SPECIAL & set(
            self.database_relative
        )
        self._tracked: list[str] = []
        self._listing: dict[str, dict] | None = None
        self._listed_at = 0.0
        self.download_command = self._command(
            download, ("rclone", "copyto", "{remote}", "{file}"), transfer=True
        )
        self.upload_command = self._command(
            upload, ("rclone", "copyto", "{file}", "{remote}"), transfer=True
        )
        self.probe_command = self._command(
            probe, ("rclone", "lsjson", "--hash", "{remote}"), transfer=False
        )
        self.hashes: dict[str, str] = {}
        kwargs.setdefault("foreign_writers", True)
        super().__init__(name, identity or f"cloud:{remote}", **kwargs)

    def begin_run(self) -> None:
        """Import changed external credentials before rclone calls."""
        if self.credentials is not None:
            if self.transport is not None and self.credentials.import_due():
                # The rcd holds the copy and its lock; it has no reload.
                self.transport.stop()
            self.credentials.refresh()

    def end_run(self) -> None:
        """Drop drained credentials once the run is done."""
        if self.credentials is not None:
            if self.transport is not None and self.credentials.drain_due():
                self.transport.stop()
            self.credentials.finish()

    @staticmethod
    def _command(command, default, *, transfer: bool) -> tuple[str, ...]:
        if isinstance(command, str):
            raise ValueError(
                "rclone commands must be argument lists, not shell strings"
            )
        result = tuple(command) if command is not None else default
        if not result or not all(
            isinstance(arg, str) and arg for arg in result
        ):
            raise ValueError("rclone commands must be nonempty argument lists")
        if transfer and not any("{file}" in arg for arg in result):
            raise ValueError(
                "transfer command must contain a {file} placeholder"
            )
        if not transfer and any("{file}" in arg for arg in result):
            raise ValueError("probe command cannot use a {file} placeholder")
        return result

    def remote_path(self, relative: str) -> str:
        """Resolve a safe relative path under this remote root.

        Args:
            relative: POSIX path relative to the remote.

        Returns:
            Fully qualified rclone path.

        Raises:
            ValueError: If the path is invalid or escapes the root.
        """
        path = PurePosixPath(relative)
        if (
            not relative
            or path.is_absolute()
            or ".." in path.parts
            or ":" in relative
            or "\\" in relative
            or path == PurePosixPath(".")
        ):
            raise ValueError(
                "remote path must be a relative path below the remote root"
            )
        separator = "" if self.root.endswith(":") else "/"
        return f"{self.root}{separator}{path}"

    def _execute(self, command: Sequence[str]) -> subprocess.CompletedProcess:
        if self.transport is not None:
            result = self._execute_rcd(command)
            if result is not None:
                return result
        if self.credentials is None:
            return self._execute_with(command, self.rclone_config)
        # The shared lock keeps imports and drains out while rclone,
        # which may write refreshed tokens back, uses the copy.
        with self.credentials.in_use() as config:
            return self._execute_with(command, config)

    def _execute_rcd(
        self, command: Sequence[str]
    ) -> subprocess.CompletedProcess | None:
        """Run a generated command through the rcd, if it is ready.

        Returns ``None`` when the command must run as a process instead:
        it is not a generated shape, the rcd is not ready, or it could
        not be reached before anything was requested. Failures after a
        request are raised like failed commands.
        """
        from kp_sync import rclone_rcd

        argv = list(command)
        request = rclone_rcd.translate(argv, self.root)
        if request is None or not self.transport.ready():
            return None
        started = time.monotonic()
        outcome = "failed"
        try:
            stdout = request(self.transport, self.timeout)
            outcome = "ok"
        except rclone_rcd.RcdUnreachable:
            outcome = "unreachable; using a command"
            return None
        except rclone_rcd.RcdTimeout as exc:
            raise EndpointUnavailable(
                f"{self.name}: rcd request timed out after "
                f"{self.timeout} seconds"
            ) from exc
        except rclone_rcd.RcdError as exc:
            # Same exceptions as a failed command, so the engine's
            # retry and restart rules apply unchanged.
            error = EndpointMissing if exc.missing else EndpointUnavailable
            raise error(f"{self.name}: rcd request failed") from exc
        finally:
            info(
                f"rclone {argv[1]} for {self.name} via rcd: "
                f"{time.monotonic() - started:.3f}s ({outcome})"
            )
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    def _execute_with(
        self, command: Sequence[str], config: str | None
    ) -> subprocess.CompletedProcess:
        argv = list(command)
        if config is not None:
            argv.extend(("--config", config))
        started = time.monotonic()
        succeeded = False
        try:
            result = self._runner(
                argv,
                check=True,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            if result.returncode:
                raise subprocess.CalledProcessError(result.returncode, argv)
            succeeded = True
            return result
        except subprocess.CalledProcessError as exc:
            error = (
                EndpointMissing
                if exc.returncode in (3, 4)
                else EndpointUnavailable
            )
            raise error(
                f"{self.name}: command failed (exit status {exc.returncode})"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise EndpointUnavailable(
                f"{self.name}: command timed out after {self.timeout} seconds"
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise EndpointUnavailable(
                f"{self.name}: command failed ({type(exc).__name__})"
            ) from exc
        finally:
            operation = (
                argv[1]
                if argv[0] == "rclone" and len(argv) > 1
                else "custom command"
            )
            info(
                f"rclone {operation} for {self.name}: "
                f"{time.monotonic() - started:.3f}s "
                f"({'ok' if succeeded else 'failed'})"
            )

    def _render(
        self, command: Sequence[str], file_path: Path | None = None
    ) -> list[str]:
        return [
            arg.replace("{remote}", self.database_remote).replace(
                "{file}", str(file_path)
            )
            for arg in command
        ]

    def _objects(self, command: Sequence[str]) -> list[dict]:
        try:
            result = self._execute(command)
        except EndpointMissing:
            return []
        try:
            objects = json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise EndpointError(f"{self.name}: malformed rclone JSON") from exc
        if not isinstance(objects, list) or not all(
            isinstance(item, dict) for item in objects
        ):
            raise EndpointError(
                f"{self.name}: rclone JSON must be a list of objects"
            )
        names = set()
        for item in objects:
            name = item.get("Path", item.get("Name"))
            if not isinstance(name, str) or not name:
                raise EndpointError(
                    f"{self.name}: rclone object has no valid path"
                )
            if name in names:
                raise EndpointError(
                    f"{self.name}: duplicate remote objects; resolve by hand"
                )
            names.add(name)
            hashes = item.get("Hashes", {})
            if not isinstance(hashes, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in hashes.items()
            ):
                raise EndpointError(f"{self.name}: malformed rclone hashes")
            size = item.get("Size")
            if size is not None and (
                type(size) is not int
                or (
                    size < 0 and not (item.get("IsDir") is True and size == -1)
                )
            ):
                raise EndpointError(f"{self.name}: malformed rclone size")
            if "IsDir" in item and type(item["IsDir"]) is not bool:
                raise EndpointError(
                    f"{self.name}: malformed rclone object type"
                )
            if "ModTime" in item and not isinstance(item["ModTime"], str):
                raise EndpointError(
                    f"{self.name}: malformed rclone modification time"
                )
        return objects

    def _single(self, command: Sequence[str]) -> dict | None:
        objects = self._objects(command)
        if len(objects) > 1:
            raise EndpointError(
                f"{self.name}: multiple remote objects; resolve by hand"
            )
        if objects and objects[0].get("IsDir"):
            raise EndpointError(
                f"{self.name}: expected a remote file, found a directory"
            )
        return objects[0] if objects else None

    def track_directory(self, relative: str) -> None:
        """Include a history directory in the filtered remote listing.

        Args:
            relative: Relative history directory below the remote root.

        Notes:
            Invalidates the listing; glob-special paths disable shared
            filtering.
        """
        relative = str(PurePosixPath(relative))
        self.remote_path(relative)
        if _GLOB_SPECIAL & set(relative):
            self._listing_enabled = False
        elif relative not in self._tracked:
            self._tracked.append(relative)
        self._listing = None

    def _relative(self, remote_path: str) -> str | None:
        separator = "" if self.root.endswith(":") else "/"
        prefix = f"{self.root}{separator}"
        if not remote_path.startswith(prefix):
            return None
        relative = remote_path[len(prefix) :]
        try:
            if self.remote_path(relative) != remote_path:
                return None
        except ValueError:
            return None
        return relative

    def _listed(self, relative: str) -> bool:
        if not self._listing_enabled or relative is None:
            return False
        if relative == self.database_relative:
            return True
        path = PurePosixPath(relative)
        return any(
            relative == f"{directory}/state.json"
            or (
                str(path.parent) == f"{directory}/bases"
                and path.suffix == ".kdbx"
            )
            for directory in self._tracked
        )

    def refresh(self) -> dict[str, dict]:
        """Fetch a fresh listing of the database and tracked history.

        Returns:
            Mapping from remote-relative path to rclone object metadata.

        Raises:
            EndpointUnavailable: If rclone cannot inspect the remote.
            EndpointError: If JSON or object metadata is invalid.
        """
        includes = ["/" + self.database_relative]
        depth = len(PurePosixPath(self.database_relative).parts)
        for directory in self._tracked:
            includes += [
                f"/{directory}/state.json",
                f"/{directory}/bases/*.kdbx",
            ]
            depth = max(depth, len(PurePosixPath(directory).parts) + 2)
        argv = [
            "rclone",
            "lsjson",
            "--hash",
            "-R",
            "--files-only",
            "--max-depth",
            str(depth),
        ]
        for pattern in includes:
            argv += ["--include", pattern]
        argv.append(self.root)
        self._listing = None
        listing = {
            item.get("Path", item.get("Name")): item
            for item in self._objects(argv)
        }
        for item in listing.values():
            if item.get("IsDir"):
                raise EndpointError(
                    f"{self.name}: expected remote files, found a directory"
                )
        self._listing = listing
        self._listed_at = time.monotonic()
        return listing

    def _listing_for(
        self, cached: bool, max_age: float | None = None
    ) -> dict[str, dict]:
        if self._listing is not None and (
            cached
            or (
                max_age is not None
                and time.monotonic() - self._listed_at <= max_age
            )
        ):
            return self._listing
        return self.refresh()

    def stat(
        self,
        remote_path: str,
        *,
        cached: bool = False,
        max_age: float | None = None,
    ) -> dict | None:
        """Return metadata for one remote path.

        Args:
            remote_path: Fully qualified path beneath this remote root.
            cached: Reuse any current listing when possible.
            max_age: Maximum allowed age of a reusable listing.

        Returns:
            Object metadata, or ``None`` if the path is absent.
        """
        relative = self._relative(remote_path)
        if self._listed(relative):
            return self._listing_for(cached, max_age).get(relative)
        return self._single(("rclone", "lsjson", "--hash", remote_path))

    def list_files(
        self, remote_directory: str, *, cached: bool = False
    ) -> list[dict]:
        """List files directly within a remote directory.

        Args:
            remote_directory: Fully qualified directory path.
            cached: Reuse a current history listing if available.

        Returns:
            Rclone object metadata for files in the requested directory.
        """
        relative = self._relative(remote_directory)
        if self._listing_enabled and relative in {
            f"{d}/bases" for d in self._tracked
        }:
            prefix = f"{relative}/"
            return [
                {**item, "Name": PurePosixPath(path).name}
                for path, item in self._listing_for(cached).items()
                if path.startswith(prefix) and "/" not in path[len(prefix) :]
            ]
        return self._objects(("rclone", "lsjson", "--hash", remote_directory))

    def probe(
        self,
        *,
        cached: bool = False,
        max_age: float | None = None,
    ) -> EndpointFingerprint | None:
        """Observe database metadata without downloading its content.

        Args:
            cached: Reuse any listing already collected for this run.
            max_age: Maximum age for reusing an existing listing.

        Returns:
            Fingerprint with available hashes, size, and metadata token;
            ``None`` when the remote database is absent.

        Raises:
            EndpointUnavailable: If rclone cannot query the remote.
            EndpointError: If metadata or its SHA-256 hash is invalid.
        """
        if self._listing_enabled:
            item = self._listing_for(cached, max_age).get(
                self.database_relative
            )
        else:
            item = self._single(self._render(self.probe_command))
        self.hashes = dict(item.get("Hashes", {})) if item else {}
        if item is None:
            return None
        sha256 = next(
            (
                value.lower()
                for key, value in self.hashes.items()
                if key.lower().replace("-", "") == "sha256" and value
            ),
            None,
        )
        if sha256 is not None and (
            len(sha256) != 64
            or any(char not in "0123456789abcdef" for char in sha256)
        ):
            raise EndpointError(f"{self.name}: malformed SHA-256 hash")
        token_data = {
            "hashes": self.hashes,
            "size": item.get("Size"),
            "mtime": item.get("ModTime"),
        }
        token = hashlib.sha256(
            json.dumps(
                token_data, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        pairs = _hash_pairs(self.hashes)
        return EndpointFingerprint(
            sha256,
            item.get("Size"),
            token,
            pairs,
            # Without a listed content hash the token is only size and
            # modification time, which a same-size rewrite can keep.
            token_is_content=any(
                algorithm in _CONTENT_HASHES for algorithm, _ in pairs
            ),
        )

    def forget_observations(self) -> None:
        """Invalidate the cached listing before a new sync run."""
        self._listing = None

    def probe_cached(self) -> EndpointFingerprint | None:
        """Probe while reusing this run's listing if available."""
        return self.probe(cached=True)

    def probe_recent(self, max_age: float) -> EndpointFingerprint | None:
        """Probe with a listing no older than ``max_age`` seconds."""
        return self.probe(max_age=max_age)

    def _download(
        self, command: Sequence[str], destination: str | Path
    ) -> None:
        target = Path(destination)
        staged = target.with_name(f".{target.name}.{uuid.uuid4().hex}")
        try:
            if target.is_symlink() or (
                target.exists() and not target.is_file()
            ):
                raise EndpointError(
                    f"{self.name}: download destination is not a regular file"
                )
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            descriptor = os.open(staged, flags, 0o600)
            try:
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
            self._execute(self._render(command, staged))
            if not stat.S_ISREG(staged.lstat().st_mode):
                raise EndpointError(
                    f"{self.name}: download did not produce a regular file"
                )
            os.chmod(staged, 0o600, follow_symlinks=False)
            with staged.open("rb") as downloaded:
                os.fsync(downloaded.fileno())
            os.replace(staged, target)
        except OSError as exc:
            raise EndpointError(
                f"{self.name}: cannot prepare or read download output"
            ) from exc
        finally:
            staged.unlink(missing_ok=True)

    def fetch(self, destination: str | Path) -> None:
        """Download the current database to a private local path.

        Args:
            destination: Local file to receive the remote database.

        Raises:
            :exc:`kp_sync.endpoints.rclone.EndpointMissing`: If absent.
            EndpointUnavailable: If the metadata query fails.
            EndpointError: If the download cannot be safely installed.
        """
        if self.probe(cached=True) is None:
            raise EndpointMissing(f"{self.name}: remote database is absent")
        self._download(self.download_command, destination)

    def _writable(self) -> None:
        if self.readonly:
            raise PermissionError(f"endpoint {self.name} is read-only")

    def store(self, source: str | Path) -> None:
        """Upload a candidate and invalidate cached observations.

        Args:
            source: Verified local database candidate.

        Raises:
            PermissionError: If the endpoint is read-only.
            EndpointUnavailable: If the rclone upload fails.

        Notes:
            Writes the remote database using ``copyto``.
        """
        self._writable()
        try:
            self._execute(self._render(self.upload_command, Path(source)))
        finally:
            self._listing = None

    def download_file(
        self,
        remote_path: str,
        destination: str | Path,
        *,
        cached: bytes | None = None,
        check_metadata: bool = True,
        max_age: float | None = None,
    ) -> bool:
        """Download a remote file or reuse verified cached bytes.

        Args:
            remote_path: Fully qualified source path.
            destination: Local destination path.
            cached: Prior bytes eligible for checksum reuse.
            check_metadata: Check source existence before downloading.
            max_age: Maximum age of reusable metadata.

        Returns:
            ``True`` if bytes were installed, or ``False`` if absent.

        Notes:
            Matching bytes are atomically written to ``destination``.
            Otherwise it downloads to a staged file before replacement.
        """
        metadata = (
            self.stat(remote_path, max_age=max_age)
            if check_metadata or cached is not None
            else {}
        )
        if metadata is None:
            return False
        if cached is not None and metadata.get("Size") == len(cached):
            hashes = {
                key.lower().replace("-", ""): value.lower()
                for key, value in metadata.get("Hashes", {}).items()
                if key.lower().replace("-", "") in {"sha256", "sha1", "md5"}
                and value
            }
            if hashes and all(
                hashlib.new(algorithm, cached).hexdigest() == digest
                for algorithm, digest in hashes.items()
            ):
                target = Path(destination)
                if target.is_symlink() or (
                    target.exists() and not target.is_file()
                ):
                    raise EndpointError(
                        f"{self.name}: download destination is not a "
                        "regular file"
                    )
                atomic_write(target, cached)
                return True
        try:
            self._download(
                ("rclone", "copyto", remote_path, "{file}"), destination
            )
        except EndpointMissing:
            return False
        return True

    def upload_file(self, source: str | Path, remote_path: str) -> None:
        """Upload one file and invalidate the cached listing.

        Args:
            source: Local source file.
            remote_path: Fully qualified remote destination.

        Raises:
            PermissionError: If the endpoint is read-only.
            EndpointUnavailable: If the upload fails.
        """
        self._writable()
        try:
            self._execute(("rclone", "copyto", str(source), remote_path))
        finally:
            self._listing = None

    @staticmethod
    def _batch_names(names: Iterable[str]) -> list[str]:
        result = sorted(set(names))
        for name in result:
            if (
                not isinstance(name, str)
                or not name
                or name in {".", ".."}
                or any(char in name for char in "/\\\n\r\x00")
            ):
                raise ValueError(f"unsafe batch file name: {name!r}")
        return result

    def _files_from(self, names: list[str], directory: Path) -> Path:
        # One name per line, read raw: no globbing or comments.
        listing = directory / "files-from.txt"
        atomic_write(listing, "".join(f"{name}\n" for name in names).encode())
        return listing

    def upload_files(
        self,
        files: Mapping[str, str | Path],
        remote_directory: str,
        *,
        temporary: str | Path,
    ) -> None:
        """Upload several files into one remote directory in one call.

        Args:
            files: Remote file name mapped to its local source.
            remote_directory: Fully qualified destination directory.
            temporary: Private local directory for staging.

        Raises:
            PermissionError: If the endpoint is read-only.
            ValueError: If a name or the directory is unsafe.
            EndpointUnavailable: If the upload fails; some files may
                already have been uploaded.

        Notes:
            Meant for content-addressed files, where a partial upload is
            harmless. Uses ``copy --files-from-raw --no-traverse``.
        """
        self._writable()
        self._child_path(remote_directory)
        names = self._batch_names(files)
        if not names:
            return
        with tempfile.TemporaryDirectory(
            prefix="rclone-upload-", dir=temporary
        ) as name:
            staging = Path(name) / "files"
            staging.mkdir(mode=0o700)
            for item in names:
                source = Path(files[item])
                try:
                    os.link(source, staging / item, follow_symlinks=False)
                except OSError:
                    shutil.copyfile(source, staging / item)
            listing = self._files_from(names, Path(name))
            try:
                self._execute(
                    (
                        "rclone",
                        "copy",
                        "--files-from-raw",
                        str(listing),
                        "--no-traverse",
                        str(staging),
                        remote_directory,
                    )
                )
            finally:
                self._listing = None

    def download_files(
        self,
        remote_directory: str,
        names: Iterable[str],
        destination: str | Path,
    ) -> set[str]:
        """Download several files from one remote directory in one call.

        Args:
            remote_directory: Fully qualified source directory.
            names: File names below ``remote_directory``.
            destination: Empty private local directory to fill.

        Returns:
            Names now present in ``destination`` as regular files;
            absent remote files are simply missing from the result.

        Raises:
            ValueError: If a name or the directory is unsafe.
            EndpointUnavailable: If the download fails.
            EndpointError: If the destination is not an empty directory.

        Notes:
            The caller must verify each file's content before use.
        """
        self._child_path(remote_directory)
        names = self._batch_names(names)
        target = Path(destination)
        try:
            metadata = target.lstat()
        except OSError as exc:
            raise EndpointError(
                f"{self.name}: download directory is missing"
            ) from exc
        if not stat.S_ISDIR(metadata.st_mode) or any(target.iterdir()):
            raise EndpointError(
                f"{self.name}: download directory must be empty"
            )
        if not names:
            return set()
        with tempfile.TemporaryDirectory(
            prefix="rclone-download-", dir=target.parent
        ) as name:
            listing = self._files_from(names, Path(name))
            try:
                self._execute(
                    (
                        "rclone",
                        "copy",
                        "--files-from-raw",
                        str(listing),
                        "--no-traverse",
                        remote_directory,
                        str(target),
                    )
                )
            except EndpointMissing:
                return set()
        present = set()
        for item in names:
            path = target / item
            try:
                if not stat.S_ISREG(path.lstat().st_mode):
                    raise EndpointError(
                        f"{self.name}: download did not produce a "
                        "regular file"
                    )
            except FileNotFoundError:
                continue
            os.chmod(path, 0o600, follow_symlinks=False)
            present.add(item)
        return present

    def delete_files(
        self,
        remote_directory: str,
        names: Iterable[str],
        *,
        temporary: str | Path,
    ) -> None:
        """Delete several files directly within one remote directory.

        Args:
            remote_directory: Fully qualified directory path.
            names: File names directly below it; absent ones are fine.
            temporary: Private local directory for the name list.

        Raises:
            PermissionError: If the endpoint is read-only.
            ValueError: If a name or the directory is unsafe.
            EndpointUnavailable: If deletion fails; some files may
                already be deleted.
        """
        self._writable()
        self._child_path(remote_directory)
        names = self._batch_names(names)
        if not names:
            # An empty list must never reach rclone delete.
            return
        with tempfile.TemporaryDirectory(
            prefix="rclone-delete-", dir=temporary
        ) as name:
            listing = self._files_from(names, Path(name))
            try:
                self._execute(
                    (
                        "rclone",
                        "delete",
                        "--files-from-raw",
                        str(listing),
                        "--max-depth",
                        "1",
                        remote_directory,
                    )
                )
            except EndpointMissing:
                pass
            finally:
                self._listing = None

    def _child_path(self, remote_path: str) -> None:
        separator = "" if self.root.endswith(":") else "/"
        prefix = f"{self.root}{separator}"
        if not remote_path.startswith(prefix):
            raise ValueError(
                "refusing to remove a path outside the remote root"
            )
        relative = remote_path[len(prefix) :]
        if self.remote_path(relative) != remote_path:
            raise ValueError("refusing to remove an unsafe remote path")

    def delete_file(self, remote_path: str) -> None:
        """Delete a file below the configured remote root.

        Args:
            remote_path: Fully qualified file path.

        Raises:
            PermissionError: If the endpoint is read-only.
            ValueError: If the path is outside the root or unsafe.
            EndpointUnavailable: If deletion fails.
        """
        self._writable()
        self._child_path(remote_path)
        try:
            self._execute(("rclone", "deletefile", remote_path))
        except EndpointMissing:
            pass
        finally:
            self._listing = None

    def remove_directory(self, remote_path: str) -> None:
        """Remove an empty directory below the configured remote root.

        Args:
            remote_path: Fully qualified directory path.

        Raises:
            PermissionError: If the endpoint is read-only.
            ValueError: If the path is outside the root or unsafe.
            EndpointUnavailable: If removal fails.
        """
        self._writable()
        self._child_path(remote_path)
        try:
            self._execute(("rclone", "rmdir", remote_path))
        except EndpointMissing:
            pass
