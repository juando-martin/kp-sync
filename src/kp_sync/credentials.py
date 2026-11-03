"""Cache and retrieve database credentials through the user keyring."""

import errno
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from linux_keyctl import (
    KEY_POS_READ,
    KEY_POS_SEARCH,
    KEY_POS_SETATTR,
    KEY_POS_VIEW,
    KEY_POS_WRITE,
    KEY_SPEC_SESSION_KEYRING,
    KEY_SPEC_USER_KEYRING,
)

from kp_sync.config import Config
from kp_sync.ui.base import UI


# Before profiles had their own keys, every profile shared this one.
LEGACY_DESCRIPTION = b"kp-sync:password"
# VIEW lets `forget` find a profile's copy keys by description.
PASSWORD_PERMISSIONS = (
    KEY_POS_VIEW
    | KEY_POS_READ
    | KEY_POS_SEARCH
    | KEY_POS_WRITE
    | KEY_POS_SETATTR
)
# A copy password is needed only until the copy is rewritten.
COPY_PASSWORD_TIMEOUT = 300
COPY_SLOT_RE = re.compile(r"[0-9a-f]{16}\Z")


def copy_slot(name: str) -> str:
    """Return a keyring slot for a copy with a separate password."""
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]


class Keyring(Protocol):
    """keyctl operations required by :class:`CredentialProvider`."""

    def search(self, keyring: int, description: bytes) -> int:
        """Return the matching key ID or raise ``OSError``."""
        ...

    def read_key(self, key_id: int) -> bytes:
        """Read a key's payload bytes."""
        ...

    def add_key(
        self, key: bytes, value: bytes, keyring: int, key_type: bytes
    ) -> int:
        """Create a key in the selected keyring and return its ID."""
        ...

    def set_perm(self, key_id: int, perm: int) -> int:
        """Set key permissions, returning the keyctl result."""
        ...

    def set_timeout(self, key_id: int, timeout: int) -> int:
        """Set the key's lifetime in seconds."""
        ...

    def link(self, key_id: int, keyring: int) -> int:
        """Link a keyring into another keyring."""
        ...

    def revoke(self, key_id: int) -> int:
        """Revoke a key immediately."""
        ...

    def update_key(self, key_id: int, value: bytes) -> int:
        """Replace a key's payload bytes."""
        ...

    def list_keyring(self, keyring: int):
        """Return key IDs linked to the selected keyring."""
        ...

    def describe_key(self, key_id: int) -> bytes:
        """Return the key description as bytes."""
        ...


class CredentialError(RuntimeError):
    """Raised when credentials cannot safely be read or changed."""

    pass


def _missing_key(exc: OSError) -> bool:
    return exc.errno in {
        errno.ENOKEY,
        errno.ENOENT,
        errno.EKEYEXPIRED,
        errno.EKEYREVOKED,
    }


@dataclass
class CredentialProvider:
    """Manage per-profile database and copy passwords in the keyring.

    An injected backend supports testing. The default loads lazily from
    ``keyctl``.
    """

    config: Config
    keyring: Keyring | None = None

    def _backend(self) -> Keyring:
        if self.keyring is not None:
            return self.keyring
        import linux_keyctl as keyctl

        return keyctl

    def _prefix(self) -> str:
        return f"kp-sync:{self.config.profile or 'default'}:"

    def _description(self, slot: str | None = None) -> bytes:
        if slot is None:
            return f"{self._prefix()}password".encode()
        if not COPY_SLOT_RE.fullmatch(slot):
            raise ValueError("invalid copy password slot")
        return f"{self._prefix()}copy:{slot}".encode()

    def _read(self, backend: Keyring, description: bytes) -> str | None:
        try:
            key_id = backend.search(KEY_SPEC_USER_KEYRING, description)
            value = backend.read_key(key_id)
        except OSError as exc:
            if _missing_key(exc):
                return None
            raise CredentialError(
                f"cannot read password from user keyring: {exc}"
            ) from exc
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CredentialError(
                "cached password is not valid UTF-8"
            ) from exc

    def cached_password(self) -> str | None:
        """Return the cached password, adopting a legacy key once."""
        backend = self._backend()
        _link_user_keyring(backend)
        value = self._read(backend, self._description())
        if value is None and self.config.profile is None:
            # Only the default profile adopts the legacy key.
            # Named profiles must not read it.
            value = self._read(backend, LEGACY_DESCRIPTION)
            if value is not None:
                self._store(
                    backend,
                    self._description(),
                    value,
                    self.config.credentials["cache_timeout"],
                )
                self._revoke(backend, LEGACY_DESCRIPTION)
        return value

    def cached_copy_password(self, slot: str) -> str | None:
        """Return a cached password for one validated copy slot."""
        backend = self._backend()
        _link_user_keyring(backend)
        return self._read(backend, self._description(slot))

    def cache_password(self, password: str) -> None:
        """Cache the profile password for its configured timeout."""
        backend = self._backend()
        _link_user_keyring(backend)
        self._store(
            backend,
            self._description(),
            password,
            self.config.credentials["cache_timeout"],
        )

    def cache_copy_password(self, slot: str, password: str) -> None:
        """Cache a copy-specific password for at most five minutes."""
        backend = self._backend()
        _link_user_keyring(backend)
        self._store(
            backend,
            self._description(slot),
            password,
            min(
                self.config.credentials["cache_timeout"], COPY_PASSWORD_TIMEOUT
            ),
        )

    def _store(
        self, backend: Keyring, description: bytes, password: str, timeout: int
    ) -> None:
        if not password:
            raise ValueError("password must not be empty")
        try:
            key_id = backend.search(KEY_SPEC_USER_KEYRING, description)
        except OSError as exc:
            if not _missing_key(exc):
                raise CredentialError(
                    f"cannot search user keyring: {exc}"
                ) from exc
            try:
                key_id = backend.add_key(
                    description,
                    password.encode("utf-8"),
                    KEY_SPEC_USER_KEYRING,
                    b"user",
                )
            except OSError as add_exc:
                raise CredentialError(
                    f"cannot cache password in user keyring: {add_exc}"
                ) from add_exc
        else:
            try:
                backend.update_key(key_id, password.encode("utf-8"))
            except AttributeError:
                raise CredentialError(
                    "keyring backend cannot update cached passwords"
                )
            except OSError as exc:
                raise CredentialError(
                    f"cannot update cached password: {exc}"
                ) from exc
        try:
            backend.set_perm(key_id, PASSWORD_PERMISSIONS)
            backend.set_timeout(key_id, timeout)
        except OSError as exc:
            try:
                backend.revoke(key_id)
            except OSError as revoke_exc:
                raise CredentialError(
                    "cannot secure password cache and could not revoke the key"
                ) from revoke_exc
            raise CredentialError(
                f"cannot secure password cache: {exc}"
            ) from exc

    def password(self, ui: UI) -> str | None:
        """Return a cached password or ask the UI and cache its answer.

        Returns ``None`` if the UI cannot provide a password.
        """
        cached = self.cached_password()
        if cached is not None:
            return cached
        supplied = ui.password("KeePass database password")
        if supplied is None:
            return None
        self.cache_password(supplied)
        return supplied

    def key_file(self, ui: UI) -> Path | None:
        """Return the configured key file or ask for an alternative.

        An alternative is returned for this call and is not persisted.
        """
        path = self.config.key_file
        if path.is_file() and os.access(path, os.R_OK):
            return path
        selected = ui.file_path(
            f"Key file unavailable at {path}; choose a key file"
        )
        if selected is None:
            return None
        if not selected.is_file() or not os.access(selected, os.R_OK):
            raise CredentialError(f"key file is not readable: {selected}")
        return selected

    def _revoke(self, backend: Keyring, description: bytes) -> bool:
        try:
            key_id = backend.search(KEY_SPEC_USER_KEYRING, description)
        except OSError as exc:
            if _missing_key(exc):
                return False
            raise CredentialError(
                f"cannot find cached password: {exc}"
            ) from exc
        try:
            backend.revoke(key_id)
        except OSError as exc:
            raise CredentialError(
                f"cannot revoke cached password: {exc}"
            ) from exc
        return True

    def forget(self) -> bool:
        """Revoke this profile's database password."""
        backend = self._backend()
        _link_user_keyring(backend)
        forgotten = self._revoke(backend, self._description())
        if self.config.profile is None:
            forgotten = self._revoke(backend, LEGACY_DESCRIPTION) or forgotten
        return forgotten

    def forget_copy_password(self, slot: str) -> bool:
        """Revoke one copy-specific password, if it is present."""
        backend = self._backend()
        _link_user_keyring(backend)
        return self._revoke(backend, self._description(slot))

    def forget_all(self) -> bool:
        """Revoke this profile's password and all copy passwords."""
        forgotten = self.forget()
        backend = self._backend()
        prefix = f"{self._prefix()}copy:".encode()
        try:
            key_ids = list(backend.list_keyring(KEY_SPEC_USER_KEYRING))
        except OSError as exc:
            raise CredentialError(f"cannot list user keyring: {exc}") from exc
        for key_id in key_ids:
            try:
                description = (
                    backend.describe_key(key_id)
                    .rstrip(b"\0")
                    .rsplit(b";", 1)[-1]
                )
            except OSError:
                continue  # not ours to view, or already gone
            if description.startswith(prefix):
                try:
                    backend.revoke(key_id)
                    forgotten = True
                except OSError as exc:
                    if not _missing_key(exc):
                        raise CredentialError(
                            f"cannot revoke cached password: {exc}"
                        ) from exc
        return forgotten


def _link_user_keyring(backend: Keyring) -> None:
    try:
        backend.link(KEY_SPEC_USER_KEYRING, KEY_SPEC_SESSION_KEYRING)
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise CredentialError(
                f"cannot link user keyring into session: {exc}"
            ) from exc
