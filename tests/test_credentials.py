import errno
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from linux_keyctl import (
    KEY_POS_READ,
    KEY_POS_SEARCH,
    KEY_POS_VIEW,
    KEY_POS_WRITE,
    KEY_POS_SETATTR,
    KEY_SPEC_SESSION_KEYRING,
    KEY_SPEC_USER_KEYRING,
)

from kp_sync.config import load_config
from kp_sync.credentials import (
    COPY_PASSWORD_TIMEOUT,
    CredentialError,
    CredentialProvider,
    LEGACY_DESCRIPTION,
    copy_slot,
)
from kp_sync.ui.fake import FakeUI


class FakeKeyring:
    def __init__(self) -> None:
        self.value: bytes | None = None
        self.permissions: int | None = None
        self.timeout: int | None = None
        self.revoked = False
        self.linked: tuple[int, int] | None = None
        self.fail_timeout = False
        self.link_error: int | None = None
        self.description: bytes | None = None

    def search(self, keyring: int, description: bytes) -> int:
        if (
            self.value is None
            or self.revoked
            or description != self.description
        ):
            raise OSError(errno.ENOKEY, "missing")
        return 123

    def read_key(self, key_id: int) -> bytes:
        if self.value is None:
            raise OSError(errno.ENOKEY, "missing")
        return self.value

    def add_key(
        self, description: bytes, value: bytes, keyring: int, key_type=b"user"
    ) -> int:
        self.value = value
        self.description = description
        self.revoked = False
        return 123

    def update_key(self, key_id: int, value: bytes) -> int:
        if not self.permissions & KEY_POS_WRITE:
            raise OSError(errno.EACCES, "write denied")
        self.value = value
        return 0

    def set_perm(self, key_id: int, perm: int) -> int:
        self.permissions = perm
        return 0

    def set_timeout(self, key_id: int, timeout: int) -> int:
        if not self.permissions & KEY_POS_SETATTR:
            raise OSError(errno.EACCES, "setattr denied")
        if self.fail_timeout:
            raise OSError(errno.EACCES, "timeout denied")
        self.timeout = timeout
        return 0

    def link(self, key_id: int, keyring: int) -> int:
        if self.link_error is not None:
            raise OSError(self.link_error, "link denied")
        self.linked = (key_id, keyring)
        return 0

    def revoke(self, key_id: int) -> int:
        if not self.permissions & (KEY_POS_WRITE | KEY_POS_SETATTR):
            raise OSError(errno.EACCES, "revoke denied")
        self.revoked = True
        return 0


class MultiKeyring:
    """Several user keys, as the kernel keyring holds them."""

    def __init__(self) -> None:
        self.keys: dict[int, dict] = {}
        self.ids = iter(range(1000, 2000))

    def _live(self, key_id: int) -> dict:
        key = self.keys.get(key_id)
        if key is None or key["revoked"]:
            raise OSError(errno.EKEYREVOKED, "revoked")
        return key

    def search(self, keyring: int, description: bytes) -> int:
        for key_id, key in self.keys.items():
            if key["description"] == description and not key["revoked"]:
                return key_id
        raise OSError(errno.ENOKEY, "missing")

    def read_key(self, key_id: int) -> bytes:
        return self._live(key_id)["value"]

    def add_key(self, description, value, keyring, key_type=b"user") -> int:
        key_id = next(self.ids)
        self.keys[key_id] = {
            "description": description,
            "value": value,
            "revoked": False,
            "timeout": None,
        }
        return key_id

    def update_key(self, key_id: int, value: bytes) -> int:
        self._live(key_id)["value"] = value
        return 0

    def set_perm(self, key_id: int, perm: int) -> int:
        return 0

    def set_timeout(self, key_id: int, timeout: int) -> int:
        self._live(key_id)["timeout"] = timeout
        return 0

    def link(self, key_id: int, keyring: int) -> int:
        return 0

    def revoke(self, key_id: int) -> int:
        self._live(key_id)["revoked"] = True
        return 0

    def list_keyring(self, keyring: int):
        yield from list(self.keys)

    def describe_key(self, key_id: int) -> bytes:
        return b"user;1001;1001;3f010000;" + self._live(key_id)["description"]

    def live(self) -> dict[bytes, bytes]:
        return {
            key["description"]: key["value"]
            for key in self.keys.values()
            if not key["revoked"]
        }


def _isolated_config(test: unittest.TestCase):
    # Never the real home: it may hold production profiles.
    temp = tempfile.TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    return load_config(
        environ={"HOME": temp.name, "XDG_RUNTIME_DIR": temp.name}
    )


class CopyPasswordSlotTest(unittest.TestCase):
    def setUp(self) -> None:
        self.keyring = MultiKeyring()
        self.config = _isolated_config(self)
        self.provider = CredentialProvider(self.config, self.keyring)
        self.test_profile = CredentialProvider(
            replace(self.config, profile="kp-test"), self.keyring
        )

    def test_copy_password_never_replaces_the_main_password(self) -> None:
        self.provider.cache_password("main-secret")
        slot = copy_slot("drive:SERIAL:UUID")
        self.provider.cache_copy_password(slot, "old-drive-secret")
        self.assertEqual(self.provider.cached_password(), "main-secret")
        self.assertEqual(
            self.provider.cached_copy_password(slot), "old-drive-secret"
        )
        timeouts = {
            key["description"]: key["timeout"]
            for key in self.keyring.keys.values()
        }
        self.assertLessEqual(
            timeouts[f"kp-sync:default:copy:{slot}".encode()],
            COPY_PASSWORD_TIMEOUT,
        )
        with self.assertRaises(ValueError):
            self.provider.cache_copy_password("../password", "x")

    def test_separate_profile_keys_and_default_adopts_legacy_key(
        self,
    ) -> None:
        self.keyring.add_key(
            LEGACY_DESCRIPTION, b"shared-secret", KEY_SPEC_USER_KEYRING
        )
        self.assertIsNone(self.test_profile.cached_password())
        self.test_profile.cache_password("test-only")
        self.assertEqual(self.provider.cached_password(), "shared-secret")
        self.assertEqual(
            self.keyring.live(),
            {
                b"kp-sync:kp-test:password": b"test-only",
                b"kp-sync:default:password": b"shared-secret",
            },
        )

    def test_forget_all_revokes_only_this_profiles_keys(self) -> None:
        slot = copy_slot("drive:SERIAL:UUID")
        self.provider.cache_password("main-secret")
        self.provider.cache_copy_password(slot, "old-drive-secret")
        self.test_profile.cache_password("test-only")
        self.test_profile.cache_copy_password(slot, "test-copy")
        self.assertTrue(self.provider.forget_all())
        self.assertEqual(
            self.keyring.live(),
            {
                b"kp-sync:kp-test:password": b"test-only",
                f"kp-sync:kp-test:copy:{slot}".encode(): b"test-copy",
            },
        )


class CredentialProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = _isolated_config(self)
        self.keyring = FakeKeyring()
        self.provider = CredentialProvider(self.config, self.keyring)

    def test_caches_password_with_possessor_permissions_and_timeout(
        self,
    ) -> None:
        self.provider.cache_password("database-secret")
        self.assertEqual(self.keyring.description, b"kp-sync:default:password")
        self.assertEqual(self.provider.cached_password(), "database-secret")
        self.assertEqual(
            self.keyring.permissions,
            KEY_POS_VIEW
            | KEY_POS_READ
            | KEY_POS_SEARCH
            | KEY_POS_WRITE
            | KEY_POS_SETATTR,
        )
        self.assertEqual(
            self.keyring.timeout,
            self.config.credentials["cache_timeout"],
        )
        self.assertEqual(
            self.keyring.linked,
            (KEY_SPEC_USER_KEYRING, KEY_SPEC_SESSION_KEYRING),
        )
        self.provider.cache_password("replacement-secret")
        self.assertEqual(self.provider.cached_password(), "replacement-secret")

    def test_password_prompt_is_cached_without_recording_secret_in_ui_events(
        self,
    ) -> None:
        ui = FakeUI(passwords=("database-secret",))
        self.assertEqual(self.provider.password(ui), "database-secret")
        self.assertEqual(
            ui.events, [("password", "KeePass database password")]
        )
        self.assertNotIn("database-secret", repr(ui.events))

    def test_forget_revokes_cached_password(self) -> None:
        self.provider.cache_password("database-secret")
        self.assertTrue(self.provider.forget())
        self.assertIsNone(self.provider.cached_password())
        self.assertTrue(self.keyring.revoked)

    def test_failed_timeout_revokes_new_key(self) -> None:
        self.keyring.fail_timeout = True
        with self.assertRaises(CredentialError):
            self.provider.cache_password("database-secret")
        self.assertTrue(self.keyring.revoked)

    def test_keyring_link_permission_error_is_not_ignored(self) -> None:
        self.keyring.link_error = errno.EPERM
        with self.assertRaises(CredentialError):
            self.provider.cache_password("database-secret")
        self.assertIsNone(self.keyring.value)

    def test_key_file_uses_configured_path_or_defers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = replace(
                self.config, key_file=Path(temp_dir) / "unavailable.keyx"
            )
            provider = CredentialProvider(config, self.keyring)
            ui = FakeUI()
            self.assertIsNone(provider.key_file(ui))
            self.assertEqual(ui.events[0][0], "file_path")


if __name__ == "__main__":
    unittest.main()
