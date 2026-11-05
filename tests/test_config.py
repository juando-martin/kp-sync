import json
import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path

from kp_sync.config import ConfigError, load_config, require_encrypted_hub


class ConfigTest(unittest.TestCase):
    def test_keepassxc_url_opts_in_and_rejects_malformed_settings(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "config.toml"
            for value in (
                'url = "https://kp-sync-test.invalid"',
                'url = ""',
                'url = "not-a-url"',
                'url = "https://user:secret@example.invalid"',
                "url = 42",
                "enabled = true",
            ):
                with self.subTest(value=value):
                    path.write_text("[credentials.keepassxc]\n" + value)
                    if value == 'url = "https://kp-sync-test.invalid"':
                        config = load_config(
                            path, environ={"XDG_RUNTIME_DIR": name}
                        )
                        self.assertEqual(
                            config.credentials["keepassxc"]["url"],
                            "https://kp-sync-test.invalid",
                        )
                    else:
                        with self.assertRaises(ConfigError):
                            load_config(
                                path, environ={"XDG_RUNTIME_DIR": name}
                            )

    def test_defaults_and_named_profile_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            config_dir = home / ".config" / "kp-sync"
            config_dir.mkdir(parents=True)
            (config_dir / "work.toml").write_text(
                "hub_persistent = true\n", encoding="utf-8"
            )
            config = load_config(
                profile="work",
                home=home,
                environ={
                    "HOME": str(home),
                    "XDG_RUNTIME_DIR": "/run/user/1000",
                },
                hostname="test-host",
            )
        self.assertEqual(config.hub, Path("/run/user/1000/work/pass.kdbx"))
        self.assertTrue(config.require_encrypted)
        self.assertTrue(config.daemon["idle_sync"])
        self.assertEqual(config.host, "test-host")
        self.assertEqual(config.verify["foreign_check"], "always")
        self.assertEqual(config.sync["max_restarts"], 3)

    def test_custom_gui_command_is_a_non_empty_argv_array(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "config.toml"
            path.write_text('[ui]\ngui = ["dialog-helper", "--json"]\n')
            config = load_config(
                path,
                environ={"HOME": name, "XDG_RUNTIME_DIR": name},
            )
            self.assertEqual(config.ui["gui"], ["dialog-helper", "--json"])

            for value in ("[]", '[""]', '["dialog-helper", 1]'):
                with self.subTest(value=value):
                    path.write_text(f"[ui]\ngui = {value}\n")
                    with self.assertRaisesRegex(ConfigError, "ui.gui"):
                        load_config(
                            path,
                            environ={"HOME": name, "XDG_RUNTIME_DIR": name},
                        )

    def test_named_ssh_sessions_and_access_owners(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "config.toml"
            path.write_text(
                "[sessions]\nenabled = true\n"
                '[ssh_sessions.home]\ntarget = "bob@host:~/kp"\nmode = "rw"\n'
                'mount_dir = "~/vault"\n'
                "[access_owners.laptop]\n"
                'command = ["kp-access-owner", "--config", "owner.toml"]\n',
                encoding="utf-8",
            )
            config = load_config(
                path,
                environ={"HOME": name, "XDG_RUNTIME_DIR": name},
            )

        self.assertEqual(
            config.ssh_sessions["home"],
            {
                "target": "bob@host:~/kp",
                "mode": "rw",
                "mount_dir": "~/vault",
            },
        )
        self.assertEqual(
            config.access_owners["laptop"]["command"],
            ["kp-access-owner", "--config", "owner.toml"],
        )

    def test_named_service_config_is_validated(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "config.toml"
            invalid_configs = (
                ('[ssh_sessions.bad]\ntarget = ""\n', "target"),
                (
                    '[ssh_sessions.good]\ntarget = "host"\nmode = "rwx"\n',
                    "mode",
                ),
                ('[ssh_sessions."bad/name"]\ntarget = "host"\n', "name"),
                ("[access_owners.owner]\ncommand = []\n", "command"),
            )
            for text, error in invalid_configs:
                with self.subTest(text=text):
                    path.write_text(text, encoding="utf-8")
                    with self.assertRaisesRegex(ConfigError, error):
                        load_config(
                            path,
                            environ={"HOME": name, "XDG_RUNTIME_DIR": name},
                        )

    def test_explicit_configuration_and_unknown_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(
                f'hub = {json.dumps(str(Path(temp_dir) / "hub.kdbx"))}\n'
                '[[endpoints]]\nname = "backup"\ntype = "file"\n'
                f'path = {json.dumps(str(Path(temp_dir) / "copy.kdbx"))}\n',
                encoding="utf-8",
            )
            config = load_config(
                config_path,
                environ={"HOME": temp_dir},
                hostname="host",
            )
            self.assertEqual(config.endpoints[0].identity, None)
            self.assertEqual(
                config.endpoints[0].path, Path(temp_dir) / "copy.kdbx"
            )

            config_path.write_text("unexpected = true\n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "unknown top-level"):
                load_config(config_path, environ={"HOME": temp_dir})

    def test_invalid_endpoint_and_profile_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(
                '[[endpoints]]\nname = "remote"\ntype = "command"\n'
                'fetch = ["echo", "{file}"]\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "store"):
                load_config(config_path, environ={"XDG_RUNTIME_DIR": temp_dir})
            with self.assertRaisesRegex(ConfigError, "invalid profile"):
                load_config(
                    profile="../oops",
                    home=temp_dir,
                    environ={"HOME": temp_dir, "XDG_RUNTIME_DIR": temp_dir},
                )

    def test_probe_is_content_hash_is_command_only_and_needs_a_probe(
        self,
    ) -> None:
        command = (
            '[[endpoints]]\nname = "remote"\ntype = "command"\n'
            'fetch = ["get", "{file}"]\nstore = ["put", "{file}"]\n'
        )
        cases = (
            (command, False, None),
            (
                command + 'probe = ["stat"]\nprobe_is_content_hash = true\n',
                True,
                None,
            ),
            (command + "probe_is_content_hash = true\n", None, "requires"),
            (
                command + 'probe = ["stat"]\nprobe_is_content_hash = "yes"\n',
                None,
                "probe_is_content_hash",
            ),
            (
                '[[endpoints]]\nname = "cloud"\ntype = "rclone"\n'
                'remote = "gd"\nprobe_is_content_hash = true\n',
                None,
                "invalid key",
            ),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            environ = {"HOME": temp_dir, "XDG_RUNTIME_DIR": temp_dir}
            for text, expected, error in cases:
                with self.subTest(text=text):
                    config_path.write_text(text, encoding="utf-8")
                    if error is not None:
                        with self.assertRaisesRegex(ConfigError, error):
                            load_config(config_path, environ=environ)
                        continue
                    config = load_config(config_path, environ=environ)
                    self.assertIs(
                        config.endpoints[0].probe_is_content_hash, expected
                    )

    def test_missing_default_config_with_named_profiles_needs_a_profile(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            environ = {"HOME": temp_dir, "XDG_RUNTIME_DIR": temp_dir}
            self.assertIsNone(load_config(environ=environ).source)
            directory = Path(temp_dir) / ".config" / "kp-sync"
            directory.mkdir(parents=True)
            (directory / "beta.toml").write_text("")
            (directory / "kp-test.toml").write_text("")
            (directory / "notes.txt").write_text("")
            with self.assertRaisesRegex(
                ConfigError, "--profile with one of: beta, kp-test"
            ):
                load_config(environ=environ)
            self.assertEqual(
                load_config(profile="beta", environ=environ).profile,
                "beta",
            )
            (directory / "config.toml").write_text("")
            self.assertEqual(
                load_config(environ=environ).source, directory / "config.toml"
            )

    def test_rclone_transport_defaults_to_auto(self) -> None:
        rclone = (
            '[[endpoints]]\nname = "cloud"\ntype = "rclone"\nremote = "gd"\n'
        )
        cases = (
            (rclone, "auto", None),
            (rclone + 'rclone_transport = "auto"\n', "auto", None),
            (rclone + 'rclone_transport = "cli"\n', "cli", None),
            (rclone + 'rclone_transport = "rcd"\n', None, "cli or auto"),
            (rclone + "rclone_transport = true\n", None, "rclone_transport"),
            (
                '[[endpoints]]\nname = "f"\ntype = "file"\npath = "/x"\n'
                'rclone_transport = "auto"\n',
                None,
                "invalid key",
            ),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            environ = {"HOME": temp_dir, "XDG_RUNTIME_DIR": temp_dir}
            for text, expected, error in cases:
                with self.subTest(text=text):
                    config_path.write_text(text, encoding="utf-8")
                    if error is not None:
                        with self.assertRaisesRegex(ConfigError, error):
                            load_config(config_path, environ=environ)
                        continue
                    config = load_config(config_path, environ=environ)
                    self.assertEqual(
                        config.endpoints[0].rclone_transport, expected
                    )

    def test_runtime_drive_endpoint_does_not_need_a_static_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(
                '[[endpoints]]\nname = "pendrive"\ntype = "file"\n'
                'source = "runtime"\n',
                encoding="utf-8",
            )
            config = load_config(
                config_path,
                environ={"HOME": temp_dir, "XDG_RUNTIME_DIR": temp_dir},
            )
        self.assertEqual(config.endpoints[0].source, "runtime")
        self.assertIsNone(config.endpoints[0].identity)
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(
                '[[endpoints]]\nname = "one"\ntype = "file"\n'
                'source = "runtime"\n'
                '[[endpoints]]\nname = "two"\ntype = "file"\n'
                'source = "runtime"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ConfigError, "only one enabled file endpoint"
            ):
                load_config(
                    config_path,
                    environ={"HOME": temp_dir, "XDG_RUNTIME_DIR": temp_dir},
                )

    def test_runtime_database_name_cannot_escape_mountpoint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(
                f'hub = {json.dumps(str(Path(temp_dir) / "hub.kdbx"))}\n'
                'db_name = "../outside.kdbx"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "single file name"):
                load_config(config_path, environ={"HOME": temp_dir})

    def test_baseline_directory_cannot_escape_or_remove_store_root(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "config.toml"
            for directory in (
                "",
                ".",
                "..",
                "../outside",
                "/outside",
                "a/../../outside",
                "C:\\outside",
            ):
                with self.subTest(directory=directory):
                    path.write_text(
                        f"[baseline]\ndir = {json.dumps(directory)}\n"
                    )
                    with self.assertRaisesRegex(ConfigError, "baseline.dir"):
                        load_config(
                            path,
                            environ={"HOME": name, "XDG_RUNTIME_DIR": name},
                        )

    def test_baseline_store_names_warn_when_partially_unusable(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "config.toml"
            path.write_text(
                'hub = "hub.kdbx"\n'
                '[baseline]\nstores = ["cloud", "pendrive"]\n'
                '[[endpoints]]\nname = "cloud"\ntype = "rclone"\n'
                'remote = "Drive:"\n',
                encoding="utf-8",
            )
            output = io.StringIO()
            with contextlib.redirect_stderr(output):
                config = load_config(path, environ={"HOME": name})
        self.assertEqual(config.baseline["stores"], ["cloud", "pendrive"])
        self.assertIn(
            "WARNING: baseline store 'pendrive' does not match",
            output.getvalue(),
        )

    def test_explicit_baseline_stores_must_have_a_usable_store(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "config.toml"
            path.write_text(
                'hub = "hub.kdbx"\n'
                '[baseline]\nstores = ["backup"]\n'
                '[[endpoints]]\nname = "backup"\ntype = "file"\n'
                'path = "copy.kdbx"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ConfigError, "no usable configured stores"
            ):
                load_config(path, environ={"HOME": name})

            path.write_text(
                'hub = "hub.kdbx"\n[baseline]\nstores = []\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "the list is empty"):
                load_config(path, environ={"HOME": name})

    def test_drive_directory_must_stay_inside_mountpoint(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "config.toml"
            for directory in (
                "",
                "..",
                "../outside",
                "/outside",
                "a/../../outside",
                "C:\\outside",
                "kp\0",
            ):
                with self.subTest(directory=directory):
                    path.write_text(
                        f"[drives]\ndirectory = {json.dumps(directory)}\n"
                    )
                    with self.assertRaisesRegex(
                        ConfigError, "drives.directory"
                    ):
                        load_config(
                            path,
                            environ={"HOME": name, "XDG_RUNTIME_DIR": name},
                        )

    def test_drive_aliases_validate_identity_and_display_name(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "config.toml"
            path.write_text(
                "[drives.aliases]\n"
                '"serial-a:UUID-1" = "Backup drive"\n'
                '"serial-b:UUID-2" = "Backup drive"\n'
            )
            config = load_config(
                path, environ={"HOME": name, "XDG_RUNTIME_DIR": name}
            )
            self.assertEqual(
                config.drives["aliases"],
                {
                    "serial-a:UUID-1": "Backup drive",
                    "serial-b:UUID-2": "Backup drive",
                },
            )
            for entry in (
                '"serial-a:UUID/1" = "bad"\n',
                '"serial-a:UUID-1" = "bad\\u001b"\n',
                '"serial-a:UUID-1" = "   "\n',
            ):
                with self.subTest(entry=entry):
                    path.write_text("[drives.aliases]\n" + entry)
                    with self.assertRaises(ConfigError):
                        load_config(
                            path,
                            environ={"HOME": name, "XDG_RUNTIME_DIR": name},
                        )

    def test_negative_daemon_debounce_is_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "config.toml"
            path.write_text("[daemon]\ndebounce = -1.0\n")
            with self.assertRaisesRegex(ConfigError, "daemon.debounce"):
                load_config(
                    path, environ={"HOME": name, "XDG_RUNTIME_DIR": name}
                )

    def test_persistent_hub_requires_encryption_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            hub = Path(temp_dir) / "hub.kdbx"
            calls = []

            def runner(argv, **kwargs):
                calls.append(argv)
                if argv[0] == "findmnt":
                    return subprocess.CompletedProcess(
                        argv, 0, "ext4 /dev/mapper/secure\n", ""
                    )
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    json.dumps(
                        {"blockdevices": [{"type": "crypt", "children": []}]}
                    ),
                    "",
                )

            require_encrypted_hub(hub, runner=runner)
            self.assertEqual([call[0] for call in calls], ["findmnt", "lsblk"])

            def unencrypted_runner(argv, **kwargs):
                if argv[0] == "findmnt":
                    return subprocess.CompletedProcess(
                        argv, 0, "ext4 /dev/sda1\n", ""
                    )
                return subprocess.CompletedProcess(
                    argv, 0, '{"blockdevices":[{"type":"part"}]}', ""
                )

            with self.assertRaisesRegex(ConfigError, "not on a dm-crypt"):
                require_encrypted_hub(hub, runner=unencrypted_runner)

    def test_persistent_hub_accepts_encrypted_zfs_with_key_loaded(self):
        hub = Path("/home/user/keepass/pass.kdbx")

        def zfs_runner(encryption, keystatus, calls):
            def runner(argv, **kwargs):
                calls.append(argv)
                if argv[0] == "findmnt":
                    return subprocess.CompletedProcess(
                        argv, 0, "zfs pool/home/user\n", ""
                    )
                return subprocess.CompletedProcess(
                    argv, 0, f"{encryption}\n{keystatus}\n", ""
                )

            return runner

        calls = []
        require_encrypted_hub(
            hub, runner=zfs_runner("aes-256-gcm", "available", calls)
        )
        self.assertEqual([call[0] for call in calls], ["findmnt", "zfs"])
        self.assertEqual(calls[1][-1], "pool/home/user")
        for encryption, keystatus in (
            ("off", "-"),
            ("aes-256-gcm", "unavailable"),
            ("-", "-"),
        ):
            with (
                self.subTest(encryption=encryption, keystatus=keystatus),
                self.assertRaisesRegex(ConfigError, "encrypted ZFS"),
            ):
                require_encrypted_hub(
                    hub, runner=zfs_runner(encryption, keystatus, [])
                )


if __name__ == "__main__":
    unittest.main()
