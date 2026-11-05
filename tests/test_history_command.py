import json
import tempfile
import unittest
from pathlib import Path

from pykeepass import PyKeePass

from tests.fixtures import create_database

from kp_sync.backups import backup_file
from kp_sync.config import load_config
from kp_sync.history_command import history_text
from kp_sync.kdbx import inspect_kdbx
from kp_sync.paths import ensure_private_directory, ensure_run_dir
from kp_sync.state import HistoryState


class CachedCredentials:
    def __init__(self, password):
        self.password = password

    def cached_password(self):
        return self.password


class HistoryCommandTest(unittest.TestCase):
    def test_history_lists_versions_backups_and_cached_entry_counts(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            runtime = root / "runtime"
            runtime.mkdir()
            hub = root / "hub.kdbx"
            password = "synthetic-history-password"
            create_database(str(hub), password)
            database = PyKeePass(str(hub), password)
            database.add_entry(
                database.root_group, "Synthetic", "user", "secret"
            )
            database.save()
            config_file = root / "config.toml"
            config_file.write_text(
                f"hub = {json.dumps(str(hub))}\n"
                f"key_file = {json.dumps(str(root / 'missing.keyx'))}\n",
                encoding="utf-8",
            )
            config = load_config(
                config_file,
                environ={"HOME": str(root), "XDG_RUNTIME_DIR": str(runtime)},
                hostname="history-test",
            )
            run_directory = ensure_run_dir(
                config.profile,
                xdg_runtime_dir=runtime,
            )
            base_directory = ensure_private_directory(run_directory / "bases")
            digest = inspect_kdbx(hub).sha256
            (base_directory / f"{digest}.kdbx").write_bytes(hub.read_bytes())
            (base_directory / f"{digest}.kdbx").chmod(0o600)
            state = HistoryState()
            state.commit(
                digest,
                (),
                "history-test",
                ("hub:history-test",),
                size=hub.stat().st_size,
            )
            state.save(run_directory / "state.json")
            backup_file(
                hub,
                run_directory / "backups",
                identity="hub:history-test",
            )

            result = history_text(
                config,
                run_directory=run_directory,
                credentials=CachedCredentials(password),
            )

            self.assertIn(digest, result)
            self.assertIn("history-test", result)
            self.assertEqual(result.count("entries: 1"), 2)
            self.assertIn("Backups from this boot:", result)


if __name__ == "__main__":
    unittest.main()
