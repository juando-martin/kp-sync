"""Unit tests for kp-sync.

Importing this package points ``HOME``, the XDG base directories and
``XDG_RUNTIME_DIR`` at a private temporary directory, so no test can
read or change the real home (production profiles, ``~/.ssh``,
KeePassXC association metadata) or reach the real runtime directory
(daemon sockets, hubs, pending decisions). ``tests/test_isolation.py``
imports it, so it also applies under ``unittest discover -s tests``,
which imports every test module before running any test. ``TMPDIR`` is
left as the caller set it (runtime storage, by the documented command).
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path

REAL_HOME = os.environ.get("HOME")
REAL_RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR")
ROOT = Path(tempfile.mkdtemp(prefix="kp-sync-tests-"))
atexit.register(shutil.rmtree, ROOT, ignore_errors=True)

HOME = ROOT / "home"
RUNTIME_DIR = ROOT / "run"
for _directory in (HOME, RUNTIME_DIR):
    _directory.mkdir(mode=0o700)
os.environ["HOME"] = str(HOME)
os.environ["XDG_RUNTIME_DIR"] = str(RUNTIME_DIR)
for _name, _relative in (
    ("XDG_CONFIG_HOME", ".config"),
    ("XDG_DATA_HOME", ".local/share"),
    ("XDG_STATE_HOME", ".local/state"),
    ("XDG_CACHE_HOME", ".cache"),
):
    os.environ[_name] = str(HOME / _relative)
