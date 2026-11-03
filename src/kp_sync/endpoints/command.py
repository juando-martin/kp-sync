"""Endpoint adapter for configured argument-vector transfer commands."""

import os
import stat
import subprocess
from pathlib import Path
from typing import Sequence

from kp_sync.endpoints.base import (
    Endpoint,
    EndpointFingerprint,
    EndpointUnavailable,
)


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
        identity: str | None = None,
        **kwargs,
    ) -> None:
        """Configure command templates for endpoint operations.

        Args:
            name: Endpoint name.
            fetch: Command template that writes to ``{file}``.
            store: Command template that reads from ``{file}``.
            probe: Optional command returning a fingerprint token.
            identity: Copy identity; defaults to ``command:<name>``.
            **kwargs: Policy options accepted by ``Endpoint``.

        Raises:
            ValueError: If a required command or placeholder is invalid.
        """
        self.fetch_command = tuple(fetch)
        self.store_command = tuple(store)
        self.probe_command = tuple(probe) if probe is not None else None
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
        self._run(self.fetch_command, target)
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
        self._run(self.store_command, Path(source))

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
        result = self._execute(self.probe_command)
        token = result.stdout.strip()
        if not token:
            raise EndpointUnavailable(
                f"{self.name}: probe returned an empty fingerprint"
            )
        return EndpointFingerprint(None, token=token)

    def _run(self, command: tuple[str, ...], file_path: Path) -> None:
        self._execute(command, file_path=file_path)

    def _execute(
        self,
        command: tuple[str, ...],
        *,
        file_path: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        argv = [arg.replace("{file}", str(file_path)) for arg in command]
        try:
            return subprocess.run(
                argv,
                check=True,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
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
