#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Run the unit tests in parallel, one process per test module.

Same tests and checks as ``python -m unittest discover -s tests``; each
module runs as ``python -m unittest tests.MODULE`` in its own process
(so ``tests/__init__.py`` gives it its own private home and runtime
directory), up to one process per CPU, slowest modules first. Output is
shown only for modules that fail; the exit status is non-zero if any
module fails or reports no tests.

Run from the repository root with the interpreter that has the test
dependencies, for example::

    TMPDIR="$XDG_RUNTIME_DIR" python tools/run_tests.py
"""

import argparse
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Modules that take longest (measured), started first so the run is not
# held up by one starting last. Others follow in file-size order.
SLOW_FIRST = (
    "test_compatibility",
    "test_merge",
    "test_engine",
    "test_cloud_sync",
    "test_documentation",
    "test_content",
    "test_kdbx",
)
# Modules whose tests run one per process: the two KeePassXC
# compatibility tests are the slowest single tests (real Argon2), and
# finish sooner in parallel. Splitting the large engine modules too was
# measured and did not help (CPU-bound: 4 physical cores on beta).
SPLIT = {"test_compatibility": 2}
_RAN = re.compile(r"^Ran (\d+) tests? in", re.MULTILINE)
_SKIPPED = re.compile(r"skipped=(\d+)")


def modules(root: Path = ROOT) -> list[str]:
    """Return the test modules, slowest first."""
    found = {path.stem: path for path in (root / "tests").glob("test_*.py")}
    first = [name for name in SLOW_FIRST if name in found]
    rest = sorted(
        (name for name in found if name not in first),
        key=lambda name: found[name].stat().st_size,
        reverse=True,
    )
    return first + rest


def _test_ids(name: str) -> list[str]:
    import unittest

    def flatten(suite):
        for item in suite:
            if isinstance(item, unittest.TestSuite):
                yield from flatten(item)
            else:
                yield item.id()

    return list(
        flatten(unittest.defaultTestLoader.loadTestsFromName(f"tests.{name}"))
    )


def jobs(names: list[str], split: dict[str, int]) -> list[list[str]]:
    """Return unittest targets per process, splitting some modules."""
    result = []
    for name in names:
        parts = split.get(name, 1)
        if parts <= 1:
            result.append([f"tests.{name}"])
            continue
        ids = _test_ids(name)
        for index in range(parts):
            chunk = ids[index::parts]
            if chunk:
                result.append(chunk)
    return result


def run_module(targets: list[str], python: str, root: Path = ROOT) -> dict:
    """Run unittest on ``targets`` in one process; return its result."""
    name = targets[0].split(".")[1] + (
        f" ({len(targets)} tests)" if len(targets) > 1 else ""
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(root / "src"), environment.get("PYTHONPATH")])
    )
    started = time.monotonic()
    result = subprocess.run(
        [python, "-m", "unittest", *targets],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    ran = _RAN.search(output)
    skipped = _SKIPPED.search(output.splitlines()[-1] if output else "")
    return {
        "name": name,
        "ok": result.returncode == 0 and ran is not None and ran[1] != "0",
        "tests": int(ran.group(1)) if ran else 0,
        "skipped": int(skipped.group(1)) if skipped else 0,
        "seconds": time.monotonic() - started,
        "output": output,
    }


def main(argv=None) -> int:
    """Run the command line."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=os.cpu_count() or 2,
        help="parallel processes (default: the number of CPUs)",
    )
    parser.add_argument(
        "--split",
        type=int,
        help="processes for each split module (default: per module)",
    )
    parser.add_argument(
        "module",
        nargs="*",
        help="only these modules (e.g. test_merge); default: all",
    )
    args = parser.parse_args(argv)
    names = args.module or modules()
    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(0, str(ROOT))
    split = {
        name: parts if args.split is None else args.split
        for name, parts in SPLIT.items()
        if name in names
    }
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        results = list(
            pool.map(
                lambda targets: run_module(targets, sys.executable),
                jobs(names, split),
            )
        )
    failed = [result for result in results if not result["ok"]]
    for result in failed:
        print(f"===== {result['name']} FAILED =====")
        print(result["output"].rstrip())
        print()
    slowest = sorted(results, key=lambda result: -result["seconds"])[:3]
    print(
        "Slowest: "
        + ", ".join(f"{r['name']} {r['seconds']:.1f}s" for r in slowest)
    )
    total = sum(result["tests"] for result in results)
    skipped = sum(result["skipped"] for result in results)
    print(
        f"Ran {total} tests in {len(results)} processes in "
        f"{time.monotonic() - started:.1f}s with {args.jobs} jobs"
        + (f" (skipped={skipped})" if skipped else "")
    )
    if failed:
        print(f"FAILED: {', '.join(result['name'] for result in failed)}")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
