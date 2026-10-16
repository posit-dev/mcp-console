# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "py-yaml12>=0.2.0",
#     "joblib",
#     "numpy",
#     "anyio>=4.15.1",
#     "mcp==2.*,>=2.3.0",
#     "anthropic[mcp]>=1.15.0",
#     "chatlas[mcp]>=0.23.0",
#     "openai>=3.31.0",
#     "openai-agents>=0.24.0",
#     "openai-codex>=0.163.0",
# ]
# ///

import argparse
import json
import math
import os
import pickle
import runpy
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, SimpleQueue
from traceback import TracebackException

directory = Path(__file__).resolve().parent
root = directory.parents[1]
sys.path.insert(0, str(root / "tests"))


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        super().error(
            f"{message}\nSee scripts/test --help for selectors and execution modes."
        )


parser = ArgumentParser(
    prog="scripts/test",
    epilog=(
        "Execution modes come from @executions(DIRECT, SANDBOXED) in the suite. "
        "Use execution.serve() for common arguments and SANDBOXED.serve("
        "'--writable-root', str(path)) for sandbox-only policy."
    ),
)
actions = parser.add_mutually_exclusive_group()
actions.add_argument("--list", action="store_true", dest="list_tests")
actions.add_argument("--locate", metavar="SELECTOR")
parser.add_argument("--update", action="store_true")
profiles = parser.add_mutually_exclusive_group()
profiles.add_argument(
    "--quick",
    action="store_true",
    help="alias for the default smoke profile; explicit selectors keep their scope",
)
profiles.add_argument(
    "--full",
    action="store_true",
    help="run all functional capability-applicable cases when no selectors are supplied",
)
profiles.add_argument(
    "--stress",
    action="store_true",
    help="run allocation scale cases; explicit selectors keep their scope",
)
parser.add_argument(
    "--timeout",
    type=float,
    default=600.0,
    help="case deadline in seconds; increase for slow resolver workflows (default: 600)",
)
parser.add_argument("--bootstrap", action="store_true", help=argparse.SUPPRESS)
parser.add_argument("--build", action="store_true", help=argparse.SUPPRESS)
parser.add_argument("--record-case", nargs=3, help=argparse.SUPPRESS)
parser.add_argument("--supervise-case", nargs=4, help=argparse.SUPPRESS)
parser.add_argument(
    "-j",
    "--jobs",
    type=int,
    default=max(2, 2 * (os.cpu_count() or 1)),
    help="concurrent transcript cases (default: %(default)s; twice the logical CPU count, at least 2)",
)
parser.add_argument("selectors", nargs="*", metavar="BOUNDARY/SUITE[::CASE]")


def parse_arguments() -> argparse.Namespace:
    options = parser.parse_args()
    if options.jobs < 1:
        parser.error("--jobs must be at least 1")
    if not math.isfinite(options.timeout) or options.timeout <= 0:
        parser.error("--timeout must be a positive finite number of seconds")
    if options.locate is not None and options.update:
        parser.error("--locate cannot be combined with --update")
    if options.locate is not None and options.selectors:
        parser.error("--locate does not accept additional selectors")
    return options


# Help and syntax errors need only Python's standard library, even offline.
if "--bootstrap" in sys.argv[1:]:
    parse_arguments()
    arguments = sys.argv[1:]
    arguments.remove("--bootstrap")
    command = [
        "uv",
        "run",
        *(["--python", sys.executable] if os.name == "nt" else []),
        "--script",
        __file__,
        *arguments,
    ]
    if os.name == "nt":
        raise SystemExit(subprocess.call(command, env=os.environ | {"PYTHONUTF8": "1"}))
    os.execvp("uv", command)


from support.cases import (
    CaseCancelled,
    CaseProcess,
    run_case_subprocess,
    supervise_case,
)
from support.execution import Execution
from support.records import Transcript, TranscriptWithCompanions
from support.requirements import missing_reasons
from support.snapshots import (
    check_recording,
    initialization_case,
    initialization_suite,
    snapshot_directory,
    snapshot_path,
)

binary = Path(
    os.environ.get(
        "MCP_CONSOLE_TEST_BINARY",
        root
        / "target"
        / ("debug/mcp-console.exe" if os.name == "nt" else "release/mcp-console"),
    )
).resolve()
boundaries = {"client_server", "server_relay", "relay_worker", "cli"}
suite_paths = sorted(
    path
    for path in directory.rglob("*.py")
    if not any(part.startswith("_") for part in path.relative_to(directory).parts)
)
SLOW_TEST_SECONDS = 60.0
FREQUENT_STATUS_SECONDS = 120.0
FREQUENT_STATUS_UNTIL_SECONDS = 600.0
LATER_STATUS_SECONDS = 300.0


RecordedTranscript = Transcript | TranscriptWithCompanions
TranscriptCase = Callable[..., RecordedTranscript]


def suite_identifier(suite_path: Path) -> str:
    relative = suite_path.relative_to(directory).with_suffix("")
    assert len(relative.parts) >= 2 and relative.parts[0] in boundaries, (
        f"{suite_path.relative_to(root)} is not under a test boundary"
    )
    return relative.as_posix()


def load_suite(
    suite_path: Path,
) -> dict[str, TranscriptCase]:
    namespace = runpy.run_path(str(suite_path))
    cases = {
        name.removeprefix("test_"): value
        for name, value in namespace.items()
        if name.startswith("test_") and callable(value)
    }
    assert cases, f"{suite_path.relative_to(root)} defines no test_ functions"
    assert "PLATFORMS" not in namespace and "REQUIRED_COMMANDS" not in namespace, (
        f"{suite_path.relative_to(root)}: declare requirements beside cases"
    )
    return cases


def available_executions(
    case: TranscriptCase, selector: str, *, report: bool = False
) -> list[Execution | None]:
    available = []
    for execution in getattr(case, "executions", (None,)):
        requirements = getattr(case, "requirements", ())
        if execution is not None:
            requirements += execution.requirements
        reason = missing_reasons(requirements)
        if reason:
            if report:
                suffix = f"[{execution.name}]" if execution is not None else ""
                print(f"{selector}{suffix}: skipped; {reason}", flush=True)
        else:
            available.append(execution)
    return available


def orphan_snapshots(suites: dict[str, Path]) -> list[Path]:
    cases_by_suite: dict[str, tuple[str, ...]] = {}
    orphans = []
    for snapshot in sorted(snapshot_directory.rglob("*")):
        if not snapshot.is_file() or snapshot.suffix not in {".yaml", ".md", ".qmd"}:
            continue
        suite_name = snapshot.parent.relative_to(snapshot_directory).as_posix()
        if suite_name not in suites:
            orphans.append(snapshot)
            continue
        if suite_name not in cases_by_suite:
            cases = load_suite(suites[suite_name])
            cases_by_suite[suite_name] = tuple(f"{case_name}." for case_name in cases)
        if not snapshot.name.startswith(cases_by_suite[suite_name]):
            orphans.append(snapshot)
    return orphans


def locate(suites: dict[str, Path], selector: str) -> None:
    suite_name, separator, case_name = selector.partition("::")
    if suite_name not in suites:
        parser.error(f"unknown transcript suite: {suite_name}")

    suite_path = suites[suite_name]
    cases = load_suite(suite_path)
    if separator:
        if case_name not in cases:
            parser.error(f"unknown transcript case in {suite_name}: {case_name}")
        case_names = [case_name]
    else:
        case_names = list(cases)

    source = suite_path.relative_to(root)
    for case_name in case_names:
        line = cases[case_name].__code__.co_firstlineno
        snapshot = snapshot_path(suite_name, case_name).relative_to(root)
        print(f"{suite_name}::{case_name}")
        print(f"  source: {source}:{line}")
        print(f"  snapshot: {snapshot}")


def record_case(suite_path: Path, case_name: str, *, update: bool) -> set[Path]:
    os.environ["MCP_CONSOLE_TEST_PYTHON"] = sys.executable
    case = load_suite(suite_path)[case_name]
    suite_name = suite_identifier(suite_path)
    modes = available_executions(case, f"{suite_name}::{case_name}")
    assert modes, "selected case has no available execution"
    checked = set()
    initialization = (suite_name, case_name) == (
        initialization_suite,
        initialization_case,
    )
    for index, execution in enumerate(modes):
        started = time.monotonic()
        status = "failed"
        try:
            recorded = case(binary) if execution is None else case(binary, execution)
            mode_snapshots = check_recording(
                suite_name,
                case_name,
                recorded,
                update=update
                and (
                    index == 0
                    or initialization
                    or getattr(case, "execution_snapshots", False)
                ),
                execution=execution.name if execution is not None else None,
                platform_specific=sys.platform
                in getattr(case, "snapshot_platforms", ()),
                execution_specific=getattr(case, "execution_snapshots", False),
            )
            assert (
                initialization
                or index == 0
                or getattr(case, "execution_snapshots", False)
                or mode_snapshots == checked
            ), "execution modes produced different companion snapshots"
            checked.update(mode_snapshots)
            status = "passed"
        except BaseException as error:
            if execution is not None:
                error.add_note(f"execution mode: {execution.name}")
            raise
        finally:
            if timing_path := os.environ.get("MCP_CONSOLE_TEST_TIMINGS"):
                timing = {
                    "selector": f"{suite_name}::{case_name}",
                    "execution": execution.name if execution is not None else None,
                    "status": status,
                    "elapsed_seconds": time.monotonic() - started,
                }
                # One append write per record; parallel cases share the file.
                with open(timing_path, "ab", buffering=0) as output:
                    output.write((json.dumps(timing) + "\n").encode())

    return checked


def format_duration(elapsed: float) -> str:
    seconds = max(0, int(elapsed))
    minutes, seconds = divmod(seconds, 60)
    if minutes == 0:
        return f"{seconds}s"
    if seconds == 0:
        return f"{minutes}m"
    return f"{minutes}m {seconds}s"


def next_status_after(elapsed: float) -> float:
    if elapsed < FREQUENT_STATUS_UNTIL_SECONDS:
        next_status = (int(elapsed // FREQUENT_STATUS_SECONDS) + 1) * (
            FREQUENT_STATUS_SECONDS
        )
        return max(FREQUENT_STATUS_SECONDS, next_status)
    later_elapsed = elapsed - FREQUENT_STATUS_UNTIL_SECONDS
    return (
        FREQUENT_STATUS_UNTIL_SECONDS
        + (int(later_elapsed // LATER_STATUS_SECONDS) + 1) * LATER_STATUS_SECONDS
    )


@dataclass
class RunningCase:
    selector: str
    started_at: float
    next_status_at: float = SLOW_TEST_SECONDS
    reported: bool = False


class ProgressReporter:
    def __init__(
        self,
        *,
        update: bool,
        full_update: bool,
        jobs: int,
        timeout: float,
    ) -> None:
        self.update = update
        self.full_update = full_update
        self.rerun = ["scripts/test"]
        if full_update:
            self.rerun.append("--full")
        if update:
            self.rerun.append("--update")
        if full_update and jobs != parser.get_default("jobs"):
            self.rerun += ["--jobs", str(jobs)]
        if timeout != parser.get_default("timeout"):
            self.rerun += ["--timeout", str(timeout)]
        self.running: dict[int, RunningCase] = {}
        self.progress_line_open = False

    def start(self, index: int, selector: str, started_at: float) -> None:
        assert index not in self.running, index
        self.running[index] = RunningCase(selector, started_at)

    def is_running(self, index: int) -> bool:
        return index in self.running

    def report_due(self) -> None:
        now = time.monotonic()
        for running in self.running.values():
            elapsed = now - running.started_at
            if elapsed < running.next_status_at:
                continue
            self._line(f"{running.selector}: running for {format_duration(elapsed)}")
            running.reported = True
            running.next_status_at = next_status_after(elapsed)

    def finish(
        self,
        index: int,
        *,
        succeeded: bool,
    ) -> None:
        running = self.running.pop(index)
        elapsed = time.monotonic() - running.started_at
        if succeeded:
            if running.reported or elapsed >= SLOW_TEST_SECONDS:
                self._line(
                    f"{running.selector}: finished in {format_duration(elapsed)}"
                )
            if not self.update:
                self._dot()
        elif running.reported or elapsed >= SLOW_TEST_SECONDS:
            self._line(
                f"{running.selector}: failed in {format_duration(elapsed)}",
                error=True,
            )
        else:
            self._line(f"{running.selector}: failed", error=True)
        if not succeeded:
            rerun = self.rerun if self.full_update else [*self.rerun, running.selector]
            self._line(f"rerun: {shlex.join(rerun)}", error=True)

    def cancel(self, index: int, diagnostics: str) -> None:
        running = self.running.pop(index)
        self._line(f"{running.selector}: cancelled", error=True)
        if diagnostics:
            print(diagnostics, end="", file=sys.stderr, flush=True)

    def close(self) -> None:
        if self.progress_line_open:
            print(flush=True)
            self.progress_line_open = False

    def _dot(self) -> None:
        print(".", end="", flush=True)
        self.progress_line_open = True

    def _line(self, message: str, *, error: bool = False) -> None:
        self.close()
        print(message, file=sys.stderr if error else sys.stdout, flush=True)


def selected_cases(
    suites: dict[str, Path],
    selectors: Sequence[str],
    *,
    report: bool = True,
    check_requirements: bool = True,
) -> list[tuple[str, str, Path]]:
    selected_suites: dict[str, list[str] | None] = {}
    if selectors:
        for selector in selectors:
            suite_name, separator, case_name = selector.partition("::")
            if suite_name not in suites:
                parser.error(f"unknown transcript suite: {suite_name}")

            if not separator:
                selected_suites[suite_name] = None
            elif suite_name not in selected_suites:
                selected_suites[suite_name] = [case_name]
            elif selected_suites[suite_name] is not None:
                selected_suites[suite_name].append(case_name)
    else:
        selected_suites = dict.fromkeys(suites)

    selected: list[tuple[str, str, Path]] = []
    for suite_name, selected_case_names in selected_suites.items():
        suite_path = suites[suite_name]
        cases = load_suite(suite_path)

        if selected_case_names is None:
            case_names = list(cases)
        else:
            unknown_cases = [name for name in selected_case_names if name not in cases]
            if unknown_cases:
                parser.error(
                    f"unknown transcript case in {suite_name}: "
                    f"{', '.join(unknown_cases)}"
                )
            case_names = selected_case_names

        selected.extend(
            (suite_name, case_name, suite_path)
            for case_name in case_names
            if not check_requirements
            or available_executions(
                cases[case_name], f"{suite_name}::{case_name}", report=report
            )
        )
    return selected


def prune_stale_snapshots(checked_snapshots: set[Path], orphans: list[Path]) -> None:
    snapshot_root = snapshot_directory
    checked_cases = {
        snapshot.parent / snapshot.name.split(".", 1)[0]
        for snapshot in checked_snapshots
        if snapshot.suffix == ".yaml"
    }
    platform_cases = {
        snapshot.parent / snapshot.name.split(".", 1)[0]
        for snapshot in checked_snapshots
        if sys.platform in snapshot.name.split(".")[1:]
    }
    orphans = set(orphans)
    declared_platforms = {}
    declared_execution_modes = {}
    suites = {}
    for owner in checked_cases:
        suite = owner.parent.relative_to(snapshot_root)
        if suite not in suites:
            suites[suite] = load_suite(
                root / "tests" / "boundaries" / suite.with_suffix(".py")
            )
        case = suites[suite][owner.name]
        declared_platforms[owner] = set(getattr(case, "snapshot_platforms", ()))
        if getattr(case, "execution_snapshots", False):
            declared_execution_modes[owner] = {
                execution.name for execution in getattr(case, "executions", ())
            }

    for snapshot in snapshot_root.rglob("*"):
        if not snapshot.is_file() or snapshot.suffix not in {".yaml", ".md", ".qmd"}:
            continue
        owner = snapshot.parent / snapshot.name.split(".", 1)[0]
        other_platform = bool(
            (declared_platforms.get(owner, set()) - {sys.platform})
            & set(snapshot.name.split(".")[1:])
        )
        shared_reference = (
            owner in platform_cases and sys.platform not in snapshot.name.split(".")[1:]
        )
        unavailable_initialization_mode = owner == snapshot_path(
            initialization_suite, initialization_case
        ).with_suffix("") and not any(
            ("direct" in snapshot.name.split(".")[1:])
            == ("direct" in checked.name.split(".")[1:])
            for checked in checked_snapshots
            if checked.parent / checked.name.split(".", 1)[0] == owner
        )
        mode = "sandbox" if "sandbox" in snapshot.name.split(".")[1:] else "direct"
        retired_platform = bool(
            ({"darwin", "linux", "win32"} - declared_platforms.get(owner, set()))
            & set(snapshot.name.split(".")[1:])
        )
        unavailable_execution_mode = (
            not retired_platform
            and mode in declared_execution_modes.get(owner, set())
            and not any(
                ("sandbox" in checked.name.split(".")[1:]) == (mode == "sandbox")
                for checked in checked_snapshots
                if checked.parent / checked.name.split(".", 1)[0] == owner
            )
        )
        stale = snapshot in orphans or (
            owner in checked_cases
            and snapshot not in checked_snapshots
            and not other_platform
            and not shared_reference
            and not unavailable_initialization_mode
            and not unavailable_execution_mode
        )

        if stale:
            snapshot.unlink()
            print(f"removed {snapshot.relative_to(root)}", flush=True)

    for path in sorted(snapshot_root.rglob("*"), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def run_cases(
    selected: list[tuple[str, str, Path]],
    *,
    jobs: int,
    timeout: float,
    update: bool,
    checked_snapshots: set[Path],
    reporter: ProgressReporter,
    initialize_first: bool,
) -> None:
    if not selected:
        return

    events: SimpleQueue[tuple[int, float | None, CaseProcess | None]] = SimpleQueue()
    executor = ThreadPoolExecutor(max_workers=min(jobs, len(selected)))
    futures: dict[int, Future[set[Path]]] = {}
    active: dict[int, CaseProcess] = {}
    errors: list[BaseException] = []
    failed_selectors: list[str] = []
    next_index = 0
    stopped = False
    interrupted = False
    # SimpleQueue.put is reentrant: SIGINT can wake the loop without
    # interrupting submission or completion bookkeeping.
    previous_signals = {
        number: signal.signal(
            number, lambda received, _frame: events.put((-received, None, None))
        )
        for number in (
            signal.SIGINT,
            signal.SIGTERM,
            *([signal.SIGBREAK] if os.name == "nt" else [signal.SIGHUP]),
        )
    }

    def interruption(index: int) -> BaseException:
        number = signal.Signals(-index)
        if number == signal.SIGINT:
            return KeyboardInterrupt()
        return RuntimeError(f"transcript runner received {number.name}")

    def submit_ready() -> None:
        nonlocal next_index
        capacity = 1 if initialize_first else jobs
        while not stopped and next_index < len(selected) and len(futures) < capacity:
            index = next_index
            _, case_name, suite_path = selected[index]
            future = executor.submit(
                run_case_subprocess,
                suite_path,
                case_name,
                timeout,
                events,
                index,
                update=update,
            )
            futures[index] = future
            future.add_done_callback(
                lambda _future, index=index: events.put((index, None, None))
            )
            next_index += 1

    try:
        submit_ready()
        while futures:
            try:
                reporter.report_due()
                deadlines = [
                    running.started_at + running.next_status_at
                    for running in reporter.running.values()
                ]
                wait_seconds = (
                    max(0.0, min(deadlines) - time.monotonic()) if deadlines else None
                )
                index, started_at, case = events.get(timeout=wait_seconds)
                if index < 0:
                    errors.append(interruption(index))
                    stopped = interrupted = True
                    for future in futures.values():
                        future.cancel()
                    for active_case in active.values():
                        active_case.interrupt()
                    continue
                suite_name, case_name, _ = selected[index]
                if started_at is not None:
                    assert case is not None
                    active[index] = case
                    reporter.start(index, f"{suite_name}::{case_name}", started_at)
                    if interrupted:
                        case.interrupt()
                    continue

                future = futures.pop(index)
                if future.cancelled():
                    continue
                if not reporter.is_running(index):
                    # Process launch can fail before the start event is sent.
                    reporter.start(
                        index, f"{suite_name}::{case_name}", time.monotonic()
                    )
                try:
                    checked = future.result()
                except CaseCancelled as cancelled:
                    reporter.cancel(index, str(cancelled))
                except BaseException as error:
                    reporter.finish(index, succeeded=False)
                    errors.append(error)
                    failed_selectors.append(f"{suite_name}::{case_name}")
                    if not stopped and len(failed_selectors) * 100 > len(selected) * 15:
                        stopped = True
                        reporter.close()
                        print(
                            f"Stopping new cases: {len(failed_selectors)} of "
                            f"{len(selected)} transcript cases failed (more than 15%); "
                            "finishing running cases.",
                            file=sys.stderr,
                            flush=True,
                        )
                else:
                    checked_snapshots.update(checked)
                    reporter.finish(index, succeeded=True)
                active.pop(index, None)
                initialize_first = False
                submit_ready()
            except Empty:
                continue
    finally:
        executor.shutdown(cancel_futures=True)
        for number, handler in previous_signals.items():
            signal.signal(number, handler)

    # A signal can arrive while reporting the final completion. All controllers
    # have joined, so any remaining event was queued by our signal handler.
    while True:
        try:
            index, _, _ = events.get_nowait()
        except Empty:
            break
        assert index < 0, index
        errors.append(interruption(index))

    if failed_selectors:
        reporter.close()
        print(
            f"{len(failed_selectors)} of {len(selected)} transcript cases failed; "
            f"not started: {len(selected) - next_index}",
            file=sys.stderr,
        )
        for selector in failed_selectors:
            print(f"  {selector}", file=sys.stderr)
    if len(errors) > 1:
        raise BaseExceptionGroup("multiple transcript cases failed", errors) from None
    if errors:
        raise errors[0]


def main() -> None:
    options = parse_arguments()
    if options.supervise_case is not None:
        suite_path, case_name, output_path, owner = options.supervise_case
        status = supervise_case(
            Path(suite_path),
            case_name,
            Path(output_path),
            int(owner),
            update=options.update,
        )
        if status < 0:
            number = -status
            if number != signal.SIGKILL:
                signal.signal(number, signal.SIG_DFL)
            os.kill(os.getpid(), number)
        raise SystemExit(status)
    if options.record_case is not None:
        if os.name == "nt":
            signal.signal(signal.SIGBREAK, signal.default_int_handler)
        suite_path, case_name, output_path = options.record_case
        checked = record_case(Path(suite_path), case_name, update=options.update)
        with Path(output_path).open("wb") as output:
            pickle.dump(checked, output)
        return

    assert suite_paths, "no transcript suites found"

    suites = {suite_identifier(path): path for path in suite_paths}
    # The global audit imports every suite and its capability probes.
    # Keep smoke and focused runs confined to their selected suites.
    full_selection = options.full and not options.selectors and options.locate is None
    orphans = orphan_snapshots(suites) if full_selection else []
    full_update = options.update and full_selection and not options.list_tests
    if orphans and not full_update:
        for orphan in orphans:
            print(f"orphan snapshot: {orphan.relative_to(root)}", file=sys.stderr)
        raise SystemExit("run scripts/test --full --update to remove orphan snapshots")

    if options.locate is not None:
        locate(suites, options.locate)
        return

    selectors = options.selectors
    if not selectors:
        from _profiles import SMOKE, STRESS

        if options.stress:
            selectors = STRESS
        elif not options.full:
            selectors = SMOKE
    selected = selected_cases(
        suites,
        selectors,
        report=not options.build,
        check_requirements=not options.list_tests,
    )
    if options.full and not options.selectors:
        selected = [
            (suite, case, path)
            for suite, case, path in selected
            if f"{suite}::{case}" not in STRESS
        ]
    if options.list_tests:
        for suite_name, case_name, _ in selected:
            print(f"{suite_name}::{case_name}")
        return
    if options.build:
        arguments = sys.argv[1:]
        arguments.remove("--build")
        # Re-enter through the shared owner only after metadata and arguments
        # are valid. The execution child consumes the prepared release binary.
        if os.name == "nt":
            raise SystemExit(
                subprocess.call(
                    [
                        sys.executable,
                        str(root / "scripts/checkout_workflow.py"),
                        "test",
                        *arguments,
                    ]
                )
            )
        os.execv(
            sys.executable,
            [
                sys.executable,
                str(root / "scripts/checkout_workflow.py"),
                "test",
                *arguments,
            ],
        )
    assert binary.is_file(), f"{binary} is missing; run scripts/test"
    checked_snapshots: set[Path] = set()
    initialize_first = False
    for index, (suite_name, case_name, _) in enumerate(selected):
        if (suite_name, case_name) != (initialization_suite, initialization_case):
            continue
        selected.insert(0, selected.pop(index))
        initialize_first = True
        break

    reporter = ProgressReporter(
        update=options.update,
        full_update=full_update,
        jobs=options.jobs,
        timeout=options.timeout,
    )
    try:
        run_cases(
            selected,
            jobs=options.jobs,
            timeout=options.timeout,
            update=options.update,
            checked_snapshots=checked_snapshots,
            reporter=reporter,
            initialize_first=initialize_first,
        )
        if options.update:
            prune_stale_snapshots(checked_snapshots, orphans)
    finally:
        reporter.close()


if __name__ == "__main__":
    try:
        main()
    except BaseExceptionGroup as error:
        TracebackException.from_exception(
            error, max_group_width=len(error.exceptions)
        ).print()
        raise SystemExit(1) from None
