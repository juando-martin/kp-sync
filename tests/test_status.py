import contextlib
import io
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kp_sync.cli import main
from kp_sync.config import load_config
from kp_sync.engine import SyncEngine, SyncError
from kp_sync.paths import atomic_write
from kp_sync.pending import PendingStore
from kp_sync.state import HistoryState, StateError
from kp_sync.ui.none import NoneUI


class StatusTest(unittest.TestCase):
    def test_hash_history_read_failure_is_reported_without_traceback(self):
        with tempfile.TemporaryDirectory() as name:
            config = load_config(
                environ={"HOME": name, "XDG_RUNTIME_DIR": name}
            )
            output = io.StringIO()
            with (
                patch("kp_sync.cli.load_config", return_value=config),
                patch("kp_sync.cli.ensure_run_dir", return_value=Path(name)),
                patch("kp_sync.cli.SyncEngine"),
                patch(
                    "kp_sync.cli.HistoryState.load",
                    side_effect=StateError("history changed during status"),
                ),
                contextlib.redirect_stderr(output),
            ):
                self.assertEqual(main(["status"]), 1)
            self.assertIn(
                "ERROR: history changed during status", output.getvalue()
            )

    def test_hash_prefixes_distinguish_loaded_history_and_long_output(self):
        from kp_sync.cli import _print_status

        current = "a" * 12 + "1" + "b" * 51
        ancestor = "a" * 12 + "2" + "b" * 51
        result = {
            "current": current,
            "copies": {"hub:test": {"version": current}},
            "sessions": {"session:test": {"version": ancestor}},
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            _print_status(result, versions=(current, ancestor))
        text = output.getvalue()
        self.assertIn("unchanged: " + current[:13] + "\n", text)
        self.assertEqual(text.count("Version: " + current[:13] + "\n"), 1)
        self.assertIn("Version: " + ancestor[:13] + "\n", text)
        self.assertNotIn(current, text)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            _print_status(result, long=True, versions=(current, ancestor))
        self.assertIn(current, output.getvalue())
        self.assertIn(ancestor, output.getvalue())

    def test_status_uses_display_aliases_and_separates_unknown_sessions(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            runtime = root / "runtime"
            runtime.mkdir()
            config_path = root / "config.toml"
            config_path.write_text(
                f'hub = {json.dumps(str(root / "hub.kdbx"))}\n'
                "[drives.aliases]\n"
                '"serial:uuid" = "Daily backup"\n'
            )
            config = load_config(
                config_path,
                environ={"HOME": name, "XDG_RUNTIME_DIR": str(runtime)},
            )
            run = root / "kp-sync"
            run.mkdir(mode=0o700)
            state = HistoryState()
            state.commit(
                "a" * 64,
                (),
                "host",
                ("drive:serial:uuid", "session:remote:abcdef12"),
                size=100,
            )
            state.save(run / "state.json")
            atomic_write(run / "last-sync.json", '{"action":"unchanged"}')
            status = SyncEngine(
                config, (), run_directory=run, ui=NoneUI()
            ).status()
            self.assertEqual(
                status.copies["drive:serial:uuid"]["name"], "Daily backup"
            )
            self.assertEqual(
                status.sessions["session:remote:abcdef12"]["lifecycle"],
                "historical_unknown",
            )
            self.assertEqual(status.summary["recorded_copies"], 1)
            self.assertEqual(status.summary["unfinished_sessions"], 0)
            output = io.StringIO()
            with (
                patch("kp_sync.cli.load_config", return_value=config),
                patch("kp_sync.cli.ensure_run_dir", return_value=run),
                patch(
                    "kp_sync.cli.endpoints_from_config",
                    side_effect=AssertionError("status must not probe"),
                ),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(main(["status"]), 0)
                self.assertIn("Version: " + "a" * 12 + "\n", output.getvalue())
                self.assertNotIn("a" * 64, output.getvalue())
                output.seek(0)
                output.truncate()
                self.assertEqual(main(["status", "--long"]), 0)
                self.assertIn("a" * 64, output.getvalue())
                output.seek(0)
                output.truncate()
                self.assertEqual(main(["status", "--json"]), 0)
                payload = json.loads(output.getvalue())
                self.assertEqual(payload["current"], "a" * 64)
                output.seek(0)
                output.truncate()
                self.assertEqual(main(["status"]), 0)
            self.assertIn("Daily backup", output.getvalue())
            self.assertIn(
                "historical_unknown: remote (abcdef12)", output.getvalue()
            )

    def test_cached_status_offline_records_distances_pending_and_exit(
        self,
    ):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = load_config(
                environ={"HOME": name, "XDG_RUNTIME_DIR": name}
            )
            run = root / "kp-sync"
            run.mkdir(mode=0o700)
            state = HistoryState()
            state.commit(
                "a" * 64, (), "old", ("hub:offline", "cloud:fake"), size=100
            )
            state.commit(
                "b" * 64,
                ("a" * 64,),
                "new",
                ("cloud:fake", "hub:new"),
                size=110,
            )
            state.save(run / "state.json")
            atomic_write(
                run / "last-sync.json",
                json.dumps(
                    {
                        "action": "committed",
                        "classifications": {"cloud:fake": "changed"},
                        "missing": [],
                    }
                ),
            )
            engine = SyncEngine(config, run_directory=run, ui=NoneUI())
            status = engine.status()
            self.assertEqual(
                status.copies["hub:offline"]["versions_behind"], 1
            )
            self.assertEqual(status.copies["cloud:fake"]["versions_behind"], 0)
            self.assertEqual(status.classifications["cloud:fake"], "unchanged")
            with (
                patch("kp_sync.cli.load_config", return_value=config),
                patch("kp_sync.cli.ensure_run_dir", return_value=run),
                patch(
                    "kp_sync.cli.endpoints_from_config",
                    side_effect=AssertionError("live probe"),
                ),
            ):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(main(["status", "--json"]), 0)
                self.assertIn(
                    "hub:offline", json.loads(output.getvalue())["copies"]
                )
                PendingStore(run).save("merge", "review required")
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["status"]), 10)
                self.assertEqual(
                    engine.status().decisions["merge"]["reason"],
                    "review required",
                )

    def test_status_reports_a_hub_saved_since_the_last_sync(self):
        import hashlib

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = load_config(
                environ={"HOME": name, "XDG_RUNTIME_DIR": name}
            )
            run = root / "kp-sync"
            run.mkdir(mode=0o700)
            config.hub.parent.mkdir(parents=True, exist_ok=True)
            config.hub.write_bytes(b"synced hub")
            synced = hashlib.sha256(b"synced hub").hexdigest()
            state = HistoryState()
            state.commit(
                synced, (), config.host, (f"hub:{config.host}",), size=10
            )
            state.save(run / "state.json")
            engine = SyncEngine(config, run_directory=run, ui=NoneUI())
            status = engine.status()
            hub = status.copies[f"hub:{config.host}"]
            self.assertNotEqual(hub["state"], "saved since the last sync")
            self.assertNotIn("hub_unsynced", status.summary)

            config.hub.write_bytes(b"edited offline")
            status = engine.status()
            self.assertEqual(
                status.copies[f"hub:{config.host}"]["state"],
                "saved since the last sync",
            )
            self.assertEqual(status.summary["hub_unsynced"], 1)
            with (
                patch("kp_sync.cli.load_config", return_value=config),
                patch("kp_sync.cli.ensure_run_dir", return_value=run),
                patch(
                    "kp_sync.cli.endpoints_from_config",
                    side_effect=AssertionError("live probe"),
                ),
            ):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    main(["status"])
            self.assertIn("not synchronized yet", output.getvalue())

    def test_status_rejects_symlink_and_malformed_cached_report(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = load_config(
                environ={"HOME": name, "XDG_RUNTIME_DIR": name}
            )
            engine = SyncEngine(config, run_directory=root, ui=NoneUI())
            atomic_write(root / "last-sync.json", "[]")
            with self.assertRaises(SyncError):
                engine.status()
            (root / "last-sync.json").unlink()
            (root / "last-sync.json").symlink_to(root / "elsewhere")
            with self.assertRaises(SyncError):
                engine.status()

    def test_live_status_includes_leases_and_uses_daemon_exit_status(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = load_config(
                environ={"HOME": name, "XDG_RUNTIME_DIR": name}
            )
            run = root / "kp-sync"
            run.mkdir(mode=0o700)
            listener = socket.socket(socket.AF_UNIX)
            self.addCleanup(listener.close)
            listener.bind(str(run / "control.sock"))
            reply = {
                "action": "pending",
                "leases": {"drive:serial:uuid": {"kind": "drive"}},
                "classifications": {},
                "decisions": {},
                "copies": {},
            }
            output = io.StringIO()
            with (
                patch.dict(os.environ, {"XDG_RUNTIME_DIR": name}),
                patch("kp_sync.cli.load_config", return_value=config),
                patch("kp_sync.cli.ensure_run_dir", return_value=run),
                patch(
                    "kp_sync.daemon.client.ensure_run_dir",
                    side_effect=AssertionError("client re-derived its path"),
                ),
                patch(
                    "kp_sync.daemon.client.Client.request",
                    autospec=True,
                    return_value=reply,
                ) as request,
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(main(["status"]), 10)
            self.assertEqual(request.call_args.args[0].directory, run)
            self.assertIn("lease drive:serial:uuid: drive", output.getvalue())

    def test_stale_socket_uses_cached_status_without_starting_daemon(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = load_config(
                environ={"HOME": name, "XDG_RUNTIME_DIR": name}
            )
            run = root / "kp-sync"
            run.mkdir(mode=0o700)
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(str(run / "control.sock"))
            os.chmod(run / "control.sock", 0o600)
            listener.close()
            atomic_write(run / "last-sync.json", '{"action":"unchanged"}')
            with (
                patch.dict(os.environ, {"XDG_RUNTIME_DIR": name}),
                patch("kp_sync.cli.load_config", return_value=config),
                patch("kp_sync.cli.ensure_run_dir", return_value=run),
                patch(
                    "kp_sync.daemon.client.ensure_run_dir",
                    side_effect=AssertionError("client re-derived its path"),
                ),
                patch(
                    "kp_sync.daemon.client.Client._start",
                    side_effect=AssertionError("no activation"),
                ),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(main(["status"]), 0)
