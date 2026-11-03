"""Persistent named and transient connection-bound daemon leases."""

import json
import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from kp_sync.paths import atomic_write
from kp_sync.pending import _read_private_json


@dataclass(frozen=True)
class Lease:
    """One daemon activity lease.

    Attributes:
        name: Unique lease name.
        kind: Lease category used by daemon lifecycle management.
        owner: Connection identifier for transient leases.
        expires: Optional wall-clock expiry for access leases.
    """

    name: str
    kind: str
    owner: str | None = None
    expires: float | None = None


class LeaseStore:
    """Maintain leases and persist only restart-safe named leases.

    Drive and access leases without a connection owner are serialized to
    ``leases.json``. Client, session, app, and pending leases remain in
    memory and disappear with their owner or daemon.
    """

    def __init__(self, directory: Path, *, clock=time.time, access_ttl=3600):
        """Load persisted leases and discard expired access leases.

        Args:
            directory: Private profile runtime directory.
            clock: Wall-clock source used for expiration timestamps.
            access_ttl: Lifetime in seconds assigned to access leases.

        Raises:
            ValueError: If the persisted lease file is malformed.
        """
        self.path = Path(directory) / "leases.json"
        self.clock = clock
        self.access_ttl = access_ttl
        self.leases: dict[str, Lease] = {}
        if self.path.exists() or self.path.is_symlink():
            payload = _read_private_json(self.path)
            if not isinstance(payload, dict) or payload.get("version") != 1:
                raise ValueError("invalid leases file")
            rows = payload.get("leases")
            if not isinstance(rows, list):
                raise ValueError("invalid leases list")
            for row in rows:
                if not isinstance(row, dict) or set(row) != {
                    "name",
                    "kind",
                    "owner",
                    "expires",
                }:
                    raise ValueError("invalid saved lease")
                lease = Lease(**row)
                self._validate(lease)
                if (
                    lease.kind not in {"drive", "access"}
                    or lease.owner is not None
                ):
                    raise ValueError(
                        "only named drive/access leases may be persisted"
                    )
                self.leases[lease.name] = lease
        self.expire()

    @staticmethod
    def _validate(lease):
        if (
            not isinstance(lease.name, str)
            or not lease.name
            or len(lease.name) > 4096
            or lease.kind
            not in {"drive", "access", "app", "session", "client", "pending"}
            or (lease.owner is not None and not isinstance(lease.owner, str))
            or (
                lease.expires is not None
                and (
                    type(lease.expires) not in {int, float}
                    or not math.isfinite(lease.expires)
                )
            )
        ):
            raise ValueError("invalid lease")

    def take(self, name, kind, *, owner=None):
        """Create or refresh a named lease.

        Args:
            name: Unique lease name.
            kind: Supported lease category.
            owner: Owning connection for transient leases.

        Returns:
            ``True`` if a new lease was created, else ``False``.

        Raises:
            ValueError: If invalid or owned by another connection.
        """
        lease = Lease(
            name,
            kind,
            owner,
            self.clock() + self.access_ttl if kind == "access" else None,
        )
        self._validate(lease)
        old = self.leases.get(name)
        if old is not None and old.owner != owner:
            raise ValueError("lease belongs to another connection")
        if old == lease:
            return False
        self.leases[name] = lease
        self._save()
        return old is None

    def release(self, name, *, owner=None):
        """Release a lease if the caller owns it.

        Args:
            name: Lease name to remove.
            owner: Connection owner, or ``None`` for named leases.

        Returns:
            ``True`` if removed, or ``False`` if it did not exist.

        Raises:
            ValueError: If another connection owns the lease.
        """
        lease = self.leases.get(name)
        if lease is None:
            return False
        if lease.owner != owner:
            raise ValueError("lease belongs to another connection")
        del self.leases[name]
        self._save()
        return True

    def disconnect(self, owner):
        """Remove every transient lease owned by a disconnected client.

        Args:
            owner: Connection identifier whose leases are released.

        Returns:
            Whether any leases were removed.
        """
        removed = [
            name for name, lease in self.leases.items() if lease.owner == owner
        ]
        for name in removed:
            del self.leases[name]
        if removed:
            self._save()
        return bool(removed)

    def expire(self):
        """Remove leases whose expiry time has passed.

        Returns:
            Whether any leases were removed.

        Notes:
            Persists remaining named leases if expiry changed the set.
        """
        removed = [
            name
            for name, lease in self.leases.items()
            if lease.expires is not None and lease.expires <= self.clock()
        ]
        for name in removed:
            del self.leases[name]
        if removed:
            self._save()
        return bool(removed)

    def _save(self):
        rows = [
            asdict(lease)
            for lease in self.leases.values()
            if lease.kind in {"drive", "access"} and lease.owner is None
        ]
        atomic_write(
            self.path, json.dumps({"version": 1, "leases": rows}) + "\n"
        )

    def to_dict(self):
        """Return all leases as a name-keyed JSON-compatible mapping."""
        return {name: asdict(lease) for name, lease in self.leases.items()}
