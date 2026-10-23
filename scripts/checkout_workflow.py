"""Shared ownership and run records for commands that mutate a source checkout."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import BinaryIO

LOCKS_ENV = "MCP_CONSOLE_CHECKOUT_LOCKS"
RUN_ENV = "MCP_CONSOLE_VALIDATION_RUN"
GROUP_ENV = "MCP_CONSOLE_VALIDATION_GROUP"
WINDOWS = sys.platform == "win32"
CANCELLATION_SIGNALS = (
    (signal.SIGINT, signal.SIGTERM, signal.SIGBREAK)
    if WINDOWS
    else (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)
)
FAILURE = re.compile(
    r"^((?:client_server|server_relay|relay_worker|cli)/\S+::\S+): failed(?: in .*)?$"
)


@contextmanager
def exclusive(paths: list[Path], label: str, *, wait: bool = False) -> Iterator[None]:
    """Claim ownership, optionally waiting; descendants inherit the active token."""
    # Ownership and cleanup require this process to stay alive. The inherited
    # token admits synchronous children; it is not recovery after owner death.
    inherited = os.environ.get(LOCKS_ENV, "{}")
    tokens = json.loads(inherited)
    owner = ""
    # A nested owner must find its inherited lock before claiming a free one.
    for path in sorted(paths, key=lambda path: str(path) not in tokens):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+") as lock:
            try:
                lock_file(lock, wait=False)
            except BlockingIOError:
                # Windows byte-range locks also exclude diagnostic reads. Keep
                # the owner record after the locked byte, including for nested
                # packaging hooks using the same inherited ownership token.
                lock.seek(1 if WINDOWS else 0)
                # flock decides admission. Its separately published diagnostic
                # can be empty or stale while ownership changes hands.
                owner = lock.read()
                if (
                    str(path) in tokens
                    and owner
                    and json.loads(owner)["token"] == tokens[str(path)]
                ):
                    yield
                    return
                if not wait:
                    continue
                print(f"waiting for {label}", file=sys.stderr, flush=True)
                lock_file(lock, wait=True)
            token = uuid.uuid4().hex
            lock.seek(1 if WINDOWS else 0)
            lock.truncate()
            json.dump({"pid": os.getpid(), "command": sys.argv, "token": token}, lock)
            lock.flush()
            os.environ[LOCKS_ENV] = json.dumps(tokens | {str(path): token})
            try:
                yield
            finally:
                os.environ[LOCKS_ENV] = inherited
            return
    raise SystemExit(
        f"{label} is busy; retry after its owner finishes. Lock: {path}\n"
        f"Last recorded owner (may be stale): {owner or 'not yet recorded'}"
    )


@contextmanager
def checkout_owner(root: Path, *, wait: bool = False) -> Iterator[None]:
    # Installation tests rename target, so ownership must live outside it.
    with exclusive(
        [root.resolve() / ".dev-workflow/checkout.lock"], "checkout", wait=wait
    ):
        yield


def lock_file(lock, *, wait: bool) -> None:
    if WINDOWS:
        from checkout_windows import lock_file as native_lock

        native_lock(lock.fileno(), wait=wait)
    else:
        import fcntl

        fcntl.flock(lock, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))


@contextmanager
def cancellation_blocked() -> Iterator[None]:
    if WINDOWS:
        previous = {
            number: signal.signal(number, signal.SIG_IGN)
            for number in CANCELLATION_SIGNALS
        }
        try:
            yield
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)
    else:
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, CANCELLATION_SIGNALS)
        try:
            yield
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def r_executable(name: str) -> str | None:
    if home := os.environ.get("R_HOME"):
        executable = name + (".exe" if WINDOWS else "")
        candidates = [
            Path(home) / "bin" / executable,
            Path(home) / "bin/x64" / executable,
        ]
        return str(next((path for path in candidates if path.is_file()), candidates[0]))
    return shutil.which(name)


def wait_process(process: subprocess.Popen) -> int:
    if WINDOWS:
        from checkout_windows import wait_process as native_wait

        return native_wait(process)
    return process.wait()


def cache_directory() -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache"))
    if not cache.is_absolute():
        raise SystemExit("XDG_CACHE_HOME must be an absolute path")
    return cache.resolve()


def stop_phase(process: subprocess.Popen, *, owns_group: bool) -> None:
    def deliver(number: int) -> bool:
        try:
            if owns_group:
                os.killpg(process.pid, number)
            else:
                os.kill(process.pid, number)
        except ProcessLookupError:
            return False
        except PermissionError:
            if not owns_group:
                raise
            # macOS can deny signaling a group containing only zombies.
            # Confirm that no live member remains; other denials still fail.
            members = subprocess.check_output(
                ["/bin/ps", "-eo", "pgid=,stat="], text=True, timeout=5
            )
            if any(
                int(group) == process.pid and not state.startswith("Z")
                for group, state in (line.split() for line in members.splitlines())
            ):
                raise
            return False
        return True

    deadline = time.monotonic() + 5
    deliver(signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    if owns_group:
        if not deliver(0):
            return
        # Group membership has no portable wait primitive once the leader exits.
        # Give remaining members the rest of the grace period without polling.
        remaining = deadline - time.monotonic()
        if remaining > 0:
            print(
                "[cleanup] waiting for phase descendants", file=sys.stderr, flush=True
            )
            time.sleep(remaining)
        deliver(signal.SIGKILL)
    elif process.poll() is None:
        deliver(signal.SIGKILL)
    process.wait()


@contextmanager
def command_process(
    command: list[str],
    *,
    log: BinaryIO | None = None,
    environment: dict[str, str] | None = None,
) -> Iterator[subprocess.Popen]:
    """Own command lifetime without intercepting its standard streams."""
    if WINDOWS:
        from checkout_windows import command_process as native_command

        command = [shutil.which(command[0]) or command[0], *command[1:]]
        with ExitStack() as stack:
            process = stack.enter_context(
                native_command(command, log=log, environment=environment)
            )
            try:
                yield process
            finally:
                with cancellation_blocked():
                    stack.close()
        return
    owns_group = os.environ.get(GROUP_ENV) != str(os.getpgrp())
    launch = (
        [sys.executable, str(Path(__file__).resolve()), "phase", *command]
        if owns_group
        else command
    )
    with subprocess.Popen(
        launch,
        stdout=log,
        stderr=None if log is None else subprocess.STDOUT,
        env=environment,
        start_new_session=owns_group,
    ) as process:
        try:
            yield process
        finally:
            # Cleanup is one critical section, including after normal exit.
            # Deliver pending cancellation only after retirement has finished.
            with cancellation_blocked():
                stop_phase(process, owns_group=owns_group)


class Run:
    def __init__(self, root: Path, command: list[str]) -> None:
        runs = root / ".dev-workflow/runs"
        runs.mkdir(parents=True, exist_ok=True)
        self.directory = Path(
            tempfile.mkdtemp(prefix=time.strftime("%Y%m%d-%H%M%S-"), dir=runs)
        )
        self.path = self.directory / "result.json"
        self.started = time.monotonic()
        self.record = {
            "checkout": str(root),
            "command": command,
            "revision": None,
            "worktree_status": None,
            "parent_record": os.environ.get(RUN_ENV),
            "phases": [],
            "failing_selectors": [],
            "exit_status": None,
        }
        self.save()

    def capture_checkout(self) -> None:
        root = Path(self.record["checkout"])
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=root,
            capture_output=True,
            text=True,
        )
        revision = status = None
        if top.returncode == 0 and Path(top.stdout.strip()).resolve() == root.resolve():
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True
            )
            changes = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=root,
                capture_output=True,
                text=True,
            )
            revision = head.stdout.strip() if head.returncode == 0 else None
            status = changes.stdout if changes.returncode == 0 else None
        self.record.update(revision=revision, worktree_status=status)
        self.save()

    def save(self) -> None:
        self.record["elapsed_seconds"] = time.monotonic() - self.started
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.record, indent=2) + "\n")
        for attempt in range(8):
            try:
                temporary.replace(self.path)
                break
            except PermissionError as error:
                if not WINDOWS or error.winerror not in {5, 32} or attempt == 7:
                    raise
                # Windows readers may briefly exclude deletion. There is no
                # notification when such a handle closes; bound the retries
                # and keep the preceding complete record until replacement.
                time.sleep(0.01 * 2**attempt)

    def phase(self, name: str, command: list[str]) -> int:
        log_path = self.directory / f"{len(self.record['phases']) + 1:02}-{name}.log"
        environment = os.environ | {RUN_ENV: str(self.path)}
        timings = None
        if self.record["command"][0] == "test" and name == "transcripts":
            timings = self.directory / "case-timings.jsonl"
            timings.touch()
            environment["MCP_CONSOLE_TEST_TIMINGS"] = str(timings)
            if WINDOWS:
                environment["PYTHONUTF8"] = "1"
        started = time.monotonic()
        status = 1
        process = None
        print(
            f"[{name}] {' '.join(command)}\nLog: {log_path}",
            file=sys.stderr,
            flush=True,
        )
        try:
            with (
                log_path.open("wb") as log,
                command_process(command, log=log, environment=environment) as process,
            ):
                status = wait_process(process)
        finally:
            if process is not None:
                status = process.returncode
            # Persist the completed phase before scanning or replaying its log.
            # Cancelling diagnostics must not erase the underlying result.
            self.record["phases"].append(
                {
                    "name": name,
                    "command": command,
                    "log": str(log_path),
                    "elapsed_seconds": time.monotonic() - started,
                    "exit_status": status,
                    **({"case_timings": str(timings)} if timings else {}),
                }
            )
            self.save()
            failures = []
            rerun_seen = False
            with log_path.open(errors="replace") as log:
                for line in log:
                    if match := FAILURE.fullmatch(line.strip()):
                        failures.append(match[1])
                    elif line.startswith("rerun: scripts/test "):
                        rerun_seen = True
            self.record["failing_selectors"] = sorted(
                set(self.record["failing_selectors"] + failures)
            )
            self.save()
            if status:
                with log_path.open(errors="replace") as log:
                    for line in log:
                        sys.stderr.write(line)
            if not rerun_seen:
                for case in failures:
                    print(f"rerun: scripts/test {case}", file=sys.stderr)
            print(
                f"[{name}] exit {status}; log: {log_path}", file=sys.stderr, flush=True
            )
        return status


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "check-core", "test", "run", "phase"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    options = parser.parse_args()
    if options.mode in {"check", "check-core"} and options.arguments not in (
        [],
        ["--quick"],
        ["--full"],
    ):
        parser.error(f"{options.mode} accepts --full or --quick (the default)")
    full = options.arguments == ["--full"]
    if options.mode in {"run", "phase"} and not options.arguments:
        parser.error("run requires a command")
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)
    if options.mode == "phase":
        # The new session has its final group identity here, before exec.
        # Nested workflows keep this group so the outer owner can retire it.
        os.environ[GROUP_ENV] = str(os.getpgrp())
        os.execvp(options.arguments[0], options.arguments)
    tooling = [
        ("release-tests", [sys.executable, "tests/release.py"]),
        ("staging-tests", [sys.executable, "tests/staging.py"]),
        ("runner-tests", ["uv", "run", "--script", "tests/transcript_runner.py"]),
        ("workflow-tests", [sys.executable, "tests/workflow.py"]),
        ("format-tests", [sys.executable, "tests/format.py"]),
        ("development-tests", [sys.executable, "tests/development.py"]),
        ("client-tests", ["uv", "run", "--script", "tests/mcp_client.py"]),
    ]
    if WINDOWS:
        tooling = [
            ("workflow-tests", [sys.executable, "tests/windows_workflow.py"]),
            ("format-tests", [sys.executable, "tests/format.py"]),
            ("development-tests", [sys.executable, "tests/development.py"]),
            ("runner-tests", ["uv", "run", "--script", "tests/transcript_runner.py"]),
            ("client-tests", ["uv", "run", "--script", "tests/mcp_client.py"]),
            ("release-tests", [sys.executable, "tests/release.py"]),
        ]
    core = [
        ("runtime-sources", [sys.executable, "scripts/validate_runtime_sources.py"]),
        *(tooling if full else []),
        (
            "architecture",
            [
                sys.executable,
                "tests/architecture.py",
                *([] if full or WINDOWS else ["SandboxProcessBoundaryTests"]),
            ],
        ),
        ("rust-format", ["cargo", "fmt", "--all", "--check"]),
        (
            "clippy",
            [
                "cargo",
                "clippy",
                "--all-targets",
                "--all-features",
                "--",
                "-D",
                "warnings",
            ],
        ),
        ("rust-tests", ["cargo", "test", "--all-targets", "--all-features"]),
    ]
    plans = {
        "check": [
            *(
                []
                if WINDOWS
                else [("stage", [sys.executable, "scripts/stage-sandbox-runner"])]
            ),
            (
                "core",
                [sys.executable, "scripts/check-core", *(["--full"] if full else [])],
            ),
            (
                "native-tests" if WINDOWS else "transcripts",
                [sys.executable, "scripts/test", *(["--full"] if full else [])],
            ),
            *(
                [
                    (
                        "installation",
                        [
                            sys.executable,
                            "tests/windows_install.py"
                            if WINDOWS
                            else "tests/install.py",
                        ],
                    )
                ]
                if full
                else []
            ),
        ],
        "check-core": core,
        "test": [
            *(
                []
                if os.environ.get("MCP_CONSOLE_TEST_BINARY")
                else [
                    (
                        "build",
                        [
                            "cargo",
                            "build",
                            *([] if WINDOWS else ["--release"]),
                            "--target-dir",
                            "target",
                        ],
                    )
                ]
            ),
            (
                "native-tests" if WINDOWS else "transcripts",
                [
                    sys.executable,
                    "tests/windows.py",
                    "-v",
                    *[
                        argument
                        for argument in options.arguments
                        if argument not in {"--full", "--quick"}
                    ],
                ]
                if WINDOWS
                else [
                    "uv",
                    "run",
                    "--script",
                    "tests/boundaries/_run.py",
                    *options.arguments,
                ],
            ),
        ],
    }

    if WINDOWS and options.mode == "test":
        native_arguments = []
        arguments = iter(options.arguments)
        for argument in arguments:
            if argument in {"--jobs", "--timeout"}:
                next(arguments)
            elif argument not in {"--full", "--quick", "--stress", "--update"}:
                native_arguments.append(argument)
        boundary_selection = "--stress" in options.arguments or any(
            "/" in argument for argument in options.arguments
        )
        if boundary_selection:
            plans["test"] = [
                phase for phase in plans["test"] if phase[0] != "native-tests"
            ]
        else:
            plans["test"][-1] = (
                "native-tests",
                [sys.executable, "tests/windows.py", "-v", *native_arguments],
            )
        if boundary_selection or (
            "--full" in options.arguments and not native_arguments
        ):
            plans["test"].append(
                (
                    "transcripts",
                    [
                        "uv",
                        "run",
                        "--python",
                        sys.executable,
                        "--script",
                        "tests/boundaries/_run.py",
                        *options.arguments,
                    ],
                )
            )

    cancelling = False

    def interrupted(number: int, _frame: object) -> None:
        nonlocal cancelling
        if not cancelling:
            cancelling = True
            raise SystemExit(128 + number)

    with ExitStack() as stack:
        stack.enter_context(checkout_owner(root))
        run = None if options.mode == "run" else Run(root, sys.argv[1:])
        status = 1
        try:
            # Arm cancellation only inside the initial record's finalization
            # scope. Git metadata collection can block or be interrupted.
            for number in CANCELLATION_SIGNALS:
                previous = signal.signal(number, interrupted)
                stack.callback(signal.signal, number, previous)
            if run is None:
                with command_process(options.arguments) as process:
                    status = wait_process(process)
                raise SystemExit(128 - status if status < 0 else status)
            run.capture_checkout()
            for name, command in plans[options.mode]:
                status = 1
                status = run.phase(name, command)
                if status < 0:
                    status = 128 - status
                if status:
                    break
        except SystemExit as error:
            status = error.code
            raise
        except KeyboardInterrupt:
            status = 130
            raise
        finally:
            if run is not None:
                # Work and retirement have ended. Freeze their status through
                # publication and process exit; a late signal cannot contradict
                # the completed record. Deliberately do not unblock before exit.
                if WINDOWS:
                    for number in CANCELLATION_SIGNALS:
                        signal.signal(number, signal.SIG_IGN)
                else:
                    signal.pthread_sigmask(signal.SIG_BLOCK, CANCELLATION_SIGNALS)
                run.record["exit_status"] = status
                run.save()
                print(f"Validation record: {run.path}", file=sys.stderr, flush=True)
        raise SystemExit(status)


if __name__ == "__main__":
    main()
