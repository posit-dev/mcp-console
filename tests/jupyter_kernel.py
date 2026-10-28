#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["jupyter-client>=8,<9", "matplotlib", "duckdb", "psutil>=7,<8"]
# ///
"""Public Jupyter acceptance checks; no Console-specific client messages.

Run after building: uv run tests/jupyter_kernel.py --binary target/release/mcp-console
Use --language python to run just the Python protocol/lifecycle checks.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from queue import Empty
from textwrap import dedent as code
from typing import Any

import jupyter_client
import psutil
from jupyter_client import BlockingKernelClient
from jupyter_client.connect import write_connection_file


class Kernel:
    def __init__(
        self,
        binary: Path,
        language: str,
        root: Path,
        configuration: dict[str, Any] | None = None,
    ) -> None:
        self.root = root
        self.language = language
        self.connection = root / "connection.json"
        write_connection_file(str(self.connection), key=b"console-jupyter-test")
        config = root / "console.json"
        config.write_text(
            json.dumps(
                {
                    "cache": "host",
                    "python": sys.executable,
                    "r": {"packages": [], "vanilla": True},
                }
                | (configuration or {})
            )
        )
        self.log = (root / "kernel.log").open("w+")
        self.process = subprocess.Popen(
            [
                str(binary),
                "kernel",
                "--jupyter-python",
                sys.executable,
                "--language",
                language,
                "--connection-file",
                str(self.connection),
                "--config-file",
                str(config),
                "--no-sandbox",
            ],
            cwd=root,
            env=os.environ | {"MCP_CONSOLE_HOME": str(root / "console-home")},
            stdin=subprocess.DEVNULL,
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        self.client = self.connect()

    def connect(self, *, wait_ready: bool = True) -> BlockingKernelClient:
        client = BlockingKernelClient(connection_file=str(self.connection))
        client.load_connection_file()
        client.start_channels()
        try:
            if wait_ready:
                client.wait_for_ready(timeout=30)
        except BaseException:
            client.stop_channels()
            self.close()
            raise
        return client

    def close(self) -> None:
        if hasattr(self, "client"):
            self.client.stop_channels()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.log.seek(0)
        log = self.log.read()
        self.log.close()
        if log and self.process.returncode:
            print(log, file=sys.stderr)

    def reply(self, request: str, *, control: bool = False) -> dict[str, Any]:
        receive = self.client.get_control_msg if control else self.client.get_shell_msg
        deadline = time.monotonic() + 120
        while True:
            message = receive(timeout=max(0, deadline - time.monotonic()))
            if message["parent_header"].get("msg_id") == request:
                return message

    def collect(self, request: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        messages = []
        deadline = time.monotonic() + 120
        while True:
            message = self.client.get_iopub_msg(
                timeout=max(0, deadline - time.monotonic())
            )
            if message["parent_header"].get("msg_id") != request:
                continue
            messages.append(message)
            if message["msg_type"] == "status" and message["content"] == {
                "execution_state": "idle"
            }:
                break
        assert messages[0]["content"] == {"execution_state": "busy"}, messages
        assert messages[-1]["content"] == {"execution_state": "idle"}, messages
        return self.reply(request)["content"], messages

    def execute(
        self, code: str, **flags: Any
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        request = self.client.execute(code, **flags)
        reply, messages = self.collect(request)
        inputs = [m["content"] for m in messages if m["msg_type"] == "execute_input"]
        if not flags.get("silent", False):
            assert inputs == [
                {"code": code, "execution_count": reply["execution_count"]}
            ], inputs
        return reply, messages

    def control(self, kind: str, content: dict[str, Any]) -> dict[str, Any]:
        message = self.client.session.msg(kind, content)
        self.client.control_channel.send(message)
        return self.reply(message["header"]["msg_id"], control=True)["content"]

    def shutdown(self, *, restart: bool) -> None:
        assert self.control("shutdown_request", {"restart": restart}) == {
            "status": "ok",
            "restart": restart,
        }
        assert self.process.wait(timeout=30) == 0
        journals = list(
            (self.root / "console-home").glob("sessions/*/capture/events.jsonl")
        )
        assert journals, "execution did not create a recording"
        for journal in journals:
            events = [json.loads(line) for line in journal.read_text().splitlines()]
            assert events[-1]["event"] == "recording_ended", events[-3:]


def text(messages: list[dict[str, Any]]) -> str:
    return "".join(
        message["content"]["text"]
        for message in messages
        if message["msg_type"] == "stream"
    )


def results(messages: list[dict[str, Any]]) -> list[str]:
    return [
        message["content"]["data"]["text/plain"]
        for message in messages
        if message["msg_type"] == "execute_result"
    ]


def assert_error(
    reply: dict[str, Any], messages: list[dict[str, Any]], name: str | None = None
) -> None:
    assert reply["status"] == "error", reply
    assert reply["ename"] and isinstance(reply["evalue"], str), reply
    assert isinstance(reply["traceback"], list), reply
    if name is not None:
        assert reply["ename"] == name, reply
    errors = [m["content"] for m in messages if m["msg_type"] == "error"]
    assert errors == [{key: reply[key] for key in ("ename", "evalue", "traceback")}], (
        errors
    )


def python_checks(kernel: Kernel) -> None:
    reply, output = kernel.execute("42")
    assert reply["status"] == "ok" and results(output) == ["42"], (reply, output)
    assert text(output) == "", output
    result = next(m["content"] for m in output if m["msg_type"] == "execute_result")
    assert result["execution_count"] == reply["execution_count"], result
    reply, output = kernel.execute("answer = 6 * 7\nprint(answer)")
    assert reply["status"] == "ok" and text(output) == "42\n", (reply, output)
    count = reply["execution_count"]
    reply, output = kernel.execute("print(answer)", store_history=False)
    assert reply["status"] == "ok" and reply["execution_count"] == count, reply
    assert text(output) == "42\n", output
    reply, output = kernel.execute("print('hidden')", silent=True)
    assert reply["status"] == "ok" and reply["execution_count"] == count, reply
    assert [m["msg_type"] for m in output] == ["status", "status"], output
    reply, output = kernel.execute("raise ValueError('hidden error')", silent=True)
    assert reply["status"] == "error" and reply["ename"] == "ValueError", reply
    assert reply["execution_count"] == count, reply
    assert [m["msg_type"] for m in output] == ["status", "status"], output
    history = kernel.reply(
        kernel.client.history(hist_access_type="tail", n=10, output=False)
    )["content"]
    assert history["status"] == "ok", history
    assert [entry[1:] for entry in history["history"]] == [
        [1, "42"],
        [2, "answer = 6 * 7\nprint(answer)"],
    ], history
    selected = kernel.reply(
        kernel.client.history(hist_access_type="range", session=0, start=2, stop=3)
    )["content"]
    assert (
        selected["status"] == "ok" and selected["history"] == history["history"][1:]
    ), selected
    reply, output = kernel.execute(
        # fmt: python
        code("""
            repr_calls = 0


            class Result:
                def __repr__(self):
                    global repr_calls
                    repr_calls += 1
                    return "value"


            value = Result()
            value
            """)
    )
    assert reply["status"] == "ok" and results(output) == ["value"], (reply, output)
    reply, output = kernel.execute("print(repr_calls, _ is value)")
    assert reply["status"] == "ok" and text(output) == "1 True\n", (reply, output)
    reply, output = kernel.execute(
        "import sys\nsys.displayhook = lambda value: print('custom', value)\n41"
    )
    assert reply["status"] == "ok" and text(output) == "custom 41\n", (reply, output)
    assert results(output) == [], output
    assert kernel.execute("sys.displayhook = sys.__displayhook__")[0]["status"] == "ok"
    reply, output = kernel.execute(
        "import io\nsaved_stdout = sys.stdout\ncapture = io.StringIO()\nsys.stdout = capture\n42"
    )
    assert reply["status"] == "ok" and text(output) == "" and results(output) == [], (
        reply,
        output,
    )
    reply, output = kernel.execute(
        "sys.stdout = saved_stdout\nprint(repr(capture.getvalue()))"
    )
    assert reply["status"] == "ok" and text(output) == "'42\\n'\n", (reply, output)
    reply, output = kernel.execute("import sys\nprint('diagnostic', file=sys.stderr)")
    assert reply["status"] == "ok" and "diagnostic\n" in text(output), (reply, output)
    assert any(m["content"].get("name") == "stderr" for m in output), output
    # These direct file-descriptor writes bypass Python's managed streams and
    # exceed native preview budgets. Jupyter must receive every byte.
    reply, output = kernel.execute(
        # fmt: python
        code("""
            import os

            stdout_payload = b"O" * 20000 + b"\\n"
            stderr_payload = b"E" * 20000 + b"\\n"
            stdout_written = os.write(1, stdout_payload)
            stderr_written = os.write(2, stderr_payload)
            """)
    )
    assert reply["status"] == "ok", reply
    for name, expected in (
        ("stdout", "O" * 20000 + "\n"),
        ("stderr", "E" * 20000 + "\n"),
    ):
        observed = "".join(
            message["content"]["text"]
            for message in output
            if message["msg_type"] == "stream" and message["content"]["name"] == name
        )
        assert observed == expected, (
            name,
            len(observed),
            observed[:100],
            observed[-100:],
        )
    reply, output = kernel.execute("raise ValueError('fixture')")
    assert_error(reply, output, "ValueError")
    assert reply["evalue"] == "fixture", reply

    # A socket gate proves execution is active. Keep it blocked across two
    # adapter observation budgets; only the worker's completion ends a request.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(120)
        request = kernel.client.execute(
            "import socket\n"
            f"with socket.create_connection({listener.getsockname()!r}) as gate:\n"
            "    gate.recv(1)\n"
            "print('released')"
        )
        connection, _ = listener.accept()
        with connection:
            try:
                unexpected = kernel.client.get_shell_msg(timeout=2.2)
            except Empty:
                pass
            else:
                raise AssertionError(f"unfinished execution replied: {unexpected}")
            connection.sendall(b"x")
        reply, output = kernel.collect(request)
        assert reply["status"] == "ok" and text(output) == "released\n", (reply, output)
    reply, output = kernel.execute(
        "import matplotlib.pyplot as plt\nplt.plot([1, 2])\nplt.show()"
    )
    assert reply["status"] == "ok", reply
    images = [
        m["content"]["data"]["image/png"]
        for m in output
        if m["msg_type"] == "display_data"
    ]
    assert images and base64.b64decode(images[0]).startswith(b"\x89PNG\r\n\x1a\n"), (
        output
    )

    request = kernel.client.execute(
        "name = input('name> ')\nempty = input('empty> ')\nprint(repr((name, empty)))",
        allow_stdin=True,
    )
    prompt = kernel.client.get_stdin_msg(timeout=120)
    assert prompt["parent_header"]["msg_id"] == request, prompt
    assert prompt["content"] == {"prompt": "name> ", "password": False}, prompt
    kernel.client.input("café")
    prompt = kernel.client.get_stdin_msg(timeout=120)
    assert prompt["parent_header"]["msg_id"] == request, prompt
    assert prompt["content"] == {"prompt": "empty> ", "password": False}, prompt
    kernel.client.input("")
    reply, output = kernel.collect(request)
    assert reply["status"] == "ok" and "('café', '')\n" in text(output), (reply, output)

    reply, output = kernel.execute("input('forbidden> ')", allow_stdin=False)
    assert_error(reply, output)
    try:
        unexpected = kernel.client.get_stdin_msg(timeout=0)
    except Empty:
        pass
    else:
        raise AssertionError(f"allow_stdin=False sent an input request: {unexpected}")
    reply, output = kernel.execute("print(answer)")
    assert reply["status"] == "ok" and text(output) == "42\n", (reply, output)

    request = kernel.client.execute(
        "print('interrupt-ready', flush=True)\nwhile True:\n    pass"
    )
    prefix = []
    while "interrupt-ready\n" not in text(prefix):
        message = kernel.client.get_iopub_msg(timeout=120)
        if message["parent_header"].get("msg_id") == request:
            prefix.append(message)
    assert kernel.control("interrupt_request", {}) == {"status": "ok"}
    # The busy message was consumed while establishing execution before interrupt.
    reply, tail = collect_after_busy(kernel, request)
    assert_error(reply, prefix + tail)
    reply, output = kernel.execute("print(answer)")
    assert reply["status"] == "ok" and text(output) == "42\n", (reply, output)

    kernel.client.stop_channels()
    kernel.client = kernel.connect()
    reply, output = kernel.execute("print(answer)")
    assert reply["status"] == "ok" and text(output) == "42\n", (reply, output)

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(120)
        request = kernel.client.execute(
            "answer += 1\n"
            f"with socket.create_connection({listener.getsockname()!r}) as gate:\n"
            "    gate.recv(1)\n"
            "print('reconnected')"
        )
        connection, _ = listener.accept()
        with connection:
            kernel.client.stop_channels()
            assert kernel.process.poll() is None
            kernel.client = kernel.connect(wait_ready=False)
            # JEP 92's welcome establishes the new IOPub subscription before
            # releasing execution. Missed earlier output need not be replayed.
            while True:
                message = kernel.client.get_iopub_msg(timeout=30)
                if message["msg_type"] == "iopub_welcome":
                    break
            connection.sendall(b"x")
        output = []
        while True:
            message = kernel.client.get_iopub_msg(timeout=120)
            if message["parent_header"].get("msg_id") != request:
                continue
            output.append(message)
            if message["msg_type"] == "status" and message["content"] == {
                "execution_state": "idle"
            }:
                break
        assert text(output) == "reconnected\n", output
        reply, output = kernel.execute("print(answer)")
        assert reply["status"] == "ok" and text(output) == "43\n", (reply, output)
        assert kernel.execute("answer = 42")[0]["status"] == "ok"

    complete = kernel.reply(kernel.client.complete("ans", cursor_pos=3))["content"]
    assert complete["status"] == "ok" and complete["matches"] == [], complete
    inspect = kernel.reply(kernel.client.inspect("answer", cursor_pos=6))["content"]
    assert inspect["status"] == "ok" and inspect["found"] is False, inspect
    is_complete = kernel.reply(kernel.client.is_complete("if True:"))["content"]
    assert is_complete["status"] == "unknown", is_complete

    reply, _ = kernel.execute("pass", user_expressions={"answer": "answer"})
    assert reply["status"] == "ok", reply
    expression = reply["user_expressions"]["answer"]
    assert (
        expression["status"] == "error" and expression["ename"] == "NotImplementedError"
    ), expression

    for stop_on_error in (True, False):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(120)
            failed = kernel.client.execute(
                "import socket\n"
                f"with socket.create_connection({listener.getsockname()!r}) as gate:\n"
                "    gate.recv(1)\n"
                "raise ValueError('queue fixture')",
                stop_on_error=stop_on_error,
            )
            queued = kernel.client.execute("answer = 42")
            connection, _ = listener.accept()
            with connection:
                connection.sendall(b"x")
            assert_error(*kernel.collect(failed), "ValueError")
            if stop_on_error:
                assert kernel.reply(queued)["content"]["status"] == "aborted"
            else:
                assert kernel.collect(queued)[0]["status"] == "ok"


def collect_after_busy(
    kernel: Kernel, request: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    messages = []
    while True:
        message = kernel.client.get_iopub_msg(timeout=120)
        if message["parent_header"].get("msg_id") != request:
            continue
        messages.append(message)
        if message["msg_type"] == "status" and message["content"] == {
            "execution_state": "idle"
        }:
            return kernel.reply(request)["content"], messages


def startup_input_checks(binary: Path) -> None:
    startup = {
        "startup": {
            "language": "python",
            "code": (
                "import sqlite3\n"
                "input('startup> ')\n"
                "_console.sql_connection(sqlite3.connect(':memory:'))"
            ),
        }
    }
    for allow_stdin in (True, False):
        with tempfile.TemporaryDirectory(
            prefix="console-jupyter-startup-"
        ) as directory:
            kernel = Kernel(binary, "sql", Path(directory), startup)
            try:
                # Connection/readiness completed before interactive preparation.
                request = kernel.client.execute("SELECT 42", allow_stdin=allow_stdin)
                if allow_stdin:
                    prompt = kernel.client.get_stdin_msg(timeout=120)
                    assert prompt["parent_header"]["msg_id"] == request, prompt
                    assert prompt["content"] == {
                        "prompt": "startup> ",
                        "password": False,
                    }, prompt
                    kernel.client.input("continue")
                reply, output = kernel.collect(request)
                if allow_stdin:
                    assert reply["status"] == "ok" and "42" in "".join(
                        results(output)
                    ), (
                        reply,
                        output,
                    )
                else:
                    assert_error(reply, output)
                    try:
                        unexpected = kernel.client.get_stdin_msg(timeout=0)
                    except Empty:
                        pass
                    else:
                        raise AssertionError(f"disabled startup input: {unexpected}")
                kernel.shutdown(restart=False)
            finally:
                kernel.close()


def transport_exit_check(binary: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="console-jupyter-eof-") as directory:
        root = Path(directory)
        kernel = Kernel(binary, "python", root)
        try:
            reply, output = kernel.execute("import os\nprint(os.getpid())")
            assert reply["status"] == "ok", reply
            worker_pid = int(text(output).strip())
            owned = psutil.Process(kernel.process.pid).children(recursive=True)
            assert any(process.pid == worker_pid for process in owned), owned
            # Terminating the public transport closes its private controller
            # pipes. It must not leave the controller or workload running.
            kernel.process.terminate()
            kernel.process.wait(timeout=15)
            _, alive = psutil.wait_procs(owned, timeout=30)
            assert not alive, [(process.pid, process.cmdline()) for process in alive]
            journals = list(
                (root / "console-home").glob("sessions/*/capture/events.jsonl")
            )
            assert len(journals) == 1, journals
            events = [json.loads(line) for line in journals[0].read_text().splitlines()]
            assert events[-1]["event"] == "recording_ended", events[-3:]
        finally:
            kernel.close()


def missing_transport_dependencies(binary: Path) -> None:
    with tempfile.TemporaryDirectory(
        prefix="console-jupyter-dependencies-"
    ) as directory:
        root = Path(directory)
        environment = root / "python"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(environment)],
            check=True,
        )
        python = environment / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )
        connection = root / "connection.json"
        write_connection_file(str(connection))
        result = subprocess.run(
            [
                str(binary),
                "kernel",
                "--language",
                "python",
                "--jupyter-python",
                str(python),
                "--connection-file",
                str(connection),
                "--no-config",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
            env=os.environ | {"MCP_CONSOLE_HOME": str(root / "console-home")},
        )
        assert result.returncode != 0, result
        assert (
            str(python) in result.stderr and "mcp-console[jupyter]" in result.stderr
        ), result.stderr


def run(binary: Path, languages: list[str]) -> None:
    help_result = subprocess.run(
        [binary, "kernel", "--help"], text=True, capture_output=True, check=False
    )
    assert help_result.returncode == 0, help_result.stderr
    missing_transport_dependencies(binary)
    for arguments in (
        ["--connection-file", "unused.json"],
        ["--connection-file", "unused.json", "--language", "javascript"],
    ):
        rejected = subprocess.run(
            [binary, "kernel", *arguments], capture_output=True, check=False
        )
        assert rejected.returncode != 0, rejected
    with tempfile.TemporaryDirectory(prefix="console-jupyter-language-") as directory:
        config = Path(directory) / "console.json"
        config.write_text(json.dumps({"languages": ["r"]}))
        rejected = subprocess.run(
            [
                binary,
                "kernel",
                "--language",
                "python",
                "--connection-file",
                "unused.json",
                "--config-file",
                config,
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
            env=os.environ | {"MCP_CONSOLE_HOME": directory},
        )
        assert rejected.returncode != 0 and "python" in rejected.stderr, rejected
    for language in languages:
        with tempfile.TemporaryDirectory(prefix="console-jupyter-") as directory:
            root = Path(directory)
            kernel = Kernel(binary, language, root)
            try:
                info = kernel.reply(kernel.client.kernel_info())["content"]
                assert info["implementation"] == "mcp-console", info
                assert info["language_info"]["name"].lower() == language, info
                assert info["debugger"] is False, info
                if language == "python":
                    python_checks(kernel)
                else:
                    create, read, fail = (
                        (
                            "answer <- 42",
                            "cat(answer, '\\n', sep = '')",
                            "stop('fixture')",
                        )
                        if language == "r"
                        else (
                            "CREATE TABLE answer AS SELECT 42 AS value",
                            "SELECT value FROM answer",
                            "SELECT * FROM absent_fixture_table",
                        )
                    )
                    assert kernel.execute(create)[0]["status"] == "ok"
                    reply, output = kernel.execute(read)
                    visible = (
                        "".join(results(output)) if language == "sql" else text(output)
                    )
                    assert reply["status"] == "ok" and "42" in visible, (
                        reply,
                        output,
                    )
                    assert_error(*kernel.execute(fail))
                kernel.shutdown(restart=True)
                if language == "python":
                    journals = list(
                        (root / "console-home").glob("sessions/*/capture/events.jsonl")
                    )
                    recorded_input = [
                        event["message"]["data"]
                        for journal in journals
                        for line in journal.read_text().splitlines()
                        if (event := json.loads(line)).get("message", {}).get("op")
                        == "input"
                    ]
                    assert recorded_input == ["café\n", "\n"], recorded_input
            finally:
                kernel.close()
            replacement = Kernel(binary, language, root)
            try:
                missing = {
                    "python": "print(answer)",
                    "r": "answer",
                    "sql": "SELECT * FROM answer",
                }[language]
                assert_error(*replacement.execute(missing))
                pending = None
                if language == "python":
                    pending = replacement.client.execute(
                        "input('shutdown> ')", allow_stdin=True
                    )
                    prompt = replacement.client.get_stdin_msg(timeout=120)
                    assert prompt["parent_header"]["msg_id"] == pending, prompt
                replacement.shutdown(restart=False)
                if pending is not None:
                    assert_error(*replacement.collect(pending))
            finally:
                replacement.close()
            print(
                json.dumps(
                    {
                        "language": language,
                        "status": "passed",
                        "protocol_version": info["protocol_version"],
                        "implementation_version": info["implementation_version"],
                        "jupyter_client": jupyter_client.__version__,
                    }
                )
            )
    if "python" in languages:
        transport_exit_check(binary)
        print(json.dumps({"transport_exit": "passed"}))
    if "sql" in languages:
        startup_input_checks(binary)
        print(json.dumps({"startup_input": "passed"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--language", action="append", choices=("python", "r", "sql"))
    options = parser.parse_args()
    run(options.binary.resolve(), options.language or ["python", "r", "sql"])
