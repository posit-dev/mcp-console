#!/usr/bin/env -S uv run --script

import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.requirements import POSIX, SANDBOX, SQL, WORKER, requires
from support.client import McpClient
from support.evidence import compact_text
from support.previews import compact_previews
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.linux_sandbox import retain_system_bwrap
from support.normalization import code
from support.records import Transcript, TranscriptWithCompanions
from support.resolvers import bare_runtime_environment
from support.suites import run_this_suite
from support.snapshots import execution_snapshots


@contextmanager
def _admission_client(binary: Path) -> Iterator[tuple[McpClient, Path]]:
    """Allow capability discovery, but record and reject actual preparation."""
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        fake_bin = temporary / "bin"
        fake_bin.mkdir()
        resolver_probe = fake_bin / "resolver-probe"
        resolver_probe.write_text(
            code(r"""
                #!/bin/sh

                set -eu
                printf '%s\n' "$*" >> "$MCP_CONSOLE_TEST_RESOLVER_RECORD"
                if [ "$#" -eq 1 ] && [ "$1" = "--version" ]; then
                  printf 'ir 0.4.0\n'
                  exit 0
                fi
                exit 97
                """),
            encoding="utf-8",
        )
        resolver_probe.chmod(0o755)
        (fake_bin / "ir").symlink_to(resolver_probe)
        (fake_bin / "uv").symlink_to(resolver_probe)

        environment = os.environ.copy()
        path = environment.get("PATH")
        assert path is not None, "PATH is required"
        environment["PATH"] = os.pathsep.join((str(fake_bin), path))
        environment["RETICULATE_UV"] = str(resolver_probe)
        resolver_record = temporary / "resolver-record"
        resolver_record.touch()
        worker_started = temporary / "zod-started"
        environment["TMPDIR"] = str(temporary)
        environment["MCP_CONSOLE_TEST_RESOLVER_RECORD"] = str(resolver_record)
        environment["MCP_CONSOLE_TEST_ZOD_STARTED"] = str(worker_started)
        environment["MCP_CONSOLE_TEST_ZOD_PYTHON_CELLS"] = str(
            temporary / "cells.jsonl"
        )

        with McpClient(
            binary, DIRECT.serve("--worker", str(zod)), environment
        ) as client:
            client.initialize_and_list_tools()
            yield client, temporary
            invocations = resolver_record.read_text(encoding="utf-8").splitlines()
            assert all(arguments == "--version" for arguments in invocations), (
                "invalid input started dependency preparation",
                invocations,
            )


@requires(POSIX)
def test_invalid_send_has_no_external_effects(binary: Path) -> Transcript:
    with _admission_client(binary) as (client, temporary):
        invalid = (
            {"r": "stop('invalid R cell ran')", "requirements": {"r": [""]}},
            {
                "python": "raise AssertionError('invalid Python cell ran')",
                "requirements": {
                    "python": ["example @ https://example.invalid/example.whl"]
                },
            },
            {
                "sql": "SELECT 1",
                "requirements": {"duckdb": ["spatial FROM community"]},
            },
        )
        for arguments in invalid:
            result = client.send(**arguments)
            assert result["isError"] is True, result

        assert not (temporary / "zod-started").exists(), (
            "invalid input started a worker"
        )
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_initializes_and_lists_tools(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    companions = {
        "custom.yaml": _initializes_and_lists_tools(binary, execution, custom=True),
        "custom-sql.yaml": _initializes_and_lists_tools(
            binary, execution, custom=True, languages=("sql",)
        ),
        "bare.yaml": _initializes_and_lists_tools(binary, execution, bare=True),
        "python-only.yaml": _initializes_and_lists_tools(
            binary, execution, python_only=True
        ),
        "configured-python.yaml": _initializes_and_lists_tools(
            binary, execution, bootstrap_languages="python"
        ),
        "python-managed.yaml": _initializes_and_lists_tools(
            binary, execution, python_only=True, python_managed=True
        ),
        "r-sql.yaml": _initializes_and_lists_tools(
            binary, execution, bootstrap_languages="r,sql"
        ),
        "sql.yaml": _initializes_and_lists_tools(binary, execution, languages=("sql",)),
        "sql-python.yaml": _initializes_and_lists_tools(
            binary, execution, languages=("sql", "python")
        ),
        "configured-r-sql.yaml": _initializes_and_lists_tools(
            binary, execution, languages=("r", "sql")
        ),
        "r-only.yaml": _initializes_and_lists_tools(
            binary, execution, bootstrap_languages="r"
        ),
    }
    if execution == SANDBOXED:
        companions["proxy.yaml"] = _initializes_and_lists_tools(
            binary, execution, proxy=True
        )
        companions["workspace.yaml"] = _initializes_and_lists_tools(
            binary, execution, workspace_profile=True
        )
        companions["writable.yaml"] = _initializes_and_lists_tools(
            binary, execution, writable=True
        )
        companions["custom-writable.yaml"] = _initializes_and_lists_tools(
            binary, execution, custom=True, writable=True
        )
        companions["r-only-writable.yaml"] = _initializes_and_lists_tools(
            binary, execution, bootstrap_languages="r", writable=True
        )
    baseline = _initializes_and_lists_tools(binary, execution)
    # Runtime discovery must not change the configured public interface.
    for name in ("bare.yaml", "python-only.yaml", "python-managed.yaml"):
        assert (
            companions[name][2]["result"]["tools"] == baseline[2]["result"]["tools"]
        ), name
    return TranscriptWithCompanions(baseline, companions)


def _initializes_and_lists_tools(
    binary: Path,
    execution: Execution,
    *,
    custom: bool = False,
    bare: bool = False,
    python_only: bool = False,
    python_managed: bool = False,
    proxy: bool = False,
    workspace_profile: bool = False,
    writable: bool = False,
    bootstrap_languages: str | None = None,
    languages: tuple[str, ...] | None = None,
) -> Transcript:
    environment = os.environ.copy()
    environment.pop("MCP_CONSOLE_LANGUAGES", None)
    if bootstrap_languages is not None:
        environment["MCP_CONSOLE_LANGUAGES"] = bootstrap_languages
    with tempfile.TemporaryDirectory() as library:
        if bare:
            environment = bare_runtime_environment(environment, Path(library))
        if python_only:
            python_bin = Path(library) / "bin"
            python_bin.mkdir()
            retain_system_bwrap(python_bin, environment.get("PATH"))
            if python_managed:
                if os.name == "nt":
                    shutil.copyfile(shutil.which("uv"), python_bin / "uv.exe")
                else:
                    (python_bin / "uv").symlink_to(shutil.which("uv"))
            elif os.name != "nt":
                (python_bin / "python3").symlink_to(sys.executable)
            environment["PATH"] = str(python_bin)
            for name in (
                "R_HOME",
                "R_LIBS",
                "R_LIBS_USER",
                "RETICULATE_PYTHON",
                "RETICULATE_UV",
            ):
                environment.pop(name, None)
            if not python_managed:
                environment["RETICULATE_PYTHON"] = str(
                    sys.executable if os.name == "nt" else python_bin / "python3"
                )
        workspace = (Path(library) / "workspace").resolve()
        workspace.mkdir()
        if proxy or workspace_profile:
            config = workspace / ".agents/console/config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps(
                    {"sandbox": {"filesystem": {"read_write": ["."]}}}
                    if workspace_profile
                    else {"sandbox": {"network": {"proxy": {}}}}
                ),
                encoding="utf-8",
            )
        with McpClient(
            binary,
            execution.serve(
                *(
                    "--worker",
                    str(Path(__file__).resolve().parents[3] / "fixtures/zod"),
                )
                if custom
                else (),
                *("-c", "languages=" + json.dumps(languages))
                if languages is not None
                else (),
                *("--writable-root", str(workspace)) if writable else (),
            ),
            environment,
            workspace,
            record_in_project=False,
        ) as client:
            client.initialize_and_list_tools()
            listed_tools = client.transcript[-1]["result"]["tools"]
            assert [tool["name"] for tool in listed_tools] == ["send"], listed_tools
            send = listed_tools[0]
            instructions = client.transcript[0]["result"].get("instructions")
            assert isinstance(instructions, str), client.transcript[0]
            assert instructions == send["description"].split("\n\n", 1)[0]
            assert "\n" not in instructions and len(instructions) <= 250, instructions
            assert "Send one complete" not in instructions
            if not (writable or workspace_profile):
                assert len(send["description"]) <= 2048, send["description"]
            if custom:
                assert "custom-worker" in instructions
            else:
                for capability in ("debugging", "simulations", "test hypotheses"):
                    assert capability in instructions, instructions
            if languages is not None:
                assert set(send["inputSchema"]["properties"]) == {
                    *languages,
                    "control",
                    "requirements",
                    "stdin",
                    "timeout_ms",
                }, send
            properties = send["inputSchema"]["properties"]
            visible = set(properties) & {"r", "python", "sql"}
            names = {name for name in ("R", "Python", "SQL") if name.lower() in visible}
            assert set(re.findall(r"\b(?:R|Python|SQL)\b", instructions)) == names
            assert (
                set(
                    re.findall(r"\b(?:R|Python|SQL)\b", json.dumps(send["inputSchema"]))
                )
                == names
            )
            cell_guidance = (
                send["description"]
                .split("Send one complete ")[1]
                .split(" cell per call")[0]
            )
            assert {
                field
                for field in ("r", "python", "sql")
                if f"`{field}`" in cell_guidance
            } == visible
            assert list(properties) == [
                field
                for field in (
                    "r",
                    "python",
                    "sql",
                    "timeout_ms",
                    "control",
                    "stdin",
                    "requirements",
                )
                if field in properties
            ], list(properties)
            for field in ("r", "python", "sql"):
                if field in properties:
                    assert properties[field]["description"].startswith(
                        "Evaluate one complete"
                    )
            description = send["description"]
            assert ("ir run script.R" in description) == (
                not custom and "r" in send["inputSchema"]["properties"]
            ), description
            assert "Run one cell at a time" in description
            assert "An error can leave earlier changes in place" in description
            assert f"`timeout_ms={properties['timeout_ms']['default']}`" in description
            assert '`requirements.action="add"`' in description
            for guidance in (description, properties["timeout_ms"]["description"]):
                assert "timeout_ms=300000" in guidance, guidance
                assert "returns early" in guidance, guidance
                assert "Avoid repeated short polls" in guidance, guidance
                assert "timeout_ms=1000" in guidance, guidance
            assert "active host resolver" not in properties["control"]["description"]
            if custom:
                assert "custom-worker" in description
                assert "does not supply built-in runtime packages" in description
                assert "defaults include SQLite" not in description
            elif SQL.available and "sql" in properties:
                assert description.index("Send one complete") < description.index(
                    "consider DuckDB SQL first"
                )
                assert "Results display automatically" in description
                assert "CSV, Parquet, JSON, and JSONL directly" in description
                sql_description = send["inputSchema"]["properties"]["sql"][
                    "description"
                ]
                assert (
                    "bounded table previews that abbreviate long text cells"
                    in sql_description
                )
                assert "TYPE sqlite, READ_ONLY" in sql_description
                assert "missing-provider" not in description
                assert "Language fields describe" not in description
                if "r" in send["inputSchema"]["properties"]:
                    assert (
                        "Use R for vectorized data and string operations" in description
                    )
                extensions = send["inputSchema"]["properties"]["requirements"][
                    "properties"
                ]["duckdb"]
                assert "fts" in extensions["description"]
            if proxy:
                assert (
                    "network subject to the launcher's proxy settings"
                    in send["description"]
                )
            control = send["inputSchema"]["properties"]["control"]
            assert control["type"] == "string", control
            assert control["enum"] == ["interrupt", "restart"], control
            send_schema = json.dumps(send["inputSchema"])
            assert '"$defs"' not in send_schema, send["inputSchema"]
            assert '"$ref"' not in send_schema, send["inputSchema"]

            if proxy or workspace_profile:
                assert list(config.parent.iterdir()) == [config], workspace
            else:
                assert not (workspace / ".agents/console").exists(), workspace
            if python_only:
                assert {"r", "python", "sql"} <= send["inputSchema"][
                    "properties"
                ].keys()
            send_requirements = send["inputSchema"]["properties"]["requirements"]
            assert send_requirements["type"] == ["object", "null"], send_requirements
            assert send_requirements["additionalProperties"] is False, send_requirements
            requirement_properties = send_requirements["properties"]
            assert requirement_properties.keys() == {
                "action",
                "duckdb",
                "r",
                "python",
                "python_version",
                "exclude_newer",
            }, requirement_properties
            assert requirement_properties["action"]["enum"] == [
                "get",
                "add",
                "set",
                "reset",
            ]
            for name in ("duckdb", "r", "python"):
                requirement = requirement_properties[name]
                assert requirement["type"] == "array", requirement
                assert "default" not in requirement, requirement
                assert "maxItems" not in requirement, requirement
                assert requirement["items"]["type"] == "string", requirement
                assert requirement["items"]["minLength"] == 1, requirement
            assert requirement_properties["duckdb"]["items"]["maxLength"] == 64
            if not SQL.available:
                assert "sql" not in send["inputSchema"]["properties"]
                if not custom:
                    assert "SQL is not yet supported" in description
            # Inspection does not wait for preparation; it must leave the
            # configured schema unchanged. Keep the handshake-only snapshot.
            transcript = list(client.transcript)
            prepared = client.send(requirements={"action": "get"})
            assert not prepared.get("isError", False), prepared
            assert client.request("tools/list")["result"] == transcript[2]["result"]
            client.finish()
            for response in transcript:
                for tool in response.get("result", {}).get("tools", []):
                    tool["description"] = tool["description"].replace(
                        json.dumps(str(workspace))[1:-1], "<workspace>"
                    )
            return transcript


@requires(SANDBOX)
def test_describes_project_network_access(binary: Path) -> Transcript:
    restricted_filesystem = "Writable locations: private `TMPDIR`"
    cases = (
        (
            "restricted",
            "sandbox: {network: restricted}",
            False,
            "cannot directly access the network",
            restricted_filesystem,
        ),
        (
            "enabled",
            "sandbox: {network: enabled}",
            False,
            "can directly access the network",
            restricted_filesystem,
        ),
        (
            "proxy",
            json.dumps({"sandbox": {"network": {"proxy": {}}}}),
            False,
            "network subject to the launcher's proxy settings",
            restricted_filesystem,
        ),
        (
            "proxy with local binding",
            json.dumps(
                {"sandbox": {"network": {"proxy": {}, "allow_local_binding": True}}}
            ),
            False,
            "network subject to the launcher's proxy settings",
            restricted_filesystem,
        ),
        (
            "no sandbox",
            "{}",
            True,
            "without a sandbox, with the server's permissions, including filesystem and network access",
            "filesystem and network access",
        ),
    )
    transcript: Transcript = []
    for name, source, no_sandbox, expected, filesystem_access in cases:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            config = workspace / ".agents/console/config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text(source, encoding="utf-8")
            arguments = ["serve", "--worker", "unused-worker"]
            if no_sandbox:
                arguments.append("--no-sandbox")
            with McpClient(binary, arguments, current_directory=workspace) as client:
                client.initialize_and_list_tools()
                description = client.transcript[-1]["result"]["tools"][0]["description"]
                assert expected in description, (name, description)
                assert filesystem_access in description, (name, description)
                if not no_sandbox:
                    assert (restricted_filesystem in description) == (
                        filesystem_access == restricted_filesystem
                    ), (name, description)
                    assert "separate filesystem and network permissions" in description
                config.write_text("invalid: [", encoding="utf-8")
                listed = client.request("tools/list")
                assert listed["result"]["tools"][0]["description"] == description
                client.finish()
                transcript.append({"configuration": name, "description": description})
    return transcript


@requires(SANDBOX)
def test_describes_captured_writable_locations(binary: Path) -> Transcript:
    records = []
    for workspace_writable in (False, True):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory).resolve()
            config = workspace / ".agents/console/config.yaml"
            config.parent.mkdir(parents=True)
            policy = {
                "sandbox": {
                    "filesystem": {
                        "read_write": ["configured output"],
                        "read_only": ["read only"],
                        "deny": ["denied"],
                    }
                }
            }
            if workspace_writable:
                policy["sandbox"]["filesystem"]["read_write"].append(".")
            config.write_text(json.dumps(policy), encoding="utf-8")
            with McpClient(
                binary,
                ("serve", "--worker", "unused-worker", "--writable-root", "cli output"),
                current_directory=workspace,
                record_in_project=False,
            ) as client:
                client.initialize_and_list_tools()
                tool = client.transcript[-1]["result"]["tools"][0]
                description = tool["description"]
                assert "Writable locations: private `TMPDIR`" in description
                for path in ("configured output", "cli output"):
                    assert json.dumps(str(workspace / path)) in description
                assert json.dumps(str(workspace / "read only")) not in description
                assert json.dumps(str(workspace / "denied")) not in description
                if workspace_writable:
                    assert json.dumps(str(workspace)) in description
                assert "more specific read/deny rules" in description
                config.write_text("invalid: [", encoding="utf-8")
                assert client.request("tools/list")["result"]["tools"] == [tool]
                client.finish()
                records.append(
                    {
                        "workspace_writable": workspace_writable,
                        "description": description.replace(
                            json.dumps(str(workspace))[1:-1], "<workspace>"
                        ),
                    }
                )
    return records


def test_language_switching_guidance_matches_enabled_fields(binary: Path) -> Transcript:
    return _language_switching_guidance(binary, ("r", "python", "r,python"))


@requires(SQL)
def test_sql_language_switching_guidance_matches_enabled_fields(
    binary: Path,
) -> Transcript:
    return _language_switching_guidance(
        binary, ("sql", "r,sql", "python,sql", "r,python,sql")
    )


def _language_switching_guidance(
    binary: Path, selections: tuple[str, ...]
) -> Transcript:
    transcript = []
    for enabled in selections:
        environment = dict(os.environ, MCP_CONSOLE_LANGUAGES=enabled)
        with McpClient(
            binary, DIRECT.serve("--worker", "unused-worker"), environment
        ) as client:
            client.initialize_and_list_tools()
            tool = client.transcript[-1]["result"]["tools"][0]
            fields = set(tool["inputSchema"]["properties"]) & {"r", "python", "sql"}
            assert fields == set(enabled.split(",")), fields
            description = tool["description"]
            instructions = client.transcript[0]["result"]["instructions"]
            assert instructions == description.split("\n\n", 1)[0]
            assert set(re.findall(r"\b(?:R|Python|SQL)\b", description)) == {
                name for name in ("R", "Python", "SQL") if name.lower() in fields
            }, (enabled, description)
            cell_guidance = description.split("Send one complete ")[1].split(
                " cell per call"
            )[0]
            assert {
                field
                for field in ("r", "python", "sql")
                if f"`{field}`" in cell_guidance
            } == fields, (enabled, cell_guidance)
            assert ("Switch languages when useful" in description) == (
                len(fields) > 1
            ), (
                enabled,
                description,
            )
            transcript.append({"languages": enabled, "description": description})
            client.finish()
        with McpClient(
            binary,
            DIRECT.serve(
                "--worker",
                "unused-worker",
                "-c",
                "languages=" + json.dumps(enabled.split(",")),
            ),
            environment,
        ) as configured:
            configured.initialize_and_list_tools()
            assert configured.transcript[-1]["result"]["tools"] == [tool], enabled
            assert configured.transcript[0]["result"]["instructions"] == instructions
            configured.finish()
    return transcript


@requires(POSIX)
def test_limits_send_languages_from_environment(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    environment = os.environ.copy()
    environment["MCP_CONSOLE_LANGUAGES"] = "r,sql"
    with tempfile.TemporaryDirectory() as client_directory:
        worker_started = Path(client_directory) / "zod-started"
        environment["MCP_CONSOLE_TEST_ZOD_STARTED"] = str(worker_started)
        with McpClient(
            binary, DIRECT.serve("--worker", str(zod)), environment
        ) as client:
            client.initialize_and_list_tools()

            tools = {
                tool["name"]: tool for tool in client.transcript[-1]["result"]["tools"]
            }
            send_properties = tools["send"]["inputSchema"]["properties"]
            assert send_properties.keys() == {
                "r",
                "sql",
                "control",
                "requirements",
                "stdin",
                "timeout_ms",
            }
            result = client.send(python="raise AssertionError('disabled cell ran')")
            assert result["isError"] is True, result
            assert not worker_started.exists(), worker_started
            return client.finish()


@requires(POSIX, SQL)
@requires(WORKER)
def test_validates_send_arguments(binary: Path) -> Transcript:
    with _admission_client(binary) as (client, temporary):
        _reject_send_arguments(client)
        assert not (temporary / "zod-started").exists(), (
            "invalid input started a worker"
        )

        # fmt: python
        setup = code("""
            import os

            worker_pid = os.getpid()
            sentinel = 42
            """)
        result = client.send(python=setup)
        assert result["content"] == [{"type": "text", "text": "[done]"}], result
        _reject_send_arguments(client)
        # fmt: python
        verify = code("""
            assert os.getpid() == worker_pid
            assert sentinel == 42
            print(sentinel)
            """)
        result = client.send(python=verify)
        assert result["content"] == [{"type": "text", "text": "42\n"}], result
        assert not (temporary / "zod-sigint-received").exists()

        # An accepted stdin write follows every rejected stdin request in order.
        result = client.send(python="print(input())", stdin="accepted\n")
        assert result["content"] == [{"type": "text", "text": "accepted\n"}], result
        cells = [
            json.loads(line)
            for line in (temporary / "cells.jsonl").read_text().splitlines()
        ]
        assert cells == [
            {"kind": "evaluate", "language": "python", "source": source}
            for source in (setup, verify, "print(input())")
        ], cells
        result = client.send(r=None)
        assert result["content"] == [{"type": "text", "text": "\n[idle]"}], result
        return client.finish()


def _reject_send_arguments(client: McpClient) -> None:
    result = client.send(
        # fmt: python
        python=code("""
            print("hello")
            """),
        wait_ms=0,
    )
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == (
        "failed to deserialize parameters: unknown field `wait_ms`, expected one "
        "of `r`, `python`, `sql`, `control`, `requirements`, `stdin`, `timeout_ms`"
    ), result
    result = client.send(
        r="1",
        python="1",
        sql="SELECT 1",
        control="restart",
        requirements={"r": ["praise"]},
    )
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == (
        "only one of `r`, `python`, or `sql` may be supplied"
    ), result

    result = client.send(control="prepare")
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == (
        "failed to deserialize parameters: unknown variant `prepare`, expected "
        "`interrupt` or `restart`"
    ), result

    result = client.send(stdin="answer\n", requirements={"r": ["praise"]})
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == (
        "requirements-only `send` performs standalone preparation and cannot also "
        "queue stdin"
    ), result

    result = client.send(
        control="interrupt",
        stdin="must not queue\n",
        requirements={"r": ["praise"]},
    )
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == (
        '`requirements` with `control = "interrupt"` requires a code cell'
    ), result

    for control in (None, "restart"):
        result = client.send(r="stop('cell was run')", control=control, requirements={})
        assert result["isError"] is True, result
        assert result["content"][0]["text"] == (
            "at least one of `requirements.r`, `requirements.python`, or "
            "`requirements.duckdb` is required"
        ), result

    result = client.send(
        r="stop('cell was run')",
        requirements={"r": [""]},
    )
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == "R requirement strings must not be empty", (
        result
    )

    invalid_python = "example @ https://example.invalid/example.whl"
    result = client.send(
        r="stop('cell was run')",
        requirements={"python": [invalid_python]},
    )
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == (
        f"Python requirement `{invalid_python}` is not accepted: host-side managed "
        "resolution accepts named package requirements only"
    ), result

    result = client.send(
        r="stop('cell was run')",
        requirements={"duckdb": ["spatial FROM community"]},
    )
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == (
        "DuckDB extension names must start with a lowercase ASCII letter and "
        "contain only lowercase ASCII letters, digits, and underscores"
    ), result


@execution_snapshots
@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_bounds_argument_decoding_errors(
    binary: Path, execution: Execution
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures/zod"
    value = "argument head " + "éλ" * 4096 + " argument tail"
    cases = (
        ({value: True}, "unknown field", "`timeout_ms`"),
        ({"control": value}, "unknown variant", "`interrupt` or `restart`"),
        ({"timeout_ms": value}, "invalid type", "expected u64"),
    )
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary).resolve()
        started = workspace / "worker-started"
        roots = ("--writable-root", str(workspace)) if execution == SANDBOXED else ()
        with McpClient(
            binary,
            execution.serve("--worker", str(zod), *roots),
            {**os.environ, "MCP_CONSOLE_TEST_ZOD_STARTED": str(started)},
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            results = []
            for arguments, kind, ending in cases:
                result = client.send(**arguments)
                assert result["isError"] is True, result
                text = "".join(block["text"] for block in result["content"])
                assert text.startswith(f"failed to deserialize parameters: {kind}")
                assert "argument head " in text and " argument tail" in text
                assert text.endswith(ending), text[-200:]
                results.append(result)
            lengths = [
                sum(len(block["text"].encode()) for block in result["content"])
                for result in results
            ]
            assert all(length <= 8192 for length in lengths), lengths
            assert all("omitted" in result["content"][0]["text"] for result in results)
            assert not started.exists(), "invalid arguments started the worker"
            session = next((workspace / ".agents/console/sessions").iterdir())
            recorded = [
                event["result"]
                for line in (session / "internal/events.jsonl").read_text().splitlines()
                if (event := json.loads(line))["event"] == "tool_result"
            ]
            assert recorded == results
            result = client.send(r="echo ready")
            assert result["content"] == [{"type": "text", "text": "zod: ready\n"}], (
                result
            )
            assert result["isError"] is False, result
            compact_previews(client, "éλ")
            for entry in client.transcript:
                if "send" in entry and entry["send"] in [case[0] for case in cases]:
                    entry["send"] = {
                        "json": compact_text(
                            json.dumps(entry["send"], ensure_ascii=False), "éλ"
                        )
                    }
            return client.finish()


@requires(POSIX)
def test_validates_standalone_requirement_arguments(binary: Path) -> Transcript:
    with _admission_client(binary) as (client, temporary):
        client.send(requirements={})
        result = client.transcript[-1]["result"]
        assert result["isError"] is True
        assert result["content"][0]["text"] == (
            "at least one of `requirements.r`, `requirements.python`, or "
            "`requirements.duckdb` is required"
        )

        client.send(requirements={"duckdb": ["spatial FROM community"]})
        result = client.transcript[-1]["result"]
        assert result["isError"] is True
        assert result["content"][0]["text"] == (
            "DuckDB extension names must start with a lowercase ASCII letter and "
            "contain only lowercase ASCII letters, digits, and underscores"
        )

        client.send(requirements={"r": [""]})
        result = client.transcript[-1]["result"]
        assert result["isError"] is True
        assert result["content"][0]["text"] == "R requirement strings must not be empty"

        client.send(requirements={"r": ["cli\ndplyr"]})
        result = client.transcript[-1]["result"]
        assert result["isError"] is True
        assert result["content"][0]["text"] == (
            "R requirement strings must not contain NUL or line breaks"
        )

        client.send(
            control="interrupt",
            requirements={"python": ["py-yaml12"]},
        )
        result = client.transcript[-1]["result"]
        assert result["isError"] is True
        assert result["content"][0]["text"] == (
            '`requirements` with `control = "interrupt"` requires a code cell'
        )

        client.send(
            control="restart",
            requirements={},
        )
        result = client.transcript[-1]["result"]
        assert result["isError"] is True
        assert result["content"][0]["text"] == (
            "at least one of `requirements.r`, `requirements.python`, or "
            "`requirements.duckdb` is required"
        )

        client.send(
            control="restart",
            requirements={"r": ["cli\ndplyr"]},
        )
        result = client.transcript[-1]["result"]
        assert result["isError"] is True
        assert result["content"][0]["text"] == (
            "R requirement strings must not contain NUL or line breaks"
        )

        client.send(
            control="restart",
            requirements={"duckdb": ["spatial FROM community"]},
        )
        result = client.transcript[-1]["result"]
        assert result["isError"] is True
        assert result["content"][0]["text"] == (
            "DuckDB extension names must start with a lowercase ASCII letter and "
            "contain only lowercase ASCII letters, digits, and underscores"
        )
        assert not (temporary / "zod-started").exists(), (
            "invalid input started a worker"
        )
        return client.finish()


@requires(POSIX)
@requires(WORKER)
def test_rejects_interrupt_without_worker(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with McpClient(binary, DIRECT.serve("--worker", str(zod))) as client:
        client.initialize_and_list_tools()
        client.send(requirements={"action": "get"})
        client.send(control="interrupt", timeout_ms=0)
        result = client.transcript[-1]["result"]
        assert result["isError"] is True
        assert result["content"][0]["text"] == "[worker is not running]"
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
