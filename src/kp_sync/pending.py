# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Persist deferred decisions and merge choices in runtime storage."""

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kp_sync.paths import atomic_write, ensure_private_directory


class PendingError(ValueError):
    """Raised for invalid persisted decisions or choices."""

    pass


class PendingStore:
    """Read and update one profile's deferred decisions."""

    def __init__(self, run_directory: str | os.PathLike[str]) -> None:
        self.directory = Path(run_directory)
        self.pending_path = self.directory / "pending.json"
        self.choices_path = self.directory / "choices.json"

    def load(self) -> dict[str, Any]:
        """Return validated decisions, converting the legacy format."""
        if not os.path.lexists(self.pending_path):
            return {"schema_version": 1, "decisions": {}}
        payload = _read_private_json(self.pending_path)
        if (
            isinstance(payload, dict)
            and isinstance(payload.get("reason"), str)
            and isinstance(payload.get("copies"), dict)
        ):
            return {
                "schema_version": 1,
                "decisions": {
                    "sync": {
                        "created_at": payload.get("created_at", ""),
                        "reason": payload["reason"],
                        "details": {"copies": payload["copies"]},
                    }
                },
            }
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or not isinstance(payload.get("decisions"), dict)
        ):
            raise PendingError(
                f"invalid pending decisions file: {self.pending_path}"
            )
        return payload

    def save(
        self, key: str, reason: str, details: dict[str, Any] | None = None
    ) -> None:
        """Add or replace a decision and persist the updated record."""
        if not key or not reason:
            raise ValueError(
                "pending decision key and reason must not be empty"
            )
        payload = self.load()
        payload["decisions"][key] = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "details": details or {},
        }
        self._write(self.pending_path, payload)

    def resolve(self, key: str) -> bool:
        """Remove one decision and report whether it existed."""
        payload = self.load()
        removed = payload["decisions"].pop(key, None) is not None
        if removed:
            if payload["decisions"]:
                self._write(self.pending_path, payload)
            else:
                self.pending_path.unlink()
                directory_fd = os.open(
                    self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        return removed

    def choices(self) -> dict[str, dict[str, str]]:
        """Load cached choices keyed by merge input hash."""
        if not os.path.lexists(self.choices_path):
            return {}
        payload = _read_private_json(self.choices_path)
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise PendingError(
                f"invalid conflict choices file: {self.choices_path}"
            )
        choices = payload.get("choices")
        if not isinstance(choices, dict):
            raise PendingError(
                f"invalid conflict choices file: {self.choices_path}"
            )
        result: dict[str, dict[str, str]] = {}
        for key, value in choices.items():
            if (
                not isinstance(key, str)
                or not isinstance(value, dict)
                or any(
                    not isinstance(k, str) or v not in {"hub", "other", "base"}
                    for k, v in value.items()
                )
            ):
                raise PendingError(
                    f"invalid conflict choice in {self.choices_path}"
                )
            result[key] = value
        return result

    def save_choices(self, input_key: str, choices: dict[str, str]) -> None:
        """Persist choices for one unchanged merge input set."""
        if not input_key or any(
            value not in {"hub", "other", "base"} for value in choices.values()
        ):
            raise ValueError("invalid conflict choice set")
        payload = {"schema_version": 1, "choices": self.choices()}
        payload["choices"][input_key] = choices
        self._write(self.choices_path, payload)

    def _write(self, path: Path, payload: dict[str, Any]) -> None:
        ensure_private_directory(self.directory)
        atomic_write(
            path, json.dumps(payload, sort_keys=True, indent=2) + "\n"
        )


def _read_private_json(path: Path) -> Any:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "r", encoding="utf-8") as source:
            metadata = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
            ):
                raise PendingError(
                    f"pending file is not a regular user-owned file: {path}"
                )
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise PendingError(
                    f"pending file permissions are not private: {path}"
                )
            return json.load(source)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PendingError(f"cannot read pending file {path}: {exc}") from exc
