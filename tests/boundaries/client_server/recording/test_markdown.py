#!/usr/bin/env -S uv run --script

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.requirements import POSIX, R, SQL, command, requires
from support.assertions import assert_result_content
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
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
        inspection = client.send(requirements={"action": "get"})["structuredContent"]
        assert inspection["requirements"]["python"] == [], inspection
        client.expect("42\n", r="cat('42\\n')", requirements={"r": ["jsonlite"]})
        accepted = client.send(requirements={"action": "get"})["structuredContent"]
        assert "jsonlite" in accepted["requirements"]["r"], accepted
        client.expect("42\n", python="print(42)")
        client.finish()

        (session,) = (workspace / ".agents/console/sessions").iterdir()
        quarto = (session / "transcript.qmd").read_text(encoding="utf-8")
        assert "  python-packages: []\n" in quarto, quarto
        assert "    - jsonlite\n" in quarto, quarto
        events = [
            json.loads(line)
            for line in (session / "internal/events.jsonl").read_text().splitlines()
        ]
        (startup,) = [
            event["startup_requirements"]
            for event in events
            if event["event"] in ("recording_started", "environment_discovered")
            and event["startup_requirements"] is not None
        ]
        assert startup["python"] == [], startup
        assert startup["r"] == inspection["requirements"]["r"], startup
        return [{"recorded_startup_requirements": startup}]


@requires(R, SQL)
@executions(DIRECT, SANDBOXED)
def test_records_real_mixed_language_session(
    binary: Path,
    execution: Execution,
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(
        prefix="console-recording-é-"
    ) as temporary_directory:
        workspace = Path(temporary_directory)
        environment, rscript = r_test_environment()
        environment.pop("RETICULATE_PYTHON", None)
        client = McpClient(
            binary,
            execution.serve(),
            environment,
            current_directory=workspace,
        )
        client.initialize_and_list_tools()
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
        transcript = client.finish()

        session = next((workspace / ".agents/console" / "sessions").iterdir())
        events = [
            json.loads(line)
            for line in (session / "internal" / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        artifacts = [event for event in events if event["event"] == "artifact_created"]
        assert len(artifacts) == 1, artifacts
        artifact = artifacts[0]
        assert (session / artifact["path"]).read_bytes() == expected_plot[0]

        markdown = (session / "transcript.md").read_text(encoding="utf-8")
        quarto = (session / "transcript.qmd").read_text(encoding="utf-8")
        assert "[Artifact 1 from call 2]" in markdown
        assert "![Artifact 1]" in markdown
        assert artifact["path"] in markdown
        assert f"```{{r}}\n{r}```" in quarto
        assert f"```{{python}}\n{python}```" in quarto
        assert f"```{{sql}}\n{sql}```" in quarto
        assert "Artifact 1" not in quarto
        assert "execute:" not in quarto
        assert (
            "# Run `ir render transcript.qmd` in a prepared environment to execute these cells."
            in quarto.split("---", 2)[1]
        )
        assert markdown.endswith("\n")
        assert quarto.endswith("\n")

        session_event = events[0]
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
        quarto = (session / "transcript.qmd").read_text(encoding="utf-8")
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
        assert "```{r}\nemit image\n```" in quarto
        assert f"`````{{python}}\n{source}\n`````" in quarto
        assert (
            """```{sql}

  --| eval: false
echo SELECT 42
```"""
            in quarto
        )
        assert "execute:" not in quarto
        assert '    - "foo:"' in quarto
        assert "    - foo#bar" in quarto
        assert '    - "delete\\x7Fcontrol"' in quarto
        assert '    - "c1-start\\x80control"' in quarto
        assert '    - "c1-end\\x9Fcontrol"' in quarto
        assert '    - "bom\\uFEFFvalue"' in quarto
        assert '    - "noncharacter\\uFFFEvalue"' in quarto
        assert '    - "noncharacter\\uFFFFvalue"' in quarto
        assert '    - "line\\Lseparator"' in quarto
        assert '    - "paragraph\\Pseparator"' in quarto
        formatting = subprocess.run(
            [
                "yamark",
                "format",
                "--diff",
                "--wrap",
                "sentence",
                "--skip-embedded-formatters",
                "transcript.md",
                "transcript.qmd",
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
            "transcript.qmd": (session / "transcript.qmd").read_text(encoding="utf-8"),
        }
        transcript.append(
            {
                "generated documents": {
                    "transcript.md": "Yamark formatted",
                    "transcript.qmd": "Yamark formatted",
                }
            }
        )
        return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
