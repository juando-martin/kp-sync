import base64
import importlib.util
import json
import os
import socket
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from kp_sync.config import load_config
from kp_sync.keepassxc import (
    ConnectorError,
    _connection,
    association_directory,
    disconnect,
    retrieve,
)


HAS_CLIENT = importlib.util.find_spec("keepassxc_proxy_client") is not None


@unittest.skipUnless(HAS_CLIENT, "optional KeePassXC client not installed")
class KeePassXCTest(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temp)
        self.enterContext(
            patch.dict(
                os.environ,
                {"XDG_STATE_HOME": self.temp, "XDG_RUNTIME_DIR": self.temp},
            )
        )
        self.config = load_config(
            home=self.root,
            environ={"HOME": self.temp, "XDG_RUNTIME_DIR": self.temp},
            hostname="synthetic",
        )
        self.config.credentials["keepassxc"] = {
            "url": "https://kp-sync-test.invalid"
        }
        self.socket = socket.socket(socket.AF_UNIX)
        self.addCleanup(self.socket.close)
        self.socket.bind(str(self.root / "browser.sock"))
        self.client = Mock()
        self.client.get_socket_path.return_value = str(
            self.root / "browser.sock"
        )
        self.client.get_databasehash.return_value = "database-identity"
        self.client.response_hash = "database-identity"
        self.client.dump_associate.return_value = ("kp-sync-test", b"x" * 32)
        self.client.get_logins.return_value = [{"password": "test-secret"}]
        self.factory = self.enterContext(
            patch("kp_sync.keepassxc._connection", return_value=self.client)
        )
        self.warn = Mock()

    def test_pair_reconnect_profile_isolation_and_local_disconnect(self):
        self.assertEqual(retrieve(self.config, self.warn), "test-secret")
        directory = association_directory(self.config)
        path = directory / "association.json"
        saved = json.loads(path.read_text())
        self.assertEqual(saved["id"], "kp-sync-test")
        self.assertNotIn("test-secret", path.read_text())
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(retrieve(self.config, self.warn), "test-secret")
        self.client.associate.assert_called_once()
        self.client.test_associate.assert_called_once()
        self.client.get_logins.assert_called_with(
            "https://kp-sync-test.invalid"
        )
        self.assertNotEqual(
            directory,
            association_directory(replace(self.config, profile="default")),
        )
        self.assertNotEqual(
            directory,
            association_directory(replace(self.config, host="another-host")),
        )
        disconnect(self.config)
        self.assertFalse(path.exists())
        self.warn.assert_not_called()

    def test_ambiguity_warns_without_choosing_or_disclosing_credentials(self):
        self.client.get_logins.return_value = [
            {"password": "secret-a"},
            {"password": "secret-b"},
        ]
        self.assertIsNone(retrieve(self.config, self.warn))
        self.assertIn(
            "Multiple KeePassXC entries", self.warn.call_args.args[0]
        )
        self.assertNotIn("secret", self.warn.call_args.args[0])

    def test_revoked_pairing_is_not_silently_replaced(self):
        from keepassxc_proxy_client.protocol import (
            ResponseUnsuccesfulException,
        )

        retrieve(self.config, self.warn)
        self.client.test_associate.side_effect = ResponseUnsuccesfulException(
            {"errorCode": "8", "error": "must not appear in logs"}
        )
        self.assertIsNone(retrieve(self.config, self.warn))
        self.client.associate.assert_called_once()
        self.assertIn("association rejected", self.warn.call_args.args[0])

    def test_locked_denied_no_match_and_timeout_close_connection(self):
        from keepassxc_proxy_client.protocol import (
            ResponseUnsuccesfulException,
        )

        for code in ("1", "6", "15", "19"):
            with self.subTest(code=code):
                self.client.get_databasehash.side_effect = (
                    ResponseUnsuccesfulException({"errorCode": code})
                )
                self.assertIsNone(retrieve(self.config, self.warn))
                self.warn.assert_not_called()
        self.client.get_databasehash.side_effect = TimeoutError
        self.assertIsNone(retrieve(self.config, self.warn))
        self.warn.assert_called_once()
        self.assertEqual(self.client.socket.close.call_count, 5)

    def test_corrupt_or_symlink_state_is_preserved_and_warned(self):
        retrieve(self.config, self.warn)
        path = association_directory(self.config) / "association.json"
        for content in ("invalid json", '{"version": 1}'):
            path.write_text(content)
            self.assertIsNone(retrieve(self.config, self.warn))
            self.assertEqual(path.read_text(), content)
        path.unlink()
        elsewhere = self.root / "elsewhere"
        elsewhere.write_text("untouched")
        path.symlink_to(elsewhere)
        self.assertIsNone(retrieve(self.config, self.warn))
        self.assertTrue(path.is_symlink())
        self.assertEqual(elsewhere.read_text(), "untouched")
        with self.assertRaises(OSError):
            disconnect(self.config)

    def test_disconnect_can_remove_corrupt_private_regular_state(self):
        retrieve(self.config, self.warn)
        path = association_directory(self.config) / "association.json"
        path.write_text("invalid json")
        disconnect(self.config)
        self.assertFalse(path.exists())

    def test_other_database_and_changed_reply_are_rejected(self):
        retrieve(self.config, self.warn)
        self.client.get_databasehash.return_value = "another-database"
        self.assertIsNone(retrieve(self.config, self.warn))
        self.assertIn("select the paired", self.warn.call_args.args[0])
        self.client.get_databasehash.return_value = "database-identity"
        self.client.response_hash = "another-database"
        self.assertIsNone(retrieve(self.config, self.warn))
        self.assertIn("invalid protocol", self.warn.call_args.args[0])

    def test_duplicate_lookup_does_not_wait_or_open_second_dialog(self):
        from kp_sync.lock import file_lock

        retrieve(self.config, self.warn)
        self.factory.reset_mock()
        with file_lock(
            association_directory(self.config) / "association.lock"
        ):
            self.assertIsNone(retrieve(self.config, self.warn))
        self.factory.assert_not_called()

    def test_bounded_reader_handles_fragments_events_and_bad_nonce(self):
        connection = _connection(1)
        self.addCleanup(connection.socket.close)
        actual_socket = connection.socket
        connection.socket = Mock()
        self.addCleanup(actual_socket.close)
        nonce = base64.b64encode(
            (int.from_bytes(connection.nonce, "little") + 1).to_bytes(
                24, "little"
            )
        ).decode()
        reply = json.dumps(
            {
                "action": "change-public-keys",
                "nonce": nonce,
                "success": "true",
            }
        ).encode()
        event = b'{"action":"database-locked"}'
        connection.socket.recv.side_effect = [event + reply[:4], reply[4:]]
        result = connection.get_unencrypted_response()
        self.assertEqual(result["nonce"], nonce)
        connection.socket.recv.side_effect = [
            json.dumps({**result, "nonce": "wrong"}).encode()
        ]
        with self.assertRaises(ConnectorError):
            connection.get_unencrypted_response()
        connection.socket.recv.side_effect = [b""]
        with self.assertRaises(ConnectorError):
            connection.get_unencrypted_response()

    def test_reader_times_out_and_rejects_oversized_reply(self):
        connection = _connection(1)
        self.addCleanup(connection.socket.close)
        actual_socket = connection.socket
        connection.socket = Mock()
        self.addCleanup(actual_socket.close)
        with patch(
            "kp_sync.keepassxc.time.monotonic", return_value=float("inf")
        ):
            with self.assertRaises(TimeoutError):
                connection.get_unencrypted_response()
        connection.socket.recv.return_value = b"x" * 4096
        with patch("kp_sync.keepassxc.MAX_REPLY", 4096):
            with self.assertRaises(ConnectorError):
                connection.get_unencrypted_response()

    def test_real_client_encrypted_exchange_and_no_unlock_requests(self):
        from nacl.public import Box, PrivateKey, PublicKey

        server_key = PrivateKey.generate()
        connection = _connection(1)
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.root)}):
            self.assertEqual(
                connection.get_socket_path(),
                str(self.root / "org.keepassxc.KeePassXC.BrowserServer"),
            )
        actual_socket = connection.socket
        self.addCleanup(actual_socket.close)
        wire = Mock()
        connection.socket = wire
        packets = []

        def send(data):
            request = json.loads(data)
            self.assertNotIn("triggerUnlock", request)
            nonce = base64.b64decode(request["nonce"])
            reply_nonce = (int.from_bytes(nonce, "little") + 1).to_bytes(
                24, "little"
            )
            reply = {
                "action": request["action"],
                "nonce": base64.b64encode(reply_nonce).decode(),
                "success": "true",
            }
            if request["action"] == "change-public-keys":
                wire.box = Box(
                    server_key,
                    PublicKey(base64.b64decode(request["publicKey"])),
                )
                reply["publicKey"] = base64.b64encode(
                    bytes(server_key.public_key)
                ).decode()
            else:
                body = json.loads(
                    wire.box.decrypt(
                        base64.b64decode(request["message"]), nonce
                    )
                )
                self.assertEqual(body["action"], request["action"])
                payload = {
                    "nonce": reply["nonce"],
                    "success": "true",
                    "hash": "database-identity",
                    "id": "kp-sync-test",
                }
                if request["action"] == "get-logins":
                    payload.update(
                        count=1, entries=[{"password": "test-secret"}]
                    )
                reply["message"] = base64.b64encode(
                    wire.box.encrypt(
                        json.dumps(payload).encode(), reply_nonce
                    ).ciphertext
                ).decode()
            encoded = json.dumps(reply).encode()
            packets.extend((encoded[:7], encoded[7:]))

        wire.sendall.side_effect = send
        wire.recv.side_effect = lambda _: packets.pop(0)
        connection.connect("synthetic")
        self.assertEqual(connection.get_databasehash(), "database-identity")
        self.assertTrue(connection.associate())
        self.assertTrue(connection.test_associate())
        self.assertEqual(
            connection.get_logins("https://kp-sync-test.invalid"),
            [{"password": "test-secret"}],
        )
        self.assertEqual(connection.response_hash, "database-identity")

    def test_missing_extra_warns_and_disabled_lookup_does_not_import(self):
        with patch.dict(
            "sys.modules", {"keepassxc_proxy_client.protocol": None}
        ):
            self.assertIsNone(retrieve(self.config, self.warn))
            self.assertIn(
                "install the keepassxc extra", self.warn.call_args.args[0]
            )
            self.warn.reset_mock()
            self.config.credentials.pop("keepassxc")
            self.assertIsNone(retrieve(self.config, self.warn))
            self.warn.assert_not_called()
