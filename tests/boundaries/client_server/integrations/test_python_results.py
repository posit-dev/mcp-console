#!/usr/bin/env -S uv run --script

import asyncio
import base64
import inspect
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "python"))

from mcp_console import AsyncMCPConsole, MCPConsole
from support.execution import RUNTIME, Execution, executions
from support.normalization import code
from support.r import r_test_environment, reference_plots
from support.records import McpTranscript, Transcript, TranscriptWithCompanions
from support.requirements import R, requires
from support.suites import run_this_suite


def test_results_keep_native_content_and_display_in_order(binary: Path) -> Transcript:
    from IPython.core.interactiveshell import InteractiveShell
    from IPython.display import display
    from IPython.utils.capture import capture_output
    from mcp.types import CallToolResult, ImageContent, TextContent
    from mcp_console import ConsoleResult

    # The base package and client constructors remain usable without extras.
    imported = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            # fmt: python
            code(r"""
                import sys

                sys.path.insert(0, sys.argv[1])
                import mcp_console
                from mcp_console import AsyncMCPConsole, MCPConsole

                AsyncMCPConsole()
                MCPConsole()
                assert {"ConsoleError", "ConsoleResult"} <= set(dir(mcp_console))
                assert "mcp" not in sys.modules
                assert "IPython" not in sys.modules
                """),
            str(ROOT / "python"),
        ],
        capture_output=True,
        text=True,
    )
    assert imported.returncode == 0, (imported.stdout, imported.stderr)
    assert issubclass(ConsoleResult, CallToolResult)
    image = ImageContent(
        data="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aB8sAAAAASUVORK5CYII=",
        mime_type="image/png",
        annotations={"audience": ["user"], "priority": 0.5},
        meta={"caption": "original image"},
    )
    result = ConsoleResult(
        content=[
            TextContent(text="before image\n"),
            image,
            TextContent(text="between images\n"),
            image,
            TextContent(text="after image\n"),
        ],
        structured_content={"summary": "original structured content"},
        meta={"source": "original result"},
    )
    assert isinstance(result, CallToolResult)
    assert not hasattr(result, "raw")
    assert result.images == [image, image]
    assert result.images[0] is image
    assert result.structured_content == {"summary": "original structured content"}
    assert result.meta == {"source": "original result"}
    assert result.images[0].meta == {"caption": "original image"}
    assert result.is_error is False
    assert result.result_type == "complete"
    assert result.text == "before image\nbetween images\nafter image\n"
    assert (
        str(result)
        == "before image\n[image/png output]\nbetween images\n[image/png output]\nafter image\n"
    )
    serialized = result.model_dump(mode="json", by_alias=True, exclude_none=True)
    native = CallToolResult.model_validate_json(result.model_dump_json())
    assert serialized == native.model_dump(
        mode="json", by_alias=True, exclude_none=True
    )
    restored = ConsoleResult.model_validate_json(result.model_dump_json())
    assert restored.model_dump() == result.model_dump()
    assert restored.text == result.text
    assert restored.images == result.images

    InteractiveShell.instance()
    with capture_output() as shown:
        display(result)
    assert shown.stdout == shown.stderr == ""
    shown_content = [output.data for output in shown.outputs]
    assert shown_content == [
        {"text/plain": "before image\n"},
        {"image/png": image.data, "text/plain": "[image/png output]"},
        {"text/plain": "between images\n"},
        {"image/png": image.data, "text/plain": "[image/png output]"},
        {"text/plain": "after image\n"},
    ], shown_content

    empty = ConsoleResult(content=[])
    assert empty.text == str(empty) == ""
    assert empty.images == []
    with capture_output() as empty_display:
        display(empty)
    assert empty_display.stdout == empty_display.stderr == ""
    assert empty_display.outputs == []

    split_text = ConsoleResult(
        content=[TextContent(text="first"), TextContent(text="second")]
    )
    assert split_text.text == "firstsecond"
    assert str(split_text) == "first\nsecond"

    requirements = {"requirements": {"python": []}}
    inspected = ConsoleResult(
        content=[TextContent(text="bounded preview")],
        structured_content=requirements,
    )
    assert inspected.structured_content == requirements
    assert inspected.structured_content is requirements
    assert inspected.text == "bounded preview"
    assert str(inspected) == json.dumps(requirements, indent=2)
    return [
        {
            "fixture": "The public result extends the MCP SDK model, retaining native content, metadata, and serialization; IPython displays it without a framework adapter. Client imports and constructors remain usable without optional dependencies."
        },
        {"result": serialized},
        {"text": result.text, "printed": str(result), "displayed": shown_content},
        {
            "empty_display": [],
            "requirements_text": inspected.text,
            "requirements_printed": str(inspected),
        },
    ]


@requires(R)
@executions(RUNTIME)
def test_clients_return_images_and_keep_error_results(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    from IPython.core.interactiveshell import InteractiveShell
    from IPython.display import display
    from IPython.utils.capture import capture_output
    from mcp.types import CallToolResult, ImageContent, TextContent
    from mcp_console import ConsoleError, ConsoleResult

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
    error_arguments = {"r": plot_code + "\n" + exit_code, "timeout_ms": 300_000}

    async def exercise(console) -> list[CallToolResult]:
        async def invoke(function, *args, **kwargs):
            value = function(*args, **kwargs)
            return await value if inspect.isawaitable(value) else value

        InteractiveShell.instance()
        with capture_output() as during_send:
            result = await invoke(console.send, **image_arguments)
        assert during_send.stdout == during_send.stderr == ""
        assert during_send.outputs == []
        assert isinstance(result, ConsoleResult), type(result)
        assert isinstance(result, CallToolResult)
        assert not hasattr(result, "raw")
        assert [type(item) for item in result.content] == [
            TextContent,
            ImageContent,
            TextContent,
        ]
        assert result.images == [result.content[1]]
        assert result.images[0].mime_type == "image/png"
        assert result.images[0].data == expected_image
        assert result.text == "before image\nafter image\n"
        assert str(result) == "before image\n[image/png output]\nafter image\n"
        assert result.is_error is False
        assert result.structured_content is None
        with capture_output() as displayed:
            display(result)
        assert displayed.stdout == displayed.stderr == ""
        assert [output.data for output in displayed.outputs] == [
            {"text/plain": "before image\n"},
            {"image/png": expected_image, "text/plain": "[image/png output]"},
            {"text/plain": "after image\n"},
        ]

        text = await invoke(console, r='cat("text\\n")')
        assert isinstance(text, ConsoleResult), type(text)
        assert isinstance(text, CallToolResult)
        assert text.text == str(text) == "text\n"
        assert text.images == []
        raw = await invoke(console.call_tool, "send", {"r": 'cat("raw\\n")'})
        assert isinstance(raw, CallToolResult), type(raw)
        assert raw.content == [TextContent(text="raw\n")]

        try:
            await invoke(console.send, r='cat("r\\n")', python='print("python")')
        except ConsoleError as rejected:
            assert isinstance(rejected, RuntimeError)
            invalid = rejected.result
            assert isinstance(invalid, ConsoleResult)
            assert isinstance(invalid, CallToolResult)
            assert invalid.is_error is True
            assert invalid.images == []
            assert (
                invalid.text
                == str(rejected)
                == "only one of `r`, `python`, or `sql` may be supplied"
            )
        else:
            raise AssertionError("invalid send did not raise ConsoleError")

        try:
            await invoke(console, **error_arguments)
        except ConsoleError as rejected:
            error = rejected.result
            assert isinstance(error, ConsoleResult)
            assert isinstance(error, CallToolResult)
            assert error.is_error is True
            assert error.images[0].data == expected_image
            assert error.images[0].mime_type == "image/png"
            assert error.content[0] == TextContent(text="before image\n")
            assert error.content[-1].text.startswith("after image\n")
            assert "[worker exited with status 33]" in error.text
            assert str(rejected) == str(error)
            assert "[image/png output]" in str(rejected)
            with capture_output() as error_display:
                display(error)
            assert error_display.stdout == error_display.stderr == ""
            assert [output.data for output in error_display.outputs] == [
                {"text/plain": error.content[0].text},
                {"image/png": expected_image, "text/plain": "[image/png output]"},
                {"text/plain": error.content[-1].text},
            ]
        else:
            raise AssertionError("image-bearing failure did not raise ConsoleError")
        return [result, text, raw, invalid, error]

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
                        results = await exercise(console)
                else:
                    with client_type(**settings) as console:
                        results = await exercise(console)
                transcript = json.loads(capture.read_text(encoding="utf-8"))
                calls = [
                    entry
                    for entry in transcript
                    if entry["input"]["method"] == "tools/call"
                ]
                assert len(calls) == len(results)
                for entry, result in zip(calls, results, strict=True):
                    native = CallToolResult.model_validate(entry["result"])
                    assert result.model_dump() == native.model_dump()
                    assert result.model_fields_set == native.model_fields_set
                    for content in entry["result"]["content"]:
                        if content["type"] == "image":
                            assert content == {
                                "type": "image",
                                "mimeType": "image/png",
                                "data": expected_image,
                            }
                            content["data"] = (
                                "<PNG byte-identical to live Rscript page 1>"
                            )
                assert transcript[0]["input"]["method"] == "server/discover"
                transcripts.append(
                    [
                        {
                            "scenario": f"Rich results through {client_type.__name__}: an image, text, a raw call, an invalid call, then an image-bearing worker exit.",
                            "fixture": "The built-in R runtime produces PNG bytes compared with a live Rscript reference. The proxy records MCP exchanges; Python asserts result preservation and notebook display without model API calls.",
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


if __name__ == "__main__":
    run_this_suite(__file__)
