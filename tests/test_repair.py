import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kp_sync.config import load_config
from kp_sync.credentials import CredentialProvider
from kp_sync.endpoints import FileEndpoint
from kp_sync.engine import SyncEngine
from kp_sync.history import sha256_file
from kp_sync.pending import PendingStore
from kp_sync.repair import repair_history
from kp_sync.state import HistoryState
from kp_sync.ui.none import NoneUI
from tests.fixtures import kdbx4_bytes
from tests.test_baseline import FakeStore
from tests.test_credentials import FakeKeyring

LOST = "b" * 64


class RepairHistoryTest(unittest.TestCase):
    """C (base in the cloud) <- B (base lost) <- A (held by the hub)."""

    def setUp(self):
        self.enterContext(
            patch.object(
                CredentialProvider, "_backend", return_value=FakeKeyring()
            )
        )
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        runtime = self.root / "runtime"
        runtime.mkdir()
        self.hub = self.root / "hub.kdbx"
        config_path = self.root / "config.toml"
        config_path.write_text(
            f"hub = {json.dumps(str(self.hub))}\n"
            f'key_file = {json.dumps(str(self.root / "test.keyx"))}\n'
            "[sync]\nlock_timeout = 1\n",
            encoding="utf-8",
        )
        self.config = load_config(
            config_path,
            environ={"HOME": str(self.root), "XDG_RUNTIME_DIR": str(runtime)},
            hostname="test-host",
        )
        self.run_dir = runtime / "kp-sync"
        self.run_dir.mkdir(mode=0o700)
        self.cloud = FakeStore(self.root / "cloud")

        c_data = kdbx4_bytes(b"version c" * 50)
        a_data = kdbx4_bytes(b"version a" * 50)
        c_file = self.root / "c.kdbx"
        c_file.write_bytes(c_data)
        self.c_sha = sha256_file(str(c_file))[0]
        self.hub.write_bytes(a_data)
        self.a_sha = sha256_file(str(self.hub))[0]
        self.a_data = a_data
        state = HistoryState()
        state.commit(self.c_sha, (), "h", ("file:old",), size=len(c_data))
        state.commit(LOST, (self.c_sha,), "h", ("drive:serial:uuid",), size=9)
        state.commit(self.a_sha, (LOST,), "h", ("hub:h",), size=len(a_data))
        self.cloud.put(f"bases/{self.c_sha}.kdbx", c_file)
        state.save(self.run_dir / "state.json")
        self.cloud.put("state.json", self.run_dir / "state.json")
        self.engine = SyncEngine(
            self.config,
            (FileEndpoint("a", self.root / "absent.kdbx", identity="file:a"),),
            run_directory=self.run_dir,
            baseline_stores=(self.cloud,),
            ui=NoneUI(),
        )

    def snapshot(self):
        return (
            (self.run_dir / "state.json").read_bytes(),
            (self.cloud.root / "state.json").read_bytes(),
            sorted(
                path.name for path in (self.cloud.root / "bases").iterdir()
            ),
            (self.run_dir / "bases").exists(),
        )

    def test_preview_reports_and_changes_nothing(self):
        before = self.snapshot()
        report = repair_history(self.config, engine=self.engine)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(set(report["versions"]), {self.a_sha, LOST})
        a = report["versions"][self.a_sha]
        self.assertTrue(a["recoverable"])
        self.assertEqual(a["missing_from"], ["cloud"])
        self.assertIn(f"local copy {self.hub}", a["available_from"])
        lost = report["versions"][LOST]
        self.assertFalse(lost["recoverable"])
        self.assertEqual(lost["held_by"], ["drive:serial:uuid"])
        self.assertEqual(report["detached_drives"], ["drive:serial:uuid"])
        self.assertEqual(report["blocked"], [])

    def test_apply_requires_confirmation_and_no_blockers(self):
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "confirm-writers-paused"):
            repair_history(self.config, engine=self.engine, apply=True)
        PendingStore(self.run_dir).save("sync", "synthetic decision")
        with self.assertRaisesRegex(ValueError, "pending decisions"):
            repair_history(
                self.config,
                engine=self.engine,
                apply=True,
                writers_paused=True,
            )
        self.assertEqual(self.snapshot(), before)

    def test_unavailable_history_blocks_apply(self):
        self.cloud.online = False
        with self.assertRaisesRegex(ValueError, "unavailable history"):
            repair_history(
                self.config,
                engine=self.engine,
                apply=True,
                writers_paused=True,
            )

    def test_apply_restores_bases_and_contracts_lost_versions(self):
        report = repair_history(
            self.config, engine=self.engine, apply=True, writers_paused=True
        )
        self.assertEqual(report["removed"], [LOST])
        self.assertEqual(report["removed_copies"], ["drive:serial:uuid"])
        for path in (
            self.run_dir / "state.json",
            self.cloud.root / "state.json",
        ):
            state = HistoryState.load(path)
            self.assertEqual(set(state.versions), {self.a_sha, self.c_sha})
            # B's parent passes to its child, so ancestry survives.
            self.assertEqual(state.versions[self.a_sha].parents, (self.c_sha,))
            self.assertNotIn("drive:serial:uuid", state.copies)
        self.assertEqual(
            (self.cloud.root / "bases" / f"{self.a_sha}.kdbx").read_bytes(),
            self.a_data,
        )
        # A second run finds nothing left to repair.
        self.assertEqual(
            repair_history(self.config, engine=self.engine)["versions"], {}
        )
