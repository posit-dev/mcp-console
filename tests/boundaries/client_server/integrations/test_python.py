#!/usr/bin/env -S uv run --script

import asyncio
import base64
import inspect
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "python"))

import mcp_console
from mcp_console import AsyncMCPConsole, MCPConsole
from support.client import server_identity
from support.execution import DIRECT, RUNTIME, SANDBOXED, Execution, executions
from support.r import r_test_environment, reference_plots
from support.normalization import code
from support.records import McpTranscript, Transcript, TranscriptWithCompanions
from support.previews import assert_preview
from support.progress import elapsed_progress, normalize_elapsed, without_elapsed
from support.evidence import compact_text
from support.requirements import POSIX, R, SQL, WORKER, command, requires
from support.resolvers import (
    bare_runtime_environment,
    fake_ir_environment,
    recording_uv_environment,
)
from support.suites import run_this_suite


def adapter_text(value):
    """Unwrap a chatlas ContentToolResult to its text, passing through plain text.

    Cross-adapter comparisons exercise result equivalence; the chatlas
    adapter's ContentToolResult shape is asserted by its dedicated tests.
    """
    from chatlas import ContentToolResult

    if isinstance(value, ContentToolResult):
        value = value.value
    return getattr(value, "text", value)


def options(binary: Path, execution: Execution) -> dict:
    return {
        "command": binary,
        "args": execution.serve("--worker", str(ROOT / "tests/fixtures/zod")),
        "server_parameters": {
            "env": {**os.environ, "MCP_CONSOLE_TEST_PYTHON": sys.executable}
        },
    }


def test_clients_and_adapters_discover_multiple_tools_without_send(
    binary: Path,
) -> Transcript:
    from agents.tool_context import ToolContext

    async def exercise(console) -> dict:
        definitions = console.tools
        names = [tool.name for tool in definitions]
        assert names == ["evaluate", "inspect"], names
        responses = mcp_console.openai.responses_tools(console)
        agents = mcp_console.openai.agents_tools(console)
        anthropic = mcp_console.anthropic.callable_tools(console)
        chatlas = mcp_console.chatlas.tools(console)
        outputs = []
        for definition, response, agent, claude, chat in zip(
            definitions, responses, agents, anthropic, chatlas, strict=True
        ):
            assert [
                response.definition["name"],
                agent.name,
                claude.to_dict()["name"],
                chat.name,
            ] == [definition.name] * 4
            for description in (
                response.definition["description"],
                agent.description,
                claude.to_dict()["description"],
                chat.schema["function"]["description"],
            ):
                assert description == definition.description
            for schema in (
                response.definition["parameters"],
                agent.params_json_schema,
                claude.to_dict()["input_schema"],
                chat.schema["function"]["parameters"],
            ):
                assert schema == definition.input_schema
            arguments = {"value": None}
            encoded = json.dumps(arguments)
            context = ToolContext(
                context=None,
                tool_name=definition.name,
                tool_call_id="call_1",
                tool_arguments=encoded,
            )
            result = console.call_tool(definition.name, arguments)
            if inspect.isawaitable(result):
                result = await result
            assert result.content[0].text == definition.name
            values = {}
            for adapter, invoke in (
                ("responses", lambda: response.call(arguments)),
                ("agents", lambda: agent.on_invoke_tool(context, encoded)),
                ("anthropic", lambda: claude.call(arguments)),
                ("chatlas", lambda: chat.func(**arguments)),
            ):
                value = invoke()
                values[adapter] = await value if inspect.isawaitable(value) else value
            values = {adapter: adapter_text(value) for adapter, value in values.items()}
            assert values == dict.fromkeys(
                ("responses", "agents", "anthropic", "chatlas"), definition.name
            ), values
            outputs.append({"name": definition.name, "outputs": values})
        try:
            console.get_tool("send")
        except RuntimeError as error:
            assert str(error) == "MCP Console did not expose a tool named 'send'"
        else:
            raise AssertionError("missing send tool was accepted")
        return {"client": type(console).__name__, "tools": outputs}

    async def run() -> Transcript:
        transcript = [
            {
                "fixture": "A paginated MCP peer exposes evaluate and inspect, with no send tool."
            }
        ]
        for client_type in (AsyncMCPConsole, MCPConsole):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                console = client_type(
                    command=sys.executable,
                    args=[
                        "-u",
                        ROOT / "tests/fixtures/python_sync_server.py",
                        "multiple-tools",
                    ],
                    server_parameters={
                        "cwd": root,
                        "env": dict(os.environ, MCP_CONSOLE_HOME=str(root)),
                    },
                )
                if isinstance(console, AsyncMCPConsole):
                    async with console:
                        transcript.append(await exercise(console))
                else:
                    with console:
                        transcript.append(await exercise(console))
                assert (root / "closed").exists()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            async with mcp_console.anthropic.tools(
                command=sys.executable,
                args=[
                    "-u",
                    ROOT / "tests/fixtures/python_sync_server.py",
                    "multiple-tools",
                ],
                server_parameters={
                    "cwd": root,
                    "env": dict(os.environ, MCP_CONSOLE_HOME=str(root)),
                },
            ) as native:
                assert [tool.to_dict()["name"] for tool in native] == [
                    "evaluate",
                    "inspect",
                ]
                results = [await tool.call({"value": None}) for tool in native]
                assert [[item["text"] for item in result] for result in results] == [
                    ["evaluate"],
                    ["inspect"],
                ]
            assert (root / "closed").exists()
            transcript.append({"native_anthropic": ["evaluate", "inspect"]})
        return transcript

    return asyncio.run(run())


def test_clients_record_notes_with_and_without_code(binary: Path) -> Transcript:
    notes = [
        "## Investigation\n\nCheck the observed **total**.",
        "The total is 42; compare it with the expected value next. λ",
    ]

    async def exercise(console) -> list[str]:
        outputs = []
        for arguments in (
            {"python": "print(6 * 7)", "note": notes[0]},
            {"note": notes[1]},
        ):
            output = console.send(**arguments)
            output = await output if inspect.isawaitable(output) else output
            outputs.append(output.text)
        assert outputs == ["42\n", "[done]"], outputs
        return outputs

    async def run() -> Transcript:
        transcript = []
        with tempfile.TemporaryDirectory() as directory:
            for client_type in (AsyncMCPConsole, MCPConsole):
                workspace = Path(directory) / client_type.__name__
                (workspace / ".agents/console").mkdir(parents=True)
                settings = {
                    "command": binary,
                    "args": DIRECT.serve(
                        "--no-config",
                        "-c",
                        "languages=[python]",
                        "-c",
                        "python=" + json.dumps(sys.executable),
                    ),
                    "server_parameters": {
                        "cwd": str(workspace),
                        "env": dict(os.environ, MCP_CONSOLE_HOME=str(workspace)),
                    },
                }
                if client_type is AsyncMCPConsole:
                    async with client_type(**settings) as console:
                        outputs = await exercise(console)
                else:
                    with client_type(**settings) as console:
                        outputs = await exercise(console)
                (recording,) = (workspace / ".agents/console/sessions").iterdir()
                events = [
                    json.loads(line)
                    for line in (recording / "capture/events.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ]
                recorded_notes = [
                    event["request"]["arguments"]["note"]
                    for event in events
                    if event["event"] == "tool_call"
                ]
                assert recorded_notes == notes, recorded_notes
                transcript.append(
                    {
                        "client": client_type.__name__,
                        "outputs": outputs,
                        "notes": recorded_notes,
                    }
                )
        return transcript

    return asyncio.run(run())


def test_clients_preserve_session_selection_and_omission(binary: Path) -> Transcript:
    async def exercise(console) -> None:
        for arguments, expected in (
            ({"python": "named output", "session": "analysis"}, "named output"),
            ({"session": "default"}, ""),
            ({"session": None}, ""),
            ({}, ""),
        ):
            output = console.send(**arguments)
            if inspect.isawaitable(output):
                output = await output
            assert output.text == expected, output

    async def run() -> Transcript:
        results = []
        for client_type in (AsyncMCPConsole, MCPConsole):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                console = client_type(
                    command=sys.executable,
                    args=[
                        "-u",
                        ROOT / "tests/fixtures/python_sync_server.py",
                        "normal",
                    ],
                    server_parameters={
                        "cwd": root,
                        "env": dict(os.environ, MCP_CONSOLE_HOME=str(root)),
                    },
                )
                if isinstance(console, AsyncMCPConsole):
                    async with console:
                        await exercise(console)
                else:
                    with console:
                        await exercise(console)
                requests = [
                    json.loads(line)
                    for line in (root / "requests.jsonl").read_text().splitlines()
                ]
                arguments = [
                    request["params"]["arguments"]
                    for request in requests
                    if request["method"] == "tools/call"
                ]
                assert arguments == [
                    {"python": "named output", "session": "analysis"},
                    {"session": "default"},
                    {},
                    {},
                ], arguments
                results.append({"client": client_type.__name__, "arguments": arguments})
        return results

    return asyncio.run(run())


def test_clients_and_adapters_preserve_hidden_timeout(binary: Path) -> Transcript:
    from agents.tool_context import ToolContext

    async def exercise(console) -> dict:
        tool = console.get_tool("send")
        assert "timeout_ms" not in tool.input_schema["properties"]
        assert "session" not in tool.input_schema["properties"]
        assert tool.description == "Evaluation tool."
        assert (
            tool.input_schema["properties"]["python"]["description"] == "Python cell."
        )
        source = "print(6 * 7)"
        arguments = {"python": source}
        encoded = json.dumps(arguments)
        context = ToolContext(
            context=None,
            tool_name="send",
            tool_call_id="call_1",
            tool_arguments=encoded,
        )
        responses = next(
            tool
            for tool in mcp_console.openai.responses_tools(console)
            if tool.definition["name"] == "send"
        )
        agent = next(
            tool
            for tool in mcp_console.openai.agents_tools(console)
            if tool.name == "send"
        )
        anthropic = next(
            tool
            for tool in mcp_console.anthropic.callable_tools(console)
            if tool.to_dict()["name"] == "send"
        )
        chatlas = next(
            tool for tool in mcp_console.chatlas.tools(console) if tool.name == "send"
        )
        assert (
            responses.definition["description"]
            == agent.description
            == anthropic.to_dict()["description"]
            == chatlas.schema["function"]["description"]
            == tool.description
        )
        for schema in (
            responses.definition["parameters"],
            agent.params_json_schema,
            anthropic.to_dict()["input_schema"],
            chatlas.schema["function"]["parameters"],
        ):
            assert schema == tool.input_schema
        outputs = []
        for invoke in (
            lambda: console.send(**arguments),
            lambda: console(**arguments, timeout_ms=None),
        ):
            output = invoke()
            output = await output if inspect.isawaitable(output) else output
            outputs.append(output.text)
        for invoke in (
            lambda: responses.call(arguments),
            lambda: agent.on_invoke_tool(context, encoded),
            lambda: anthropic.call(arguments),
            lambda: chatlas.func(**arguments),
        ):
            output = invoke()
            outputs.append(await output if inspect.isawaitable(output) else output)
        outputs = [adapter_text(output) for output in outputs]
        assert outputs == ["42\n"] * 6, outputs
        try:
            output = console.send(**arguments, timeout_ms=60_000)
            if inspect.isawaitable(output):
                await output
        except RuntimeError as error:
            assert str(error) == "`timeout_ms` is hidden by `tool.parameters`", error
            rejected = str(error)
        else:
            raise AssertionError("explicit hidden timeout was accepted")
        return {
            "client": type(console).__name__,
            "tool": tool.model_dump(exclude_none=True),
            "outputs": outputs,
            "explicit_timeout_error": rejected,
        }

    async def run() -> Transcript:
        with tempfile.TemporaryDirectory() as directory:
            settings = {
                "command": binary,
                "args": DIRECT.serve(
                    "--no-config",
                    "-c",
                    "languages=[python]",
                    "-c",
                    "python=" + json.dumps(sys.executable),
                    "-c",
                    "tool.description=Evaluation tool.",
                    "-c",
                    "tool.parameters.python.description=Python cell.",
                    "-c",
                    "tool.parameters.timeout_ms.enabled=false",
                    "-c",
                    "tool.parameters.session.enabled=false",
                ),
                "server_parameters": {
                    "cwd": directory,
                    "env": dict(
                        os.environ,
                        MCP_CONSOLE_LANGUAGES="python",
                        MCP_CONSOLE_HOME=directory,
                    ),
                },
            }
            async with AsyncMCPConsole(**settings) as console:
                asynchronous = await exercise(console)
            with MCPConsole(**settings) as console:
                synchronous = await exercise(console)
            return [asynchronous, synchronous]

    return asyncio.run(run())


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@requires(command("ir"))
def test_clients_inspect_and_replace_requirements(
    binary: Path, execution: Execution
) -> Transcript:
    results = []
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        environment, _ = recording_uv_environment(root)
        # No cell is evaluated, but eager startup still prepares DuckDB extensions.
        # Supply those real packages alongside deterministic resolver results.
        library = Path(
            subprocess.run(
                [
                    "ir",
                    "run",
                    "--with",
                    "DBI",
                    "--with",
                    "duckdb",
                    "--with",
                    "jsonlite",
                    "--isolated",
                    "--vanilla",
                    "-e",
                    "cat(.libPaths()[[1L]])",
                ],
                cwd=directory,
                env=r_test_environment()[0],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout
        )
        assert library.is_absolute() and library.is_dir(), library
        environment.update(fake_ir_environment(root, [library] * 4))
        # Preparation must not borrow packages from the caller's R libraries.
        empty_library = root / "empty-r-library"
        empty_library.mkdir()
        environment.update(
            {
                name: str(empty_library)
                for name in ("R_LIBS", "R_LIBS_USER", "R_LIBS_SITE")
            }
        )
        environment.pop("RETICULATE_PYTHON", None)
        environment["MCP_CONSOLE_TEST_UV_PYTHON"] = sys.executable
        settings = {
            "command": binary,
            "args": execution.serve("-c", "cache=host"),
            "server_parameters": {"cwd": directory, "env": environment},
        }
        with MCPConsole(**settings) as console:
            startup = console.send(requirements={"action": "get"}).structured_content
            assert startup["prepared"] is True, startup
            console.send(
                requirements={
                    "action": "set",
                    "python": [],
                    "python_version": [">=3.11"],
                    "exclude_newer": "2026-01-01",
                }
            )
            selected = console.send(requirements={"action": "get"}).structured_content
            console.send(requirements=dict(selected["requirements"], action="set"))
            assert selected["prepared"] is True, selected
            assert selected["requirements"]["python"] == []
            results.append({"sync": selected})

        async def asynchronous() -> None:
            async with AsyncMCPConsole(**settings) as console:
                startup = (
                    await console.send(requirements={"action": "get"})
                ).structured_content
                assert startup["prepared"] is True, startup
                await console.send(requirements={"action": "set"})
                selected = (
                    await console.send(requirements={"action": "get"})
                ).structured_content
                assert selected["requirements"]["python"] == []
                results.append({"async": selected})

        asyncio.run(asynchronous())
        assert (root / "ir-counter").read_text() == "4"
    return results


@executions(RUNTIME)
def test_sync_and_async_clients_receive_bounded_previews(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    emitted = "x" * (8 * 1024 * 1024 + 7)
    source = f'print("x" * {len(emitted)}, end="")'

    async def exercise(console):
        output = console.send(python=source, timeout_ms=600_000)
        if inspect.isawaitable(output):
            output = await output
        assert output.result_type == "complete", output
        assert output.is_error is False, output
        assert_preview(output.text, emitted)
        idle = console.send(session="default")
        if inspect.isawaitable(idle):
            idle = await idle
        assert idle.text == "\n[idle]", idle
        return output

    async def run() -> TranscriptWithCompanions:
        transcripts = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for client_type in (MCPConsole, AsyncMCPConsole):
                workspace = root / client_type.__name__
                workspace.mkdir()
                capture = root / f"{client_type.__name__}.json"
                settings = {
                    "command": sys.executable,
                    "args": [
                        "-u",
                        ROOT / "tests/fixtures/mcp_recording_proxy.py",
                        "--workspace",
                        workspace,
                        capture,
                        binary,
                        *execution.serve(),
                    ],
                }
                if client_type is AsyncMCPConsole:
                    async with client_type(**settings) as console:
                        result = await exercise(console)
                else:
                    with client_type(**settings) as console:
                        result = await exercise(console)
                (session,) = (workspace / ".agents/console/sessions").iterdir()
                assert (
                    session / "capture/sessions/001/cell-001.txt"
                ).read_bytes() == emitted.encode()
                transcript = json.loads(capture.read_text(encoding="utf-8"))
                output = next(
                    entry["result"]
                    for entry in transcript
                    if entry["input"]["method"] == "tools/call"
                )
                assert output["content"] == [{"type": "text", "text": result.text}]
                # Check the original byte budget and complete raw log before
                # normalizing only the fixture-owned recording path.
                output["content"][0]["text"] = result.text.replace(
                    session.name, "<run ID>"
                ).replace(os.sep, "/")
                assert transcript[0]["input"]["method"] == "server/discover"
                transcripts.append(
                    [
                        {
                            "scenario": f"{client_type.__name__} receives a bounded preview and then idle.",
                            "fixture": "The real Python runtime emits more than the retained preview budget. The full raw log and exact preview bytes are checked; the proxy retains the MCP exchanges.",
                        },
                        *transcript,
                    ]
                )
        synchronous, asynchronous = transcripts
        assert synchronous[1:] == asynchronous[1:]
        return TranscriptWithCompanions(
            synchronous, {"async.yaml": McpTranscript(asynchronous)}
        )

    return asyncio.run(run())


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_initialization_keeps_one_lifecycle_while_startup_is_pending(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        gate = Path(directory) / "startup.py"
        capture = Path(directory) / "requests.jsonl"
        gate.write_text(
            # fmt: python
            code("""
                import json
                from pathlib import Path
                import subprocess
                import sys

                pending = [sys.stdin.buffer.readline()]
                if json.loads(pending[0])["method"] == "server/discover":
                    # Release on the SDK's initialize fallback, without a timing sleep.
                    # The real server sees every request, including the pending discovery.
                    pending.append(sys.stdin.buffer.readline())
                capture = Path(sys.argv[1])
                capture.write_bytes(b"".join(pending))
                child = subprocess.Popen(sys.argv[2:], stdin=subprocess.PIPE)
                try:
                    for message in pending:
                        child.stdin.write(message)
                    child.stdin.flush()
                    for message in sys.stdin.buffer:
                        with capture.open("ab") as record:
                            record.write(message)
                        child.stdin.write(message)
                        child.stdin.flush()
                finally:
                    child.stdin.close()
                    child.wait(timeout=10)
                """)
        )
        settings = options(binary, execution)
        settings["command"] = sys.executable
        settings["args"] = [
            "-u",
            str(gate),
            str(capture),
            str(binary),
            *settings["args"],
        ]
        with MCPConsole(**settings) as console:
            result = console.send(r="echo initialized")
            assert result.text == "zod: initialized\n", result
        requests = [json.loads(line) for line in capture.read_text().splitlines()]
        methods = [request["method"] for request in requests]
        assert methods == [
            "server/discover",
            "initialize",
            "notifications/initialized",
            "tools/list",
            "tools/call",
        ], requests
        return [
            {
                "startup_kept_one_lifecycle": True,
                "requests": methods,
                "output": result.text,
            }
        ]


@requires(POSIX, R)
@executions(DIRECT, SANDBOXED)
def test_callable_tools_follow_connected_server_fields(
    binary: Path, execution: Execution
) -> Transcript:
    from agents.tool_context import ToolContext
    from chatlas import ChatOpenAI
    from jsonschema import Draft202012Validator

    for console_type in (AsyncMCPConsole, MCPConsole):
        console = console_type()
        for create_tools in (
            mcp_console.openai.responses_tools,
            mcp_console.openai.agents_tools,
            mcp_console.anthropic.callable_tools,
            mcp_console.chatlas.tools,
        ):
            try:
                create_tools(console)
            except RuntimeError as error:
                assert "connect" in str(error).lower(), error
            else:
                raise AssertionError("disconnected console created tools")

    async def exercise_tools(console, source, expected_fields):
        live_tool = console.get_tool("send")
        live_schema = live_tool.input_schema
        assert set(live_schema["properties"]) == expected_fields, live_schema
        chat = ChatOpenAI(model="unused", api_key="unused")

        def existing_tool() -> str:
            """An application-owned tool."""
            return "existing"

        chat.register_tool(existing_tool)
        existing = chat.get_tools()[0]
        chat.set_tools([*chat.get_tools(), *mcp_console.chatlas.tools(console)])
        assert chat.get_tools()[0] is existing
        chat_tool = next(tool for tool in chat.get_tools() if tool.name == "send")
        agent = next(
            tool
            for tool in mcp_console.openai.agents_tools(console)
            if tool.name == "send"
        )
        anthropic = next(
            tool
            for tool in mcp_console.anthropic.callable_tools(console)
            if tool.to_dict()["name"] == "send"
        )
        arguments = {"r": source}
        encoded = json.dumps(arguments)
        context = ToolContext(
            context=None,
            tool_name="send",
            tool_call_id="call_1",
            tool_arguments=encoded,
        )
        responses = next(
            tool
            for tool in mcp_console.openai.responses_tools(console)
            if tool.definition["name"] == "send"
        )
        tools = (
            (responses.definition["parameters"], lambda: responses.call(arguments)),
            (
                chat_tool.schema["function"]["parameters"],
                lambda: chat_tool.func(**arguments),
            ),
            (agent.params_json_schema, lambda: agent.on_invoke_tool(context, encoded)),
            (anthropic.to_dict()["input_schema"], lambda: anthropic.call(arguments)),
        )
        outputs = []
        assert chat_tool.schema["function"]["strict"] is False
        assert agent.strict_json_schema is False
        assert responses.definition["strict"] is False
        assert (
            chat_tool.schema["function"]["description"]
            == agent.description
            == anthropic.to_dict()["description"]
            == responses.definition["description"]
            == live_tool.description
        )
        for invoke in (lambda: console.send(**arguments), lambda: console(**arguments)):
            output = invoke()
            output = await output if inspect.isawaitable(output) else output
            outputs.append(output.text)
        for schema, invoke in tools:
            assert schema == live_schema, schema
            validator = Draft202012Validator(schema)
            validator.validate(arguments)
            for omitted in {"r", "python", "sql", "requirements"} - expected_fields:
                value = {"r": ["praise"]} if omitted == "requirements" else "1"
                assert not validator.is_valid(arguments | {omitted: value}), schema
            output = invoke()
            outputs.append(await output if inspect.isawaitable(output) else output)
        texts = [adapter_text(output) for output in outputs]
        assert all(text == texts[0] for text in texts), outputs
        return texts[0]

    async def exercise():
        transcript = []
        with tempfile.TemporaryDirectory() as directory:
            custom = options(binary, execution)
            custom["server_parameters"]["env"]["MCP_CONSOLE_LANGUAGES"] = "r"
            bare = {
                "command": binary,
                "args": execution.serve(),
                "server_parameters": {
                    "env": bare_runtime_environment(
                        r_test_environment()[0], Path(directory)
                    )
                },
            }
            for label, settings, source, expected_fields in (
                (
                    "bare",
                    bare,
                    "1 + 1",
                    {
                        "r",
                        "python",
                        "sql",
                        "note",
                        "session",
                        "control",
                        "requirements",
                        "stdin",
                        "timeout_ms",
                    },
                ),
                (
                    "custom",
                    custom,
                    "echo configured",
                    {
                        "r",
                        "note",
                        "session",
                        "control",
                        "requirements",
                        "stdin",
                        "timeout_ms",
                    },
                ),
            ):
                async with AsyncMCPConsole(**settings) as console:
                    if label == "bare":
                        assert console.get_tool("send").input_schema["properties"][
                            "requirements"
                        ]["properties"]["action"]["enum"] == [
                            "get",
                            "add",
                            "set",
                            "reset",
                        ]
                    output = await exercise_tools(console, source, expected_fields)
                with MCPConsole(**settings) as console:
                    assert (
                        await exercise_tools(console, source, expected_fields) == output
                    )
                transcript.append({label: output, "fields": sorted(expected_fields)})
        return transcript

    return asyncio.run(exercise())


@requires(WORKER)
@executions(DIRECT, SANDBOXED)
@requires(R, SQL)
def test_callable_preserves_mixed_language_state(
    binary: Path, execution: Execution
) -> Transcript:
    environment, _ = r_test_environment()
    # Retain the test caches while exercising Linux's C locale on every host.
    environment["LC_ALL"] = "C"
    settings = {
        "command": binary,
        "args": execution.serve(),
        "server_parameters": {"env": environment},
    }
    cells = {
        "r": "answer <- 42; answer",
        "python": "r.answer + 1",
        "sql": "SELECT 42 AS answer",
    }
    running = "\n[running; poll with an empty send]"

    def collected(chunks):
        # Completion without new output is represented by the server's done notice.
        return "".join(
            without_elapsed(chunk).removesuffix(running)
            for chunk in chunks
            if chunk != "[done]"
        )

    async def exercise():
        async with AsyncMCPConsole(**settings) as console:
            assert await console.connect() is console
            transcript = []
            for language, source in cells.items():
                chunks = [(await console(**{language: source}, timeout_ms=0)).text]
                # A returned send can still be running. Collect its complete output
                # before submitting the next cell, including on a cold R startup.
                while chunks[-1].endswith(running):
                    chunks.append((await console.send(session="default")).text)
                transcript.append({language: collected(chunks)})
            return transcript

    transcript = asyncio.run(exercise())
    with MCPConsole(**settings) as console:
        synchronous = []
        for language, source in cells.items():
            chunks = [console(**{language: source}, timeout_ms=0).text]
            while chunks[-1].endswith(running):
                chunks.append(console.send(session="default").text)
            synchronous.append({language: collected(chunks)})
    assert synchronous == transcript
    return transcript


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_errors_close_and_reconnect(binary: Path, execution: Execution) -> Transcript:
    async def exercise():
        console = AsyncMCPConsole(**options(binary, execution))
        transcript = []
        async with console:
            try:
                await console.send(r="echo r", python="echo python")
            except RuntimeError as error:
                transcript.append({"error": str(error)})
            else:
                raise AssertionError("invalid send succeeded")
            transcript.append(
                {"recovery": (await console.send(r="echo recovered")).text}
            )
        await console.close()
        try:
            await console.send()
        except RuntimeError as error:
            transcript.append({"closed": str(error)})
        else:
            raise AssertionError("closed console accepted a call")
        async with console:
            transcript.append(
                {"reconnected": (await console.send(r="echo reconnected")).text}
            )
        return transcript

    return asyncio.run(exercise())


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_responses_preserves_schema_text_and_images(
    binary: Path, execution: Execution
) -> Transcript:
    from openai.types.responses import ResponseFunctionToolCall
    from openai.types.responses.response_input_item_param import FunctionCallOutput
    from pydantic import TypeAdapter

    async def exercise():
        async with AsyncMCPConsole(**options(binary, execution)) as console:
            tools = mcp_console.openai.responses_tools(console)
            by_name = {tool.definition["name"]: tool for tool in tools}
            tool = by_name["send"]
            assert isinstance(tool, mcp_console.openai.AsyncResponsesTool)
            schema = tool.definition["parameters"]
            assert set(schema["properties"]) == {
                "r",
                "python",
                "sql",
                "note",
                "session",
                "stdin",
                "control",
                "requirements",
                "timeout_ms",
            }
            assert tool.definition["strict"] is False
            outputs = []
            for source in ("echo response", "emit image"):
                call = ResponseFunctionToolCall(
                    type="function_call",
                    name="send",
                    call_id="call_1",
                    arguments=json.dumps({"r": source}),
                )
                result = await by_name[call.name](call)
                TypeAdapter(FunctionCallOutput).validate_python(result)
                outputs.append(result)
            return outputs

    return asyncio.run(exercise())


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_openai_agents_callable_preserves_optional_arguments(
    binary: Path, execution: Execution
) -> Transcript:
    from agents import Agent, FunctionTool
    from agents.tool_context import ToolContext
    from jsonschema import validate

    async def exercise():
        async with AsyncMCPConsole(**options(binary, execution)) as console:
            tools = mcp_console.openai.agents_tools(console)
            tool = next(tool for tool in tools if tool.name == "send")
            assert isinstance(tool, FunctionTool)
            validate({"requirements": {"r": ["praise"]}}, tool.params_json_schema)
            agent = Agent(name="test", tools=tools)
            assert agent.tools == tools
            arguments = json.dumps({"r": "echo agent"})
            context = ToolContext(
                context=None,
                tool_name="send",
                tool_call_id="call_1",
                tool_arguments=arguments,
            )
            output = await tool.on_invoke_tool(context, arguments)
            return [{"output": output}]

    return asyncio.run(exercise())


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_native_openai_agents_server(binary: Path, execution: Execution) -> Transcript:
    from agents import Agent
    from agents.mcp import MCPServerStdio

    async def exercise():
        settings = options(binary, execution)
        parameters = settings.pop("server_parameters")
        async with mcp_console.openai.agents_server(
            **settings, params=parameters
        ) as server:
            assert isinstance(server, MCPServerStdio)
            agent = Agent(name="test", mcp_servers=[server])
            assert agent.mcp_servers == [server]
            tools = await server.list_tools()
            assert [tool.name for tool in tools] == ["send"]
            result = await server.call_tool("send", {"r": "emit image"})
            recorded = result.model_dump(mode="json", by_alias=True, exclude_none=True)
            assert recorded["_meta"][
                "io.modelcontextprotocol/serverInfo"
            ] == server_identity(binary), recorded
            return [{"result": recorded}]

    return asyncio.run(exercise())


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_anthropic_callable_and_native_tools(
    binary: Path, execution: Execution
) -> Transcript:
    from pydantic_core import to_jsonable_python

    async def exercise():
        async with AsyncMCPConsole(**options(binary, execution)) as console:
            tools = mcp_console.anthropic.callable_tools(console)
            tool = next(tool for tool in tools if tool.to_dict()["name"] == "send")
            assert tool.to_dict()["name"] == "send"
            text = await tool.call({"r": "echo anthropic"})
        async with mcp_console.anthropic.tools(
            **options(binary, execution), tool_kwargs={"defer_loading": True}
        ) as tools:
            assert len(tools) == 1
            tool = next(tool for tool in tools if tool.to_dict()["name"] == "send")
            assert tool.to_dict()["defer_loading"] is True
            images = await tool.call({"r": "emit image"})
        return [{"callable": text}, {"native": to_jsonable_python(images)}]

    return asyncio.run(exercise())


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_native_openai_agents_waits_for_console_output(
    binary: Path, execution: Execution
) -> Transcript:
    async def exercise():
        settings = options(binary, execution)
        parameters = settings.pop("server_parameters")
        async with mcp_console.openai.agents_server(
            **settings, params=parameters
        ) as server:
            # Finish startup and one cell before measuring the adapter's wait.
            ready = await server.call_tool("send", {"r": "complete silently"})
            while ready.content[0].text.endswith(
                "\n[running; poll with an empty send]"
            ):
                ready = await server.call_tool("send", {"session": "default"})
            assert not ready.is_error
            assert ready.model_dump(mode="json", exclude_none=True)["content"] == [
                {"type": "text", "text": "[done]"}
            ]
            # The fixture stays blocked beyond the SDK's five-second default.
            started = time.monotonic()
            result = await server.call_tool("send", {"r": "stall", "timeout_ms": 6_000})
            assert time.monotonic() - started >= 5.9
            recorded = result.model_dump(mode="json", by_alias=True, exclude_none=True)
            assert recorded["_meta"][
                "io.modelcontextprotocol/serverInfo"
            ] == server_identity(binary), recorded
            text = recorded["content"][0]["text"]
            _, silent = elapsed_progress(text)
            assert silent
            recorded["content"][0]["text"] = normalize_elapsed(text)
            return [{"result": recorded}]

    return asyncio.run(exercise())


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_chatlas_registers_on_existing_chat(
    binary: Path, execution: Execution
) -> Transcript:
    from chatlas import ChatOpenAI

    async def exercise():
        chat = ChatOpenAI(model="unused", api_key="unused")
        settings = options(binary, execution)
        parameters = settings.pop("server_parameters")
        try:
            await mcp_console.chatlas.register(
                chat, **settings, transport_kwargs=parameters
            )
            tools = chat.get_tools()
            assert len(tools) == 1
            tool = next(tool for tool in tools if tool.name == "send")
            result = await tool.func(r="echo chatlas")
            return [{"name": tool.name, "value": result.value}]
        finally:
            await chat.cleanup_mcp_tools()

    return asyncio.run(exercise())


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_chatlas_callable_registers_concrete_schema(
    binary: Path, execution: Execution
) -> Transcript:
    from chatlas import ChatOpenAI

    async def exercise():
        chat = ChatOpenAI(model="unused", api_key="unused")
        async with AsyncMCPConsole(**options(binary, execution)) as console:
            chat.set_tools([*chat.get_tools(), *mcp_console.chatlas.tools(console)])
            tools = chat.get_tools()
            assert [tool.name for tool in tools] == ["send"]
            tool = next(tool for tool in tools if tool.name == "send")
            return [
                {"output": adapter_text(await tool.func(r="echo callable chatlas"))}
            ]

    return asyncio.run(exercise())


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_sync_callable_and_framework_tools(
    binary: Path, execution: Execution
) -> Transcript:
    from agents.tool_context import ToolContext
    from chatlas import ChatOpenAI
    from jsonschema import validate

    console = MCPConsole(**options(binary, execution))
    transcript = []
    with console:
        assert console.connect() is console
        assert not inspect.iscoroutinefunction(console.send)
        transcript.append({"callable": console(r="echo sync").text})
        try:
            console.send(r="echo r", python="echo python")
        except RuntimeError as error:
            transcript.append({"error": str(error)})
        else:
            raise AssertionError("invalid send succeeded")

        chat = ChatOpenAI(model="unused", api_key="unused")
        chat.set_tools([*chat.get_tools(), *mcp_console.chatlas.tools(console)])
        chat_tool = next(tool for tool in chat.get_tools() if tool.name == "send")
        assert not inspect.iscoroutinefunction(chat_tool.func)
        transcript.append({"chatlas": adapter_text(chat_tool.func(r="echo chatlas"))})

        anthropic_tool = next(
            tool
            for tool in mcp_console.anthropic.callable_tools(console)
            if tool.to_dict()["name"] == "send"
        )
        transcript.append({"anthropic": anthropic_tool.call({"r": "echo anthropic"})})

        agent_tool = next(
            tool
            for tool in mcp_console.openai.agents_tools(console)
            if tool.name == "send"
        )
        validate({"requirements": {"r": ["praise"]}}, agent_tool.params_json_schema)
        arguments = json.dumps({"r": "echo agent"})
        context = ToolContext(
            context=None,
            tool_name="send",
            tool_call_id="call_1",
            tool_arguments=arguments,
        )
        transcript.append(
            {"agent": asyncio.run(agent_tool.on_invoke_tool(context, arguments))}
        )
    console.close()
    try:
        console.send()
    except RuntimeError as error:
        transcript.append({"closed": str(error)})
    else:
        raise AssertionError("closed console accepted a call")
    with console:
        transcript.append({"reconnected": console.send(r="echo reconnected").text})
        # Closing must also clean up an active worker and the MCP transport.
        assert "running" in console.send(r="stall", timeout_ms=0).text
    return transcript


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_sync_responses_preserves_text_and_images(
    binary: Path, execution: Execution
) -> Transcript:
    from openai.types.responses import ResponseFunctionToolCall
    from openai.types.responses.response_input_item_param import FunctionCallOutput
    from pydantic import TypeAdapter

    with MCPConsole(**options(binary, execution)) as console:
        tools = mcp_console.openai.responses_tools(console)
        by_name = {tool.definition["name"]: tool for tool in tools}
        tool = by_name["send"]
        assert isinstance(tool, mcp_console.openai.ResponsesTool)
        assert tool.definition["name"] == "send"
        assert tool.definition["strict"] is False
        outputs = []
        for source in ("echo response", "emit image"):
            call = ResponseFunctionToolCall(
                type="function_call",
                name="send",
                call_id="call_1",
                arguments=json.dumps({"r": source}),
            )
            result = by_name[call.name](call)
            TypeAdapter(FunctionCallOutput).validate_python(result)
            outputs.append(result)
        assert tool.call({"r": "echo direct"}) == "zod: direct\n"
        return outputs


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_callable_tools_preserve_image_results(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    from agents import ItemHelpers, ToolOutputImage, ToolOutputText
    from agents.tool_context import ToolContext
    from anthropic.lib.tools import ToolError
    from anthropic.types.beta import BetaToolResultBlockParam
    from chatlas import ContentToolResult
    from chatlas.types import ContentImageInline
    from openai.types.responses import ResponseFunctionToolCall
    from openai.types.responses.response_input_item_param import FunctionCallOutput
    from pydantic import TypeAdapter

    environment, rscript = r_test_environment()
    # fmt: r
    plot_code = code(r"""
        options(
          console.plot.width_in = 4,
          console.plot.height_in = 3,
          console.plot.dpi = 100
        )
        cat("before image\n")
        plot(1:3)
        invisible(dev.off())
        cat("after image\n")
        """)
    (expected_plot,) = reference_plots(
        rscript, environment, plot_code, width=4, height=3, dpi=100, pages=1
    )
    expected_image = base64.b64encode(expected_plot).decode("ascii")
    image_arguments = {"r": plot_code, "timeout_ms": 300_000}
    # fmt: r
    exit_code = code(r"""
        quit(save = "no", status = 33L, runLast = FALSE)
        """)
    image_error_arguments = {
        "r": plot_code + "\n" + exit_code,
        "timeout_ms": 300_000,
    }

    def check_image(result) -> None:
        assert isinstance(result, ContentToolResult), type(result)
        assert result.error is None
        info = result.extra["io.modelcontextprotocol/serverInfo"]
        assert info["name"] == "mcp-console", result.extra
        # Text parts stay plain strings, matching chatlas's own MCP adapter
        # (Tool.from_mcp): providers can serialize the value as-is (a bare
        # ContentText produced an invalid Anthropic tool_result payload —
        # posit-dev/chatlas#494).
        kinds = [type(item) for item in result.value]
        assert kinds == [str, ContentImageInline, str], kinds
        text, image, after = result.value
        assert (text, after) == ("before image\n", "after image\n")
        assert image.image_content_type == "image/png"
        assert image.data == expected_image

    expected_responses_content = [
        {"type": "input_text", "text": "before image\n"},
        {
            "type": "input_image",
            "image_url": f"data:image/png;base64,{expected_image}",
            "detail": "auto",
        },
        {"type": "input_text", "text": "after image\n"},
    ]

    async def exercise(console) -> list:
        chatlas = next(
            tool for tool in mcp_console.chatlas.tools(console) if tool.name == "send"
        )
        agents = next(
            tool
            for tool in mcp_console.openai.agents_tools(console)
            if tool.name == "send"
        )
        anthropic = next(
            tool
            for tool in mcp_console.anthropic.callable_tools(console)
            if tool.to_dict()["name"] == "send"
        )
        responses = next(
            tool
            for tool in mcp_console.openai.responses_tools(console)
            if tool.definition["name"] == "send"
        )

        def invoke_agents(arguments):
            encoded = json.dumps(arguments)
            context = ToolContext(
                context=None,
                tool_name="send",
                tool_call_id="call_1",
                tool_arguments=encoded,
            )
            return agents.on_invoke_tool(context, encoded)

        for adapter, call in (
            ("chatlas", lambda arguments: chatlas.func(**arguments)),
            ("agents", invoke_agents),
            ("anthropic", anthropic.call),
            ("responses", responses.call),
        ):

            async def invoke(arguments):
                result = call(arguments)
                return await result if inspect.isawaitable(result) else result

            result = await invoke(image_arguments)
            if adapter == "chatlas":
                check_image(result)
            elif adapter == "agents":
                assert isinstance(result, list), type(result)
                assert [type(item) for item in result] == [
                    ToolOutputText,
                    ToolOutputImage,
                    ToolOutputText,
                ]
                assert result == [
                    ToolOutputText(text="before image\n"),
                    ToolOutputImage(
                        image_url=f"data:image/png;base64,{expected_image}",
                        detail="auto",
                    ),
                    ToolOutputText(text="after image\n"),
                ]
                function_call = ResponseFunctionToolCall(
                    type="function_call",
                    name="send",
                    call_id="call_1",
                    arguments=json.dumps(image_arguments),
                )
                provider_result = ItemHelpers.tool_call_output_item(
                    function_call, result
                )
                TypeAdapter(FunctionCallOutput).validate_python(provider_result)
                assert provider_result == {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": expected_responses_content,
                }
            elif adapter == "anthropic":
                assert result == [
                    {"type": "text", "text": "before image\n"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": expected_image,
                        },
                    },
                    {"type": "text", "text": "after image\n"},
                ], type(result)
                TypeAdapter(BetaToolResultBlockParam).validate_python(
                    {"type": "tool_result", "tool_use_id": "call_1", "content": result}
                )
            else:
                assert result == expected_responses_content, result
                TypeAdapter(FunctionCallOutput).validate_python(
                    {
                        "type": "function_call_output",
                        "call_id": "call_1",
                        "output": result,
                    }
                )
            text = await invoke({"r": 'cat("text\\n")'})
            assert adapter_text(text) == "text\n", (adapter, type(text), text)
            try:
                await invoke({"r": 'cat("r\\n")', "python": 'print("python")'})
            except RuntimeError as rejected:
                assert (
                    str(rejected)
                    == "only one of `r`, `python`, or `sql` may be supplied"
                )
            else:
                raise AssertionError(f"{adapter} errored result did not raise")

        try:
            error_result = anthropic.call(image_error_arguments)
            if inspect.isawaitable(error_result):
                await error_result
        except ToolError as error:
            TypeAdapter(BetaToolResultBlockParam).validate_python(
                {
                    "type": "tool_result",
                    "tool_use_id": "call_1",
                    "content": error.content,
                    "is_error": True,
                }
            )
            assert isinstance(error.content, list), type(error.content)
            return error.content
        else:
            raise AssertionError("Anthropic image error did not raise ToolError")

    async def run() -> TranscriptWithCompanions:
        transcripts = []
        with tempfile.TemporaryDirectory() as directory:
            for client_type in (MCPConsole, AsyncMCPConsole):
                capture = Path(directory) / f"{client_type.__name__}.json"
                settings = {
                    "command": sys.executable,
                    "args": [
                        "-u",
                        ROOT / "tests/fixtures/mcp_recording_proxy.py",
                        capture,
                        binary,
                        *execution.serve(),
                    ],
                    "server_parameters": {"env": environment},
                }
                if client_type is AsyncMCPConsole:
                    async with client_type(**settings) as console:
                        image_error = await exercise(console)
                else:
                    with client_type(**settings) as console:
                        image_error = await exercise(console)
                transcript = json.loads(capture.read_text(encoding="utf-8"))
                error_call = next(
                    entry
                    for entry in transcript
                    if entry["input"].get("params", {}).get("arguments")
                    == image_error_arguments
                )
                error_result = error_call["result"]
                assert error_result["isError"] is True, error_result
                assert [item["type"] for item in error_result["content"]] == [
                    "text",
                    "image",
                    "text",
                ], error_result
                before, _, after = error_result["content"]
                assert before["text"] == "before image\n", before
                assert "[worker exited with status 33]" in after["text"], after
                assert image_error == [
                    {"type": "text", "text": before["text"]},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": expected_image,
                        },
                    },
                    {"type": "text", "text": after["text"]},
                ], image_error
                image_calls = [
                    entry
                    for entry in transcript
                    if entry["input"].get("params", {}).get("arguments")
                    == image_arguments
                ]
                assert len(image_calls) == 4, image_calls
                for image_call in [*image_calls, error_call]:
                    image = image_call["result"]["content"][1]
                    assert image == {
                        "type": "image",
                        "mimeType": "image/png",
                        "data": expected_image,
                    }, image
                    image["data"] = "<PNG byte-identical to live Rscript page 1>"
                assert transcript[0]["input"]["method"] == "server/discover"
                transcripts.append(
                    [
                        {
                            "scenario": f"Callable tools through {client_type.__name__}: "
                            "Chatlas, OpenAI Agents, Anthropic, and OpenAI Responses each "
                            "receive an image result, a text result, then an invalid call. "
                            "Anthropic also receives an image followed by a worker exit error.",
                            "fixture": "The built-in R runtime produces a plot whose PNG bytes "
                            "are compared with a live Rscript reference. The proxy records MCP "
                            "exchanges; adapter results are checked in Python.",
                        },
                        *transcript,
                    ]
                )
        sync_transcript, async_transcript = transcripts
        assert sync_transcript[1:] == async_transcript[1:]
        return TranscriptWithCompanions(
            sync_transcript, {"async.yaml": McpTranscript(async_transcript)}
        )

    return asyncio.run(run())


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_chatlas_tool_returns_tool_result_for_text(
    binary: Path, execution: Execution
) -> Transcript:
    from chatlas import ContentToolResult

    async def exercise(console) -> dict:
        tool = next(
            tool for tool in mcp_console.chatlas.tools(console) if tool.name == "send"
        )

        async def invoke(arguments):
            result = tool.func(**arguments)
            return await result if inspect.isawaitable(result) else result

        result = await invoke({"r": "echo text"})
        assert isinstance(result, ContentToolResult), type(result)
        assert result.error is None
        # A single text part becomes a plain string, matching chatlas's own
        # MCP adapter (Tool.from_mcp): providers serialize the value as-is
        # (posit-dev/chatlas#494).
        assert isinstance(result.value, str)
        assert result.value == "zod: text\n"
        info = result.extra["io.modelcontextprotocol/serverInfo"]
        assert info["name"] == "mcp-console", result.extra
        return {"type": type(result).__name__, "text": result.value}

    async def asynchronous() -> dict:
        async with AsyncMCPConsole(**options(binary, execution)) as console:
            return await exercise(console)

    async_result = asyncio.run(asynchronous())
    with MCPConsole(**options(binary, execution)) as console:
        sync_result = asyncio.run(exercise(console))
    assert sync_result == async_result
    return [sync_result]


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_chatlas_tool_forwards_result_meta(
    binary: Path, execution: Execution
) -> Transcript:
    async def exercise(console) -> dict:
        tool = next(
            tool for tool in mcp_console.chatlas.tools(console) if tool.name == "send"
        )

        async def invoke(arguments):
            result = tool.func(**arguments)
            return await result if inspect.isawaitable(result) else result

        # The server's standard identity arrives untouched on every
        # result. Presentation belongs to chat UIs, not this adapter.
        noted = (
            await invoke(
                {"r": "echo 42", "note": "Check the <answer> first.\nSecond line."}
            )
        ).extra["io.modelcontextprotocol/serverInfo"]
        assert noted["name"] == "mcp-console", noted
        assert isinstance(noted["version"], str) and noted["version"], noted

        assert (await invoke({"python": "echo snake"})).extra[
            "io.modelcontextprotocol/serverInfo"
        ]["name"] == "mcp-console"

        assert (await invoke({"note": "Just a thought"})).extra[
            "io.modelcontextprotocol/serverInfo"
        ]["name"] == "mcp-console"

        # Image results carry the same identity; no display html is composed.
        assert (await invoke({"r": "emit image"})).extra[
            "io.modelcontextprotocol/serverInfo"
        ]["name"] == "mcp-console"

        # Polls carry the identity too.
        assert (await invoke({})).extra["io.modelcontextprotocol/serverInfo"][
            "name"
        ] == "mcp-console"

        return {"noted": noted["name"]}

    async def asynchronous() -> dict:
        async with AsyncMCPConsole(**options(binary, execution)) as console:
            return await exercise(console)

    async_result = asyncio.run(asynchronous())
    with MCPConsole(**options(binary, execution)) as console:
        sync_result = asyncio.run(exercise(console))
    assert sync_result == async_result
    return [sync_result]


def test_thread_configuration_preserves_existing_servers(binary: Path) -> Transcript:
    from openai_codex.generated.v2_all import ThreadStartParams

    parameters = {"env": {"MODE": "test"}}
    config = {
        "model": "caller-selected-model",
        "mcp_servers": {
            "existing": {"command": "other"},
            "console": mcp_console.codex.server(
                command="custom-console",
                server_parameters=parameters,
            ),
        },
    }
    assert parameters == {"env": {"MODE": "test"}}
    ThreadStartParams(config=config)
    return [{"config": config}]


if __name__ == "__main__":
    run_this_suite(__file__)
