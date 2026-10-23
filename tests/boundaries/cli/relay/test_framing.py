#!/usr/bin/env -S uv run --script

import json
import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.requirements import NATIVE_FIXTURES, POSIX, WORKER, requires
from support.capture import read_lines
from support.checkpoints import FifoCheckpoint
from support.native import LOADER_VARIABLE, build_interposer
from support.capture import read_relay_ready
from support.records import Transcript
from support.suites import run_this_suite


@contextmanager
def framing_relay(
    binary: Path, mode: str, prefix: str = ""
) -> Iterator[tuple[subprocess.Popen[bytes], Path, dict[str, FifoCheckpoint]]]:
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gates = {
            name: FifoCheckpoint.create(root / name)
            for name in ("consumed", "release", "suffix")
        }
        environment = dict(os.environ, TMPDIR=directory, MCP_CONSOLE_HOME=directory)
        if prefix:
            environment.update(
                {
                    LOADER_VARIABLE: str(build_interposer(root, "jsonl_prefix")),
                    "MCP_CONSOLE_TEST_JSONL_PREFIX": prefix,
                    "MCP_CONSOLE_TEST_JSONL_CONSUMED": str(root / "consumed"),
                    "MCP_CONSOLE_TEST_JSONL_RELEASE": str(root / "release"),
                }
            )
        process = subprocess.Popen(
            [
                binary,
                "worker-relay",
                sys.executable,
                fixtures / "relay_worker/framing.py",
                mode,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        try:
            assert read_relay_ready(process.stdout) == {"kind": "ready"}
            yield process, root, gates
        finally:
            holder = root / "holder-pid"
            if holder.exists():
                os.kill(int(holder.read_text()), signal.SIGKILL)
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
            for gate in gates.values():
                gate.close()


def finish(process: subprocess.Popen[bytes], input_: bytes = b"") -> list[dict]:
    output, errors = process.communicate(input_, timeout=5)
    assert process.returncode == 0 and errors == b"", (process.returncode, errors)
    return [json.loads(line) for line in output.splitlines()]


@requires(WORKER, NATIVE_FIXTURES)
def test_fragmented_commands_split_utf8_and_batch_tail(binary: Path) -> Transcript:
    with framing_relay(binary, "commands", '{"kind":"evaluate"') as (
        process,
        root,
        gates,
    ):
        command = json.dumps(
            {"kind": "evaluate", "language": "r", "source": "command 🦀"},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        split = command.index("🦀".encode()) + 1
        process.stdin.write(command[:split])
        process.stdin.flush()
        gates["consumed"].wait("incomplete command read before suffix")
        assert not (root / "dispatched").exists()
        process.stdin.write(command[split:] + b'\r\n{"kind":')
        process.stdin.flush()
        gates["release"].release()
        events = [
            json.loads(line)
            for line in read_lines(process.stdout, 2, "completed command")
        ]
        assert events == [
            {"kind": "console_output", "data": "command 🦀"},
            {"kind": "completed"},
        ]
        assert (root / "dispatched").read_text() == "command 🦀"
        tail = finish(process)
        assert {
            "kind": "fatal",
            "message": "relay stdin closed midway through a frame",
        } in tail, tail
        return events + tail


@requires(WORKER, NATIVE_FIXTURES)
def test_fragmented_semantic_frames_and_retiring_tail(binary: Path) -> Transcript:
    with framing_relay(binary, "fragmented", '{"kind":"console_output"') as (
        process,
        root,
        gates,
    ):
        process.stdin.write(b'{"kind":"stdin","data":"start\\n"}\n')
        process.stdin.flush()
        gates["consumed"].wait("semantic prefix read with a split UTF-8 scalar")
        gates["suffix"].release()
        gates["release"].release()
        events = [
            json.loads(line)
            for line in read_lines(process.stdout, 2, "semantic completion")
        ]
        assert events == [
            {"kind": "console_output", "data": "fragmented 🦀"},
            {"kind": "completed"},
        ]
        tail = finish(process, b'{"kind":"shutdown","grace_millis":1000}\n')
        assert not any(event["kind"] == "fatal" for event in tail), tail
        os.kill(int((root / "holder-pid").read_text()), 0)
        return events + tail


@requires(POSIX)
@requires(WORKER)
def test_unterminated_commands_are_never_dispatched(binary: Path) -> Transcript:
    transcript: Transcript = []
    for command in (
        {"kind": "evaluate", "language": "r", "source": "42"},
        {"kind": "stdin", "data": "x"},
    ):
        with framing_relay(
            binary, "commands" if command["kind"] == "evaluate" else "stdin"
        ) as (process, root, _):
            events = finish(process, json.dumps(command).encode())
            assert {
                "kind": "fatal",
                "message": "relay stdin closed midway through a frame",
            } in events, events
            assert not (root / "dispatched").exists(), (
                "unterminated command was dispatched"
            )
            transcript.append({"command": command, "events": events})
    return transcript


@requires(POSIX)
@requires(WORKER)
def test_malformed_complete_commands_keep_diagnostics(binary: Path) -> Transcript:
    transcript: Transcript = []
    for frame, diagnostic in (
        (b'{"kind":\n', "EOF while parsing a value at line 1 column 8"),
        (b"\r\n", "EOF while parsing a value at line 1 column 0"),
    ):
        with framing_relay(binary, "commands") as (process, root, _):
            events = finish(process, frame)
            assert {
                "kind": "fatal",
                "message": f"relay stdin frame is invalid: {diagnostic}",
            } in events, events
            assert not (root / "dispatched").exists()
            transcript.append({"frame": frame.decode(), "events": events})
    return transcript


@requires(POSIX)
@requires(WORKER)
def test_malformed_complete_frames_keep_diagnostics(binary: Path) -> Transcript:
    transcript: Transcript = []
    for mode, diagnostic in (
        ("malformed", "EOF while parsing a value at line 1 column 8"),
        ("empty", "EOF while parsing a value at line 1 column 0"),
    ):
        with framing_relay(binary, mode) as (process, _, _):
            process.stdin.write(b'{"kind":"stdin","data":"start\\n"}\n')
            process.stdin.flush()
            assert process.wait(timeout=5) == 0
            events = finish(process)
            assert {
                "kind": "fatal",
                "message": f"worker sideband read failed: {diagnostic}",
            } in events, events
            transcript.append({"frame": mode, "events": events})
    return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
