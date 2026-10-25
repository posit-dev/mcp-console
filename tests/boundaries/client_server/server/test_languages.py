"""Captured model-facing languages do not configure SQL's embedded providers."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.installation import installed_console
from support.linux_sandbox import retain_system_bwrap
from support.normalization import code
from support.presentation import assert_tool_guidance, without_descriptions
from support.records import McpTranscript, Transcript, TranscriptWithCompanions
from support.requirements import POSIX, R, SQL, requires
from support.resolvers import expose_uv
from boundaries.client_server.python.test_without_r import (
    environment as without_r_environment,
)


def _configure(root: Path, languages: list[str]) -> Path:
    config = root / ".agents/console/config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"languages": languages}), encoding="utf-8")
    return config


def _environment(root: Path, *, without_r: bool = False) -> dict[str, str]:
    env = dict(os.environ, MCP_CONSOLE_HOME=str(root / "console-home"))
    env.pop("MCP_CONSOLE_LANGUAGES", None)
    if without_r:
        bin_dir = root / "bin"
        bin_dir.mkdir()
        expose_uv(bin_dir)
        retain_system_bwrap(bin_dir)
        env = without_r_environment(bin_dir, inherited=env)
    return env


def _tool(client: McpClient) -> dict:
    listed = client.request("tools/list")
    (tool,) = listed["result"]["tools"]
    return tool


@requires(SQL)
@executions(DIRECT)
def test_configures_captured_tool_surface(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    transcripts = {}
    context = {
        "scenario": (
            "Discover the configured tool interface, then verify that changing "
            "the project file does not change this running server's tools."
        ),
        "fixture": (
            "The project file initially enables R, Python, and SQL; a CLI override "
            "selects the languages below. The custom worker path is deliberately "
            "unused: this case only performs discovery, then rewrites the file "
            "to enable R alone and checks that tools/list is unchanged."
        ),
    }
    for languages in (
        ["r", "python", "sql"],
        ["sql"],
        ["sql", "python"],
        ["r"],
        ["python"],
        ["r", "sql"],
        ["r", "python"],
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _configure(root, ["r", "python", "sql"])
            with McpClient(
                binary,
                DIRECT.serve(
                    "--worker",
                    "unused-worker",
                    "-c",
                    "languages=" + json.dumps(languages),
                ),
                _environment(root),
                current_directory=root,
            ) as client:
                client.initialize_and_list_tools()
                tool = _tool(client)
                assert_tool_guidance(tool, set(languages), custom=True)
                schema = without_descriptions(tool)
                if languages == ["r", "python", "sql"]:
                    baseline = schema
                else:
                    expected = without_descriptions(baseline)
                    for field in {"r", "python", "sql"} - set(languages):
                        del expected["inputSchema"]["properties"][field]
                    assert schema == expected
                fields = set(tool["inputSchema"]["properties"])
                assert fields == {
                    *languages,
                    "note",
                    "control",
                    "requirements",
                    "stdin",
                    "timeout_ms",
                }, fields
                description = tool["description"]
                instructions = client.transcript[0]["result"].get("instructions")
                assert isinstance(instructions, str), client.transcript[0]
                assert instructions == description.split("\n\n", 1)[0]
                assert "\n" not in instructions and len(instructions) <= 250, (
                    instructions
                )
                assert set(re.findall(r"\b(?:R|Python|SQL)\b", instructions)) == {
                    name for name in ("R", "Python", "SQL") if name.lower() in languages
                }, instructions
                cell_guidance = description.split("Send one complete ")[1].split(
                    " cell per call"
                )[0]
                assert {
                    name
                    for name in ("r", "python", "sql")
                    if f"`{name}`" in cell_guidance
                } == set(languages)
                config.write_text("languages: [r]\n", encoding="utf-8")
                assert _tool(client) == tool
                name = "-".join(languages) + ("-only" if len(languages) == 1 else "")
                transcripts[f"{name}.yaml"] = McpTranscript(
                    [{**context, "languages": languages}] + client.finish()[:3]
                )
    return TranscriptWithCompanions(
        transcripts.pop("sql-only.yaml").transcript, transcripts
    )


@requires(SQL, R)
@executions(DIRECT, SANDBOXED)
def test_sql_provider_guidance_is_independent_of_visibility(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    transcripts = {}
    context = {
        "scenario": (
            "Evaluate SQL with the visible languages below, checking that the "
            "answer is 42 and that tool guidance stays independent of which "
            "installed runtime provides SQL."
        ),
        "fixture": (
            "These are real runtimes, not a fake worker. For the Python provider, "
            "a private PATH exposes uv but hides R, and inherited R-selection "
            "variables are removed; the R-provider case uses the host R. The "
            "languages and provider below identify this invocation."
        ),
    }
    for languages in (["sql", "python"], ["r", "sql"]):
        advertised = None
        for without_r in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _configure(root, languages)
                with McpClient(
                    installed_console(binary),
                    execution.serve(),
                    _environment(root, without_r=without_r),
                    root,
                ) as client:
                    client.initialize_and_list_tools()
                    tool = _tool(client)
                    description = tool["description"]
                    assert "provider" not in description.lower()
                    if advertised is None:
                        advertised = tool
                    else:
                        assert tool == advertised
                    if "python" in languages:
                        assert (
                            "on Python-owned DuckDB"
                            in tool["inputSchema"]["properties"]["python"][
                                "description"
                            ]
                        )
                        client.send(
                            # fmt: python
                            python=code("""
                                import pandas as pd

                                frame = pd.DataFrame({"value": [19, 23]})
                                """)
                        )
                        assert "Error" not in last_tool_text(client)
                        if without_r:
                            client.send(
                                python='_ = _console.sql_connection().register("visible_frame", frame)'
                            )
                        else:
                            client.send(python="r.visible_frame = frame")
                        assert "Error" not in last_tool_text(client)
                        client.send(
                            sql="SELECT sum(value) AS answer FROM visible_frame"
                        )
                    else:
                        client.send(sql="SELECT 42 AS answer")
                    assert "42" in last_tool_text(client), last_tool_text(client)
                    assert _tool(client) == tool
                    provider = "Python" if without_r else "R"
                    name = "-".join(languages) + f"-{provider.lower()}-provider"
                    session = client.finish()
                    transcripts[f"{name}.yaml"] = McpTranscript(
                        [{**context, "languages": languages, "provider": provider}]
                        + session[:3]
                        + [entry for entry in session[3:] if "send" in entry]
                    )
    return TranscriptWithCompanions(
        transcripts.pop("sql-python-r-provider.yaml").transcript, transcripts
    )


@requires(SQL, POSIX)
def test_rejects_hidden_source_before_custom_worker_effects(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _configure(root, ["sql"])
        started = root / "worker-started"
        env = dict(_environment(root), MCP_CONSOLE_TEST_ZOD_STARTED=str(started))
        with McpClient(binary, DIRECT.serve("--worker", str(zod)), env, root) as client:
            client.initialize_and_list_tools()
            for language in ("r", "python"):
                for source in (None, "must not run"):
                    result = client.send(
                        **{language: source},
                        control="restart",
                        requirements={"r": ["must-not-prepare"]},
                        stdin="must not queue\n",
                    )
                    assert result.get("isError"), result
                    assert result["content"] == [
                        {
                            "type": "text",
                            "text": f"`{language}` source fields are hidden by `languages`",
                        }
                    ], result
            assert not started.exists(), "rejected call launched a worker"
            result = client.send(sql="SELECT 1", wait_ms=0)
            assert "`r`" not in result["content"][0]["text"]
            assert "`python`" not in result["content"][0]["text"]
            transcript = client.finish()
            return transcript[:3] + [
                entry for entry in transcript[3:] if "send" in entry
            ]


@requires(SQL)
def test_invalid_language_configuration_fails_launch(
    binary: Path,
) -> TranscriptWithCompanions:
    transcripts = {}
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for name, value in (
            ("empty-languages", []),
            ("uppercase-language", ["SQL"]),
            ("unknown-language", ["ruby"]),
            ("scalar-language", "sql"),
            ("non-string-language", [1]),
        ):
            result = subprocess.run(
                [
                    str(binary),
                    *DIRECT.serve(
                        "--worker",
                        "unused-worker",
                        "-c",
                        "languages=" + json.dumps(value),
                    ),
                ],
                cwd=root,
                env=_environment(root),
                input="",
                text=True,
                capture_output=True,
                check=False,
            )
            assert result.returncode != 0, result
            assert "languages" in result.stderr, result.stderr
            transcripts[f"{name}.yaml"] = [
                {"languages": value, "stderr": result.stderr}
            ]
    return TranscriptWithCompanions(
        transcripts.pop("empty-languages.yaml"), transcripts
    )


def _catalog(client: McpClient) -> None:
    for source in (
        "CREATE TABLE retained(value INTEGER)",
        "INSERT INTO retained VALUES (41)",
        "BEGIN",
        "INSERT INTO retained VALUES (99)",
        "ROLLBACK",
        "BEGIN",
        "INSERT INTO retained VALUES (1)",
        "COMMIT",
    ):
        client.send(sql=source)
        assert "Error" not in last_tool_text(client), last_tool_text(client)
    client.send(sql="SELECT sum(value) AS answer FROM retained")
    assert "42" in last_tool_text(client), last_tool_text(client)


def _sql_profile(
    binary: Path,
    execution: Execution,
    *,
    without_r: bool,
    disabled_bootstrap: bool = False,
    python_visible: bool = False,
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        config = _configure(root, ["sql", "python"] if python_visible else ["sql"])
        env = _environment(root, without_r=without_r)
        if disabled_bootstrap:
            env["MCP_CONSOLE_LANGUAGES"] = "sql"
        with McpClient(
            installed_console(binary), execution.serve(), env, root
        ) as client:
            client.initialize_and_list_tools()
            tool = _tool(client)
            properties = tool["inputSchema"]["properties"]
            assert set(properties) & {"r", "python", "sql"} == (
                {"sql", "python"} if python_visible else {"sql"}
            )
            assert "DuckDB SQL first" in tool["description"]
            assert "`r`" not in tool["description"]
            assert "from an R cell" not in properties["sql"]["description"]
            if not python_visible:
                assert "register(name, frame)" not in properties["sql"]["description"]
            _catalog(client)
            assert _tool(client) == tool
            if python_visible:
                client.send(python="print(6 * 7)")
                assert last_tool_text(client) == "42\n"
                client.send(python="1", sql="SELECT 1")
                assert (
                    client.transcript[-1]["result"]["content"][0]["text"]
                    == "only one of `python` or `sql` may be supplied"
                )
            for language in ("r",) if python_visible else ("r", "python"):
                result = client.send(
                    **{language: "must not run"},
                    control="restart",
                    requirements={"action": "set"},
                )
                assert result.get("isError"), result
            client.send(sql="SELECT sum(value) AS answer FROM retained")
            assert "42" in last_tool_text(client)
            inspected = client.send(requirements={"action": "get"})
            assert {"r", "python", "duckdb"} <= set(
                inspected["structuredContent"]["requirements"]
            )
            client.send()
            client.send(control="interrupt")
            config.write_text("languages: [r]\n", encoding="utf-8")
            client.send(control="restart")
            client.send(sql="SHOW TABLES")
            assert "Error" not in last_tool_text(client)
            assert "retained" not in last_tool_text(client)
            assert _tool(client) == tool
            # The provider-specific default inventory belongs to requirements tests.
            # Assert inspection above; retain the handshake and SQL/control behavior.
            transcript = client.finish()
            return transcript[:3] + [
                entry
                for entry in transcript[3:]
                if "send" in entry
                and entry["send"] != {"requirements": {"action": "get"}}
            ]


@requires(SQL, R)
@executions(DIRECT, SANDBOXED)
def test_hidden_r_managed_sql(binary: Path, execution: Execution) -> Transcript:
    return _sql_profile(binary, execution, without_r=False)


@requires(SQL, R)
@executions(DIRECT, SANDBOXED)
def test_sql_python_with_hidden_r(binary: Path, execution: Execution) -> Transcript:
    return _sql_profile(binary, execution, without_r=False, python_visible=True)


@requires(SQL, R)
@executions(DIRECT, SANDBOXED)
def test_sql_with_disabled_direct_bootstrap(
    binary: Path, execution: Execution
) -> Transcript:
    return _sql_profile(binary, execution, without_r=False, disabled_bootstrap=True)


@requires(SQL)
@executions(DIRECT, SANDBOXED)
def test_hidden_python_managed_sql(binary: Path, execution: Execution) -> Transcript:
    return _sql_profile(binary, execution, without_r=True)


@requires(SQL)
@executions(DIRECT, SANDBOXED)
def test_sql_with_disabled_python_bootstrap(
    binary: Path, execution: Execution
) -> Transcript:
    return _sql_profile(binary, execution, without_r=True, disabled_bootstrap=True)


@requires(SQL)
@executions(DIRECT, SANDBOXED)
def test_missing_provider_recovers_without_setup_cell(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _configure(root, ["sql"])
        with McpClient(
            installed_console(binary),
            execution.serve(),
            _environment(root, without_r=True),
            root,
        ) as client:
            client.initialize_and_list_tools()
            tool = _tool(client)
            client.send(sql="SELECT 42 AS answer")
            assert "42" in last_tool_text(client)
            client.send(control="restart", requirements={"action": "set"})
            client.send(sql="SELECT 1")
            diagnostic = last_tool_text(client)
            assert "DuckDB is unavailable" in diagnostic
            assert "requirements.python" in diagnostic and "restart" in diagnostic
            assert _tool(client) == tool
            client.send(control="restart", requirements={"python": ["duckdb"]})
            client.send(sql="SELECT 42 AS recovered")
            assert "42" in last_tool_text(client)
            assert _tool(client) == tool
            transcript = client.finish()
            return transcript[:3] + [
                entry for entry in transcript[3:] if "send" in entry
            ]
