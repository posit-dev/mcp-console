#!/usr/bin/env -S uv run --script

import asyncio
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
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.r import r_test_environment
from support.normalization import code
from support.records import Transcript
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


def options(binary: Path, execution: Execution) -> dict:
    return {
        "command": binary,
        "args": execution.serve("--worker", str(ROOT / "tests/fixtures/zod")),
        "server_parameters": {
            "env": {**os.environ, "MCP_CONSOLE_TEST_PYTHON": sys.executable}
        },
    }


def test_clients_and_adapters_preserve_hidden_timeout(binary: Path) -> Transcript:
    from agents.tool_context import ToolContext

    async def exercise(console) -> dict:
        tool = console.send_tool
        assert "timeout_ms" not in tool.input_schema["properties"]
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
        responses = mcp_console.openai.responses_tool(console)
        agent = mcp_console.openai.agents_tool(console)
        anthropic = mcp_console.anthropic.tool(console)
        chatlas = mcp_console.chatlas.tool(console)
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
            lambda: responses.call(arguments),
            lambda: agent.on_invoke_tool(context, encoded),
            lambda: anthropic.call(arguments),
            lambda: chatlas.func(**arguments),
        ):
            output = invoke()
            outputs.append(await output if inspect.isawaitable(output) else output)
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
            startup = json.loads(console.send(requirements={"action": "get"}))
            assert startup["prepared"] is True, startup
            console.send(
                requirements={
                    "action": "set",
                    "python": [],
                    "python_version": [">=3.11"],
                    "exclude_newer": "2026-01-01",
                }
            )
            selected = json.loads(console.send(requirements={"action": "get"}))
            console.send(requirements=dict(selected["requirements"], action="set"))
            assert selected["prepared"] is True, selected
            assert selected["requirements"]["python"] == []
            results.append({"sync": selected})

        async def asynchronous() -> None:
            async with AsyncMCPConsole(**settings) as console:
                startup = json.loads(await console.send(requirements={"action": "get"}))
                assert startup["prepared"] is True, startup
                await console.send(requirements={"action": "set"})
                selected = json.loads(
                    await console.send(requirements={"action": "get"})
                )
                assert selected["requirements"]["python"] == []
                results.append({"async": selected})

        asyncio.run(asynchronous())
        assert (root / "ir-counter").read_text() == "4"
    return results


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_sync_and_async_clients_receive_bounded_previews(
    binary: Path, execution: Execution
) -> Transcript:
    results = []
    with tempfile.TemporaryDirectory() as temporary:
        (Path(temporary) / ".agents/console").mkdir(parents=True)
        settings = options(binary, execution)
        settings["server_parameters"]["cwd"] = temporary
        emitted = "x" * (8 * 1024 * 1024 + 7)

        def check(text: str) -> None:
            assert_preview(text, emitted)
            session = max(
                (Path(temporary) / ".agents/console/sessions").iterdir(),
                key=lambda path: path.name,
            )
            assert (
                session / "capture/sessions/001/cell-001.txt"
            ).read_text() == emitted
            results.append(
                {"preview": compact_text(text.replace(session.name, "<run ID>"), "x")}
            )

        with MCPConsole(**settings) as console:
            check(console.send(r="overflow console output"))
            assert console.send() == "\n[idle]"

        async def asynchronous() -> None:
            async with AsyncMCPConsole(**settings) as console:
                check(await console.send(r="overflow console output"))
                assert await console.send() == "\n[idle]"

        asyncio.run(asynchronous())
    return results


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_initialization_keeps_one_lifecycle_while_startup_is_pending(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        gate = Path(directory) / "startup.py"
        gate.write_text(
            # fmt: python
            code("""
                import json
                import subprocess
                import sys

                pending = [sys.stdin.buffer.readline()]
                if json.loads(pending[0])["method"] == "server/discover":
                    # Release on the SDK's initialize fallback, without a timing sleep.
                    # The real server sees every request, including the pending discovery.
                    pending.append(sys.stdin.buffer.readline())
                child = subprocess.Popen(sys.argv[1:], stdin=subprocess.PIPE)
                try:
                    for message in pending:
                        child.stdin.write(message)
                    child.stdin.flush()
                    for message in sys.stdin.buffer:
                        child.stdin.write(message)
                        child.stdin.flush()
                finally:
                    child.stdin.close()
                    child.wait(timeout=10)
                """)
        )
        settings = options(binary, execution)
        settings["command"] = sys.executable
        settings["args"] = ["-u", str(gate), str(binary), *settings["args"]]
        with MCPConsole(**settings) as console:
            result = console.send(r="echo initialized")
            assert result == "zod: initialized\n", result
        return [{"startup_kept_one_lifecycle": True, "output": result}]


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
        for create_tool in (
            mcp_console.openai.responses_tool,
            mcp_console.openai.agents_tool,
            mcp_console.anthropic.tool,
            mcp_console.chatlas.tool,
        ):
            try:
                create_tool(console)
            except RuntimeError as error:
                assert "connect" in str(error).lower(), error
            else:
                raise AssertionError("disconnected console created a tool")

    async def exercise_tools(console, source, expected_fields):
        live_tool = console.send_tool
        live_schema = live_tool.input_schema
        assert set(live_schema["properties"]) == expected_fields, live_schema
        chat = ChatOpenAI(model="unused", api_key="unused")

        def existing_tool() -> str:
            """An application-owned tool."""
            return "existing"

        chat.register_tool(existing_tool)
        existing = chat.get_tools()[0]
        chat.set_tools([*chat.get_tools(), mcp_console.chatlas.tool(console)])
        assert chat.get_tools()[0] is existing
        chat_tool = chat.get_tools()[1]
        agent = mcp_console.openai.agents_tool(console)
        anthropic = mcp_console.anthropic.tool(console)
        arguments = {"r": source}
        encoded = json.dumps(arguments)
        context = ToolContext(
            context=None,
            tool_name="send",
            tool_call_id="call_1",
            tool_arguments=encoded,
        )
        responses = mcp_console.openai.responses_tool(console)
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
            outputs.append(await output if inspect.isawaitable(output) else output)
        for schema, invoke in tools:
            assert schema == live_schema, schema
            validator = Draft202012Validator(schema)
            validator.validate(arguments)
            for omitted in {"r", "python", "sql", "requirements"} - expected_fields:
                value = {"r": ["praise"]} if omitted == "requirements" else "1"
                assert not validator.is_valid(arguments | {omitted: value}), schema
            output = invoke()
            outputs.append(await output if inspect.isawaitable(output) else output)
        assert all(output == outputs[0] for output in outputs), outputs
        return outputs[0]

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
                    {"r", "control", "requirements", "stdin", "timeout_ms"},
                ),
            ):
                async with AsyncMCPConsole(**settings) as console:
                    if label == "bare":
                        assert console.send_tool.input_schema["properties"][
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


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_callable_preserves_line_breaks_around_images(
    binary: Path, execution: Execution
) -> Transcript:
    async def exercise():
        async with AsyncMCPConsole(**options(binary, execution)) as console:
            return await console.send(r="emit image")

    asynchronous = asyncio.run(exercise())
    with MCPConsole(**options(binary, execution)) as console:
        synchronous = console.send(r="emit image")
    assert (
        synchronous
        == asynchronous
        == """before image
[image/png output]
after image
"""
    )
    return [{"output": synchronous}]


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
                chunks = [await console(**{language: source}, timeout_ms=0)]
                # A returned send can still be running. Collect its complete output
                # before submitting the next cell, including on a cold R startup.
                while chunks[-1].endswith(running):
                    chunks.append(await console.send())
                transcript.append({language: collected(chunks)})
            return transcript

    transcript = asyncio.run(exercise())
    with MCPConsole(**settings) as console:
        synchronous = []
        for language, source in cells.items():
            chunks = [console(**{language: source}, timeout_ms=0)]
            while chunks[-1].endswith(running):
                chunks.append(console.send())
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
            transcript.append({"recovery": await console.send(r="echo recovered")})
        await console.close()
        try:
            await console.send()
        except RuntimeError as error:
            transcript.append({"closed": str(error)})
        else:
            raise AssertionError("closed console accepted a call")
        async with console:
            transcript.append({"reconnected": await console.send(r="echo reconnected")})
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
            tool = mcp_console.openai.responses_tool(console)
            assert isinstance(tool, mcp_console.openai.AsyncResponsesTool)
            schema = tool.definition["parameters"]
            assert set(schema["properties"]) == {
                "r",
                "python",
                "sql",
                "stdin",
                "control",
                "requirements",
                "timeout_ms",
            }
            assert tool.definition["strict"] is False
            outputs = []
            for source in ("echo response", "emit image"):
                result = await tool(
                    ResponseFunctionToolCall(
                        type="function_call",
                        name="send",
                        call_id="call_1",
                        arguments=json.dumps({"r": source}),
                    )
                )
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
            tool = mcp_console.openai.agents_tool(console)
            assert isinstance(tool, FunctionTool)
            validate({"requirements": {"r": ["praise"]}}, tool.params_json_schema)
            agent = Agent(name="test", tools=[tool])
            assert agent.tools == [tool]
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
            return [result.model_dump(mode="json", by_alias=True, exclude_none=True)]

    return asyncio.run(exercise())


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_anthropic_callable_and_native_tools(
    binary: Path, execution: Execution
) -> Transcript:
    from pydantic_core import to_jsonable_python

    async def exercise():
        async with AsyncMCPConsole(**options(binary, execution)) as console:
            tool = mcp_console.anthropic.tool(console)
            assert tool.to_dict()["name"] == "send"
            text = await tool.call({"r": "echo anthropic"})
        async with mcp_console.anthropic.tools(
            **options(binary, execution), tool_kwargs={"defer_loading": True}
        ) as tools:
            assert len(tools) == 1
            assert tools[0].to_dict()["defer_loading"] is True
            images = await tools[0].call({"r": "emit image"})
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
            # The fixture stays blocked beyond the SDK's five-second default.
            started = time.monotonic()
            result = await server.call_tool("send", {"r": "stall", "timeout_ms": 6_000})
            assert time.monotonic() - started >= 5.9
            recorded = result.model_dump(mode="json", by_alias=True, exclude_none=True)
            text = recorded["content"][0]["text"]
            # Startup consumes the call budget before the admission clock begins.
            _, silent = elapsed_progress(text)
            assert silent
            recorded["content"][0]["text"] = normalize_elapsed(text)
            return [recorded]

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
            result = await tools[0].func(r="echo chatlas")
            return [{"name": tools[0].name, "value": result.value}]
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
            chat.set_tools([*chat.get_tools(), mcp_console.chatlas.tool(console)])
            tools = chat.get_tools()
            assert [tool.name for tool in tools] == ["send"]
            return [{"output": await tools[0].func(r="echo callable chatlas")}]

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
        transcript.append({"callable": console(r="echo sync")})
        try:
            console.send(r="echo r", python="echo python")
        except RuntimeError as error:
            transcript.append({"error": str(error)})
        else:
            raise AssertionError("invalid send succeeded")

        chat = ChatOpenAI(model="unused", api_key="unused")
        chat.set_tools([*chat.get_tools(), mcp_console.chatlas.tool(console)])
        chat_tool = chat.get_tools()[0]
        assert not inspect.iscoroutinefunction(chat_tool.func)
        transcript.append({"chatlas": chat_tool.func(r="echo chatlas")})

        anthropic_tool = mcp_console.anthropic.tool(console)
        transcript.append({"anthropic": anthropic_tool.call({"r": "echo anthropic"})})

        agent_tool = mcp_console.openai.agents_tool(console)
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
        transcript.append({"reconnected": console.send(r="echo reconnected")})
        # Closing must also clean up an active worker and the MCP transport.
        assert "running" in console.send(r="stall", timeout_ms=0)
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
        tool = mcp_console.openai.responses_tool(console)
        assert isinstance(tool, mcp_console.openai.ResponsesTool)
        assert tool.definition["name"] == "send"
        assert tool.definition["strict"] is False
        outputs = []
        for source in ("echo response", "emit image"):
            result = tool(
                ResponseFunctionToolCall(
                    type="function_call",
                    name="send",
                    call_id="call_1",
                    arguments=json.dumps({"r": source}),
                )
            )
            TypeAdapter(FunctionCallOutput).validate_python(result)
            outputs.append(result)
        assert tool.call({"r": "echo direct"}) == "zod: direct\n"
        return outputs


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
