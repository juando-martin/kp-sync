"""Render retained baseline versions and runtime backups for the CLI."""

import os
import stat
import tempfile
from datetime import datetime
from pathlib import Path

from pykeepass import PyKeePass
from pykeepass.exceptions import CredentialsError

from kp_sync.baseline import BaselineManager
from kp_sync.config import Config
from kp_sync.credentials import CredentialError, CredentialProvider
from kp_sync.engine import SyncEngine, endpoints_from_config
from kp_sync.kdbx import KdbxError, inspect_kdbx
from kp_sync.paths import ensure_private_directory, ensure_run_dir
from kp_sync.ui.none import NoneUI


def history_text(
    config: Config,
    *,
    run_directory: str | os.PathLike[str] | None = None,
    credentials: CredentialProvider | None = None,
) -> str:
    """Return formatted history and backup details for one profile.

    Baseline stores may be contacted to load history. Counts appear only
    if a cached password opens the base; no prompt is shown.
    """
    runtime = (
        Path(run_directory)
        if run_directory is not None
        else ensure_run_dir(config.profile)
    )
    temporary_root = ensure_private_directory(runtime / "tmp")
    engine = SyncEngine(
        config,
        endpoints_from_config(config, runtime),
        run_directory=runtime,
        ui=NoneUI(),
    )
    baseline = BaselineManager(runtime, engine.baseline_stores)
    with tempfile.TemporaryDirectory(
        dir=temporary_root, prefix="history-"
    ) as name:
        state = baseline.load(Path(name))

    provider = credentials or CredentialProvider(config)
    try:
        password = provider.cached_password()
    except CredentialError as exc:
        from kp_sync.log import info

        info(f"cached password is unavailable for history counts: {exc}")
        password = None
    key_file = config.key_file if config.key_file.is_file() else None
    lines = ["Retained versions:"]
    if not state.versions:
        lines.append("  (none)")
    else:
        for version in sorted(
            state.versions.values(),
            key=lambda item: (item.generation, item.time, item.host, item.sha),
            reverse=True,
        ):
            base = runtime / "bases" / f"{version.sha}.kdbx"
            count = _entry_count(base, version.sha, password, key_file)
            parents = (
                ", ".join(version.parents) if version.parents else "(root)"
            )
            lines.append(
                f"  {version.sha}  {version.time}  {version.host}  "
                f"parents: {parents}  entries: {count}"
            )

    lines.append("Backups from this boot:")
    backup_dir = runtime / "backups"
    if not os.path.lexists(backup_dir):
        lines.append("  (none)")
    else:
        metadata = backup_dir.lstat()
        if (
            backup_dir.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise RuntimeError("backup path is not a real directory")
        backups = sorted(
            backup_dir.glob("*.kdbx"), key=lambda item: item.name, reverse=True
        )
        if not backups:
            lines.append("  (none)")
        for backup in backups:
            metadata = backup.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise RuntimeError(
                    f"backup is not a private regular user-owned file: "
                    f"{backup.name}"
                )
            try:
                digest = inspect_kdbx(backup).sha256
            except (OSError, KdbxError) as exc:
                raise RuntimeError(
                    f"invalid backup {backup.name}: {exc}"
                ) from exc
            count = _entry_count(backup, digest, password, key_file)
            lines.append(f"  {backup.name}  entries: {count}")
    return "\n".join(lines) + "\n"


def _entry_count(
    path: Path,
    expected_sha: str,
    password: str | None,
    key_file: Path | None,
) -> str:
    if not os.path.lexists(path):
        return "unavailable"
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise RuntimeError(
            f"history file is not private and regular: {path.name}"
        )
    if password is None:
        return "unavailable"
    try:
        info = inspect_kdbx(path)
        if info.sha256 != expected_sha:
            raise RuntimeError(f"history base has the wrong hash: {path.name}")
        database = PyKeePass(
            str(path),
            password,
            keyfile=str(key_file) if key_file is not None else None,
        )
    except CredentialsError:
        return "unavailable"
    except (OSError, KdbxError) as exc:
        raise RuntimeError(
            f"cannot inspect history file {path.name}: {exc}"
        ) from exc
    return str(len(database.entries))
