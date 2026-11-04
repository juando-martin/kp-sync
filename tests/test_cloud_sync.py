import json
import fnmatch
import hashlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from pykeepass import PyKeePass, create_database

from kp_sync.baseline import CloudStore, DriveStore
from kp_sync.baseline import BaselineManager
from kp_sync.config import load_config
from kp_sync.drives import DriveEndpoint
from kp_sync.endpoints.command import CommandEndpoint
from kp_sync.endpoints.rclone import RcloneEndpoint
from kp_sync.endpoints.base import EndpointUnavailable, new_hash
from kp_sync.engine import SyncEngine
from kp_sync.kdbx import inspect_kdbx
from kp_sync.state import HistoryState, StateError
from kp_sync.ui.fake import FakeUI
from kp_sync.ui.none import NoneUI
from tests.fixtures import kdbx4_bytes


class FakeRclone:
    def __init__(self, root):
        self.root = root
        self.online = True
        self.hashes = True
        # Google Drive lists only MD5 before rclone 1.61.
        self.md5_only = False
        # "dropbox": Dropbox's computable hash only; "opaque": a hash
        # kp-sync cannot compute (like OneDrive's quickxor).
        self.only_hash = None
        self.calls = []

    def path(self, value):
        if value.startswith("fake:"):
            return self.root / value.partition(":")[2].lstrip("/")
        return Path(value)

    def listed_hash(self, item):
        data = item.read_bytes()
        if self.only_hash == "dropbox":
            return {"dropbox": new_hash("dropbox", data).hexdigest()}
        return {"quickxor": hashlib.sha1(data[::-1]).hexdigest()}

    def __call__(self, argv, **kwargs):
        self.calls.append(tuple(argv))
        args = list(argv[1:])
        if "--config" in args:
            index = args.index("--config")
            del args[index : index + 2]
        command = args.pop(0)
        options = {}
        paths = []
        while args:
            arg = args.pop(0)
            if arg in {"--max-depth", "--include", "--files-from-raw"}:
                options.setdefault(arg, []).append(args.pop(0))
            elif arg.startswith("-"):
                options[arg] = True
            else:
                paths.append(arg)
        status, output = 0, ""
        if not self.online:
            status = 5
        elif command == "lsjson":
            path = self.path(paths[0])
            if not path.exists():
                status = 4
            else:
                if "-R" in options:
                    patterns = options.get("--include", [])
                    objects = [
                        item
                        for item in sorted(path.rglob("*"))
                        if item.is_file()
                        and any(
                            fnmatch.fnmatchcase(
                                "/" + item.relative_to(path).as_posix(),
                                pattern,
                            )
                            for pattern in patterns
                        )
                    ]
                else:
                    objects = list(path.iterdir()) if path.is_dir() else [path]
                output = json.dumps(
                    [
                        {
                            "Name": item.name,
                            "Path": (
                                item.relative_to(path).as_posix()
                                if "-R" in options
                                else item.name
                            ),
                            "Size": (
                                item.stat().st_size if item.is_file() else -1
                            ),
                            "IsDir": item.is_dir(),
                            "ModTime": str(item.stat().st_mtime_ns),
                            "Hashes": (
                                {}
                                if not self.hashes
                                else (
                                    (
                                        self.listed_hash(item)
                                        if self.only_hash and item.is_file()
                                        else {}
                                    )
                                    if not self.hashes or self.only_hash
                                    else (
                                        {"SHA-256": inspect_kdbx(item).sha256}
                                        if item.is_file()
                                        and item.suffix == ".kdbx"
                                        and not self.md5_only
                                        else (
                                            {
                                                "MD5": hashlib.md5(
                                                    item.read_bytes()
                                                ).hexdigest()
                                            }
                                            if item.is_file()
                                            else {}
                                        )
                                    )
                                )
                            ),
                        }
                        for item in objects
                    ]
                )
        elif command == "copyto":
            source, destination = map(self.path, paths)
            if not source.exists():
                status = 4
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                destination.chmod(0o600)
        elif command in {"copy", "delete"}:
            (listing,) = options["--files-from-raw"]
            names = Path(listing).read_text().splitlines()
            assert names and all("/" not in name for name in names), names
            source = self.path(paths[0])
            if command == "delete":
                assert options["--max-depth"] == ["1"], options
                if not source.is_dir():
                    status = 3
                for name in names if source.is_dir() else ():
                    (source / name).unlink(missing_ok=True)
            else:
                assert "--no-traverse" in options, options
                destination = self.path(paths[1])
                for name in names:
                    if (source / name).is_file():
                        destination.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(source / name, destination / name)
        elif command == "deletefile":
            self.path(paths[0]).unlink(missing_ok=True)
        elif command == "rmdir":
            self.path(paths[0]).rmdir()
        else:
            raise AssertionError(f"unexpected fake rclone command: {argv}")
        if status and kwargs.get("check"):
            raise subprocess.CalledProcessError(status, argv, output=output)
        return subprocess.CompletedProcess(
            argv, status, stdout=output, stderr=""
        )


class Credentials:
    def __init__(self, key_file):
        self.path = key_file

    def cached_password(self):
        return "test-password"

    def password(self, ui):
        return "test-password"

    def key_file(self, ui):
        return self.path

    def cache_password(self, password):
        pass


class CloudSyncTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote"
        self.remote.mkdir()
        self.runner = FakeRclone(self.remote)
        self.key = self.root / "fixture.keyx"
        self.key.write_bytes(b"synthetic shared key")
        self.cloud_db = self.remote / "pass.kdbx"
        create_database(
            self.cloud_db, password="test-password", keyfile=self.key
        )
        database = PyKeePass(
            self.cloud_db, password="test-password", keyfile=self.key
        )
        database.add_entry(database.root_group, "Example", "user", "secret")
        database.save()
        self.initial = self.cloud_db.read_bytes()
        self.initial_sha = inspect_kdbx(self.cloud_db).sha256
        state = HistoryState()
        state.commit(
            self.initial_sha,
            (),
            "seed",
            ("cloud:fake",),
            size=len(self.initial),
        )
        (self.remote / "kp-sync" / "bases").mkdir(parents=True)
        shutil.copyfile(
            self.cloud_db,
            self.remote / "kp-sync" / "bases" / f"{self.initial_sha}.kdbx",
        )
        state.save(self.remote / "kp-sync" / "state.json")
        self.mount = self.root / "drive"
        self.mount.mkdir()
        (self.mount / "pass.kdbx").write_bytes(self.initial)

    def engine(self, host, *, drive=False, ui=None, extra=()):
        home = self.root / host
        home.mkdir(exist_ok=True)
        runtime = home / "runtime"
        runtime.mkdir(exist_ok=True)
        config = load_config(
            environ={"HOME": str(home), "XDG_RUNTIME_DIR": str(runtime)},
            hostname=host,
        )
        config = replace(
            config,
            hub=runtime / "pass.kdbx",
            key_file=self.key,
            ui=config.ui | {"ask_first": False},
        )
        cloud = RcloneEndpoint("cloud", "fake", runner=self.runner)
        endpoints = [cloud, *extra]
        stores = [CloudStore(cloud, "kp-sync")]
        if drive:
            pendrive = DriveEndpoint(
                "pendrive",
                self.mount,
                "pass.kdbx",
                "serial:uuid",
                identity_resolver=lambda mount: "serial:uuid",
            )
            endpoints.append(pendrive)
            stores.append(DriveStore(pendrive, "kp-sync"))
        run = runtime / "kp-sync"
        run.mkdir(exist_ok=True, mode=0o700)
        return SyncEngine(
            config,
            endpoints,
            run_directory=run,
            credentials=Credentials(self.key),
            ui=ui or NoneUI(),
            baseline_stores=stores,
        )

    def edit(self, path, **fields):
        database = PyKeePass(path, password="test-password", keyfile=self.key)
        for name, value in fields.items():
            setattr(database.entries[0], name, value)
        database.entries[0].mtime = datetime.now(timezone.utc)
        database.save()

    def test_two_hosts_recognize_stale_hub_without_password(
        self,
    ):
        left, right = self.engine("left"), self.engine("right")
        self.assertEqual(left.sync().action, "committed")
        result = right.sync()
        self.assertEqual(result.action, "committed", result.pending)
        self.edit(left.config.hub, username="left-user")
        self.assertEqual(left.sync().action, "committed")
        with patch.object(
            right,
            "_request_credentials",
            side_effect=AssertionError("password asked"),
        ):
            result = right.sync()
        self.assertEqual(result.action, "committed")
        self.assertEqual(
            right.config.hub.read_bytes(), self.cloud_db.read_bytes()
        )
        shared = HistoryState.load(self.remote / "kp-sync" / "state.json")
        self.assertEqual(
            shared.copies["hub:left"].version,
            shared.copies["hub:right"].version,
        )

    def test_offline_drive_branch_reconciles_and_merges_on_both_hosts(self):
        left, right = self.engine("left"), self.engine("right", drive=True)
        left.sync()
        right.sync()
        self.edit(left.config.hub, username="left-user")
        left.sync()
        cloud_branch = inspect_kdbx(self.cloud_db).sha256
        self.runner.online = False
        self.edit(right.config.hub, url="https://right.example")
        self.assertEqual(right.sync().action, "committed")
        drive_branch = inspect_kdbx(self.mount / "pass.kdbx").sha256
        self.assertNotEqual(cloud_branch, drive_branch)
        self.assertTrue((self.mount / "kp-sync" / "state.json").exists())
        self.runner.online = True
        left_with_drive = self.engine("left", drive=True)
        result = left_with_drive.sync()
        self.assertEqual(result.action, "committed", result.pending)
        database = PyKeePass(
            self.cloud_db, password="test-password", keyfile=self.key
        )
        self.assertEqual(database.entries[0].username, "left-user")
        self.assertEqual(database.entries[0].url, "https://right.example")
        state = HistoryState.load(self.remote / "kp-sync" / "state.json")
        merged_sha = inspect_kdbx(self.cloud_db).sha256
        self.assertEqual(state.versions[merged_sha].generation, 3)
        # The offline hub pins its branch; unused parents may be pruned.
        self.assertIn(drive_branch, state.versions[merged_sha].parents)
        self.assertEqual(
            HistoryState.load(self.mount / "kp-sync/state.json").to_dict(),
            state.to_dict(),
        )
        self.assertEqual(
            {path.stem for path in (self.mount / "kp-sync/bases").iterdir()},
            set(state.versions),
        )
        self.assertEqual(right.sync().action, "committed")
        self.assertEqual(
            right.config.hub.read_bytes(), self.cloud_db.read_bytes()
        )
        self.assertEqual(
            left.config.hub.read_bytes(), self.cloud_db.read_bytes()
        )

    def test_dry_run_fetches_and_classifies_without_publishing_or_prompting(
        self,
    ):
        engine = self.engine("left", ui=FakeUI())
        before = (self.remote / "kp-sync" / "state.json").read_bytes()
        result = engine.sync(dry_run=True)
        self.assertIn("promote", result.action)
        self.assertFalse(engine.config.hub.exists())
        self.assertFalse((engine.run_directory / "state.json").exists())
        self.assertEqual(list((engine.run_directory / "bases").iterdir()), [])
        self.assertEqual(
            (self.remote / "kp-sync" / "state.json").read_bytes(), before
        )
        self.assertFalse(engine.ui.events)
        self.assertFalse(
            any(
                argv[1] in {"copyto", "deletefile"}
                and argv[-1].startswith("fake:")
                for argv in self.runner.calls
            )
        )

    def test_single_unknown_cloud_copy_without_history_stays_pending(self):
        (self.remote / "kp-sync" / "state.json").unlink()
        engine = self.engine("left")
        self.assertEqual(engine.sync().action, "pending")
        self.assertFalse(engine.config.hub.exists())
        self.assertEqual(self.cloud_db.read_bytes(), self.initial)

    def test_known_cloud_probe_uses_cached_base_and_status_never_calls_rclone(
        self,
    ):
        engine = self.engine("left")
        engine.sync()
        self.runner.calls.clear()
        engine.sync(dry_run=True)
        self.assertFalse(
            any(
                argv[1] == "copyto" and argv[2] == "fake:pass.kdbx"
                for argv in self.runner.calls
            )
        )
        self.runner.calls.clear()
        status = engine.status()
        self.assertFalse(self.runner.calls)
        self.assertEqual(status.action, "unchanged")
        self.assertIn("cloud:fake", status.copies)
        self.assertEqual(status.copies["cloud:fake"]["versions_behind"], 0)

    def test_unchanged_sync_has_one_cloud_request_and_no_publication(self):
        engine = self.engine("left")
        engine.sync()
        engine = self.engine("left")
        before = (self.remote / "kp-sync/state.json").read_bytes()
        self.runner.calls.clear()
        result = engine.sync()
        self.assertEqual(result.action, "unchanged")
        self.assertEqual(
            (self.remote / "kp-sync/state.json").read_bytes(), before
        )
        # One listing covers DB/history; state.json comes from
        # the checksum-validated runtime cache of a new engine.
        self.assertEqual(len(self.runner.calls), 1, self.runner.calls)
        self.assertEqual(self.runner.calls[0][1], "lsjson")

    def test_listed_md5_alone_identifies_a_known_cloud_copy(self):
        self.runner.md5_only = True
        self.engine("left").sync()
        engine = self.engine("left")
        self.runner.calls.clear()
        self.assertEqual(engine.sync().action, "unchanged")
        self.assertEqual(
            [call[1] for call in self.runner.calls],
            ["lsjson"],
            self.runner.calls,
        )

    _legacy_token = False

    def test_stored_hashless_token_from_older_versions_is_not_trusted(self):
        self._legacy_token = True
        self.test_hashless_rewrite_keeping_size_and_mtime_is_downloaded()

    def cloud_downloads(self):
        return [
            call
            for call in self.runner.calls
            if call[1] == "copyto" and call[-2] == "fake:pass.kdbx"
        ]

    def test_dropbox_hash_alone_identifies_copies_and_cached_state(self):
        self.runner.only_hash = "dropbox"
        self.engine("left").sync()
        engine = self.engine("left")
        self.runner.calls.clear()
        self.assertEqual(engine.sync().action, "unchanged")
        # One listing: no database or state.json download.
        self.assertEqual(
            [call[1] for call in self.runner.calls],
            ["lsjson"],
            self.runner.calls,
        )

    def test_opaque_hash_skips_unchanged_downloads_and_rechecks(self):
        self.runner.only_hash = "opaque"
        self.engine("left").sync()
        engine = self.engine("left")
        self.runner.calls.clear()
        self.assertEqual(engine.sync().action, "unchanged")
        self.assertEqual(self.cloud_downloads(), [], self.runner.calls)
        self.edit(engine.config.hub, username="hub-edit")
        engine = self.engine("left")
        self.runner.calls.clear()
        self.assertEqual(engine.sync().action, "committed")
        # Pre-write rechecks compare tokens; only the post-write check,
        # which must prove the uploaded bytes, downloads.
        self.assertEqual(len(self.cloud_downloads()), 1, self.runner.calls)
        self.assertEqual(
            self.cloud_db.read_bytes(), engine.config.hub.read_bytes()
        )

    def test_opaque_hash_change_is_detected(self):
        self.runner.only_hash = "opaque"
        self.engine("left").sync()
        self.edit(self.cloud_db, username="phone")
        engine = self.engine("left")
        self.assertEqual(engine.sync().action, "committed")
        database = PyKeePass(
            engine.config.hub, password="test-password", keyfile=self.key
        )
        self.assertEqual(database.entries[0].username, "phone")

    def test_opaque_hash_change_before_a_write_restarts_the_run(self):
        self.runner.only_hash = "opaque"
        self.engine("left").sync()
        self.edit(self.engine("left").config.hub, url="https://hub.test")
        engine = self.engine("left")
        runner = self.runner
        changed = []
        uploads_before_change = []

        def racing(argv, **kwargs):
            if argv[1] == "copyto" and argv[-1] == "fake:pass.kdbx":
                uploads_before_change.append(not changed)
            result = runner(argv, **kwargs)
            if (
                not changed
                and argv[1] == "copyto"
                and argv[2] == "fake:kp-sync/state.json"
            ):
                # After observation, before the pre-write checks.
                changed.append(True)
                self.edit(self.cloud_db, username="phone")
            return result

        for endpoint in engine.endpoints:
            if isinstance(endpoint, RcloneEndpoint):
                endpoint._runner = racing
        # Fresh listings for pre-write checks: only the token comparison
        # can then notice the change before the upload.
        with patch("kp_sync.engine.RECHECK_WINDOW", 0):
            result = engine.sync()
        self.assertTrue(changed)
        self.assertNotIn(True, uploads_before_change)
        self.assertGreaterEqual(result.restarted, 1)
        database = PyKeePass(
            self.cloud_db, password="test-password", keyfile=self.key
        )
        self.assertEqual(database.entries[0].username, "phone")
        self.assertEqual(database.entries[0].url, "https://hub.test")

    def test_hashless_rewrite_keeping_size_and_mtime_is_downloaded(self):
        # Size and mtime survive a same-size rewrite on some backends.
        self.runner.hashes = False
        fake = self.runner

        def frozen(argv, **kwargs):
            result = fake(argv, **kwargs)
            if argv[1] == "lsjson" and result.stdout:
                items = json.loads(result.stdout)
                for item in items:
                    if item.get("Path") == "pass.kdbx":
                        item["Size"], item["ModTime"] = 4096, "frozen"
                result.stdout = json.dumps(items)
            return result

        self.runner = frozen
        self.assertEqual(self.engine("left").sync().action, "committed")
        shared = HistoryState.load(self.remote / "kp-sync" / "state.json")
        self.assertEqual(shared.copies["cloud:fake"].remote_hashes, {})
        if self._legacy_token:
            # Earlier versions stored size/mtime tokens unconditionally.
            token = RcloneEndpoint("cloud", "fake", runner=frozen).probe()
            run = self.root / "left/runtime/kp-sync"
            for path in (
                self.remote / "kp-sync/state.json",
                run / "state.json",
                *(run / "history-cache").glob("*.json"),
            ):
                state = HistoryState.load(path)
                state.copies["cloud:fake"] = replace(
                    state.copies["cloud:fake"],
                    remote_hashes={"probe": token.token},
                )
                state.save(path)
        self.edit(self.cloud_db, username="phone")
        engine = self.engine("left")
        self.assertEqual(engine.sync().action, "committed")
        database = PyKeePass(
            engine.config.hub, password="test-password", keyfile=self.key
        )
        self.assertEqual(database.entries[0].username, "phone")

    def command_endpoint(self, *, content_hash, name="command"):
        copy = self.root / f"{name}.kdbx"
        if not copy.exists():
            shutil.copyfile(self.cloud_db, copy)
        log = self.root / f"{name}-fetches.log"
        fetch = (
            sys.executable,
            "-c",
            "import shutil,sys; shutil.copyfile(sys.argv[1], sys.argv[2]);"
            "open(sys.argv[3], 'a').write('fetch\\n')",
            str(copy),
            "{file}",
            str(log),
        )
        store = (
            sys.executable,
            "-c",
            "import shutil,sys; shutil.copyfile(sys.argv[1], sys.argv[2])",
            "{file}",
            str(copy),
        )
        # A deliberately constant token: only the flag decides trust.
        probe = (sys.executable, "-c", "print('constant')")
        endpoint = CommandEndpoint(
            name,
            fetch,
            store,
            probe=probe,
            probe_is_content_hash=content_hash,
        )
        return endpoint, copy, log

    def check_command_fetches(self, *, content_hash):
        endpoint, copy, log = self.command_endpoint(content_hash=content_hash)
        self.assertEqual(
            self.engine("left", extra=(endpoint,)).sync().action, "committed"
        )
        log.unlink()
        engine = self.engine("left", extra=(endpoint,))
        self.assertEqual(engine.sync().action, "unchanged")
        return log.exists()

    def test_undeclared_command_probe_token_downloads_every_run(self):
        self.assertTrue(self.check_command_fetches(content_hash=False))

    def test_declared_content_hash_probe_skips_the_download(self):
        self.assertFalse(self.check_command_fetches(content_hash=True))

    def test_undeclared_command_probe_does_not_hide_an_edit(self):
        endpoint, copy, log = self.command_endpoint(content_hash=False)
        self.assertEqual(
            self.engine("left", extra=(endpoint,)).sync().action, "committed"
        )
        self.edit(copy, username="command-edit")
        engine = self.engine("left", extra=(endpoint,))
        self.assertEqual(engine.sync().action, "committed")
        database = PyKeePass(
            self.cloud_db, password="test-password", keyfile=self.key
        )
        self.assertEqual(database.entries[0].username, "command-edit")

    def test_hub_edit_uploads_without_downloading_cloud_copies_again(self):
        engine = self.engine("left")
        engine.sync()
        self.edit(engine.config.hub, username="edited")
        engine = self.engine("left")
        self.runner.calls.clear()
        result = engine.sync()
        self.assertEqual(result.action, "committed")
        self.assertEqual(
            self.cloud_db.read_bytes(), engine.config.hub.read_bytes()
        )
        downloads = [
            call
            for call in self.runner.calls
            if call[1] == "copyto" and call[-2].startswith("fake:")
        ]
        # Re-checks and candidate verification compare listed hashes.
        self.assertEqual(downloads, [], self.runner.calls)
        self.assertLessEqual(len(self.runner.calls), 9, self.runner.calls)

    def test_hashless_remote_save_after_upload_is_downloaded_not_trusted(self):
        self.runner.hashes = False
        engine = self.engine("left")
        engine.sync()
        self.edit(engine.config.hub, url="https://hub.example.test")
        engine = self.engine("left")
        runner = self.runner
        changed = []

        def racing(argv, **kwargs):
            result = runner(argv, **kwargs)
            if (
                not changed
                and argv[1] == "copyto"
                and argv[-1] == "fake:pass.kdbx"
            ):
                changed.append(True)
                self.edit(self.cloud_db, username="phone")
            return result

        for endpoint in engine.endpoints:
            if isinstance(endpoint, RcloneEndpoint):
                endpoint._runner = racing
        result = engine.sync()
        self.assertTrue(changed)
        self.assertGreaterEqual(result.restarted, 1)
        database = PyKeePass(
            engine.config.hub, password="test-password", keyfile=self.key
        )
        self.assertEqual(database.entries[0].username, "phone")

    def test_tampered_history_cache_is_not_trusted(self):
        engine = self.engine("left")
        engine.sync()
        caches = list(
            (self.root / "left/runtime/kp-sync/history-cache").glob("*.json")
        )
        self.assertEqual(len(caches), 1)
        caches[0].write_text('{"versions": {}, "copies": {}}')
        engine = self.engine("left")
        self.runner.calls.clear()
        self.assertEqual(engine.sync().action, "unchanged")
        self.assertIn(
            ("rclone", "copyto"),
            [
                call[:2]
                for call in self.runner.calls
                if call[-2] == "fake:kp-sync/state.json"
            ],
        )
        cached = HistoryState.load(caches[0])
        self.assertEqual(
            cached.to_dict(),
            HistoryState.load(self.remote / "kp-sync/state.json").to_dict(),
        )

    def test_cloud_change_between_listing_and_download_restarts_the_run(self):
        engine = self.engine("left")
        engine.sync()
        engine = self.engine("left")
        runner = self.runner
        changed = []

        def racing(argv, **kwargs):
            if (
                not changed
                and argv[1] == "copyto"
                and argv[-2] == "fake:pass.kdbx"
            ):
                changed.append(True)
                self.edit(self.cloud_db, username="phone")
            return runner(argv, **kwargs)

        self.edit(self.cloud_db, username="first")
        for endpoint in engine.endpoints:
            if isinstance(endpoint, RcloneEndpoint):
                endpoint._runner = racing
        result = engine.sync()
        self.assertTrue(changed)
        self.assertEqual(result.action, "committed", result.pending)
        # Downloaded bytes contradict the listed checksum: re-observe.
        self.assertEqual(result.restarted, 1)
        self.assertEqual(
            engine.config.hub.read_bytes(), self.cloud_db.read_bytes()
        )
        database = PyKeePass(
            engine.config.hub, password="test-password", keyfile=self.key
        )
        self.assertEqual(database.entries[0].username, "phone")

    def test_unchanged_sync_refreshes_old_records_not_every_poll(
        self,
    ):
        engine = self.engine("left")
        engine.sync()
        state_path = self.remote / "kp-sync/state.json"
        state = HistoryState.load(state_path)
        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        state.copies = {
            identity: replace(record, synced_at=old)
            for identity, record in state.copies.items()
        }
        state.save(state_path)
        state.save(engine.run_directory / "state.json")
        generation = state.generation
        self.assertEqual(engine.sync().action, "unchanged")
        refreshed = HistoryState.load(state_path)
        self.assertEqual(refreshed.generation, generation + 1)
        self.assertTrue(
            all(record.synced_at > old for record in refreshed.copies.values())
        )
        before = state_path.read_bytes()
        engine.sync()
        self.assertEqual(state_path.read_bytes(), before)

    def test_unchanged_database_publishes_new_copy_record(
        self,
    ):
        engine = self.engine("left")
        engine.config.hub.write_bytes(self.initial)
        self.assertEqual(engine.sync().action, "unchanged")
        shared = HistoryState.load(self.remote / "kp-sync/state.json")
        self.assertIn("hub:left", shared.copies)

    def test_session_first_edit_has_known_ancestry_without_cloud_presync(self):
        from kp_sync.endpoints.session import SessionEndpoint
        from kp_sync.sessions.session import SessionManager, stage_session
        from tests.test_sessions import record

        engine = self.engine("left")
        engine.sync()
        manager = SessionManager(engine.config, engine.run_directory)
        rec = record(mode="rw")
        self.runner.calls.clear()
        staged = stage_session(manager, rec.id, record=rec)
        manager.store.save(rec)
        self.assertEqual(self.runner.calls, [])
        local = HistoryState.load(engine.run_directory / "state.json")
        self.assertEqual(local.copies[rec.identity].version, self.initial_sha)
        self.edit(staged / engine.config.db_name, username="remote-user")
        edited = SyncEngine(
            engine.config,
            (
                *engine.endpoints[1:],
                SessionEndpoint(rec, manager.store, engine.config.db_name),
            ),
            run_directory=engine.run_directory,
            credentials=Credentials(self.key),
            ui=NoneUI(),
            baseline_stores=engine.baseline_stores,
        )
        result = edited.sync()
        self.assertEqual(result.action, "committed", result.pending)
        database = PyKeePass(
            engine.config.hub, password="test-password", keyfile=self.key
        )
        self.assertEqual(database.entries[0].username, "remote-user")

    def test_unknown_hub_snapshot_does_not_fabricate_session_ancestry(self):
        from kp_sync.sessions.session import SessionManager, stage_session
        from tests.test_sessions import record

        engine = self.engine("left")
        engine.sync()
        self.edit(engine.config.hub, username="uncommitted-user")
        manager = SessionManager(engine.config, engine.run_directory)
        rec = record(mode="rw")
        staged = stage_session(manager, rec.id, record=rec)
        state = HistoryState.load(engine.run_directory / "state.json")
        self.assertNotIn(rec.identity, state.copies)
        self.assertEqual(
            (staged / engine.config.db_name).read_bytes(),
            engine.config.hub.read_bytes(),
        )
        self.assertNotIn(
            inspect_kdbx(staged / engine.config.db_name).sha256, state.versions
        )

    def test_unchanged_copy_refresh_threshold_respects_retention(self):
        engine = self.engine("left")
        engine.sync()
        state_path = self.remote / "kp-sync/state.json"
        original = HistoryState.load(state_path)
        now = datetime.now(timezone.utc)
        for keep_days, age, refresh in (
            (180, 86399, False),
            (180, 86400, True),
            (1, 43199, False),
            (1, 43200, True),
            (0, 0, True),
        ):
            with self.subTest(keep_days=keep_days, age=age):
                state = HistoryState.from_dict(original.to_dict())
                # Older timestamps without an offset are UTC.
                observed = (
                    (now - timedelta(seconds=age))
                    .replace(tzinfo=None)
                    .isoformat()
                )
                state.copies = {
                    key: replace(record, synced_at=observed)
                    for key, record in state.copies.items()
                }
                state.save(state_path)
                state.save(engine.run_directory / "state.json")
                engine.config.baseline["keep_days"] = keep_days
                with patch("kp_sync.engine.datetime") as clock:
                    clock.now.return_value = now
                    engine.sync()
                self.assertEqual(
                    HistoryState.load(state_path).generation,
                    state.generation + int(refresh),
                )

    def test_shared_state_race_restarts_before_any_database_write(self):
        engine = self.engine("left")
        engine.sync()
        self.edit(engine.config.hub, username="left-user")
        original_cloud = self.cloud_db.read_bytes()
        check = BaselineManager.check
        raced = []
        test = self

        def concurrent_commit(manager, temporary, **options):
            if not raced:
                test.assertEqual(test.cloud_db.read_bytes(), original_cloud)
                state = HistoryState.load(
                    test.remote / "kp-sync" / "state.json"
                )
                state.record_copies(
                    test.initial_sha, ("hub:concurrent",), "concurrent"
                )
                state.save(test.remote / "kp-sync" / "state.json")
                raced.append(True)
            return check(manager, temporary, **options)

        # The race happens after the reusable listing window expires.
        with (
            patch.object(BaselineManager, "check", concurrent_commit),
            patch("kp_sync.engine.RECHECK_WINDOW", 0),
        ):
            result = engine.sync()
        self.assertEqual(result.restarted, 1)
        self.assertEqual(result.action, "committed")
        state = HistoryState.load(self.remote / "kp-sync" / "state.json")
        self.assertIn("hub:concurrent", state.copies)
        self.assertEqual(
            engine.status().copies["hub:concurrent"]["versions_behind"],
            1,
        )

    def test_lost_cloud_state_race_is_detected_as_changed_bytes_on_next_run(
        self,
    ):
        left, right = self.engine("left"), self.engine("right")
        left.sync()
        right.sync()
        old_state = HistoryState.load(self.remote / "kp-sync" / "state.json")
        self.edit(left.config.hub, username="left-user")
        left.sync()
        self.edit(right.config.hub, url="https://right.example")
        # Simulate rclone's check/write window: old state wins the
        # upload race while the cloud database contains the other edit.
        old_state.save(self.remote / "kp-sync" / "state.json")
        result = right.sync()
        self.assertEqual(result.action, "committed", result.pending)
        merged = PyKeePass(
            self.cloud_db, password="test-password", keyfile=self.key
        )
        self.assertEqual(merged.entries[0].username, "left-user")
        self.assertEqual(merged.entries[0].url, "https://right.example")

    def test_fallback_drives_update_without_resurrection(
        self,
    ):
        engine = self.engine("left", drive=True)
        engine.sync()
        second_mount = self.root / "second-drive"
        second_mount.mkdir()
        (second_mount / "pass.kdbx").write_bytes(self.initial)
        second = DriveEndpoint(
            "pendrive",
            second_mount,
            "pass.kdbx",
            "second:uuid",
            identity_resolver=lambda mount: "second:uuid",
        )
        endpoint = engine.endpoints[2]
        self.runner.online = False
        updated = SyncEngine(
            engine.config,
            (*engine.endpoints[1:], second),
            run_directory=engine.run_directory,
            credentials=Credentials(self.key),
            ui=NoneUI(),
            baseline_stores=(
                engine.baseline_stores[0],
                DriveStore(endpoint, "kp-sync"),
                DriveStore(second, "kp-sync"),
            ),
        )
        self.edit(engine.config.hub, username="newer-user")
        self.assertEqual(updated.sync().action, "committed")
        self.assertEqual(
            (second_mount / "pass.kdbx").read_bytes(),
            engine.config.hub.read_bytes(),
        )
        for mount in (self.mount, second_mount):
            self.assertTrue((mount / "kp-sync" / "state.json").exists())
        self.runner.online = True
        self.assertEqual(updated.sync().action, "committed")
        shared = HistoryState.load(self.remote / "kp-sync/state.json")
        for mount in (self.mount, second_mount):
            self.assertEqual(
                HistoryState.load(mount / "kp-sync/state.json").to_dict(),
                shared.to_dict(),
            )
            self.assertEqual(
                {path.stem for path in (mount / "kp-sync/bases").iterdir()},
                set(shared.versions),
            )


class BatchedBaseTransferTest(unittest.TestCase):
    """Base uploads, downloads and deletes take one rclone call each."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote"
        self.remote.mkdir()
        self.runner = FakeRclone(self.remote)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.work = self.runtime / "tmp"
        self.work.mkdir(mode=0o700)
        self.state = HistoryState()
        self.files = {}
        parent = None
        for index in range(4):
            data = kdbx4_bytes(b"version %d " % index * 20)
            path = self.root / f"input-{index}.kdbx"
            path.write_bytes(data)
            sha = inspect_kdbx(path).sha256
            self.state.commit(
                sha,
                (parent,) if parent else (),
                "test",
                ("hub:test",),
                size=len(data),
            )
            self.files[sha] = path
            parent = sha
        self.bases = self.remote / "kp-sync" / "bases"

    def manager(self, *stores):
        endpoint = RcloneEndpoint("cloud", "fake", runner=self.runner)
        cloud = CloudStore(endpoint, "kp-sync")
        return BaselineManager(self.runtime, (cloud, *stores))

    def cache(self, shas):
        directory = self.runtime / "bases"
        directory.mkdir(mode=0o700, exist_ok=True)
        for sha in shas:
            shutil.copyfile(self.files[sha], directory / f"{sha}.kdbx")

    def commands(self):
        return [call[1] for call in self.runner.calls]

    def test_missing_bases_are_uploaded_in_one_call_before_state(self):
        self.cache(self.files)
        manager = self.manager()
        manager.load(self.work)
        self.runner.calls.clear()
        manager.save(self.state, self.work)
        commands = self.commands()
        self.assertEqual(commands.count("copy"), 1, self.runner.calls)
        upload = self.runner.calls[commands.index("copy")]
        self.assertEqual(upload[-1], "fake:kp-sync/bases")
        state_write = next(
            index
            for index, call in enumerate(self.runner.calls)
            if call[1] == "copyto" and call[-1] == "fake:kp-sync/state.json"
        )
        self.assertLess(commands.index("copy"), state_write)
        self.assertNotIn(
            "fake:kp-sync/bases",
            {
                PurePosixPath(call[-1]).parent.as_posix()
                for call in self.runner.calls
                if call[1] == "copyto"
            },
        )
        self.assertEqual(
            {path.stem for path in self.bases.iterdir()}, set(self.files)
        )
        for sha, path in self.files.items():
            self.assertEqual(
                (self.bases / f"{sha}.kdbx").read_bytes(), path.read_bytes()
            )

    def test_failed_batch_upload_publishes_no_state(self):
        self.cache(self.files)
        manager = self.manager()
        manager.load(self.work)
        runner = self.runner

        def failing(argv, **kwargs):
            if argv[1] == "copy":
                raise subprocess.CalledProcessError(5, argv)
            return runner(argv, **kwargs)

        for store in manager.stores:
            store.endpoint._runner = failing
        with self.assertRaises(EndpointUnavailable):
            manager.save(self.state, self.work)
        self.assertFalse((self.remote / "kp-sync" / "state.json").exists())

    def test_stale_bases_are_deleted_in_one_call_after_state(self):
        self.cache(self.files)
        self.bases.mkdir(parents=True)
        stale = []
        for index in range(BaselineManager.PRUNE_BATCH):
            data = kdbx4_bytes(b"stale %d " % index * 20)
            path = self.root / "stale.kdbx"
            path.write_bytes(data)
            sha = inspect_kdbx(path).sha256
            shutil.copyfile(path, self.bases / f"{sha}.kdbx")
            stale.append(sha)
        (self.bases / "notes.txt").write_text("unrelated")
        manager = self.manager()
        manager.load(self.work)
        self.runner.calls.clear()
        manager.save(self.state, self.work)
        commands = self.commands()
        self.assertEqual(commands.count("delete"), 1, self.runner.calls)
        self.assertNotIn("deletefile", commands)
        state_write = max(
            index
            for index, call in enumerate(self.runner.calls)
            if call[1] == "copyto" and call[-1] == "fake:kp-sync/state.json"
        )
        self.assertGreater(commands.index("delete"), state_write)
        delete = self.runner.calls[commands.index("delete")]
        listing = delete[delete.index("--files-from-raw") + 1]
        self.assertFalse(Path(listing).exists())
        remaining = {path.name for path in self.bases.iterdir()}
        self.assertEqual(
            remaining, {f"{sha}.kdbx" for sha in self.files} | {"notes.txt"}
        )

    def test_drive_bases_are_downloaded_in_one_call_and_verified(self):
        self.bases.mkdir(parents=True)
        for sha, path in self.files.items():
            shutil.copyfile(path, self.bases / f"{sha}.kdbx")
        self.state.save(self.remote / "kp-sync" / "state.json")
        mount = self.root / "mount"
        mount.mkdir()
        drive = DriveStore(
            DriveEndpoint(
                "pendrive",
                mount,
                "pass.kdbx",
                "serial:uuid",
                identity_resolver=lambda mount: "serial:uuid",
            ),
            "kp-sync",
        )
        manager = self.manager(drive)
        manager.load(self.work)
        self.runner.calls.clear()
        manager.save(self.state, self.work)
        downloads = [
            call for call in self.runner.calls if call[1] in {"copy", "copyto"}
        ]
        self.assertEqual(len(downloads), 1, self.runner.calls)
        self.assertEqual(downloads[0][1], "copy")
        self.assertEqual(downloads[0][-2], "fake:kp-sync/bases")
        self.assertEqual(
            {path.stem for path in (mount / "kp-sync/bases").iterdir()},
            set(self.files),
        )
        self.assertEqual(
            {path.stem for path in (self.runtime / "bases").iterdir()},
            set(self.files),
        )

    def test_batch_downloaded_base_with_wrong_content_is_rejected(self):
        self.bases.mkdir(parents=True)
        shas = sorted(self.files)
        for sha in shas:
            shutil.copyfile(self.files[sha], self.bases / f"{sha}.kdbx")
        # A wrong file under a valid name must never enter the cache.
        shutil.copyfile(self.files[shas[1]], self.bases / f"{shas[0]}.kdbx")
        manager = self.manager()
        manager.load(self.work)
        with self.assertRaises(StateError):
            manager.prefetch_bases(shas, self.work)
        self.assertFalse((self.runtime / "bases" / f"{shas[0]}.kdbx").exists())

    def test_unavailable_batch_download_falls_back_to_other_stores(self):
        self.bases.mkdir(parents=True)
        manager = self.manager()
        manager.load(self.work)
        self.runner.online = False
        manager.prefetch_bases(sorted(self.files), self.work)
        self.assertFalse((self.runtime / "bases").exists())
