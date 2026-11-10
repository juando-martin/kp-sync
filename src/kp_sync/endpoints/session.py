# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""File endpoint specialization for a staged remote-session copy."""

from kp_sync.endpoints.file import FileEndpoint
from kp_sync.sessions.records import SessionRecord, SessionStore


class SessionEndpoint(FileEndpoint):
    """The copy staged for one remote session.

    A ``ro`` copy is target-only. A ``rw`` copy allows foreign writes.
    """

    def __init__(
        self, record: SessionRecord, store: SessionStore, database_name: str
    ):
        """Bind an endpoint to one session's staged database.

        Args:
            record: Persisted session identity and access mode.
            store: Runtime store locating the session staging directory.
            database_name: Database filename within that directory.

        Notes:
            Read-only copies are targets; writable copies allow edits.
        """
        self.record = record
        super().__init__(
            record.identity,
            store.staging_path(record.id) / database_name,
            identity=record.identity,
            optional=True,
            on_unavailable="skip",
            foreign_writers=record.mode == "rw",
        )
        self.never_source = record.mode == "ro"
