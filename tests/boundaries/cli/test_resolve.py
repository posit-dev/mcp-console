#!/usr/bin/env -S uv run --script

import json
import os
import re
import signal
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.records import Transcript
from support.capture import read_lines
from support.checkpoints import FifoCheckpoint
from support.client import TextReader
from support.events import Events
from support.native import LOADER_VARIABLE, build_interposer
from support.normalization import code
from support.processes import (
    ProcessIdentity,
    capture_process_identity,
    current_process_identity,
    kill_processes,
    stop_process_group,
)
from support.requirements import (
    NATIVE_FIXTURES,
    POSIX,
    PROCESS_EVENTS,
    command,
    requires,
)
from support.suites import run_this_suite


def test_rejects_invalid_preparation_frames(binary: Path) -> Transcript:
    transcript: Transcript = []
    with TemporaryDirectory() as temporary:
        for name, frame, diagnostic in (
            (
                "versioned_open",
                '{"Open":{"mode":"PythonOnly","version":1}}\n',
                "invalid resolver JSON: unknown field `version`, expected `mode` or `installation` at line 1 column 38",
            ),
            (
                "malformed_json",
                '{"Open":}\n',
                "invalid resolver JSON: expected value at line 1 column 9",
            ),
            (
                "unterminated_open",
                '{"Open":{"mode":"PythonOnly"}}',
                "resolver input closed",
            ),
            (
                "oversized_line",
                " " * (1024 * 1024 + 1) + "\n",
                "resolver JSON line exceeds 1 MiB",
            ),
        ):
            result = subprocess.run(
                [binary, "resolve"],
                input=frame,
                text=True,
                capture_output=True,
                timeout=10,
                cwd=temporary,
                env={
                    **os.environ,
                    "MCP_CONSOLE_HOME": str(Path(temporary) / "console"),
                },
            )
            assert result.returncode == 1, result
            assert result.stdout == "", result.stdout
            assert result.stderr == diagnostic + "\n", result.stderr
            transcript.append(
                {
                    "case": name,
                    "input_bytes": len(frame.encode("utf-8")),
                    "exit": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                }
            )
    return transcript


def test_opens_and_closes_with_batched_frames(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        environment = {
            **os.environ,
            "PATH": "",
            "MCP_CONSOLE_HOME": str(Path(temporary) / "console"),
        }
        environment.pop("RETICULATE_PYTHON", None)
        frames = '{"Open":{"mode":"PythonOnly"}}\n"Close"\n'
        process = subprocess.Popen(
            [binary, "resolve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            cwd=temporary,
        )
        try:
            assert process.stdin is not None
            assert process.stdout is not None and process.stderr is not None
            process.stdin.write(frames)
            process.stdin.flush()
            # Keep stdin open: Close must survive a read alongside Open.
            assert process.wait(timeout=10) == 0
            messages = [json.loads(line) for line in process.stdout]
            assert len(messages) == 3, messages
            assert messages[0] == "Hello" and messages[2] == "Closed", messages
            assert messages[1]["Completed"]["id"] == 0, messages
            assert messages[1]["Completed"]["confirmed"] is True, messages
            errors = process.stderr.read()
            assert errors == "", errors
            return [{"input": frames, "stdout": messages, "stderr": errors, "exit": 0}]
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


@requires(command("rustc"))
def test_resolves_python_version_over_json(binary: Path) -> Transcript:
    with TemporaryDirectory(prefix="console resolver ") as temporary:
        uv = Path(temporary) / ("uv.exe" if os.name == "nt" else "uv")
        subprocess.run(
            [
                "rustc",
                "--edition=2024",
                str(
                    Path(__file__).resolve().parents[2]
                    / "fixtures/resolver/uv_inventory.rs"
                ),
                "-o",
                str(uv),
            ],
            check=True,
        )
        process = subprocess.Popen(
            [binary, "resolve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={
                **os.environ,
                "PATH": str(uv.parent),
                "MCP_CONSOLE_HOME": str(uv.parent / "console-home"),
                "MCP_CONSOLE_TEST_RESOLVED_PYTHON": sys.executable,
            },
            cwd=temporary,
        )
        assert process.stdin is not None
        assert process.stdout is not None

        def send(message: object) -> None:
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()

        def receive() -> object:
            return json.loads(process.stdout.readline())

        try:
            send({"Open": {"mode": "PythonOnly"}})
            hello = receive()
            discovery = receive()
            assert hello == "Hello", hello
            assert discovery["Completed"]["id"] == 0, discovery
            assert discovery["Completed"]["confirmed"] is True, discovery
            send(
                {
                    "Run": {
                        "id": 1,
                        "operation": {"PythonVersion": {"constraints": [">=3.12"]}},
                    }
                }
            )
            resolved = receive()
            assert resolved["Completed"]["result"] == {"Ok": "3.12.7"}, resolved
            assert resolved["Completed"]["confirmed"] is True, resolved
            manifest = {
                "packages": ["six>=1"],
                "python_version": [">=3.12"],
                "exclude_newer": "2026-01-01",
            }
            send(
                {
                    "Run": {
                        "id": 2,
                        "operation": {"Python": {"requirements": manifest, "r": None}},
                    }
                }
            )
            prepared = receive()
            assert prepared["Completed"]["result"] == {
                "Ok": {"python": sys.executable, "requirements": manifest}
            }, prepared
            assert prepared["Completed"]["confirmed"] is True, prepared
            send("Close")
            assert receive() == "Closed"
            process.stdin.close()
            assert process.wait(timeout=10) == 0, process.stderr.read()
            assert process.stderr.read() == ""
            help_text = subprocess.run(
                [binary, "--help"], capture_output=True, text=True, check=True
            ).stdout
            assert "  resolve" not in help_text
            return [
                {
                    "hidden_command": "resolve",
                    "resolved_python": "3.12.7",
                    "prepared_manifest": prepared["Completed"]["result"]["Ok"][
                        "requirements"
                    ],
                }
            ]
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)


@requires(POSIX, NATIVE_FIXTURES, PROCESS_EVENTS)
def test_later_stage_failure_does_not_inherit_earlier_control(
    binary: Path,
) -> Transcript:
    with (
        TemporaryDirectory() as temporary,
        closing(FifoCheckpoint.create(Path(temporary) / "started")) as started,
    ):
        root = Path(temporary)
        r_home = root / "r"
        (r_home / "bin").mkdir(parents=True)
        ir = root / "ir"
        ir.write_text("#!/bin/sh\nprintf 'ir 0.4.0\\n'\n")
        ir.chmod(0o755)
        uv = root / "resolved-uv"
        uv.write_text(
            "#!/bin/sh\nprintf 'independent inventory failure\\n' >&2\nexit 23\n"
        )
        uv.chmod(0o755)
        rscript = r_home / "bin/Rscript"
        rscript.write_text(
            f"#!{sys.executable}\n"
            # fmt: python
            + code("""
                import os
                import signal

                signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
                with open(os.environ["MCP_CONSOLE_TEST_STAGE_PID"], "w") as identity:
                    identity.write(str(os.getpid()))
                with open(os.environ["MCP_CONSOLE_TEST_STAGE_STARTED"], "w") as checkpoint:
                    checkpoint.write("1")
                assert signal.sigwait({signal.SIGINT}) == signal.SIGINT
                print(os.environ["MCP_CONSOLE_TEST_STAGE_UV"])
                """),
        )
        rscript.chmod(0o755)
        environment = {
            **os.environ,
            "PATH": str(root),
            "R_HOME": str(r_home),
            "MCP_CONSOLE_HOME": str(root / "console"),
            "RETICULATE_PYTHON": "managed",
            "MCP_CONSOLE_TEST_STAGE_STARTED": str(started.path),
            "MCP_CONSOLE_TEST_STAGE_UV": str(uv),
            "MCP_CONSOLE_TEST_STAGE_PID": str(root / "stage-pid"),
            LOADER_VARIABLE: str(build_interposer(root, "preparation_stage_control")),
        }
        environment.pop("RETICULATE_UV", None)
        process = subprocess.Popen(
            [binary, "resolve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            cwd=root,
            start_new_session=True,
        )
        assert process.stdin is not None and process.stdout is not None
        reader = TextReader(process.stdout)
        messages: list[object] = []
        identities: list[ProcessIdentity] = []

        def send(message: object) -> None:
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()

        def receive() -> object:
            message = json.loads(reader.readline(timeout=10))
            messages.append(message)
            return message

        try:
            send({"Open": {"mode": "R"}})
            assert receive() == "Hello"
            discovery = receive()["Completed"]
            assert discovery["confirmed"] and "Ok" in discovery["result"], discovery
            send({"Run": {"id": 1, "operation": "Bootstrap"}})
            assert receive()["Completed"]["result"] == {"Ok": None}
            send(
                {
                    "Run": {
                        "id": 2,
                        "operation": {
                            "PythonVersion": {
                                "constraints": [],
                                "r": {
                                    "library": str(root),
                                    "r_libs": {"Unix": list(os.fsencode(root))},
                                    "requirements": [],
                                },
                            }
                        },
                    }
                }
            )
            started.wait("uv bootstrap is ready to consume interrupt")
            identities.append(
                capture_process_identity(int((root / "stage-pid").read_text()))
            )
            send({"Control": {"id": 2, "control": "Interrupted"}})
            assert receive() == {"Controlled": {"id": 2, "result": {"Ok": True}}}
            completed = receive()["Completed"]
            assert completed == {
                "id": 2,
                "result": {
                    "Err": "managed Python version resolution failed with exit status: 23: independent inventory failure"
                },
                "control": None,
                "confirmed": True,
            }, completed
            send("Close")
            assert receive() == "Closed"
            process.stdin.close()
            assert process.wait(timeout=10) == 0
            assert process.stderr.read() == ""
            discovery["result"]["Ok"]["selections"]["r_home"] = "<fixture R home>"
            discovery["result"]["Ok"]["local_r_home_bytes"] = "<fixture R home bytes>"
            return [{"messages": messages, "stderr": "", "exit": 0}]
        finally:
            # A failed checkpoint can leave the first resolver waiting for its
            # signal before the normal identity capture above.
            if not identities and (root / "stage-pid").exists():
                identity = current_process_identity(
                    int((root / "stage-pid").read_text())
                )
                if identity is not None:
                    identities.append(identity)
            kill_processes(identities)
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
            reader.close()
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()


@requires(NATIVE_FIXTURES)
def test_close_settles_resolver_observation_before_reaping(binary: Path) -> Transcript:
    return observe_resolver(binary, fail=False)


@requires(NATIVE_FIXTURES)
def test_observation_failure_retires_resolver(binary: Path) -> Transcript:
    return observe_resolver(binary, fail=True)


def observe_resolver(binary: Path, *, fail: bool) -> Transcript:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        resolver_pid = root / "resolver-pid"
        checkpoints = {
            name: FifoCheckpoint.create(root / name)
            for name in ("entered", "release", "killed")
        }
        uv = root / "uv"
        uv.write_text(f"#!{sys.executable}\nimport signal\nsignal.pause()\n")
        uv.chmod(0o755)
        environment = {
            **os.environ,
            "PATH": str(root),
            LOADER_VARIABLE: str(build_interposer(root, "child_exit_observation")),
            "MCP_CONSOLE_TEST_OBSERVER_ENTERED": str(root / "entered"),
            "MCP_CONSOLE_TEST_OBSERVER_CANCELLABLE": "1",
            "MCP_CONSOLE_TEST_OBSERVER_PID": str(resolver_pid),
            "MCP_CONSOLE_TEST_OBSERVER_RELEASE": str(root / "release"),
            "MCP_CONSOLE_TEST_CHILD_KILLED": str(root / "killed"),
            "MCP_CONSOLE_TEST_EARLY_REAP": str(root / "early-reap"),
        }
        if fail:
            environment["MCP_CONSOLE_TEST_OBSERVER_FAIL"] = "1"
        process = subprocess.Popen(
            [binary, "resolve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            start_new_session=True,
        )
        try:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(json.dumps({"Open": {"mode": "PythonOnly"}}) + "\n")
            process.stdin.flush()
            opened = [
                json.loads(line)
                for line in read_lines(process.stdout, 2, "preparation open")
            ]
            assert opened[0] == "Hello", opened
            assert opened[1]["Completed"]["confirmed"] is True, opened
            process.stdin.write(
                json.dumps(
                    {
                        "Run": {
                            "id": 1,
                            "operation": {"PythonVersion": {"constraints": [">=3.12"]}},
                        }
                    }
                )
                + "\n"
            )
            process.stdin.flush()
            checkpoints["entered"].wait("resolver observation entered")
            if fail:
                checkpoints["release"].release()
                checkpoints["killed"].wait("failed observation terminates the resolver")
                messages = [
                    json.loads(line)
                    for line in read_lines(process.stdout, 1, "failed completion")
                ]
                process.stdin.write('"Close"\n')
                process.stdin.flush()
                messages.extend(
                    json.loads(line)
                    for line in read_lines(process.stdout, 1, "preparation close")
                )
            else:
                process.stdin.write('"Close"\n')
                process.stdin.flush()
                checkpoints["killed"].wait("close terminates the active resolver")
                checkpoints["release"].release()
                messages = [
                    json.loads(line)
                    for line in read_lines(
                        process.stdout, 2, "cancelled completion and close"
                    )
                ]
            _, stderr = process.communicate(timeout=10)
            assert process.returncode == 0, stderr
            assert stderr == "", stderr
            assert not (root / "early-reap").exists(), (
                "resolver reaped before observation settled"
            )
            assert messages[-1] == "Closed", messages
            completed = next(
                message["Completed"] for message in messages if "Completed" in message
            )
            assert completed["id"] == 1 and completed["confirmed"] is True, completed
            error = "managed Python version resolution cancelled"
            if fail:
                completed["result"]["Err"] = re.sub(
                    r"child process \d+",
                    "child process PID",
                    completed["result"]["Err"].replace(str(uv), "UV"),
                )
                error = "failed to wait for managed Python version resolver `UV`: failed to observe child process PID exit: Input/output error (os error 5)"
            assert completed == {
                "id": 1,
                "result": {"Err": error},
                "control": None if fail else "Cancelled",
                "confirmed": True,
            }, completed
            return [{"messages": messages}]
        finally:
            try:
                # The resolver leads its own group, outside the resolve owner.
                if resolver_pid.exists():
                    group = int(resolver_pid.read_text())
                    with Events() as exits:
                        try:
                            exits.watch_process(group)
                        except ProcessLookupError:
                            pass
                        else:
                            stop_process_group(group)
                            assert exits.wait(10) == {group}, (
                                "resolver outlived fixture cleanup"
                            )
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
                for checkpoint in checkpoints.values():
                    checkpoint.close()


if __name__ == "__main__":
    run_this_suite(__file__)
