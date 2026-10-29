#!/usr/bin/env -S uv run --script

import json
import os
import shutil
from contextlib import ExitStack
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.requirements import POSIX, R, SQL, command, requires
from support.assertions import (
    assert_result_content,
    last_result_text,
    wait_for_worker_ready,
)
from support.client import McpClient
from support.checkpoints import wait_for_checkpoint, wait_for_path
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code, normalize_recording
from support.r import r_test_environment, reference_plots
from support.records import Transcript, TranscriptWithCompanions
from support.suites import run_this_suite


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_records_existing_python_startup_declaration(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        workspace = Path(temporary_directory)
        environment, _ = r_test_environment()
        environment["RETICULATE_PYTHON"] = sys.executable
        client = McpClient(
            binary,
            execution.serve(),
            environment,
            current_directory=workspace,
        )
        client.initialize_and_list_tools()
        wait_for_worker_ready(client, "startup declaration available for inspection")
        inspection = client.send(requirements={"action": "get"})["structuredContent"]
        assert inspection["requirements"]["python"] == [], inspection
        client.expect("42\n", r="cat('42\\n')", requirements={"r": ["jsonlite"]})
        accepted = client.send(requirements={"action": "get"})["structuredContent"]
        assert "jsonlite" in accepted["requirements"]["r"], accepted
        client.expect("42\n", python="print(42)")
        client.finish()

        (session,) = (workspace / ".agents/console/sessions").iterdir()
        events = [
            json.loads(line)
            for line in (session / "capture/events.jsonl").read_text().splitlines()
        ]
        (r_call,) = [
            event["call_id"]
            for event in events
            if event["event"] == "tool_call" and "r" in event["request"]["arguments"]
        ]
        (cell,) = [
            event
            for event in events
            if event["event"] == "cell_started" and event["call_id"] == r_call
        ]
        session_id = cell["owner"]["session_id"]
        quarto = (session / f"sessions/session-{session_id:03}.qmd").read_text(
            encoding="utf-8"
        )
        assert "  python-packages: []\n" in quarto, quarto
        assert "    - jsonlite\n" in quarto, quarto
        (startup,) = [
            event["startup_requirements"]
            for event in events
            if event["event"] in ("recording_started", "environment_discovered")
            and event["startup_requirements"] is not None
        ]
        assert startup["python"] == [], startup
        assert startup["r"] == inspection["requirements"]["r"], startup
        return [{"recorded_startup_requirements": startup}]


@requires(R, SQL, command("ir"), command("rustc"))
@executions(DIRECT, SANDBOXED)
def test_records_real_mixed_language_session(
    binary: Path,
    execution: Execution,
) -> TranscriptWithCompanions:
    with (
        tempfile.TemporaryDirectory(
            prefix="console-recording-é-"
        ) as temporary_directory,
        ExitStack() as resources,
    ):
        workspace = Path(temporary_directory)
        environment, rscript = r_test_environment()
        environment.pop("RETICULATE_PYTHON", None)
        real_ir = shutil.which("ir")
        assert real_ir is not None
        commands = workspace / "commands"
        commands.mkdir()
        fixture = commands / ("ir.exe" if os.name == "nt" else "ir")
        subprocess.run(
            [
                "rustc",
                str(Path(__file__).resolve().parents[3] / "fixtures/checkpoint_ir.rs"),
                "-o",
                str(fixture),
            ],
            check=True,
        )
        release = workspace / "preparation-release"
        started = workspace / "preparation-started"
        environment.update(
            {
                "PATH": os.pathsep.join((str(commands), environment["PATH"])),
                "UV_TOOL_DIR": str(workspace),
                "MCP_CONSOLE_TEST_REAL_IR": real_ir,
                "MCP_CONSOLE_TEST_IR_CLAIM": str(workspace / "preparation-claimed"),
                "MCP_CONSOLE_TEST_IR_STARTED": str(started),
                "MCP_CONSOLE_TEST_IR_RELEASE": str(release),
            }
        )
        client = resources.enter_context(
            McpClient(
                binary,
                execution.serve("-c", "cache=host"),
                environment,
                current_directory=workspace,
            )
        )
        resources.callback(release.touch)
        client.initialize_and_list_tools()
        # Runtime discovery precedes initial package preparation. Hold that
        # preparation until the first response is received, fixing both discovery
        # and worker-launch ordering without assuming startup speed.
        wait_for_path(
            started,
            "initial worker preparation started",
            client=client,
            timeout=client.response_timeout,
        )
        initial = client.send(requirements={"action": "get"}, timeout_ms=0)
        assert not initial.get("isError"), initial
        assert last_result_text(client) == (
            "\n[phase: dependency preparation]\n[worker starting]"
        ), initial
        ready = client.start_send(requirements={"action": "get"}, timeout_ms=60000)
        (recording,) = (workspace / ".agents/console/sessions").iterdir()

        def readiness_call_recorded() -> bool:
            data = (recording / "capture/events.jsonl").read_bytes()
            lines = data.rpartition(b"\n")[0].splitlines()
            return any(
                event["event"] == "tool_call" and event["request_id"] == ready["id"]
                for event in map(json.loads, lines)
            )

        wait_for_checkpoint(
            readiness_call_recorded,
            "readiness request captured before worker launch",
            root=recording,
            recursive=True,
            client=client,
        )
        release.touch()
        client.receive(ready)
        assert ready["result"]["structuredContent"]["prepared"] is True, ready
        # Standalone preparation finishes before recording the cells, even
        # when cold dependencies outlive an evaluation's observation budget.
        client.expect(
            "[prepared]",
            requirements={
                "r": ["graphics", "grDevices"],
                "python": ["pandas"],
                "duckdb": ["sqlite"],
            },
        )

        # fmt: r
        r = code(r"""
            options(
              console.plot.width_in = 4,
              console.plot.height_in = 3,
              console.plot.dpi = 100
            )
            measurements <- data.frame(
              label = c("a", "b"),
              value = c(2L, 5L)
            )
            plot(measurements$value, type = "b")
            """)
        expected_plot = reference_plots(
            rscript,
            environment,
            r,
            width=4,
            height=3,
            dpi=100,
            pages=1,
        )
        client.send(r=r)
        assert_result_content(client, expected_plot)

        # fmt: python
        python = code("""
            int(r.measurements["value"].sum())
            """)
        client.send(python=python)
        assert client.transcript[-1]["result"]["content"] == [
            {"type": "text", "text": "7\n"}
        ]

        sql = code("""
            SELECT label, value * 10 AS scaled
            FROM measurements
            ORDER BY label
            """)
        client.send(sql=sql)
        sql_output = client.transcript[-1]["result"]["content"][0]["text"]
        assert '"a"' in sql_output and "20" in sql_output, sql_output
        assert '"b"' in sql_output and "50" in sql_output, sql_output
        session = next((workspace / ".agents/console" / "sessions").iterdir())
        events = [
            json.loads(line)
            for line in (session / "capture" / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        artifacts = [event for event in events if event["event"] == "image_retained"]
        assert len(artifacts) == 1, artifacts
        artifact = artifacts[0]
        assert (session / artifact["path"]).read_bytes() == expected_plot[0]

        markdown = (session / "transcript.md").read_text(encoding="utf-8")
        assert "Retained text output" not in markdown, markdown
        assert "terminated by signal" not in markdown, markdown
        assert "## Runtime discovery" not in markdown, markdown
        assert markdown.count('"runtime_requirements"') == 1, markdown
        (cell,) = [
            event
            for event in events
            if event["event"] == "cell_started" and event["call_id"] == 4
        ]
        session_id = cell["owner"]["session_id"]
        quarto = (session / f"sessions/session-{session_id:03}.qmd").read_text(
            encoding="utf-8"
        )
        # Snapshot at the completed SQL response. Bounded EOF retirement can
        # legitimately exit or require a forced stop; verify its exact capture
        # below without imposing that scheduling outcome on the source snapshot.
        transcript = client.finish()
        completed = (session / "transcript.md").read_text(encoding="utf-8")
        assert completed.startswith(markdown)
        assert completed.endswith("## Recording finished\n\n")
        assert (session / f"sessions/session-{session_id:03}.qmd").read_text(
            encoding="utf-8"
        ) == quarto
        completed_events = [
            json.loads(line)
            for line in (session / "capture/events.jsonl").read_text().splitlines()
        ]
        (ended,) = [
            event
            for event in completed_events
            if event["event"] == "worker_ended" and event["session_id"] == session_id
        ]
        assert ended["outcome"], ended
        assert f"## Session {session_id:03} ended\n" in completed
        assert completed_events[-1]["event"] == "recording_ended"
        assert "## Image 1 for call 4" in markdown
        assert "![Image 1]" in markdown
        assert artifact["path"] in markdown
        assert (
            f"[Session {session_id:03} source (QMD)]"
            f"(<sessions/session-{session_id:03}.qmd>)" in markdown
        )
        assert f"```{{r}}\n#| error: true\n{r}```" in quarto
        assert f"```{{python}}\n#| error: true\n{python}```" in quarto
        assert f"```{{sql}}\n--| error: true\n{sql}```" in quarto
        assert "Image 1" not in quarto
        assert "execute:" not in quarto
        assert "original-working-directory:" in quarto
        assert "root.dir:" not in quarto
        assert markdown.endswith("\n")
        assert quarto.endswith("\n")

        session_event = events[0]
        console_version = (
            subprocess.check_output([binary, "--version"], text=True)
            .strip()
            .removeprefix("mcp-console ")
        )
        assert session_event["console_version"] == console_version
        assert f'"console_version": {json.dumps(console_version)}' in markdown
        markdown = markdown.replace(
            f'"console_version": {json.dumps(console_version)}',
            '"console_version": "<Console version>"',
        )
        recorded_workspace = session_event["working_directory"]
        assert Path(recorded_workspace).samefile(workspace)
        markdown = markdown.replace(session_event["run_id"], "<run ID>")
        for representation in (
            json.dumps(recorded_workspace, ensure_ascii=False)[1:-1],
            recorded_workspace,
        ):
            markdown = markdown.replace(representation, "<workspace>")
            quarto = quarto.replace(representation, "<workspace>")
        for event in events:
            markdown = markdown.replace(event["at"], "<UTC timestamp>")
            if event["event"] == "worker_started":
                assert isinstance(event["pid"], int) and event["pid"] > 0
                markdown = markdown.replace(
                    f"Worker process {event['pid']}.", "Worker process <PID>."
                )

        return TranscriptWithCompanions(
            transcript=transcript,
            # These are literal recorder outputs. YAML scalars keep embedded
            # formatters from rewriting code fences in the expected documents.
            companions={"documents.yaml": [{"md": markdown, "qmd": quarto}]},
        )


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@requires(command("yamark"))
def test_emits_yamark_formatted_documents(
    binary: Path, execution: Execution
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        workspace = Path(temporary_directory)
        client = McpClient(
            binary,
            execution.serve("--worker", str(zod)),
            current_directory=workspace,
        )
        client.initialize_and_list_tools()
        client.send(r="emit image")
        source = """echo before
````
<div>not markdown</div>
after"""
        client.send(python=source)
        client.request(
            "tools/call",
            name="send",
            arguments={"sql": "  --| eval: false\necho SELECT 42", "typo": True},
        )
        client.request(
            "tools/call",
            name="send",
            arguments={
                "r": "echo rejected",
                "python": "echo rejected",
                "requirements": {
                    "r": [
                        "foo:",
                        "foo#bar",
                        "delete\u007fcontrol",
                        "c1-start\u0080control",
                        "c1-end\u009fcontrol",
                        "bom\ufeffvalue",
                        "noncharacter\ufffevalue",
                        "noncharacter\uffffvalue",
                        "line\u2028separator",
                        "paragraph\u2029separator",
                    ]
                },
            },
        )
        transcript = client.finish()

        session = next((workspace / ".agents/console" / "sessions").iterdir())
        markdown = (session / "transcript.md").read_text(encoding="utf-8")
        quarto = (session / "sessions/session-001.qmd").read_text(encoding="utf-8")
        assert f"`````python\n{source}\n`````" in markdown
        assert (
            """`````text
zod python: before
````
<div>not markdown</div>
after
`````"""
            in markdown
        )
        assert (
            """```sql
  --| eval: false
echo SELECT 42
```"""
            in markdown
        )
        assert '"typo": true' in markdown
        assert "```{r}\n#| error: true\nemit image\n```" in quarto
        assert f"`````{{python}}\n#| error: true\n{source}\n`````" in quarto
        assert (
            """```{sql}
--| error: true

  --| eval: false
echo SELECT 42
```"""
            in quarto
        )
        assert "execute:" not in quarto
        for value in (
            "foo:",
            "foo#bar",
            "delete\u007fcontrol",
            "c1-start\u0080control",
            "c1-end\u009fcontrol",
            "bom\ufeffvalue",
            "noncharacter\ufffevalue",
            "noncharacter\uffffvalue",
            "line\u2028separator",
            "paragraph\u2029separator",
        ):
            assert value not in quarto, (value, quarto)
            assert value in markdown, (value, markdown)
        formatting = subprocess.run(
            [
                "yamark",
                "format",
                "--diff",
                "--wrap",
                "sentence",
                "--skip-embedded-formatters",
                "transcript.md",
                "sessions/session-001.qmd",
            ],
            cwd=session,
            check=False,
            capture_output=True,
            text=True,
        )
        assert formatting.returncode == 0, {
            "stdout": formatting.stdout,
            "stderr": formatting.stderr,
            "transcript.md": (session / "transcript.md").read_text(encoding="utf-8"),
            "sessions/session-001.qmd": (
                session / "sessions/session-001.qmd"
            ).read_text(encoding="utf-8"),
        }
        transcript.append(
            {
                "generated documents": {
                    "transcript.md": "Yamark formatted",
                    "sessions/session-001.qmd": "Yamark formatted",
                }
            }
        )
        return transcript


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_records_polls_input_controls_and_previews(
    binary: Path, execution: Execution
) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        with McpClient(
            binary,
            execution.serve("--worker", str(worker)),
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            client.expect("zod: ready\n", r="echo ready")
            request_ids = {"ready": client.transcript[-1]["id"]}
            client.expect("\n[idle]")
            request_ids["poll"] = client.transcript[-1]["id"]
            waiting = client.send(r="request input")
            request_ids["input"] = client.transcript[-1]["id"]
            assert "[waiting for stdin]" in last_result_text(client), waiting
            client.expect("zod stdin: supplied answer\n", stdin="supplied answer\n")
            request_ids["stdin"] = client.transcript[-1]["id"]
            # Recording retains every real poll, including any collected by
            # expect(). Keep an explicit extra poll to exercise that identity.
            polled = client.send(session="default")
            request_ids["extra_poll"] = client.transcript[-1]["id"]
            assert last_result_text(client) == "\n[idle]", polled
            client.send(control="interrupt")
            request_ids["interrupt"] = client.transcript[-1]["id"]
            client.send(r="preview huge line")
            request_ids["preview"] = client.transcript[-1]["id"]
            preview = last_result_text(client)
            assert "[output omitted:" in preview, preview
            transcript = client.finish()
        (recording,) = (workspace / ".agents/console/sessions").iterdir()
        capture = [
            json.loads(line)
            for line in (recording / "capture/events.jsonl").read_text().splitlines()
        ]
        calls = [event for event in capture if event["event"] == "tool_call"]
        call_ids: dict[str, int] = {}
        for name, request_id in request_ids.items():
            matches = [event for event in calls if event["request_id"] == request_id]
            assert len(matches) == 1, {
                "operation": name,
                "request_id": request_id,
                "calls": calls,
            }
            call_ids[name] = matches[0]["call_id"]
        assert list(call_ids.values()) == sorted(set(call_ids.values())), call_ids
        source_calls = {
            event["call_id"]
            for event in capture
            if event["event"] == "tool_call"
            and any(
                language in event["request"]["arguments"]
                for language in ("r", "python", "sql")
            )
        }
        assert {
            event["call_id"]
            for event in capture
            if event["event"] == "source_associated"
        } == source_calls
        markdown = (recording / "transcript.md").read_text()
        headings = [
            f"## Call {call_ids[name]}: {label}\n"
            for name, label in (
                ("ready", "R"),
                ("poll", "Poll"),
                ("input", "R"),
                ("stdin", "Stdin"),
                ("extra_poll", "Poll"),
                ("interrupt", "Interrupt"),
                ("preview", "R"),
            )
        ]
        for heading in headings:
            assert markdown.count(heading) == 1, markdown
        positions = [markdown.index(heading) for heading in headings]
        assert positions == sorted(positions), markdown
        assert "### Submitted stdin\n\n```text\nsupplied answer\n```" in markdown, (
            markdown
        )
        assert markdown.count("\nzod stdin: supplied answer\n") == 1, markdown
        assert markdown.count("\nzod: ready\n") == 1, markdown
        assert preview in markdown, markdown
        assert f"[Retained text output for call {call_ids['ready']}]" not in markdown, (
            markdown
        )
        assert f"[Retained text output for call {call_ids['preview']}](<" in markdown, (
            markdown
        )
        for chatter in (
            "## Protocol",
            "## Worker ready",
            "## Source attached",
            "## Cell started",
        ):
            assert chatter not in markdown, markdown
        source = (recording / "sessions/session-001.qmd").read_text()
        assert "request input" in source and "preview huge line" in source
        for omitted in ("supplied answer", "control:", "[idle]", "[output omitted:"):
            assert omitted not in source, source
        return normalize_recording(transcript, workspace)


if __name__ == "__main__":
    run_this_suite(__file__)
