"""Common contracts and fingerprints for synchronization endpoints."""

import hashlib
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


class EndpointError(RuntimeError):
    """Base error for endpoint operations that cannot be completed."""

    pass


class EndpointUnavailable(EndpointError):
    """Raised when an endpoint cannot be reached or accessed."""

    pass


class EndpointChanged(EndpointError):
    """Raised when a target changed during a guarded file operation.

    Attributes:
        preserved_path: A displaced copy retained for recovery, if any.
        observed_sha256: Hash of raced content, if known.
    """

    def __init__(
        self,
        message: str,
        *,
        preserved_path: str | Path | None = None,
        observed_sha256: str | None = None,
    ) -> None:
        super().__init__(message)
        self.preserved_path = (
            Path(preserved_path) if preserved_path is not None else None
        )
        self.observed_sha256 = observed_sha256


_CONTENT_HASHES = {"sha256": "sha256", "sha1": "sha1", "md5": "md5"}


@dataclass(frozen=True)
class EndpointFingerprint:
    """Observed content and metadata for one endpoint copy.

    Attributes:
        sha256: Known content hash, or ``None`` if unavailable.
        size: Observed byte count, when available.
        token: Opaque metadata token for detecting changes.
        hashes: Server-provided hashes as sorted algorithm/value pairs.
    """

    sha256: str | None
    size: int | None = None
    token: str | None = None
    # Server-side content hashes, as sorted algorithm/digest pairs.
    hashes: tuple[tuple[str, str], ...] = ()

    def content_hashes(self) -> dict[str, str]:
        """Return supported hashes, including the SHA-256 field."""
        result = {
            algorithm: value
            for algorithm, value in self.hashes
            if algorithm in _CONTENT_HASHES and value
        }
        if self.sha256:
            result["sha256"] = self.sha256
        return result

    def matches(
        self, sha256: str | None, path: str | Path | None
    ) -> bool | None:
        """Compare endpoint and local content when hashes permit.

        Args:
            sha256: Known local SHA-256 digest, if available.
            path: Local file used to calculate server-supported digests.

        Returns:
            ``True`` or ``False`` when comparable, otherwise ``None``.

        Notes:
            Reads ``path`` when SHA-256 comparison is not sufficient.
        """
        if self.sha256 is not None and sha256 is not None:
            return self.sha256 == sha256
        hashes = self.content_hashes()
        if not hashes or path is None:
            return None
        digests = {algorithm: hashlib.new(algorithm) for algorithm in hashes}
        size = 0
        with open(path, "rb") as source:
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                for digest in digests.values():
                    digest.update(chunk)
        if self.size is not None and self.size != size:
            return False
        return all(
            digests[name].hexdigest() == value
            for name, value in hashes.items()
        )

    @classmethod
    def from_file(cls, path: str | Path) -> "EndpointFingerprint":
        """Hash a local file and return its SHA-256 digest and size.

        Args:
            path: Regular file to read.

        Returns:
            Fingerprint containing the file's SHA-256 and size.
        """
        digest = hashlib.sha256()
        size = 0
        with open(path, "rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        return cls(digest.hexdigest(), size)


class Endpoint(ABC):
    """Abstract endpoint with common identity and availability policy.

    Concrete implementations transfer a database copy and report its
    fingerprint and raise ``EndpointUnavailable`` when unreachable.
    """

    def __init__(
        self,
        name: str,
        identity: str,
        *,
        readonly: bool = False,
        optional: bool = False,
        on_unavailable: str = "warn",
        timeout: float = 30,
        foreign_writers: bool = False,
    ) -> None:
        """Initialize endpoint identity and synchronization policy.

        Args:
            name: Configuration-facing endpoint name.
            identity: Stable physical-copy identity used in history.
            readonly: Whether writes must be rejected.
            optional: Whether an absent copy may be skipped.
            on_unavailable: ``skip``, ``warn``, or ``fail`` policy.
            timeout: Per-operation timeout in seconds.
            foreign_writers: Whether other programs may change the copy.

        Raises:
            ValueError: If the unavailable policy or timeout is invalid.
        """
        if on_unavailable not in {"skip", "warn", "fail"}:
            raise ValueError("on_unavailable must be skip, warn, or fail")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        self.name = name
        self.identity = identity
        self.readonly = readonly
        self.optional = optional
        self.on_unavailable = on_unavailable
        self.timeout = timeout
        self.foreign_writers = foreign_writers
        # A target that is overwritten but never promoted.
        self.never_source = False

    @abstractmethod
    def fetch(self, destination: str | Path) -> None:
        """Copy the endpoint database to a private local destination.

        Args:
            destination: Path for the fetched copy.

        Raises:
            EndpointUnavailable: If the copy is absent or unreadable.
        """
        raise NotImplementedError

    @abstractmethod
    def store(self, source: str | Path) -> None:
        """Replace the endpoint copy with a verified local candidate.

        Args:
            source: Local candidate file to publish.

        Raises:
            PermissionError: If the endpoint is read-only.
            EndpointUnavailable: If publication fails.
        """
        raise NotImplementedError

    @abstractmethod
    def probe(self) -> EndpointFingerprint | None:
        """Observe the endpoint copy without modifying it.

        Returns:
            Its fingerprint, or ``None`` when the endpoint has no copy.

        Raises:
            EndpointUnavailable: If the endpoint cannot be inspected.
        """
        raise NotImplementedError

    def forget_observations(self) -> None:
        """Drop cached observations before a new synchronization run."""

    def probe_cached(self) -> EndpointFingerprint | None:
        """Probe, possibly reusing a recent observation from this run.

        Returns:
            The observed fingerprint, or ``None`` when no copy exists.
        """
        return self.probe()

    def probe_recent(self, max_age: float) -> EndpointFingerprint | None:
        """Probe while allowing observations no older than ``max_age``.

        Args:
            max_age: Maximum age in seconds of a reusable observation.

        Returns:
            The observed fingerprint, or ``None`` when no copy exists.
        """
        return self.probe()

    def available(self) -> bool:
        """Return whether a probe can reach the endpoint.

        Returns:
            ``False`` only when probing raises ``EndpointUnavailable``.
        """
        try:
            self.probe()
        except EndpointUnavailable:
            return False
        return True
