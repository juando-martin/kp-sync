"""Tests for tools/releases.py, on throwaway repositories and fakes."""

import datetime
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools import releases


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


class ReleaseRepoTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            dir=os.environ.get("XDG_RUNTIME_DIR")
        )
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        (self.repo / "src" / "kp_sync").mkdir(parents=True)
        (self.repo / "pyproject.toml").write_text(
            '[project]\nname = "kp-sync"\nversion = "1.2.3"\n'
        )
        (self.repo / "src" / "kp_sync" / "__init__.py").write_text(
            '"""kp-sync."""\n\n__version__ = "1.2.3"\n'
        )
        (self.repo / "CHANGES.md").write_text(
            "# Changes\n\n## Unreleased\n\n- New.\n\n## 1.2.3 (2026-01-01)\n"
        )
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(self.repo)], check=True
        )
        for key, value in (
            ("user.name", "Test"),
            ("user.email", "test@example.invalid"),
            ("commit.gpgsign", "false"),
            ("tag.gpgsign", "false"),
        ):
            _git(self.repo, "config", key, value)
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "initial")
        self.checks = []

    def runner(self, check_status=0):
        def run(argv, **kwargs):
            if argv[0] == "git":
                return subprocess.run(argv, **kwargs)
            self.checks.append(argv)
            return subprocess.CompletedProcess(argv, check_status, "", "")

        return run

    def release(self, version="1.2.4", **kwargs):
        return releases.release(
            version,
            "a summary",
            repo=self.repo,
            today=datetime.date(2026, 11, 6),
            python=sys.executable,
            run=kwargs.pop("run", self.runner()),
            **kwargs,
        )

    def test_release_bumps_dates_checks_commits_and_tags(self):
        self.assertEqual(self.release(), "v1.2.4")
        self.assertIn(
            'version = "1.2.4"', (self.repo / "pyproject.toml").read_text()
        )
        self.assertIn(
            '__version__ = "1.2.4"',
            (self.repo / "src/kp_sync/__init__.py").read_text(),
        )
        changes = (self.repo / "CHANGES.md").read_text()
        self.assertIn("## 1.2.4 (2026-11-06)\n\n- New.", changes)
        self.assertNotIn("Unreleased", changes)
        log = subprocess.run(
            ["git", "-C", str(self.repo), "log", "-1", "--format=%s"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(log, "Release 1.2.4")
        message = subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "tag",
                "-n1",
                "--list",
                "v1.2.4",
            ],
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn("kp-sync 1.2.4: a summary", message)
        self.assertEqual(len(self.checks), 3)
        self.assertNotIn("discover", " ".join(map(" ".join, self.checks)))

    def test_full_tests_run_a_real_suite_command(self):
        tests = self.repo / "tests"
        tests.mkdir()
        (tests / "test_trivial.py").write_text(
            "import unittest\n\n\nclass T(unittest.TestCase):\n"
            "    def test_ok(self):\n        pass\n"
        )
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "tests")
        suites = []

        def run(argv, **kwargs):
            if "discover" in argv:
                suites.append(argv)
                return subprocess.run(argv, capture_output=True, **kwargs)
            return self.runner()(argv, **kwargs)

        self.release(full_tests=True, run=run)
        self.assertEqual(len(suites), 1)

    def test_failed_check_restores_files_and_tags_nothing(self):
        with self.assertRaisesRegex(releases.ReleaseError, "check failed"):
            self.release(run=self.runner(check_status=1))
        self.assertIn(
            'version = "1.2.3"', (self.repo / "pyproject.toml").read_text()
        )
        self.assertIn("## Unreleased", (self.repo / "CHANGES.md").read_text())
        status = subprocess.run(
            ["git", "-C", str(self.repo), "status", "--porcelain"],
            capture_output=True,
            text=True,
        ).stdout
        self.assertEqual(status, "")
        tags = subprocess.run(
            ["git", "-C", str(self.repo), "tag"],
            capture_output=True,
            text=True,
        ).stdout
        self.assertEqual(tags, "")

    def test_release_refuses_unsafe_starting_points(self):
        with self.assertRaisesRegex(releases.ReleaseError, "not newer"):
            self.release("1.2.3")
        with self.assertRaisesRegex(releases.ReleaseError, "X.Y.Z"):
            self.release("1.2")
        (self.repo / "notes.txt").write_text("dirty")
        with self.assertRaisesRegex(releases.ReleaseError, "uncommitted"):
            self.release()
        (self.repo / "notes.txt").unlink()
        _git(self.repo, "checkout", "-q", "-b", "feature")
        with self.assertRaisesRegex(releases.ReleaseError, "from main"):
            self.release()
        _git(self.repo, "checkout", "-q", "main")
        (self.repo / "CHANGES.md").write_text("# Changes\n")
        _git(self.repo, "commit", "-q", "-am", "no unreleased")
        with self.assertRaisesRegex(releases.ReleaseError, "Unreleased"):
            self.release()
        self.assertEqual(self.checks, [])

    def test_export_extracts_the_tag_into_runtime_storage(self):
        _git(self.repo, "tag", "-a", "v1.2.3", "-m", "old")
        directory = releases.export("v1.2.3", self.repo)
        self.addCleanup(subprocess.run, ["rm", "-rf", str(directory)])
        self.assertTrue((directory / "pyproject.toml").is_file())
        if os.environ.get("XDG_RUNTIME_DIR"):
            self.assertEqual(
                directory.parent, Path(os.environ["XDG_RUNTIME_DIR"])
            )
        with self.assertRaises(releases.ReleaseError):
            releases.export("v9.9.9", self.repo)


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            dir=os.environ.get("XDG_RUNTIME_DIR")
        )
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.calls = []
        self.probes = []
        self.exported = []

    def fake_run(self, version="kp-sync 1.2.4", setuptools=True):
        def run(argv, **kwargs):
            if "import setuptools.build_meta" in argv:
                self.probes.append(argv)
                status = 0 if setuptools else 1
                return subprocess.CompletedProcess(argv, status, "", "")
            self.calls.append(argv)
            stdout = version + "\n" if "--version" in argv else ""
            return subprocess.CompletedProcess(argv, 0, stdout, "")

        return run

    def install(self, **kwargs):
        def export(tag, repo, run):
            directory = self.home / "export"
            directory.mkdir()
            self.exported.append(directory)
            return directory

        original = releases.export
        releases.export = export
        self.addCleanup(setattr, releases, "export", original)
        return releases.install(
            "v1.2.4",
            profile="beta",
            home=self.home,
            python=str(self.home / "venv/bin/python"),
            run=kwargs.pop("run", self.fake_run()),
            **kwargs,
        )

    def installed_units(self, desktop):
        unit = self.home / ".config/systemd/user/kp-sync@beta.service"
        unit.parent.mkdir(parents=True)
        unit.write_text("[Service]\n")
        if desktop:
            entry = (
                self.home
                / ".local/share/applications/org.keepassxc.KeePassXC.desktop"
            )
            entry.parent.mkdir(parents=True)
            entry.write_text("Exec=kp-sync --profile beta run\n")

    def test_install_refreshes_units_with_desktop_and_restarts(self):
        self.installed_units(desktop=True)
        done = self.install()
        program = str(self.home / "venv/bin/kp-sync")
        self.assertIn("--no-deps", self.calls[0])
        self.assertIn("--no-build-isolation", self.calls[0])
        self.assertEqual(len(self.probes), 1)
        self.assertEqual(self.calls[1], [program, "--version"])
        self.assertEqual(
            self.calls[2],
            [
                program,
                "--profile",
                "beta",
                "service",
                "install",
                "daemon",
                "--desktop",
            ],
        )
        self.assertEqual(
            self.calls[3],
            [program, "--profile", "beta", "service", "restart", "daemon"],
        )
        self.assertFalse(self.exported[0].exists())
        self.assertIn("installed kp-sync 1.2.4", done[0])

    def test_install_prefers_the_local_bin_link_and_can_skip_restart(self):
        self.installed_units(desktop=False)
        link = self.home / ".local/bin/kp-sync"
        link.parent.mkdir(parents=True)
        link.write_text("")
        self.install(restart=False)
        self.assertEqual(self.calls[1], [str(link), "--version"])
        self.assertEqual(self.calls[2][-2:], ["install", "daemon"])
        self.assertEqual(len(self.calls), 3)

    def test_install_handles_the_default_profile(self):
        unit = self.home / ".config/systemd/user/kp-sync.service"
        unit.parent.mkdir(parents=True)
        unit.write_text("[Service]\n")
        original = releases.export
        releases.export = lambda tag, repo, run: self.home / "export"
        (self.home / "export").mkdir()
        self.addCleanup(setattr, releases, "export", original)
        releases.install(
            "v1.2.4",
            profile="",
            home=self.home,
            python=str(self.home / "venv/bin/python"),
            run=self.fake_run(),
        )
        program = str(self.home / "venv/bin/kp-sync")
        self.assertEqual(
            self.calls[2:],
            [
                [program, "service", "install", "daemon"],
                [program, "service", "restart", "daemon"],
            ],
        )
        self.assertEqual(releases._unit_name(""), "kp-sync.service")

    def test_install_builds_in_isolation_without_setuptools(self):
        self.install(run=self.fake_run(setuptools=False))
        self.assertIn("--no-deps", self.calls[0])
        self.assertNotIn("--no-build-isolation", self.calls[0])

    def test_install_without_units_only_explains_activation(self):
        done = self.install()
        self.assertEqual(len(self.calls), 2)
        self.assertIn("service enable daemon --now", done[-1])

    def test_install_stops_on_a_version_mismatch(self):
        self.installed_units(desktop=False)
        with self.assertRaisesRegex(releases.ReleaseError, "expected"):
            self.install(run=self.fake_run(version="kp-sync 1.2.3"))
        self.assertEqual(len(self.calls), 2)


class TestReportTest(unittest.TestCase):
    def test_report_records_results_and_daemon_state(self):
        with tempfile.TemporaryDirectory(
            dir=os.environ.get("XDG_RUNTIME_DIR")
        ) as name:
            root = Path(name)
            states = iter(["MainPID=1", "MainPID=1", "MainPID=1", "PID=2"])

            def run(argv, **kwargs):
                if argv[0] == "systemctl":
                    return subprocess.CompletedProcess(
                        argv, 0, next(states), ""
                    )
                status = 1 if "pycodestyle" in argv and fail else 0
                return subprocess.CompletedProcess(argv, status, "Ran 5\n", "")

            def export(tag, repo, run):
                directory = root / f"export-{len(list(root.iterdir()))}"
                directory.mkdir()
                return directory

            original = releases.export
            releases.export = export
            self.addCleanup(setattr, releases, "export", original)
            report = root / "report.md"
            fail = False
            self.assertTrue(
                releases.test("v1.2.4", report=report, profile="alpha", run=run)
            )
            text = report.read_text()
            self.assertIn("## Full suite: OK", text)
            self.assertIn("## Daemon after: unchanged", text)
            self.assertIn("**Result: PASS**", text)
            fail = True
            self.assertFalse(
                releases.test("v1.2.4", report=report, profile="alpha", run=run)
            )
            text = report.read_text()
            self.assertIn("## pycodestyle: FAILED", text)
            self.assertIn("## Daemon after: CHANGED", text)
            self.assertIn("**Result: FAIL**", text)


if __name__ == "__main__":
    unittest.main()
