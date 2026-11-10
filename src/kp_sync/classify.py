# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Classify copies against shared version history."""

from dataclasses import dataclass
from enum import Enum

from kp_sync.history import CopyRecord, Version, current_version, is_ancestor


class CopyState(str, Enum):
    """Possible relationships between a copy and the current version."""

    UNCHANGED = "unchanged"
    STALE = "stale"
    DIVERGED = "diverged"
    CHANGED = "changed"
    NEW = "new"
    MISSING = "missing"


class PersistentHubMissing(RuntimeError):
    """Raised when a configured persistent hub disappears."""

    pass


@dataclass(frozen=True)
class Classification:
    """Classification and observed hash for one physical copy."""

    identity: str
    state: CopyState
    sha256: str | None
    record: CopyRecord | None


def classify_copy(
    identity: str,
    sha256: str | None,
    record: CopyRecord | None,
    versions: dict[str, Version],
    current: str | None,
    *,
    hub: bool = False,
    hub_persistent: bool = False,
) -> Classification:
    """Classify a copy as current, stale, divergent, changed, or new.

    Missing persistent hubs raise ``PersistentHubMissing``; they are not
    silently repopulated.
    """
    if sha256 is None:
        if hub and hub_persistent:
            raise PersistentHubMissing(
                f"persistent hub is missing: {identity}"
            )
        return Classification(
            identity, CopyState.MISSING, None, None if hub else record
        )

    if sha256 in versions:
        if current is None or sha256 == current:
            state = CopyState.UNCHANGED
        elif is_ancestor(sha256, current, versions):
            state = CopyState.STALE
        else:
            state = CopyState.DIVERGED
        return Classification(identity, state, sha256, record)

    if record is not None and record.version in versions:
        return Classification(identity, CopyState.CHANGED, sha256, record)
    return Classification(identity, CopyState.NEW, sha256, None)


def classify_all(
    fingerprints: dict[str, str | None],
    copies: dict[str, CopyRecord],
    versions: dict[str, Version],
    *,
    hub_identity: str,
    hub_persistent: bool = False,
) -> tuple[str | None, dict[str, Classification]]:
    """Find the current version and classify every observed copy."""
    reachable_records = {
        identity: copies[identity]
        for identity, fingerprint in fingerprints.items()
        if fingerprint is not None and identity in copies
    }
    current = current_version(versions, reachable_records)
    results = {
        identity: classify_copy(
            identity,
            fingerprint,
            copies.get(identity),
            versions,
            current,
            hub=identity == hub_identity,
            hub_persistent=hub_persistent,
        )
        for identity, fingerprint in fingerprints.items()
    }
    return current, results
