import os
import io
import contextlib
import runpy
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from kp_sync.config import EndpointConfig, load_config
from kp_sync.paths import run_dir
from kp_sync.integration import (
    _rclone_checks,
    InstallError,
    InstallPaths,
    doctor,
    install_units,
    render_desktop_entries,
    render_units,
    _diagnostic_keyring_probe,
)
from tests.test_credentials import FakeKeyring


class IntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.config = load_config(
            environ={
                "HOME": str(self.home),
                "XDG_RUNTIME_DIR": str(self.runtime),
            },
            hostname="workstation",
        )

    def tearDown(self):
        self.temp.cleanup()

    def _drive_sync(self, event, mode="main", profile=None, status=0, **kw):
        from kp_sync.cli import main

        argv = ["--profile", profile] if profile else []
        argv += ["drive-sync", event]
        if mode:
            argv += ["--mode", mode]
        argv += ["--drive-id", "SERIAL:fs-123", str(self.root / "drive")]
        submitted = []
        error = io.StringIO()
        with (
            patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.runtime)}),
            patch(
                "kp_sync.drive_sync.identify_drive",
                return_value=kw.get("actual", "SERIAL:fs-123"),
            ),
            patch(
                "kp_sync.cli.main",
                side_effect=lambda args: submitted.append(args) or status,
            ),
            contextlib.redirect_stderr(error),
        ):
            result = main(argv)
        return result, submitted, error.getvalue()

    def _markers(self):
        return list((self.runtime / "kp-sync").glob("keydisk-up-*.pending"))

    def test_drive_sync_modes_submit_matching_commands_without_rclone(self):
        (self.root / "drive").mkdir()
        for mode, event, command in (
            ("main", "up", "up"),
            ("main", "down", "down"),
            ("secondary", "up", "attach"),
            ("secondary", "down", "detach"),
        ):
            with self.subTest(mode=mode, event=event):
                result, submitted, _ = self._drive_sync(
                    event, mode, profile="p"
                )
                self.assertEqual(result, 0)
                expected = [
                    "--profile",
                    "p",
                    "--no-wait",
                    command,
                    "--drive-id",
                    "SERIAL:fs-123",
                ]
                if event == "up":
                    expected.append(str((self.root / "drive").resolve()))
                self.assertEqual(submitted, [expected])
        self.assertFalse((self.runtime / "rclone").exists())
        self.assertFalse((self.home / ".dynamic_config").exists())

    def test_drive_sync_up_needs_no_rclone_config_and_keeps_marker(self):
        (self.root / "drive").mkdir()
        result, _, error = self._drive_sync("up", status=7)
        self.assertEqual(result, 7)
        self.assertIn("ERROR: kp-sync up failed (7)", error)
        self.assertEqual(len(self._markers()), 1)

    def test_drive_sync_refuses_wrong_identity_before_any_write(self):
        (self.root / "drive").mkdir()
        result, submitted, error = self._drive_sync("up", actual="OTHER:fs-9")
        self.assertEqual(result, 1)
        self.assertIn("does not match", error)
        self.assertEqual((submitted, self._markers()), ([], []))

    def test_drive_sync_down_keeps_hub_and_marker_untouched(self):
        hub = self.runtime / "pass.kdbx"
        hub.write_bytes(b"unsynced hub")
        result, submitted, _ = self._drive_sync("down")
        self.assertEqual((result, len(submitted)), (0, 1))
        self.assertEqual(hub.read_bytes(), b"unsynced hub")
        self.assertEqual(self._markers(), [])

    def test_drive_sync_requires_explicit_mode(self):
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                self._drive_sync("up", mode=None)

    def test_default_units_have_socket_activation_safety_and_idle_timer(self):
        units = render_units(self.config)
        service = units["kp-sync.service"]
        socket = units["kp-sync.socket"]
        path = units["kp-sync-hub.path"]
        timer = units["kp-sync-sync.timer"]
        self.assertIn("KeyringMode=shared", service)
        self.assertIn("Restart=on-failure", service)
        self.assertIn("TimeoutStopSec=90", service)
        self.assertIn("PassEnvironment=DISPLAY WAYLAND_DISPLAY", service)
        self.assertIn("SocketMode=0600", socket)
        self.assertIn("DirectoryMode=0700", socket)
        self.assertIn("PathChanged=", path)
        self.assertIn("OnUnitActiveSec=3600s", timer)
        self.assertIn("kp-sync@.service", units)
        self.assertIn("kp-sync@.socket", units)

    def test_named_profile_units_use_profile_safe_paths_and_commands(self):
        profile_file = self.home / ".config/kp-sync/work.toml"
        profile_file.parent.mkdir(parents=True)
        profile_file.write_text("hub_persistent = false\n", encoding="utf-8")
        named = load_config(
            profile="work",
            home=self.home,
            environ={
                "HOME": str(self.home),
                "XDG_RUNTIME_DIR": str(self.runtime),
            },
            hostname="workstation",
        )
        units = render_units(named)
        self.assertIn(
            "ExecStart=kp-sync --profile work daemon",
            units["kp-sync@work.service"],
        )
        self.assertIn(
            "ListenStream=%t/kp-sync-work/control.sock",
            units["kp-sync@work.socket"],
        )
        self.assertIn(
            f"PathChanged={self.runtime}/work/pass.kdbx",
            units["kp-sync-hub@work.path"],
        )
        desktop = render_desktop_entries(named)
        self.assertIn(
            "Exec=kp-sync --profile work run --raise-existing "
            "--app keepassxc -- %f",
            desktop["org.keepassxc.KeePassXC.desktop"],
        )
        self.assertIn(
            "Exec=kp-sync --profile work run --app keepassxc",
            desktop["kp-sync-keepassxc.desktop"],
        )

    def test_install_writes_templates_to_explicit_locations(self):
        paths = InstallPaths(
            self.home / ".config/systemd/user",
            self.home / ".local/share/applications",
            self.home / ".config/autostart",
        )
        installed = install_units(
            self.config, home=self.home, paths=paths, enable=False
        )
        self.assertIn(paths.unit_dir / "kp-sync.service", installed)
        self.assertIn(
            paths.applications_dir / "org.keepassxc.KeePassXC.desktop",
            installed,
        )
        self.assertIn(
            paths.autostart_dir / "kp-sync-keepassxc.desktop", installed
        )
        self.assertTrue((paths.unit_dir / "kp-sync@.service").is_file())
        self.assertTrue((paths.unit_dir / "kp-sync-hub.path").is_file())
        for path in installed:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_install_enables_only_socket_and_configured_triggers(self):
        paths = InstallPaths(
            self.home / ".config/systemd/user",
            self.home / ".local/share/applications",
            self.home / ".config/autostart",
        )
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        install_units(self.config, paths=paths, runner=runner)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0],
            [
                "systemctl",
                "--user",
                "enable",
                "kp-sync.socket",
                "kp-sync-hub.path",
            ],
        )

    def test_install_refuses_symlink_in_directory_tree_and_destination(self):
        target = self.root / "real"
        target.mkdir()
        symlinked = self.root / "linked"
        symlinked.symlink_to(target, target_is_directory=True)
        paths = InstallPaths(
            symlinked / "units",
            self.home / "apps",
            self.home / "autostart",
        )
        with self.assertRaisesRegex(InstallError, "symlink"):
            install_units(self.config, paths=paths)

        normal = InstallPaths(
            self.home / "units",
            self.home / "apps",
            self.home / "autostart",
        )
        normal.applications_dir.mkdir(parents=True)
        outside = self.root / "outside.desktop"
        outside.write_text("untouched", encoding="utf-8")
        (
            normal.applications_dir / "org.keepassxc.KeePassXC.desktop"
        ).symlink_to(outside)
        with self.assertRaisesRegex(InstallError, "unsafe existing"):
            install_units(self.config, paths=normal)
        self.assertEqual(outside.read_text(encoding="utf-8"), "untouched")

    def test_doctor_returns_explicit_results_and_uses_injected_probes(self):
        paths = InstallPaths(
            self.home / ".config/systemd/user",
            self.home / ".local/share/applications",
            self.home / ".config/autostart",
        )
        install_units(self.config, home=self.home, paths=paths, enable=False)
        key_file = self.home / "data/key.keyx"
        key_file.parent.mkdir()
        key_file.write_bytes(b"fixture")
        config = replace(self.config, key_file=key_file)
        ini = self.home / ".config/keepassxc/keepassxc.ini"
        ini.parent.mkdir(parents=True)
        ini.write_text("[General]\n", encoding="utf-8")
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            if argv[0] == "systemctl":
                return subprocess.CompletedProcess(argv, 0, "enabled\n", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        checks = doctor(
            config,
            home=self.home,
            paths=paths,
            runtime_dir=self.runtime,
            runner=runner,
            socket_probe=lambda path: (True, "protocol version 1"),
            keyring_probe=lambda: (True, "test key"),
            swap_probe=lambda: (True, "no swap"),
        )
        self.assertTrue(all(item.ok for item in checks), checks)
        self.assertIn("socket-protocol", {item.name for item in checks})
        by_name = {item.name: item for item in checks}
        self.assertTrue(by_name["unit:daemon-safety"].ok)
        self.assertTrue(by_name["unit:socket-safety"].ok)
        self.assertIn("keyring-possession", {item.name for item in checks})
        self.assertEqual(sum(call[0] == "systemctl" for call in calls), 2)

    def test_doctor_reports_unusable_baseline_store_names(self):
        config = replace(
            self.config,
            endpoints=(
                EndpointConfig(name="cloud", type="rclone", remote="Drive:"),
            ),
            baseline=self.config.baseline | {"stores": ["cloud", "pendrive"]},
        )
        checks = doctor(
            config,
            home=self.home,
            runtime_dir=self.runtime,
            runner=lambda argv, **kwargs: subprocess.CompletedProcess(
                argv, 0, "", ""
            ),
            socket_probe=lambda path: (True, "protocol version 1"),
            keyring_probe=lambda: (True, "test key"),
            swap_probe=lambda: (True, "no swap"),
        )
        warnings = [
            item for item in checks if item.name.startswith("baseline:")
        ]
        self.assertEqual(len(warnings), 1)
        self.assertTrue(warnings[0].ok)
        self.assertIn(
            "WARNING: baseline store 'pendrive'", warnings[0].message
        )

    def test_doctor_reports_corrupt_pending_state(self):
        from kp_sync.paths import run_dir

        runtime = run_dir(xdg_runtime_dir=self.runtime)
        runtime.mkdir(mode=0o700)
        pending = runtime / "pending.json"
        pending.write_text("{invalid", encoding="utf-8")
        pending.chmod(0o600)
        checks = doctor(
            self.config,
            home=self.home,
            runtime_dir=self.runtime,
            runner=lambda argv, **kwargs: subprocess.CompletedProcess(
                argv, 0, "", ""
            ),
            socket_probe=lambda path: (True, "protocol version 1"),
            keyring_probe=lambda: (True, "test key"),
            swap_probe=lambda: (True, "no swap"),
        )
        check = next(item for item in checks if item.name == "pending-state")
        self.assertFalse(check.ok)
        self.assertIn("pending decisions need repair", check.message)

    def test_doctor_session_self_test_reports_failures_and_is_opt_in(self):
        options = dict(
            home=self.home,
            runtime_dir=self.runtime,
            runner=lambda *args, **kwargs: subprocess.CompletedProcess(
                [], 0, "", ""
            ),
            socket_probe=lambda _: (True, "ok"),
            keyring_probe=lambda: (True, "ok"),
            swap_probe=lambda: (True, "ok"),
        )
        with patch("kp_sync.sessions.diagnostic.self_test") as probe:
            checks = doctor(self.config, **options)
            probe.assert_not_called()
            self.assertNotIn(
                "session:self-test", {item.name for item in checks}
            )
            checks = doctor(self.config, self_test=True, **options)
            self.assertTrue(
                next(
                    item for item in checks if item.name == "session:self-test"
                ).ok
            )
            probe.side_effect = RuntimeError("synthetic round trip failure")
            checks = doctor(self.config, self_test=True, **options)
            failed = next(
                item for item in checks if item.name == "session:self-test"
            )
            self.assertFalse(failed.ok)
            self.assertIn("synthetic round trip failure", failed.message)

    def test_doctor_rejects_service_without_shared_keyring_mode(self):
        paths = InstallPaths(
            self.home / ".config/systemd/user",
            self.home / ".local/share/applications",
            self.home / ".config/autostart",
        )
        install_units(self.config, home=self.home, paths=paths, enable=False)
        service = paths.unit_dir / "kp-sync.service"
        service.write_text(
            service.read_text(encoding="utf-8").replace(
                "KeyringMode=shared\n", ""
            ),
            encoding="utf-8",
        )
        checks = doctor(
            self.config,
            home=self.home,
            paths=paths,
            runtime_dir=self.runtime,
            runner=lambda argv, **kwargs: subprocess.CompletedProcess(
                argv, 0, "enabled\n", ""
            ),
            socket_probe=lambda path: (True, "protocol version 1"),
            keyring_probe=lambda: (True, "test key"),
            swap_probe=lambda: (True, "no swap"),
        )
        by_name = {item.name: item for item in checks}
        self.assertFalse(by_name["unit:daemon-safety"].ok)
        self.assertIn(
            "KeyringMode=shared", by_name["unit:daemon-safety"].message
        )

    def test_doctor_reports_bad_reload_key_and_unencrypted_hub(self):
        paths = InstallPaths(
            self.home / ".config/systemd/user",
            self.home / ".local/share/applications",
            self.home / ".config/autostart",
        )
        install_units(self.config, home=self.home, paths=paths, enable=False)
        ini = self.home / ".config/keepassxc/keepassxc.ini"
        ini.parent.mkdir(parents=True)
        ini.write_text(
            "[General]\nAutoReloadOnChange=false\n", encoding="utf-8"
        )
        checks = doctor(
            replace(self.config, require_encrypted=True),
            home=self.home,
            paths=paths,
            runtime_dir=self.runtime,
            runner=lambda argv, **kwargs: subprocess.CompletedProcess(
                argv, 0, "enabled\n", ""
            ),
            socket_probe=lambda path: (False, "no response"),
            keyring_probe=lambda: (False, "EACCES"),
            swap_probe=lambda: (False, "/dev/sda2"),
            encrypted_hub_probe=lambda config: (False, "not dm-crypt"),
        )
        by_name = {item.name: item for item in checks}
        self.assertFalse(by_name["keepassxc-reload"].ok)
        self.assertIn("reload", by_name["keepassxc-reload"].message)
        self.assertFalse(by_name["encrypted-hub"].ok)
        self.assertFalse(by_name["encrypted-swap"].ok)
        self.assertFalse(by_name["keyring-possession"].ok)
        self.assertFalse(by_name["socket-protocol"].ok)

    def test_doctor_checks_rclone_remote_and_its_database_probe(self):
        config = replace(
            self.config,
            endpoints=(
                self.config.endpoints
                + (EndpointConfig("cloud", "rclone", remote="Drive"),)
            ),
        )
        paths = InstallPaths(
            self.home / ".config/systemd/user",
            self.home / ".local/share/applications",
            self.home / ".config/autostart",
        )
        install_units(config, home=self.home, paths=paths, enable=False)
        (self.home / ".config/keepassxc").mkdir(parents=True)
        (self.home / ".config/keepassxc/keepassxc.ini").write_text(
            "[General]\nAutoReloadOnChange=true\n", encoding="utf-8"
        )

        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            if argv[0] == "systemctl":
                return subprocess.CompletedProcess(argv, 0, "enabled\n", "")
            if argv[1:2] == ["listremotes"]:
                return subprocess.CompletedProcess(argv, 0, "Drive:\n", "")
            if "lsjson" in argv:
                return subprocess.CompletedProcess(argv, 0, "[]\n", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        external = self.root / "rclone.conf"
        external.write_text("[Drive]\n", encoding="utf-8")
        for remote in ("Drive", "Drive:", "Drive:kp", "Drive:kp/"):
            with self.subTest(remote=remote):
                calls.clear()
                configured = replace(
                    config,
                    db_name="vault.kdbx",
                    endpoints=(
                        EndpointConfig(
                            "cloud",
                            "rclone",
                            remote=remote,
                            rclone_config=self.root / "rclone.conf",
                        ),
                    ),
                )
                checks = doctor(
                    configured,
                    home=self.home,
                    paths=paths,
                    runtime_dir=self.runtime,
                    runner=runner,
                    socket_probe=lambda path: (True, "protocol version 1"),
                    keyring_probe=lambda: (True, "ok"),
                    swap_probe=lambda: (True, "none"),
                )
                self.assertTrue(
                    next(
                        item for item in checks if item.name == "rclone:cloud"
                    ).ok
                )
                database = (
                    "Drive:kp/vault.kdbx"
                    if "kp" in remote
                    else "Drive:vault.kdbx"
                )
                self.assertIn(
                    [
                        "rclone",
                        "lsjson",
                        "--hash",
                        database,
                        "--config",
                        str(
                            run_dir(None, xdg_runtime_dir=self.runtime)
                            / "rclone/cloud.conf"
                        ),
                    ],
                    calls,
                )
        self.assertEqual(external.read_text(encoding="utf-8"), "[Drive]\n")

    def test_doctor_reports_unpublished_rclone_credentials(self):
        configured = replace(
            self.config,
            endpoints=(
                EndpointConfig(
                    "cloud",
                    "rclone",
                    remote="Drive",
                    rclone_config=self.root / "missing.conf",
                ),
            ),
        )
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        checks = _rclone_checks(configured, runner, self.runtime)
        self.assertIn("unavailable", checks[0].message)
        self.assertEqual(calls, [])

    def test_doctor_diagnostic_key_can_be_revoked_and_expires_if_interrupted(
        self,
    ):
        from linux_keyctl import KEY_POS_SETATTR

        keyring = FakeKeyring()
        keyring.permissions = KEY_POS_SETATTR

        def runner(argv, **kwargs):
            self.assertIn("--property=KeyringMode=shared", argv)
            return subprocess.CompletedProcess(argv, 0, keyring.value, b"")

        with contextlib.ExitStack() as stack:
            for name in ("add_key", "set_perm", "set_timeout", "revoke"):
                stack.enter_context(
                    patch(f"linux_keyctl.{name}", getattr(keyring, name))
                )
            ok, detail = _diagnostic_keyring_probe(runner)()
        self.assertTrue(ok, detail)
        self.assertTrue(keyring.revoked)
        self.assertEqual(keyring.timeout, 60)


if __name__ == "__main__":
    unittest.main()
