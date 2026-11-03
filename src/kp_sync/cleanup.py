"""Remove unreferenced bases without downloading databases."""

import os
import re
import tempfile
from pathlib import Path

from kp_sync.baseline import BaselineManager
from kp_sync.endpoints.base import EndpointUnavailable
from kp_sync.lock import file_lock
from kp_sync.paths import ensure_run_dir
from kp_sync.pending import PendingStore
from kp_sync.sessions.records import SessionStore
from kp_sync.state import HistoryState

_BASE = re.compile(r"[0-9a-f]{64}\.kdbx\Z")


def _blockers(runtime: Path) -> list[str]:
    reasons = []
    if PendingStore(runtime).load()["decisions"]:
        reasons.append("pending decisions")
    for name in (
        "inflight.json",
        "write-race.json",
        "resolved-displaced.json",
    ):
        if os.path.lexists(runtime / name):
            reasons.append(name)
    if SessionStore(runtime).pending_retirements():
        reasons.append("unfinished session retirement")
    return reasons


def cleanup(
    config,
    *,
    store_name=None,
    runtime_only=False,
    apply=False,
    writers_paused=False,
    engine=None,
):
    """Preview obsolete bases or delete them within an explicit scope.

    All versions in reconciled history are protected, including offline
    copies and merge ancestors. History is not pruned or published here.
    Apply holds local locks and rechecks available history before each
    deletion. Shared apply requires the caller to confirm other writers
    are paused for the entire operation; remote checks are not a
    cross-host lock. Unavailable history or recovery work
    blocks deletion. No base contents or credentials are fetched.
    """
    from kp_sync.engine import SyncEngine, endpoints_from_config

    if runtime_only and store_name:
        raise ValueError("choose a store or runtime, not both")
    if apply and not (store_name or runtime_only):
        raise ValueError("apply requires an explicit cleanup scope")
    if apply and store_name and not writers_paused:
        raise ValueError("confirm other shared-history writers are paused")
    runtime = (
        engine.run_directory if engine else ensure_run_dir(config.profile)
    )
    engine = engine or SyncEngine(
        config, endpoints_from_config(config, runtime), run_directory=runtime
    )
    selected = [
        store
        for store in engine.baseline_stores
        if store_name is None or store.endpoint.name == store_name
    ]
    if store_name and not selected:
        raise ValueError(f"no attached baseline store named {store_name!r}")
    if runtime_only:
        selected = []
    report = {"apply": apply, "stores": {}, "errors": []}
    with (
        file_lock(runtime / "access-op.lock", config.sync["lock_timeout"]),
        file_lock(runtime / "lock", config.sync["lock_timeout"]),
    ):
        with tempfile.TemporaryDirectory(
            dir=runtime, prefix="cleanup-"
        ) as directory:
            temporary = Path(directory)
            manager = BaselineManager(runtime, engine.baseline_stores)
            state = manager.load(temporary, read_only=True)
            local = HistoryState.load(runtime / "state.json").to_dict()
            blockers = _blockers(runtime)
            if not state.versions:
                blockers.append("no retained version history")
            unavailable = [
                name
                for name, outcome in manager.publication_outcomes.items()
                if outcome == "unavailable"
            ]
            blockers.extend(
                f"unavailable history: {name}" for name in unavailable
            )
            scopes = [(store.name, store) for store in selected]
            if runtime_only or store_name is None:
                scopes.append(("runtime", None))
            for name, store in scopes:
                entry = {
                    "candidates": [],
                    "candidate_bytes": 0,
                    "protected": [],
                    "deleted": [],
                    "blocked": blockers.copy(),
                }
                report["stores"][name] = entry
                try:
                    if store is not None:
                        if store not in manager.available:
                            raise EndpointUnavailable("history unavailable")
                        if manager.snapshots[store.name] is None:
                            entry["blocked"].append("store history is absent")
                        inventory = store.base_sizes()
                    else:
                        bases = runtime / "bases"
                        if bases.is_symlink():
                            raise ValueError(
                                "runtime bases is a symbolic link"
                            )
                        inventory = (
                            {
                                path.name: path.stat().st_size
                                for path in bases.iterdir()
                                if not path.is_symlink() and path.is_file()
                            }
                            if bases.exists()
                            else {}
                        )
                    for filename, size in sorted(inventory.items()):
                        if not _BASE.fullmatch(filename):
                            continue
                        if filename[:-5] in state.versions:
                            entry["protected"].append(filename)
                        else:
                            entry["candidates"].append(filename)
                            entry["candidate_bytes"] += size
                    if apply and entry["blocked"]:
                        raise ValueError("; ".join(entry["blocked"]))
                    for filename in entry["candidates"] if apply else ():
                        manager.check(temporary, read_only=True)
                        if HistoryState.load(
                            runtime / "state.json"
                        ).to_dict() != local or _blockers(runtime):
                            raise ValueError(
                                "runtime history/recovery changed"
                            )
                        if store is None:
                            if (runtime / "bases").is_symlink():
                                raise ValueError(
                                    "runtime bases became a symbolic link"
                                )
                            path = runtime / "bases" / filename
                            if path.is_symlink():
                                raise ValueError("base became a symbolic link")
                            path.unlink(missing_ok=True)
                            descriptor = os.open(
                                path.parent, os.O_RDONLY | os.O_DIRECTORY
                            )
                            try:
                                os.fsync(descriptor)
                            finally:
                                os.close(descriptor)
                        else:
                            store.delete_base(filename)
                        entry["deleted"].append(filename)
                except (OSError, RuntimeError, ValueError) as exc:
                    report["errors"].append(
                        f"{name}: {type(exc).__name__}: {exc}"
                    )
    return report
