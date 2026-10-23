"""Discover or run native Windows acceptance through the checkout workflow."""

import argparse
import inspect
import os
import subprocess
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))

if "--stress" in sys.argv[1:] or any("/" in argument for argument in sys.argv[1:]):
    raise SystemExit(
        subprocess.call(
            [
                sys.executable,
                str(ROOT / "tests/boundaries/_run.py"),
                "--bootstrap",
                "--build",
                *sys.argv[1:],
            ]
        )
    )

parser = argparse.ArgumentParser(
    prog="scripts/test",
    description="Run native Windows acceptance; boundary selectors also run shared capability-applicable transcripts.",
)
actions = parser.add_mutually_exclusive_group()
actions.add_argument("--list", action="store_true")
actions.add_argument("--locate", metavar="CLASS[.CASE]")
profiles = parser.add_mutually_exclusive_group()
profiles.add_argument(
    "--full",
    action="store_true",
    help="native acceptance plus all applicable shared boundary cases",
)
profiles.add_argument(
    "--quick", action="store_true", help="alias for the Windows default"
)
parser.add_argument("selectors", nargs="*", metavar="CLASS[.CASE]")
parser.add_argument(
    "--jobs",
    type=int,
    help="shared transcript concurrency; use with --full or a boundary selector",
)
parser.add_argument("--timeout", type=float, help="shared case deadline in seconds")
parser.add_argument(
    "--update",
    action="store_true",
    help="update shared snapshots with --full or a boundary selector",
)
options = parser.parse_args()
if options.jobs is not None and options.jobs <= 0:
    parser.error("--jobs must be positive")
if options.timeout is not None and options.timeout <= 0:
    parser.error("--timeout must be positive")
if (options.jobs is not None or options.timeout is not None or options.update) and (
    not options.full or options.selectors
):
    parser.error("shared transcript options require --full or a boundary selector")
if options.locate and options.selectors:
    parser.error("--locate does not accept additional selectors")

import windows

loader = unittest.TestLoader()
selectors = [options.locate] if options.locate else options.selectors
suite = (
    loader.loadTestsFromNames(selectors, windows)
    if selectors
    else loader.loadTestsFromModule(windows)
)
if loader.errors:
    parser.error("\n".join(loader.errors))


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


if options.list or options.locate:
    for case in cases(suite):
        method = getattr(type(case), case._testMethodName)
        label = f"{type(case).__name__}.{case._testMethodName}"
        print(
            f"{label}: {inspect.getsourcefile(method)}:{inspect.getsourcelines(method)[1]}"
            if options.locate
            else label
        )
    if options.list and options.full and not selectors:
        raise SystemExit(
            subprocess.call(
                [
                    sys.executable,
                    str(ROOT / "tests/boundaries/_run.py"),
                    "--bootstrap",
                    "--full",
                    "--list",
                ]
            )
        )
else:
    from checkout_workflow import main

    sys.argv.insert(1, "test")
    main()
