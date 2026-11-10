# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Prepare a key drive for this profile (``kp-sync drive-setup``).

The command never writes a database itself. It checks the plugged
drive, registers it with udevmon in kp-sync's drop-in (adding this
profile's attach/detach or up/down actions when they are missing) and
asks udevmon to run the drive's actions now. The usual attach then
synchronizes the drive: an empty drive receives the current database
and history through the engine's normal checked install
(``RENAME_NOREPLACE``); a drive that already holds a database is
classified and merged like any other copy, never overwritten.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable, Mapping

from kp_sync import udevmon
from kp_sync.drives import identify_drive
from kp_sync.log import logged


class DriveSetupError(RuntimeError):
    """The drive cannot be set up safely; nothing was changed."""


def _profile_args(config) -> list[str]:
    if config.profile:
        return ["--profile", config.profile]
    if config.source is not None:
        return ["--config", str(config.source)]
    return []


def _selects_profile(run: list, config) -> bool:
    from kp_sync.integration import _kp_sync_target

    target = _kp_sync_target(run)
    if target is None or "drive-sync" not in run:
        return False
    kind, value = target
    if config.profile:
        return kind == "profile" and value == config.profile
    if config.source is not None:
        return kind == "config" and Path(value).expanduser() == config.source
    return kind == "default"


def _key_drive_tag(documents, config) -> str:
    """The udevmon tag that selects this profile's drive actions.

    A profile keeps the tag its actions already use. Otherwise the
    first profile gets ``keepass`` and later ones ``keepass-PROFILE``,
    so one profile's drives never run another profile's actions.
    """
    from kp_sync.integration import _kp_sync_target

    others = False
    for action in udevmon.actions(documents):
        run = action.get("run")
        if not isinstance(run, list) or "drive-sync" not in run:
            continue
        if _kp_sync_target(run) is None:
            continue
        if _selects_profile(run, config):
            if isinstance(action.get("tag"), str):
                return action["tag"]
        elif action.get("tag") == "keepass":
            others = True
    return f"keepass-{config.profile or 'default'}" if others else "keepass"


def plan(
    config,
    mountpoint: str | os.PathLike[str],
    name: str,
    *,
    rclone: bool = False,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    environ: Mapping[str, str] | None = None,
    identify: Callable[[Path], str] | None = None,
) -> tuple[list[str], list[list[str]]]:
    """Return what will happen (lines) and the udevmon commands to run.

    Raises:
        DriveSetupError: When the drive or the installation is not
            suitable; nothing has been changed.
    """
    mount = Path(mountpoint).expanduser().resolve()
    if not mount.is_dir():
        raise DriveSetupError(f"not a mounted directory: {mount}")
    if not any(
        endpoint.type == "file" and endpoint.source == "runtime"
        for endpoint in config.endpoints
    ):
        raise DriveSetupError(
            "this profile has no pendrive endpoint "
            '(type = "file", source = "runtime")'
        )
    if not os.access(mount, os.W_OK):
        raise DriveSetupError(f"the drive is not writable: {mount}")
    identity = (identify or (lambda m: identify_drive(m, runner=runner)))(
        mount
    )
    serial, _, fs_uuid = identity.partition(":")
    found = udevmon.version(runner)
    if found is None or found < udevmon.MIN_VERSION:
        raise DriveSetupError(
            "udevmon "
            + ".".join(map(str, udevmon.MIN_VERSION))
            + " or later is needed"
            + (f" (found {'.'.join(map(str, found))})" if found else "")
            + "; or register the drive by hand (examples/README.md) and "
            "attach it with kp-sync attach"
        )
    documents, problems = udevmon.load(udevmon.config_dir(environ))
    if problems:
        raise DriveSetupError("; ".join(problems))
    database = mount / config.drives["directory"] / config.db_name
    mode = "secondary" if config.hub_persistent else "main"
    lines = [
        f"Drive {identity} at {mount}.",
        (
            f"It already holds {database.relative_to(mount)}: the first "
            "sync merges it with the current database if they differ; it "
            "is never overwritten."
            if database.exists()
            else f"The first sync writes the current database to "
            f"{database.relative_to(mount)} and the shared history next "
            "to it."
        ),
    ]
    commands: list[list[str]] = []
    devices = udevmon.devices(documents)
    existing = next(
        (
            device_name
            for device_name, device in devices.items()
            if (device.get("match") or {}).get("fs_uuid") == fs_uuid
            and (device.get("match") or {}).get("drive_serial") == serial
        ),
        None,
    )
    key_tag = _key_drive_tag(documents, config)
    tags = [key_tag] + (["rclone"] if rclone else [])
    if existing is None:
        if name in devices:
            raise DriveSetupError(
                f"udevmon already has a device named {name!r} for another "
                "drive; choose another name"
            )
        device_name = name
        commands.append(
            [
                "udevmon",
                "devices",
                "add",
                name,
                "--from-mountpoint",
                str(mount),
                *[part for tag in tags for part in ("--tag", tag)],
                "--file",
                udevmon.DROP_IN,
            ]
        )
        lines.append(
            f"udevmon: add device {name!r} (tags {', '.join(tags)}) to "
            f"config.d/{udevmon.DROP_IN}.toml."
        )
    else:
        device_name = existing
        status = devices[existing].get("status")
        if status in {"ignored", "disabled"}:
            commands.append(["udevmon", "devices", "enable", existing])
            lines.append(
                f"udevmon: device {existing!r} is {status}; enable it."
            )
        present = set(devices[existing].get("tags") or [])
        missing_tags = [tag for tag in tags if tag not in present]
        if missing_tags:
            commands.append(
                ["udevmon", "devices", "tag", existing, *missing_tags]
            )
        lines.append(
            f"udevmon: the drive is already device {existing!r}"
            + (
                f"; add tag(s) {', '.join(missing_tags)}."
                if missing_tags
                else "."
            )
        )
    profile_actions = [
        action
        for action in udevmon.actions(documents)
        if isinstance(action.get("run"), list)
        and _selects_profile(action["run"], config)
    ]
    names = {action.get("name") for action in udevmon.actions(documents)}
    for event, on, words in (
        (
            "up",
            "startup,mounted",
            ["--drive-id", "{drive_serial}:{fs_uuid}", "{mountpoint}"],
        ),
        ("down", "unmounted", ["--drive-id", "{drive_serial}:{fs_uuid}"]),
    ):
        if any(event in action["run"] for action in profile_actions):
            continue
        base = {
            ("secondary", "up"): "keepass-attach",
            ("secondary", "down"): "keepass-detach",
            ("main", "up"): "keepass-up",
            ("main", "down"): "keepass-down",
        }[(mode, event)]
        action_name = (
            base
            if key_tag == "keepass"
            else f"{base}-{config.profile or 'default'}"
        )
        if action_name in names:
            action_name = f"{base}-{config.profile or 'default'}"
        commands.append(
            [
                "udevmon",
                "actions",
                "add",
                on,
                "--tag",
                key_tag,
                "--name",
                action_name,
                "--lock",
                "keydisk",
                "--timeout",
                "120",
                "--notify-on-failure",
                "--file",
                udevmon.DROP_IN,
                "--",
                "kp-sync",
                *_profile_args(config),
                "drive-sync",
                event,
                "--mode",
                mode,
                *words,
            ]
        )
        lines.append(
            f"udevmon: add action {action_name!r} (drive-sync {event} "
            f"--mode {mode}) to config.d/{udevmon.DROP_IN}.toml."
        )
    # Existing up actions decide what the trigger does, not the default.
    up_modes = {
        action["run"][action["run"].index("--mode") + 1]
        for action in profile_actions
        if "up" in action["run"] and "--mode" in action["run"][:-1]
    }
    starts = "main" in up_modes if up_modes else mode == "main"
    commands.append(["udevmon", "trigger", device_name, "--wait"])
    lines.append(
        f"udevmon: run {device_name!r}'s actions now, which attaches it"
        + (" and starts the application." if starts else ".")
    )
    return lines, commands


def setup(
    config,
    mountpoint,
    name: str,
    *,
    rclone: bool = False,
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    environ: Mapping[str, str] | None = None,
    identify: Callable[[Path], str] | None = None,
    out: Callable[[str], None] = print,
    enrol: Callable[[str], dict] | None = None,
) -> int:
    """Plan and (unless ``dry_run``) run the drive setup.

    ``enrol`` lifts a decommissioning marker for the drive's identity
    (``SyncEngine.enrol_drive``) before udevmon attaches it; without
    it a decommissioned drive stays untouched.
    """
    found: list[str] = []
    identify_drive_once = identify or (
        lambda m: identify_drive(m, runner=runner)
    )

    def remember(mount: Path) -> str:
        found.append(identify_drive_once(mount))
        return found[-1]

    lines, commands = plan(
        config,
        mountpoint,
        name,
        rclone=rclone,
        runner=runner,
        environ=environ,
        identify=remember,
    )
    for line in lines:
        out(line)
    if rclone:
        out(
            "Drives tagged rclone also need udevmon's rclone-config "
            "drop-in (contrib/rclone-config in udevmon)."
        )
    if dry_run:
        out(
            "If the shared history has this drive decommissioned, setup "
            "enrols it again."
        )
        out("Dry run: nothing was changed.")
        return 0
    if enrol is not None:
        result = enrol(found[-1])
        if result.get("changed"):
            out(
                "The drive was decommissioned; it is enrolled again in the "
                "history."
            )
    for argv in commands:
        result = logged(runner)(
            argv, capture_output=True, text=True, timeout=180
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise DriveSetupError(
                f"{' '.join(argv[:3])} failed (status {result.returncode})"
                + (f": {detail}" if detail else "")
            )
    out(
        "Done. The drive is being synchronized in the background; check "
        "with kp-sync status."
    )
    return 0
