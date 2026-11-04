"""Endpoint adapter for configured argument-vector transfer commands."""

import os
import stat
import subprocess
import time
from pathlib import Path
from typing import Sequence

from kp_sync.endpoints.base import (
    Endpoint,
    EndpointFingerprint,
    EndpointUnavailable,
)
from kp_sync.log import debug, logged


class CommandEndpoint(Endpoint):
    """Endpoint whose operations run external commands.

    Commands are argument vectors, never shell strings. Fetch and store
    commands substitute ``{file}`` with their local path.
    """

    def __init__(
        self,
        name: str,
        fetch: Sequence[str],
        store: Sequence[str],
        *,
        probe: Sequence[str] | None = None,
        probe_is_content_hash: bool = False,
        identity: str | None = None,
        **kwargs,
    ) -> None:
        """Configure command templates for endpoint operations.

        Args:
            name: Endpoint name.
            fetch: Command template that writes to ``{file}``.
            store: Command template that reads from ``{file}``.
            probe: Optional command returning a fingerprint token.
            probe_is_content_hash: Whether the probe prints a content
                hash, so that it changes whenever the content changes.
                Only then may an unchanged token skip the download.
            identity: Copy identity; defaults to ``command:<name>``.
            **kwargs: Policy options accepted by ``Endpoint``.

        Raises:
            ValueError: If a required command or placeholder is invalid.
        """
        self.fetch_command = tuple(fetch)
        self.store_command = tuple(store)
        self.probe_command = tuple(probe) if probe is not None else None
        if probe_is_content_hash and self.probe_command is None:
            raise ValueError("probe_is_content_hash requires a probe command")
        self.probe_is_content_hash = bool(probe_is_content_hash)
        if not self.fetch_command or not self.store_command:
            raise ValueError("fetch and store commands must not be empty")
        if not any("{file}" in arg for arg in self.fetch_command):
            raise ValueError("fetch command must contain a {file} placeholder")
        if not any("{file}" in arg for arg in self.store_command):
            raise ValueError("store command must contain a {file} placeholder")
        super().__init__(name, identity or f"command:{name}", **kwargs)

    def fetch(self, destination: str | os.PathLike[str]) -> None:
        """Run the fetch command and validate its output file.

        Args:
            destination: Local path substituted for ``{file}``.

        Raises:
            EndpointUnavailable: If the command fails or output is bad.
        """
        target = Path(destination)
        self._run(self.fetch_command, target, operation="fetch")
        try:
            metadata = target.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise EndpointUnavailable(
                    f"{self.name}: fetch did not create a regular file"
                )
            os.chmod(target, 0o600, follow_symlinks=False)
        except OSError as exc:
            raise EndpointUnavailable(
                f"{self.name}: fetch did not create its output: {exc}"
            ) from exc

    def store(self, source: str | os.PathLike[str]) -> None:
        """Run the store command for a local candidate.

        Args:
            source: File substituted for ``{file}``.

        Raises:
            PermissionError: If this endpoint is read-only.
            EndpointUnavailable: If the command fails.
        """
        if self.readonly:
            raise PermissionError(f"endpoint {self.name} is read-only")
        self._run(self.store_command, Path(source), operation="store")

    def probe(self) -> EndpointFingerprint | None:
        """Run the optional probe command and capture its output token.

        Returns:
            Token fingerprint, or ``None`` if probing is disabled.

        Raises:
            ValueError: If the probe template uses ``{file}``.
            EndpointUnavailable: If command fails or has no token.
        """
        if self.probe_command is None:
            return None
        if any("{file}" in arg for arg in self.probe_command):
            raise ValueError("probe command cannot use a {file} placeholder")
        result = self._execute(self.probe_command, operation="probe")
        token = result.stdout.strip()
        if not token:
            raise EndpointUnavailable(
                f"{self.name}: probe returned an empty fingerprint"
            )
        return EndpointFingerprint(
            None, token=token, token_is_content=self.probe_is_content_hash
        )

    def _run(
        self, command: tuple[str, ...], file_path: Path, *, operation: str
    ) -> None:
        self._execute(command, file_path=file_path, operation=operation)

    def _execute(
        self,
        command: tuple[str, ...],
        *,
        file_path: Path | None = None,
        operation: str,
    ) -> subprocess.CompletedProcess[str]:
        argv = [arg.replace("{file}", str(file_path)) for arg in command]
        started = time.monotonic()
        outcome = "failed"
        try:
            result = logged(subprocess.run)(
                argv,
                check=True,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            outcome = "ok"
            return result
        except (OSError, subprocess.SubprocessError) as exc:
            if isinstance(exc, subprocess.CalledProcessError):
                detail = f" (exit status {exc.returncode})"
            elif isinstance(exc, subprocess.TimeoutExpired):
                detail = f" (timed out after {self.timeout} seconds)"
            elif isinstance(exc, FileNotFoundError):
                detail = " (executable not found)"
            else:
                detail = f" ({type(exc).__name__})"
            raise EndpointUnavailable(
                f"{self.name}: command failed{detail}"
            ) from exc
        finally:
            # The command line is logged by logged(); never log
            # stdin or output.
            debug(
                f"command {operation} for {self.name}: "
                f"{time.monotonic() - started:.3f}s ({outcome})"
            )
