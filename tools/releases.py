#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Cut, install and test kp-sync releases.

Run with the interpreter of the environment kp-sync lives in, for
example ``~/.venvs/tools/bin/python tools/releases.py ...``:

``release VERSION SUMMARY``
    On a clean ``main``: bump ``pyproject.toml`` and
    ``src/kp_sync/__init__.py``, date the ``## Unreleased`` section of
    ``CHANGES.md``, run the checks, then commit and add an annotated
    ``vVERSION`` tag. Failed checks restore the edited files.
``install TAG --profile NAME``
    Install ``TAG`` from a private export into this interpreter's
    environment (not editable), check the version, then refresh the
    profile's installed units and restart its daemon. First-time
    activation (``service enable daemon --now``) stays manual.
``test TAG --report FILE``
    Run the focused tests, the full suite and pycodestyle from a
    private export, record the environment and the daemon state before
    and after, and write a Markdown report.

Exports live under ``$XDG_RUNTIME_DIR`` and are removed afterwards. None
of these commands contacts endpoints or reads credentials, except that
``install`` restarts the daemon, which then works as usual.
"""

import argparse
import datetime
import io
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECK_PATHS = ["src", "tests", "examples", "tools", "docs"]
FOCUSED_TESTS = [
    "tests.test_compatibility",
    "tests.test_rclone_rcd.RealRcdTest",
]
VERSION = re.compile(r"\d+\.\d+\.\d+")


class ReleaseError(RuntimeError):
    """A release step cannot continue safely."""


def _git(repo: Path, *args: str, run=subprocess.run) -> str:
    result = run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ReleaseError(
            f"git {' '.join(args)} failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def _read_version(repo: Path) -> str:
    text = (repo / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"$', text, re.MULTILINE)
    if match is None:
        raise ReleaseError("pyproject.toml has no version line")
    return match.group(1)


def _check_commands(python: str, full: bool) -> list[list[str]]:
    commands = [
        [python, "-m", "black", "--check", "-q", *CHECK_PATHS],
        [
            python,
            "-m",
            "pycodestyle",
            "--config=.pycodestyle",
            *CHECK_PATHS,
        ],
        [python, "-m", "unittest", "-q", "tests.test_documentation"],
    ]
    if full:
        commands.append([python, "tools/run_tests.py"])
    return commands


def _test_environment(directory: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(directory / "src")
    environment["TMPDIR"] = os.environ.get(
        "XDG_RUNTIME_DIR", tempfile.gettempdir()
    )
    return environment


def release(
    version: str,
    summary: str,
    *,
    repo: Path = ROOT,
    full_tests: bool = False,
    today: datetime.date | None = None,
    python: str = sys.executable,
    run=subprocess.run,
) -> str:
    """Bump, check, commit and tag ``version``; return the tag name."""
    if not VERSION.fullmatch(version):
        raise ReleaseError("version must look like X.Y.Z")
    tag = f"v{version}"
    if _git(repo, "status", "--porcelain", run=run):
        raise ReleaseError("the working tree has uncommitted changes")
    if _git(repo, "rev-parse", "--abbrev-ref", "HEAD", run=run) != "main":
        raise ReleaseError("releases are cut from main")
    if _git(repo, "tag", "--list", tag, run=run):
        raise ReleaseError(f"tag {tag} already exists")
    current = _read_version(repo)
    if _version_tuple(version) <= _version_tuple(current):
        raise ReleaseError(f"{version} is not newer than {current}")
    changes = repo / "CHANGES.md"
    text = changes.read_text(encoding="utf-8")
    if "\n## Unreleased\n" not in text:
        raise ReleaseError("CHANGES.md has no '## Unreleased' section")
    date = (today or datetime.date.today()).isoformat()
    pyproject = repo / "pyproject.toml"
    init = repo / "src" / "kp_sync" / "__init__.py"
    edits = {
        changes: text.replace(
            "\n## Unreleased\n", f"\n## {version} ({date})\n", 1
        ),
        pyproject: pyproject.read_text(encoding="utf-8").replace(
            f'version = "{current}"', f'version = "{version}"', 1
        ),
        init: init.read_text(encoding="utf-8").replace(
            f'__version__ = "{current}"', f'__version__ = "{version}"', 1
        ),
    }
    for path, contents in edits.items():
        path.write_text(contents, encoding="utf-8")
    try:
        for command in _check_commands(python, full_tests):
            result = run(command, cwd=repo, env=_test_environment(repo))
            if result.returncode != 0:
                raise ReleaseError(f"check failed: {' '.join(command)}")
    except BaseException:
        _git(
            repo,
            "checkout",
            "--",
            *(str(path.relative_to(repo)) for path in edits),
            run=run,
        )
        raise
    _git(repo, "commit", "-q", "-am", f"Release {version}", run=run)
    _git(
        repo, "tag", "-a", tag, "-m", f"kp-sync {version}: {summary}", run=run
    )
    return tag


def export(tag: str, repo: Path, *, run=subprocess.run) -> Path:
    """Extract ``tag`` into a new private runtime directory."""
    _git(repo, "rev-parse", "--verify", f"{tag}^{{commit}}", run=run)
    archive = run(
        ["git", "-C", str(repo), "archive", tag],
        capture_output=True,
    )
    if archive.returncode != 0:
        raise ReleaseError(f"git archive {tag} failed")
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    directory = Path(tempfile.mkdtemp(prefix=f"kp-sync-{tag}.", dir=base))
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as source:
        source.extractall(directory, filter="data")
    return directory


def _unit_name(profile: str) -> str:
    # An empty profile is the default one (kp-sync.service).
    return f"kp-sync@{profile}.service" if profile else "kp-sync.service"


def _profile_args(profile: str) -> list[str]:
    return ["--profile", profile] if profile else []


def install(
    tag: str,
    *,
    profile: str,
    repo: Path = ROOT,
    restart: bool = True,
    home: Path | None = None,
    python: str = sys.executable,
    run=subprocess.run,
) -> list[str]:
    """Install ``tag`` and refresh ``profile``; return what was done."""
    home = home or Path.home()
    done = []
    # Build in place when the environment has setuptools; otherwise
    # let pip build in isolation, which may download setuptools.
    local_build = (
        run(
            [python, "-c", "import setuptools.build_meta"],
            capture_output=True,
        ).returncode
        == 0
    )
    directory = export(tag, repo, run=run)
    try:
        result = run(
            [
                python,
                "-m",
                "pip",
                "install",
                "-q",
                "--no-deps",
                *(["--no-build-isolation"] if local_build else []),
                str(directory),
            ]
        )
        if result.returncode != 0:
            raise ReleaseError("pip install failed")
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    program = Path(python).parent / "kp-sync"
    link = home / ".local" / "bin" / "kp-sync"
    command = str(link if link.exists() else program)
    version = run([command, "--version"], capture_output=True, text=True)
    expected = f"kp-sync {tag.removeprefix('v')}"
    if version.stdout.strip() != expected:
        raise ReleaseError(
            f"{command} reports {version.stdout.strip()!r}, "
            f"expected {expected!r}"
        )
    done.append(f"installed {expected} ({command})")
    unit = home / ".config" / "systemd" / "user" / _unit_name(profile)
    if not unit.exists():
        done.append(
            f"no units for profile {profile or '(default)'}; activate with "
            f"'{' '.join([command, *_profile_args(profile)])} service "
            "install daemon --desktop' and 'service enable daemon --now'"
        )
        return done
    entry = (
        home / ".local" / "share" / "applications"
    ) / "org.keepassxc.KeePassXC.desktop"
    text = entry.read_text(encoding="utf-8") if entry.exists() else None
    desktop = text is not None and (
        f"--profile {profile} " in text
        if profile
        else "--profile " not in text
    )
    for action in ["install"] + (["restart"] if restart else []):
        argv = [command, *_profile_args(profile), "service", action, "daemon"]
        if action == "install" and desktop:
            argv.append("--desktop")
        if run(argv).returncode != 0:
            raise ReleaseError(f"failed: {' '.join(argv)}")
        done.append(" ".join(argv[1:]))
    return done


def _output(argv: list[str], run, **kwargs) -> str:
    try:
        result = run(argv, capture_output=True, text=True, **kwargs)
    except OSError as exc:
        return f"(unavailable: {exc})"
    return (result.stdout + result.stderr).strip()


def _daemon_state(profile: str, run) -> str:
    return _output(
        [
            "systemctl",
            "--user",
            "show",
            "-p",
            "ActiveState",
            "-p",
            "ActiveEnterTimestamp",
            "-p",
            "MainPID",
            _unit_name(profile),
        ],
        run,
    )


def test(
    tag: str,
    *,
    report: Path,
    profile: str,
    repo: Path = ROOT,
    python: str = sys.executable,
    run=subprocess.run,
) -> bool:
    """Test ``tag`` from an export, write ``report``, return success."""
    lines = [
        f"# kp-sync {tag} test report ({socket.gethostname()}, "
        f"{datetime.date.today().isoformat()})",
        "",
    ]
    ok = True
    before = _daemon_state(profile, run)
    directory = export(tag, repo, run=run)
    try:
        lines += ["## Environment", "```"]
        for argv in (
            ["keepassxc-cli", "--version"],
            ["rclone", "version"],
            [python, "--version"],
            [python, "-c", "import pykeepass; print(pykeepass.__version__)"],
        ):
            first = _output(argv, run).splitlines()
            lines.append(first[0] if first else "(none)")
        lines += ["```", "", "## Daemon before", "```", before, "```", ""]
        environment = _test_environment(directory)
        for title, argv in (
            ("Focused tests", [python, "-m", "unittest", *FOCUSED_TESTS]),
            (
                "Full suite",
                [python, "tools/run_tests.py"],
            ),
            (
                "pycodestyle",
                [
                    python,
                    "-m",
                    "pycodestyle",
                    "--config=.pycodestyle",
                    *CHECK_PATHS,
                ],
            ),
        ):
            result = run(
                argv,
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
            )
            ok = ok and result.returncode == 0
            tail = (result.stdout + result.stderr).strip().splitlines()[-5:]
            lines += [
                f"## {title}: {'OK' if result.returncode == 0 else 'FAILED'}",
                "```",
                *tail,
                "```",
                "",
            ]
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    after = _daemon_state(profile, run)
    unchanged = before == after
    ok = ok and unchanged
    lines += [
        f"## Daemon after: {'unchanged' if unchanged else 'CHANGED'}",
        "```",
        after,
        "```",
        "",
        f"**Result: {'PASS' if ok else 'FAIL'}**",
        "",
    ]
    report.write_text("\n".join(lines), encoding="utf-8")
    return ok


def main(argv=None) -> int:
    """Run the command line."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    cut = commands.add_parser("release", help="bump, check, commit, tag")
    cut.add_argument("version")
    cut.add_argument("summary")
    cut.add_argument("--full-tests", action="store_true")
    put = commands.add_parser("install", help="install a tag")
    put.add_argument("tag")
    put.add_argument(
        "--profile",
        default=socket.gethostname().split(".")[0],
        help="profile to refresh (default: the host name; '' = default)",
    )
    put.add_argument("--no-restart", action="store_true")
    check = commands.add_parser("test", help="test a tag, write a report")
    check.add_argument("tag")
    check.add_argument("--report", type=Path, required=True)
    check.add_argument(
        "--profile",
        default=socket.gethostname().split(".")[0],
        help="daemon to watch (default: the host name; '' = default)",
    )
    for command in (cut, put, check):
        command.add_argument("--repo", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        if args.command == "release":
            tag = release(
                args.version,
                args.summary,
                repo=args.repo,
                full_tests=args.full_tests,
            )
            print(f"tagged {tag}; push with: git push origin main {tag}")
        elif args.command == "install":
            for line in install(
                args.tag,
                profile=args.profile,
                repo=args.repo,
                restart=not args.no_restart,
            ):
                print(line)
        else:
            ok = test(
                args.tag,
                report=args.report,
                profile=args.profile,
                repo=args.repo,
            )
            print(f"{'PASS' if ok else 'FAIL'}: report in {args.report}")
            return 0 if ok else 1
    except ReleaseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
