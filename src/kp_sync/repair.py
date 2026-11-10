# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Inspect and repair retained versions whose bases are missing.

See "Missing bases and repair" in the history design chapter. Preview
changes nothing; apply restores recoverable bases and removes versions
whose base exists nowhere, then publishes through the normal mechanism.
"""

import tempfile
from pathlib import Path

from kp_sync.baseline import BaselineManager
from kp_sync.endpoints.base import EndpointFingerprint, EndpointUnavailable
from kp_sync.endpoints.file import FileEndpoint
from kp_sync.history import contract_versions
from kp_sync.kdbx import KdbxError, inspect_kdbx
from kp_sync.lock import file_lock
from kp_sync.paths import ensure_run_dir
from kp_sync.state import StateError


def _present(size, expected) -> bool:
    return size is not None and (not expected or size == expected)


def _local_copies(engine, versions) -> dict[str, Path]:
    """Hash the hub and local file/pendrive copies holding versions.

    Only local files are read; nothing is downloaded. The baseline
    manager copies and re-verifies a file before using it.
    """
    candidates = [Path(engine.config.hub)]
    for endpoint in engine.endpoints:
        if not isinstance(endpoint, FileEndpoint):
            continue
        try:
            check = getattr(endpoint, "_check_mount", None)
            if check is not None:
                check()
        except EndpointUnavailable:
            continue
        candidates.append(endpoint.path)
    found = {}
    for path in candidates:
        try:
            if path.is_symlink() or not path.is_file():
                continue
            digest = EndpointFingerprint.from_file(path).sha256
        except OSError:
            continue
        if digest in versions:
            found.setdefault(digest, path)
    return found


def _cached(runtime: Path, sha: str) -> bool:
    path = runtime / "bases" / f"{sha}.kdbx"
    if path.is_symlink() or not path.is_file():
        return False
    try:
        return inspect_kdbx(path).sha256 == sha
    except (OSError, KdbxError):
        return False


def repair_history(config, *, apply=False, writers_paused=False, engine=None):
    """Report missing bases, or restore them and remove lost versions.

    Args:
        config: Loaded profile configuration.
        apply: Change history; requires ``writers_paused``.
        writers_paused: The operator confirms every other writer of this
            history (hosts and profiles) is paused throughout.
        engine: Optional engine supplying endpoints and stores (tests).

    Returns:
        A JSON-serializable report. Preview never writes.

    Raises:
        ValueError: If apply is not confirmed or is blocked.
    """
    from kp_sync.cleanup import _blockers
    from kp_sync.engine import SyncEngine, endpoints_from_config

    if apply and not writers_paused:
        raise ValueError(
            "repair-history --apply requires --confirm-writers-paused"
        )
    runtime = (
        engine.run_directory if engine else ensure_run_dir(config.profile)
    )
    engine = engine or SyncEngine(
        config, endpoints_from_config(config, runtime), run_directory=runtime
    )
    report = {
        "apply": apply,
        "versions": {},
        "detached_drives": [],
        "blocked": [],
        "removed": [],
        "removed_copies": [],
        "stores": {},
    }
    with (
        file_lock(runtime / "access-op.lock", config.sync["lock_timeout"]),
        file_lock(runtime / "lock", config.sync["lock_timeout"]),
    ):
        with tempfile.TemporaryDirectory(
            dir=runtime, prefix="repair-"
        ) as directory:
            temporary = Path(directory)
            manager = BaselineManager(runtime, engine.baseline_stores)
            state = manager.load(temporary, read_only=not apply)
            blockers = list(_blockers(runtime))
            blockers += [
                f"unavailable history: {name}"
                for name, outcome in manager.publication_outcomes.items()
                if outcome == "unavailable"
            ]
            inventories = {}
            for store in manager.available:
                try:
                    inventories[store.name] = store.base_sizes()
                except (EndpointUnavailable, OSError, StateError) as exc:
                    blockers.append(f"cannot list {store.name}: {exc}")
            local = _local_copies(engine, state.versions)
            for sha, version in sorted(state.versions.items()):
                name = f"{sha}.kdbx"
                holders = [
                    store
                    for store, inventory in sorted(inventories.items())
                    if _present(inventory.get(name), version.size)
                ]
                lacking = sorted(set(inventories) - set(holders))
                sources = holders[:]
                if _cached(runtime, sha):
                    sources.append("runtime cache")
                if sha in local:
                    sources.append(f"local copy {local[sha]}")
                if lacking or not sources:
                    report["versions"][sha] = {
                        "missing_from": lacking,
                        "available_from": sources,
                        "recoverable": bool(sources),
                        "held_by": sorted(
                            key
                            for key, record in state.copies.items()
                            if record.version == sha
                        ),
                    }
            attached = {store.name for store in manager.available}
            report["detached_drives"] = sorted(
                key
                for key in state.copies
                if key.startswith("drive:") and key not in attached
            )
            report["blocked"] = blockers
            if not apply:
                return report
            if blockers:
                raise ValueError("; ".join(blockers))
            lost = {
                sha
                for sha, entry in report["versions"].items()
                if not entry["recoverable"]
            }
            if lost:
                keep = set(state.versions) - lost
                state.versions = contract_versions(state.versions, keep)
                report["removed"] = sorted(lost)
                report["removed_copies"] = sorted(
                    key
                    for key, record in state.copies.items()
                    if record.version in lost
                )
                state.copies = {
                    key: record
                    for key, record in state.copies.items()
                    if record.version not in lost
                }
            state.save(runtime / "state.json")
            manager.save(state, temporary, sources=local)
            report["stores"] = dict(manager.publication_outcomes)
    return report
