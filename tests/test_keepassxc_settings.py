# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
import tempfile
import unittest
from pathlib import Path

from kp_sync.paths import keepassxc_settings_path

FLATPAK = ".var/app/org.keepassxc.KeePassXC/config/keepassxc/keepassxc.ini"


class KeePassXCSettingsPathTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def create(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[General]\n", encoding="utf-8")
        return path

    def test_default_is_the_native_path(self):
        self.assertEqual(
            keepassxc_settings_path(self.home, {}),
            self.home / ".config/keepassxc/keepassxc.ini",
        )

    def test_xdg_config_home_is_honoured_when_absolute(self):
        config_home = self.home / "custom-config"
        environ = {"XDG_CONFIG_HOME": str(config_home)}
        self.assertEqual(
            keepassxc_settings_path(self.home, environ),
            config_home / "keepassxc/keepassxc.ini",
        )
        # A relative value is invalid per the XDG specification.
        self.assertEqual(
            keepassxc_settings_path(self.home, {"XDG_CONFIG_HOME": "rel"}),
            self.home / ".config/keepassxc/keepassxc.ini",
        )

    def test_flatpak_settings_are_used_when_only_they_exist(self):
        flatpak = self.create(self.home / FLATPAK)
        self.assertEqual(keepassxc_settings_path(self.home, {}), flatpak)

    def test_native_settings_win_over_flatpak(self):
        native = self.create(self.home / ".config/keepassxc/keepassxc.ini")
        self.create(self.home / FLATPAK)
        self.assertEqual(keepassxc_settings_path(self.home, {}), native)
