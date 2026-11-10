# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Skip the timer's idle sync while the cloud is clearly unreachable.

The systemd timer runs ``kp-sync sync --idle`` every
``idle_sync_interval``. Offline, each run would start the daemon, sync
nothing new and end as a failed unit. Before contacting the daemon, an
idle run checks every enabled cloud locally (the cloud reach check, and
whether provider credentials are published); when none can be reached
and an idle sync ran recently, it is skipped. See "Idle sync while
offline" in the lifecycle design chapter.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from kp_sync.paths import atomic_write

RECORD = "idle-sync.json"
# While offline, one idle sync still runs per this many intervals, so
# the daemon's cloud staleness warning keeps its chances.
OFFLINE_RUN_EVERY = 8


def _last_run(run_directory: Path) -> float | None:
    try:
        value = json.loads((run_directory / RECORD).read_text())["ran_at"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return float(value) if isinstance(value, (int, float)) else None


def record_run(run_directory: Path, *, now: float | None = None) -> None:
    """Remember that an idle sync ran (whatever its outcome)."""
    atomic_write(
        run_directory / RECORD,
        json.dumps({"ran_at": time.time() if now is None else now}),
    )


def _unreachable(endpoint, run_directory: Path) -> str | None:
    """Why this cloud is clearly unreachable now, else None."""
    from kp_sync.engine import _reach_check

    config_path = None
    if endpoint.rclone_config is not None:
        from kp_sync.rclone_credentials import RcloneCredentials

        credentials = RcloneCredentials(
            endpoint.name, endpoint.rclone_config, run_directory
        )
        if not credentials.external_present():
            return f"{endpoint.name}: rclone credentials not published"
        config_path = endpoint.rclone_config
    check = _reach_check(endpoint)
    if check is None:
        return None
    target, _ = check.target(config_path)
    if target is None:
        return None
    verdict = check.run(config_path)
    return None if verdict.ok else f"{endpoint.name}: {verdict.reason}"


def skip_reason(
    config, run_directory: Path, *, now: float | None = None
) -> str | None:
    """Return why this idle sync is skipped, or None to run it.

    An idle sync runs when no cloud is configured, when any cloud may be
    reachable (including one the reach check cannot judge), or when the
    last idle sync is ``OFFLINE_RUN_EVERY`` intervals old.
    """
    clouds = [
        endpoint
        for endpoint in config.endpoints
        if endpoint.type == "rclone" and endpoint.enabled
    ]
    if not clouds:
        return None
    now = time.time() if now is None else now
    every = OFFLINE_RUN_EVERY * config.daemon["idle_sync_interval"]
    last = _last_run(run_directory)
    if last is None or not 0 <= now - last < every:
        return None
    reasons = []
    for endpoint in clouds:
        reason = _unreachable(endpoint, run_directory)
        if reason is None:
            return None
        reasons.append(reason)
    due = time.strftime("%H:%M", time.localtime(last + every))
    return (
        "idle sync skipped: the cloud is unreachable ("
        + "; ".join(reasons)
        + f"); the next one runs anyway after {due}"
    )
