import tempfile
import tomllib
import unittest
from pathlib import Path

from kp_sync.cli import _build_parser
from kp_sync.config import load_config

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
UDEVMON = sorted(EXAMPLES.glob("udevmon/*.toml"))


class ExampleTest(unittest.TestCase):
    """Examples must stay valid as configuration and commands change."""

    def test_profiles_load(self):
        profiles = sorted(EXAMPLES.glob("configs/*.toml")) + [
            EXAMPLES / "main-laptop.toml",
            EXAMPLES / "second-laptop.toml",
        ]
        self.assertGreaterEqual(len(profiles), 8)
        for path in profiles:
            with self.subTest(path=path.name):
                with tempfile.TemporaryDirectory() as home:
                    config = load_config(
                        path, environ={"HOME": home, "XDG_RUNTIME_DIR": home}
                    )
                self.assertTrue(config.endpoints)

    def test_udevmon_kp_sync_actions_parse_and_keep_their_order(self):
        parser = _build_parser()
        for path in UDEVMON:
            with self.subTest(path=path.name):
                actions = tomllib.loads(path.read_text())["actions"]
                commands = []
                for action in actions:
                    argv = action["run"]
                    if argv[0] != "kp-sync":
                        continue
                    filled = [
                        arg.replace("{drive_serial}", "SERIAL")
                        .replace("{fs_uuid}", "UUID-1")
                        .replace("{mountpoint}", "/media/key")
                        for arg in argv[1:]
                    ]
                    args = parser.parse_args(filled)
                    self.assertEqual(args.command, "drive-sync")
                    self.assertEqual(args.drive_id, "SERIAL:UUID-1")
                    commands.append((args.event, args.mode, action["on"]))
                    self.assertEqual(action.get("lock"), "keydisk")
                self.assertEqual(
                    [event for event, _, _ in commands], ["up", "down"]
                )
                self.assertEqual(len({mode for _, mode, _ in commands}), 1)
                self.assertIn("mounted", commands[0][2])
                self.assertEqual(commands[1][2], ["unmounted"])
                runs = [action["run"][:2] for action in actions]
                if ["rclone-config", "up"] in runs:
                    # Credentials before the pre-sync, removal after the
                    # draining post-sync.
                    kp = [i for i, r in enumerate(runs) if r[0] == "kp-sync"]
                    self.assertLess(runs.index(["rclone-config", "up"]), kp[0])
                    self.assertGreater(
                        runs.index(["rclone-config", "down"]), kp[-1]
                    )


if __name__ == "__main__":
    unittest.main()
