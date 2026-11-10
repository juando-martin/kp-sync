# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
import hashlib
import json
import shutil
import subprocess
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from kp_sync.endpoints.base import EndpointError, EndpointUnavailable
from kp_sync.endpoints.rclone import EndpointMissing, RcloneEndpoint


class FakeRclone:
    def __init__(self):
        self.calls = []
        self.objects = [
            {
                "Path": "pass.kdbx",
                "Name": "pass.kdbx",
                "Size": 7,
                "ModTime": "2026-10-23T10:00:00Z",
                "Hashes": {"md5": "abc"},
                "IsDir": False,
            }
        ]
        self.failure = None
        self.payload = b"payload"

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.failure is not None:
            raise self.failure
        if "lsjson" in argv or argv[0] == "inspect":
            return subprocess.CompletedProcess(
                argv, 0, json.dumps(self.objects), ""
            )
        if "copyto" in argv or argv[0] == "download":
            if argv[0] == "download":
                target = argv[2]
            else:
                target = argv[3]
            if ":" not in target:
                self.download_mode = Path(target).stat().st_mode & 0o777
                Path(target).write_bytes(self.payload)
        return subprocess.CompletedProcess(argv, 0, "", "")


class RcloneEndpointTest(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / f".test-rclone-{uuid.uuid4().hex}"
        self.root.mkdir(mode=0o700)
        self.addCleanup(shutil.rmtree, self.root)
        self.runner = FakeRclone()
        self.endpoint = RcloneEndpoint(
            "cloud", "GoogleDrive", runner=self.runner
        )

    def test_remote_roots_identity_and_relative_paths(self):
        self.assertEqual(self.endpoint.identity, "cloud:GoogleDrive")
        self.assertEqual(
            self.endpoint.database_remote, "GoogleDrive:pass.kdbx"
        )
        self.assertTrue(self.endpoint.foreign_writers)
        endpoint = RcloneEndpoint(
            "cloud",
            "GoogleDrive:path/",
            database_name="custom.kdbx",
            identity="custom",
        )
        self.assertEqual(endpoint.identity, "custom")
        self.assertEqual(
            endpoint.database_remote, "GoogleDrive:path/custom.kdbx"
        )
        self.assertEqual(
            endpoint.remote_path("kp-sync/bases/a"),
            "GoogleDrive:path/kp-sync/bases/a",
        )
        for invalid in (
            "",
            ".",
            "..",
            "../x",
            "a/../b",
            "/absolute",
            "other:x",
            "a\\b",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                endpoint.remote_path(invalid)

    def test_default_commands_secure_download_and_config(self):
        endpoint = RcloneEndpoint(
            "cloud",
            "GoogleDrive:path",
            rclone_config=self.root / "config file",
            timeout=4,
            runner=self.runner,
        )
        destination = self.root / "fetched"
        endpoint.fetch(destination)
        endpoint.store(destination)
        self.assertEqual(destination.read_bytes(), b"payload")
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.runner.download_mode, 0o600)
        # A listing probes; uploads need no preliminary request.
        probe, download, upload = self.runner.calls
        config = ["--config", str(self.root / "config file")]
        self.assertEqual(
            probe[0],
            [
                "rclone",
                "lsjson",
                "--hash",
                "-R",
                "--files-only",
                "--max-depth",
                "1",
                "--include",
                "/pass.kdbx",
                "GoogleDrive:path",
            ]
            + config,
        )
        self.assertEqual(
            download[0][:3], ["rclone", "copyto", "GoogleDrive:path/pass.kdbx"]
        )
        self.assertEqual(download[0][4:], config)
        self.assertEqual(
            upload[0],
            [
                "rclone",
                "copyto",
                str(destination),
                "GoogleDrive:path/pass.kdbx",
            ]
            + config,
        )
        for _, options in self.runner.calls:
            self.assertEqual(
                options,
                dict(check=True, capture_output=True, text=True, timeout=4),
            )
        self.assertEqual(list(self.root.iterdir()), [destination])

    def test_custom_templates_do_not_affect_generic_transport(self):
        endpoint = RcloneEndpoint(
            "cloud",
            "GoogleDrive",
            download=["download", "{remote}", "{file}"],
            upload=["upload", "{file}", "{remote}"],
            probe=["inspect", "{remote}"],
            runner=self.runner,
        )
        destination = self.root / "copy"
        endpoint.fetch(destination)
        endpoint.store(destination)
        commands = [call[0] for call in self.runner.calls]
        self.assertEqual(commands[0], ["inspect", "GoogleDrive:pass.kdbx"])
        self.assertEqual(
            commands[1][0:2], ["download", "GoogleDrive:pass.kdbx"]
        )
        self.assertEqual(
            commands[2], ["upload", str(destination), "GoogleDrive:pass.kdbx"]
        )
        self.runner.calls.clear()
        endpoint.download_file("GoogleDrive:kp-sync/state.json", destination)
        endpoint.upload_file(destination, "GoogleDrive:kp-sync/state.json")
        endpoint.list_files("GoogleDrive:kp-sync")
        endpoint.delete_file("GoogleDrive:kp-sync/state.json")
        endpoint.remove_directory("GoogleDrive:kp-sync/bases")
        commands = [call[0] for call in self.runner.calls]
        self.assertTrue(all(command[0] == "rclone" for command in commands))
        self.assertEqual(
            commands[0],
            ["rclone", "lsjson", "--hash", "GoogleDrive:kp-sync/state.json"],
        )
        self.assertEqual(
            commands[3], ["rclone", "lsjson", "--hash", "GoogleDrive:kp-sync"]
        )
        self.assertEqual(
            commands[-2:],
            [
                ["rclone", "deletefile", "GoogleDrive:kp-sync/state.json"],
                ["rclone", "rmdir", "GoogleDrive:kp-sync/bases"],
            ],
        )

    def test_missing_objects_and_directories_are_reachable(self):
        for missing in ([], 3, 4):
            with self.subTest(missing=missing):
                self.runner.objects = []
                self.runner.failure = (
                    subprocess.CalledProcessError(missing, ["rclone"])
                    if isinstance(missing, int)
                    else None
                )
                self.assertIsNone(self.endpoint.probe())
                self.assertTrue(self.endpoint.available())
                self.assertIsNone(self.endpoint.stat("GoogleDrive:missing"))
                self.assertEqual(
                    self.endpoint.list_files("GoogleDrive:missing"), []
                )
                destination = self.root / "missing"
                self.assertFalse(
                    self.endpoint.download_file(
                        "GoogleDrive:missing", destination
                    )
                )
                self.assertFalse(destination.exists())
                with self.assertRaises(EndpointMissing):
                    self.endpoint.fetch(destination)

    def test_unavailable_timeout_and_missing_executable(self):
        for error in (
            subprocess.CalledProcessError(
                5, ["rclone"], stderr="private detail"
            ),
            subprocess.TimeoutExpired(["rclone"], 2),
            FileNotFoundError("private detail"),
        ):
            with self.subTest(error=type(error).__name__):
                self.runner.failure = error
                self.assertFalse(self.endpoint.available())
                with self.assertRaises(EndpointUnavailable) as raised:
                    self.endpoint.probe()
                self.assertNotIn("private detail", str(raised.exception))
                self.assertNotIsInstance(raised.exception, EndpointMissing)

    def test_duplicates_and_malformed_json_are_fatal(self):
        original = self.runner.objects[0]
        for objects in (
            [original, original],
            {"Path": "pass.kdbx"},
            [1],
            [{}],
            [dict(original, Hashes=[])],
            [dict(original, Size="7")],
            [dict(original, Hashes={"sha256": "invalid"})],
            [dict(original, IsDir=True)],
        ):
            with self.subTest(objects=objects):
                self.runner.objects = objects
                with self.assertRaises(EndpointError) as raised:
                    self.endpoint.available()
                self.assertNotIsInstance(raised.exception, EndpointUnavailable)
        self.endpoint._runner = Mock(
            return_value=subprocess.CompletedProcess([], 0, "{bad", "")
        )
        with self.assertRaises(EndpointError) as raised:
            self.endpoint.probe()
        self.assertNotIsInstance(raised.exception, EndpointUnavailable)

    def test_hash_fingerprints_and_deterministic_fallback(self):
        first = self.endpoint.probe()
        self.assertIsNone(first.sha256)
        self.assertEqual(self.endpoint.hashes, {"md5": "abc"})
        self.assertEqual(first, self.endpoint.probe())
        # A 3-character test digest is too short to stand for content.
        self.assertFalse(first.token_is_content)
        self.runner.objects[0]["ModTime"] = "later"
        self.assertNotEqual(first.token, self.endpoint.probe().token)
        for hashes in (
            {},
            {"md5": ""},
            {"crc32": "7915140f"},
            {"CRC32": "7915140f00000000"},
            {"whatever": "abcdef012345678"},
            {"quickxor": " " + "a" * 39},
        ):
            with self.subTest(hashes=hashes):
                self.runner.objects[0]["Hashes"] = hashes
                self.assertFalse(self.endpoint.probe().token_is_content)
        for name, digest in (
            ("MD5", "a" * 32),
            ("sha1", "a" * 40),
            ("SHA-256", "a" * 64),
            ("dropbox", "b" * 64),
            # Not computable locally, but still a content hash.
            ("quickxor", "6a842b03258cd7378c01046484c31be0208637e0"),
            ("whirlpool", "c" * 128),
        ):
            with self.subTest(name=name):
                self.runner.objects[0]["Hashes"] = {name: digest}
                self.assertTrue(self.endpoint.probe().token_is_content)
        digest = hashlib.sha256(b"payload").hexdigest()
        self.runner.objects[0]["Hashes"] = {
            "SHA256": digest.upper(),
            "md5": "abc",
        }
        self.assertEqual(self.endpoint.probe().sha256, digest)
        self.runner.objects[0]["Hashes"] = {"SHA-256": digest}
        self.assertEqual(self.endpoint.probe().sha256, digest)
        self.runner.objects[0]["Hashes"] = {"sha256": ""}
        self.assertIsNone(self.endpoint.probe().sha256)
        self.runner.objects = []
        self.endpoint.probe()
        self.assertEqual(self.endpoint.hashes, {})

    def test_store_invalidates_the_reusable_listing(self):
        before = self.endpoint.probe()
        self.assertEqual(before, self.endpoint.probe_recent(60))
        source = self.root / "candidate.kdbx"
        source.write_bytes(b"candidate")
        self.runner.objects[0]["Hashes"] = {"md5": "def"}
        self.endpoint.store(source)
        self.assertNotEqual(before, self.endpoint.probe_recent(60))

    def test_directory_listing_accepts_rclone_directory_sizes(self):
        self.runner.objects = [
            {"Path": "bases", "Size": -1, "IsDir": True},
            {"Path": "state.json", "Size": 10, "IsDir": False},
        ]
        self.assertEqual(
            self.endpoint.list_files("GoogleDrive:kp-sync"),
            self.runner.objects,
        )
        self.runner.objects.append(dict(self.runner.objects[0]))
        with self.assertRaises(EndpointError) as raised:
            self.endpoint.list_files("GoogleDrive:kp-sync")
        self.assertNotIsInstance(raised.exception, EndpointUnavailable)

    def test_failed_transfer_preserves_destination_and_removes_staging(self):
        destination = self.root / "copy"
        destination.write_bytes(b"original")
        original_runner = self.runner

        def racing_runner(argv, **kwargs):
            if "copyto" in argv:
                Path(argv[3]).write_bytes(b"partial")
                raise subprocess.CalledProcessError(4, argv)
            return original_runner(argv, **kwargs)

        self.endpoint._runner = racing_runner
        self.assertFalse(
            self.endpoint.download_file("GoogleDrive:object", destination)
        )
        self.assertEqual(destination.read_bytes(), b"original")
        self.assertEqual(list(self.root.iterdir()), [destination])

    def test_cached_download_checks_hash_and_metadata_collisions(
        self,
    ):
        cached = b"payload"
        destination = self.root / "cached"
        self.runner.objects[0]["Hashes"] = {
            "MD5": hashlib.md5(cached).hexdigest()
        }
        self.assertTrue(
            self.endpoint.download_file(
                "GoogleDrive:state.json", destination, cached=cached
            )
        )
        self.assertEqual(destination.read_bytes(), cached)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(self.runner.calls), 1)

        self.runner.payload = b"changed"
        self.runner.objects[0]["Hashes"] = {
            "MD5": hashlib.md5(self.runner.payload).hexdigest()
        }
        self.runner.calls.clear()
        self.endpoint.download_file(
            "GoogleDrive:state.json", destination, cached=cached
        )
        self.assertEqual(destination.read_bytes(), b"changed")
        self.assertEqual(len(self.runner.calls), 2)

        self.runner.objects[0]["Hashes"] = {}
        self.runner.calls.clear()
        self.endpoint.download_file(
            "GoogleDrive:state.json", destination, cached=cached
        )
        self.assertEqual(len(self.runner.calls), 2)
        self.runner.objects *= 2
        with self.assertRaises(EndpointError):
            self.endpoint.download_file(
                "GoogleDrive:state.json", destination, cached=cached
            )

    def test_rclone_timings_report_failure_without_arguments_or_command_output(
        self,
    ):
        self.runner.failure = subprocess.TimeoutExpired(
            ["rclone", "sensitive argument"], 4
        )
        with (
            patch(
                "kp_sync.endpoints.rclone.time.monotonic", side_effect=[1, 3.5]
            ),
            patch("kp_sync.endpoints.rclone.detail") as log,
        ):
            with self.assertRaises(EndpointUnavailable):
                self.endpoint.probe()
        log.assert_called_once_with("rclone lsjson for cloud: 2.500s (failed)")

    def test_nonregular_output_and_destination_are_rejected(self):
        destination = self.root / "copy"
        source = self.root / "source"
        source.write_bytes(b"original")
        destination.symlink_to(source)
        with self.assertRaises(EndpointError):
            self.endpoint.fetch(destination)
        self.assertEqual(source.read_bytes(), b"original")
        destination.unlink()
        original_runner = self.runner

        def symlink_output(argv, **kwargs):
            if "copyto" in argv:
                output = Path(argv[3])
                output.unlink()
                output.symlink_to(source)
                return subprocess.CompletedProcess(argv, 0, "", "")
            return original_runner(argv, **kwargs)

        self.endpoint._runner = symlink_output
        with self.assertRaises(EndpointError):
            self.endpoint.fetch(destination)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.iterdir()), [source])

    def test_readonly_and_no_root_or_parent_removal(self):
        self.endpoint.readonly = True
        for operation in (
            lambda: self.endpoint.store(self.root / "source"),
            lambda: self.endpoint.upload_file(
                self.root / "source", "GoogleDrive:x"
            ),
            lambda: self.endpoint.delete_file("GoogleDrive:x"),
            lambda: self.endpoint.remove_directory("GoogleDrive:x"),
        ):
            with self.assertRaises(PermissionError):
                operation()
        self.assertEqual(self.runner.calls, [])
        self.endpoint.readonly = False
        for path in (
            "GoogleDrive:",
            "GoogleDrive:/",
            "OtherDrive:x",
            "GoogleDrive:../x",
            "GoogleDrive:kp-sync/../../x",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.endpoint.remove_directory(path)
        self.assertEqual(self.runner.calls, [])

    def test_batch_transfers_validate_names_and_skip_empty_lists(self):
        directory = "GoogleDrive:kp-sync/bases"
        work = self.root / "work"
        work.mkdir(mode=0o700)
        for name in ("", ".", "..", "a/b", "a\\b", "a\nb", "a\x00b"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.endpoint.delete_files(
                        directory, [name], temporary=work
                    )
                with self.assertRaises(ValueError):
                    self.endpoint.upload_files(
                        {name: work}, directory, temporary=work
                    )
        for outside in ("Other:bases", "GoogleDrive:../x", "GoogleDrive:"):
            with self.subTest(outside=outside):
                with self.assertRaises(ValueError):
                    self.endpoint.delete_files(
                        outside, ["a.kdbx"], temporary=work
                    )
        self.endpoint.delete_files(directory, [], temporary=work)
        self.endpoint.upload_files({}, directory, temporary=work)
        empty = work / "empty"
        empty.mkdir()
        self.assertEqual(
            self.endpoint.download_files(directory, [], empty), set()
        )
        self.assertEqual(self.runner.calls, [])
        readonly = RcloneEndpoint(
            "cloud", "GoogleDrive", readonly=True, runner=self.runner
        )
        with self.assertRaises(PermissionError):
            readonly.delete_files(directory, ["a.kdbx"], temporary=work)
        with self.assertRaises(PermissionError):
            readonly.upload_files({"a.kdbx": work}, directory, temporary=work)

    def test_batch_delete_lists_only_named_files_one_level_deep(self):
        work = self.root / "work"
        work.mkdir(mode=0o700)
        seen = []

        def runner(argv, **kwargs):
            listing = argv[argv.index("--files-from-raw") + 1]
            seen.append(Path(listing).read_text())
            return subprocess.CompletedProcess(argv, 0, "", "")

        self.endpoint._runner = runner
        self.endpoint.delete_files(
            "GoogleDrive:kp-sync/bases", ["b.kdbx", "a.kdbx"], temporary=work
        )
        self.assertEqual(seen, ["a.kdbx\nb.kdbx\n"])
        self.assertEqual(list(work.iterdir()), [])

    def test_batch_download_needs_an_empty_directory_and_reports_present(
        self,
    ):
        work = self.root / "work"
        work.mkdir(mode=0o700)
        target = work / "bases"
        target.mkdir()
        (target / "old").write_bytes(b"x")
        with self.assertRaisesRegex(EndpointError, "must be empty"):
            self.endpoint.download_files(
                "GoogleDrive:kp-sync/bases", ["a.kdbx"], target
            )
        (target / "old").unlink()

        def runner(argv, **kwargs):
            destination = Path(argv[-1])
            (destination / "a.kdbx").write_bytes(b"a")
            (destination / "b.kdbx").symlink_to(destination / "a.kdbx")
            return subprocess.CompletedProcess(argv, 0, "", "")

        self.endpoint._runner = runner
        with self.assertRaisesRegex(EndpointError, "regular file"):
            self.endpoint.download_files(
                "GoogleDrive:kp-sync/bases", ["a.kdbx", "b.kdbx"], target
            )

        def partial(argv, **kwargs):
            (Path(argv[-1]) / "a.kdbx").write_bytes(b"a")
            return subprocess.CompletedProcess(argv, 0, "", "")

        for path in target.iterdir():
            path.unlink()
        self.endpoint._runner = partial
        self.assertEqual(
            self.endpoint.download_files(
                "GoogleDrive:kp-sync/bases", ["a.kdbx", "c.kdbx"], target
            ),
            {"a.kdbx"},
        )
        self.assertEqual((target / "a.kdbx").stat().st_mode & 0o777, 0o600)

    def test_runner_is_resolved_at_construction_and_templates_validated(self):
        with patch("kp_sync.endpoints.rclone.subprocess.run", self.runner):
            endpoint = RcloneEndpoint("cloud", "GoogleDrive")
        self.assertEqual(endpoint.probe().size, 7)
        for options in (
            {"download": "rclone copyto"},
            {"download": []},
            {"upload": ["rclone"]},
            {"probe": ["rclone", "{file}"]},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                RcloneEndpoint("cloud", "GoogleDrive", **options)


if __name__ == "__main__":
    unittest.main()
