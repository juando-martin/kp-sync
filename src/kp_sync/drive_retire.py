# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Decommission a key drive (``kp-sync drive-retire``).

Decommissioning never needs the drive and never touches its contents.
It writes a reversible marker for ``drive:SERIAL:FS_UUID`` to the
shared history, so the drive's copy record leaves history and
``status``, stops holding old versions, and a later plain attach on any
machine leaves the drive untouched. It also removes (or sets to
``ignore``) this machine's udevmon device in kp-sync's drop-in. Only
``drive-setup`` re-enrols the identity. See "Decommissioned key drives"
in the history design chapter.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from kp_sync import udevmon
from kp_sync.log import logged

_IDENTITY = re.compile(r"[^:/\\\x00]+:[A-Za-z0-9-]+\Z")


class DriveRetireError(RuntimeError):
    """The drive cannot be resolved or decommissioned safely."""


@dataclass(frozen=True)
class Resolved:
    """A ``DRIVE`` argument resolved to an identity and local device."""

    identity: str
    device: str | None
    device_info: dict | None


def resolve(
    config,
    drive: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> Resolved:
    """Resolve a udevmon name, an alias or ``SERIAL:FS_UUID``.

    A udevmon name resolves only when its match has both
    ``drive_serial`` and ``fs_uuid``. Ambiguity is refused.
    """
    documents, problems = udevmon.load(udevmon.config_dir(environ))
    if problems:
        raise DriveRetireError("; ".join(problems))
    devices = udevmon.devices(documents)

    def device_identity(info: dict) -> str | None:
        match = info.get("match") or {}
        serial, fs_uuid = match.get("drive_serial"), match.get("fs_uuid")
        if isinstance(serial, str) and isinstance(fs_uuid, str):
            identity = f"{serial}:{fs_uuid}"
            if _IDENTITY.fullmatch(identity):
                return identity
        return None

    candidates: set[str] = set()
    if _IDENTITY.fullmatch(drive.removeprefix("drive:")):
        candidates.add(drive.removeprefix("drive:"))
    if drive in devices:
        identity = device_identity(devices[drive])
        if identity is None:
            raise DriveRetireError(
                f"udevmon device {drive!r} does not match on both "
                "drive_serial and fs_uuid; give the drive as SERIAL:FS_UUID"
            )
        candidates.add(identity)
    aliases = [
        identity
        for identity, alias in config.drives.get("aliases", {}).items()
        if alias == drive
    ]
    if len(aliases) > 1:
        raise DriveRetireError(
            f"alias {drive!r} names several drives: "
            + ", ".join(sorted(aliases))
            + "; give the drive as SERIAL:FS_UUID"
        )
    candidates.update(aliases)
    if not candidates:
        raise DriveRetireError(
            f"{drive!r} is not a udevmon device, a drives.aliases name or a "
            "SERIAL:FS_UUID identity"
        )
    if len(candidates) > 1:
        raise DriveRetireError(
            f"{drive!r} is ambiguous: " + ", ".join(sorted(candidates))
        )
    identity = candidates.pop()
    device = next(
        (
            name
            for name, info in devices.items()
            if device_identity(info) == identity
        ),
        None,
    )
    return Resolved(identity, device, devices.get(device) if device else None)


def _device_lines(resolved: Resolved, keep_device: bool) -> list[str]:
    if resolved.device is None:
        return ["udevmon: no device on this machine matches this drive."]
    info = resolved.device_info or {}
    source = Path(info.get("_file", ""))
    if source.name != f"{udevmon.DROP_IN}.toml":
        return [
            f"udevmon: device {resolved.device!r} is defined in {source}, "
            "not kp-sync's drop-in, so it is left as it is; remove it "
            f"yourself (udevmon devices remove {resolved.device})."
        ]
    lines = [
        f"udevmon: {'ignore' if keep_device else 'remove'} device "
        f"{resolved.device!r} in config.d/{udevmon.DROP_IN}.toml."
    ]
    tags = set(info.get("tags") or [])
    if "rclone" in tags:
        lines.append(
            "Its rclone tag stops too: the credentials provider no longer "
            "publishes cloud credentials for it."
        )
    # kp-sync's own tags: keepass, keepass-PROFILE and rclone.
    others = sorted(
        tag
        for tag in tags
        if tag not in {"keepass", "rclone"} and not tag.startswith("keepass-")
    )
    if others:
        lines.append("Its other tags stop too: " + ", ".join(others) + ".")
    return lines


def _device_command(resolved: Resolved, keep_device: bool):
    info = resolved.device_info or {}
    if (
        resolved.device is None
        or Path(info.get("_file", "")).name != f"{udevmon.DROP_IN}.toml"
    ):
        return None
    if keep_device:
        if info.get("status") == "ignored":
            return None
        return ["udevmon", "devices", "ignore", resolved.device]
    return ["udevmon", "devices", "remove", resolved.device]


def _record_lines(report: dict) -> list[str]:
    identity = report["identity"].removeprefix("drive:")
    lines = []
    record = report["record"]
    if record is None:
        lines.append(f"Drive {identity}: no copy record in the history.")
    else:
        lines.append(
            f"Drive {identity}: holds version {record['version'][:12]}, "
            f"last synced {record['synced_at']} on {record['host']}."
        )
    marker = report["marker"]
    if marker is not None and marker["state"] == "decommissioned":
        lines.append(
            f"Already decommissioned on {marker['host']} at "
            f"{marker['at']}; only this machine's udevmon device changes."
        )
    return lines


def erase_reminder(config) -> str:
    """How to erase the drive, with this profile's paths on it."""
    database = Path(config.drives["directory"]) / config.db_name
    history = Path(config.baseline["dir"])
    return (
        f"To erase the drive, plug it in and remove {database.as_posix()}, "
        f"the {history.as_posix()}/ history folder and any "
        "config/rclone/rclone.conf (on flash memory, deleting or "
        "overwriting is no secure erase). For a credentials drive, also "
        "revoke its rclone token."
    )


def retire(
    config,
    engine,
    drive: str,
    *,
    confirm: str | None = None,
    keep_device: bool = False,
    last_history_drive: bool = False,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    environ: Mapping[str, str] | None = None,
    out: Callable[[str], None] = print,
) -> int:
    """Preview (no ``confirm``) or apply a drive decommissioning.

    Returns 0 on success, 3 when shared publication is incomplete and
    1 when blocked.
    """
    resolved = resolve(config, drive, environ=environ)
    identity = resolved.identity
    if confirm is None:
        report = engine.drive_decommission_preview(identity)
        for line in _record_lines(report):
            out(line)
        for name, outcome in report["shared_stores"].items():
            if outcome == "unavailable":
                out(
                    f"Shared store {name}: unavailable; the preview may "
                    "miss records from it."
                )
        for line in _device_lines(resolved, keep_device):
            out(line)
        if report["first_drive_marker"]:
            out(
                "This is the first drive marker: the shared history moves "
                "to schema 3, which kp-sync before 0.5.0 cannot read. "
                "Upgrade every machine that shares this history first."
            )
        if report["blockers"]:
            for reason in report["blockers"]:
                out(f"Blocked: {reason}.")
            return 1
        out(
            f"Preview only. To apply: kp-sync drive-retire {drive} "
            f"--confirm {identity}"
            + (" --keep-device" if keep_device else "")
            + (" --last-history-drive" if last_history_drive else "")
        )
        return 0
    if confirm != identity:
        raise DriveRetireError(
            f"--confirm must be exactly {identity} (got {confirm!r})"
        )
    name = resolved.device or (config.drives.get("aliases", {}).get(identity))
    result = engine.decommission_drive(
        identity, name=name, last_history_drive=last_history_drive
    )
    for line in _record_lines(result):
        out(line)
    out(
        "Decommissioned in runtime history."
        if result["changed"]
        else "History already had it decommissioned."
    )
    incomplete = bool(result["publication_error"])
    for store, outcome in sorted(result["shared_stores"].items()):
        out(f"Shared store {store}: {outcome}.")
        if (
            outcome in {"unavailable", "awaiting publication"}
            or outcome.startswith("failed:")
        ) and result["changed"]:
            incomplete = True
    if result["publication_error"]:
        out(f"Publication failed: {result['publication_error']}.")
    if incomplete:
        out(
            "The marker is published by the next sync that reaches those "
            "stores."
        )
    command = _device_command(resolved, keep_device)
    for line in _device_lines(resolved, keep_device):
        out(line)
    if command is not None:
        completed = logged(runner)(
            command, capture_output=True, text=True, timeout=60
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise DriveRetireError(
                f"{' '.join(command[:3])} failed (status "
                f"{completed.returncode})"
                + (f": {detail}" if detail else "")
                + "; the history marker is already committed"
            )
    out(
        "Other machines leave the drive untouched from their next sync; "
        "run this command there too to remove their udevmon device."
    )
    out(erase_reminder(config))
    return 3 if incomplete else 0
