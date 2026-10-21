#!/usr/bin/env -S uv run --script

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.client import McpClient
from support.native import LOADER_VARIABLE, build_interposer
from support.normalization import code
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, POSIX, SANDBOX, requires
from support.suites import run_this_suite


CONFIG = ".agents/console/config.yaml"


def configure(workspace: Path, value: object) -> None:
    config = workspace / CONFIG
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps(value))


def test_ignores_yaml_tags_recursively(binary: Path) -> Transcript:
    yaml = code("""
        !configuration
        !key cache: !cache host
        environment: !environment {LABEL: !text tagged}
        """)
    with TemporaryDirectory() as temporary:
        workspace = Path(temporary).resolve()
        config = workspace / CONFIG
        config.parent.mkdir(parents=True)
        config.write_text(yaml)
        with McpClient(
            binary,
            (
                "serve",
                "--no-sandbox",
                "--worker",
                "unused-worker",
                "-c",
                "environment.ADDED=override",
            ),
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            _, stderr = client.finish_with_standard_error()
            assert stderr == "", stderr
    return [{"yaml": yaml, "initialized": True}]


@requires(SANDBOX, NATIVE_FIXTURES)
def test_layers_project_then_cli_in_order(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        workspace = Path(temporary).resolve()
        (workspace / "project").mkdir()
        (workspace / "cli").mkdir()
        configure(
            workspace,
            {
                "cache": "host",
                "environment": {"KEEP": "project", "CHANGE": "project"},
                "sandbox": {"filesystem": {"read_write": ["./project"]}},
            },
        )
        capture = workspace / "payloads.jsonl"
        environment = {
            **os.environ,
            LOADER_VARIABLE: str(build_interposer(workspace, "runner_configuration")),
            "MCP_CONSOLE_TEST_RUNNER_CONFIGURATION": str(capture),
        }
        overrides = [
            "-c",
            "environment.BEFORE=first",
            "-c",
            "environment.CHANGE=first",
            "-c",
            "environment={CHANGE=last, ADD: cli}",
            "-c",
            "sandbox.filesystem.read_write=[./cli]",
        ]
        for command in ("serve", "sandbox"):
            arguments = [*overrides[:2], command, *overrides[2:]]
            if command == "serve":
                zod = Path(__file__).resolve().parents[2] / "fixtures/zod"
                with McpClient(
                    binary,
                    (*arguments, "--worker", str(zod)),
                    current_directory=workspace,
                    environment=environment,
                ) as client:
                    client.initialize_and_list_tools()
                    # Native validation now belongs to the requested worker
                    # launch. Its completed cell proves that launch occurred.
                    client.expect("zod: overrides\n", r="echo overrides")
                    _, stderr = client.finish_with_standard_error()
                    assert stderr == "", stderr
            else:
                result = subprocess.run(
                    [binary, *arguments, "--", "/usr/bin/true"],
                    cwd=workspace,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                assert result.returncode == 0 and result.stderr == "", result
            payloads = [json.loads(line) for line in capture.read_text().splitlines()]
            for payload in payloads:
                assert "extends" not in payload, payload
                assert payload["environment"] == {
                    "KEEP": "project",
                    "CHANGE": "last",
                    "ADD": "cli",
                    "BEFORE": "first",
                }, payload
                filesystem = payload["filesystem"]
                assert filesystem["kind"] == "restricted", filesystem
                paths = [
                    entry["path"]["path"]
                    for entry in filesystem["entries"]
                    if entry["access"] == "write"
                ]
                assert paths == [str(workspace / "cli")], filesystem
            records.append(
                {
                    "command": command,
                    "overrides": overrides,
                    "environment": payloads[-1]["environment"],
                    "paths": [str(Path(path).relative_to(workspace)) for path in paths],
                }
            )
            capture.unlink()
    return records


@requires(SANDBOX)
def test_inline_strings_and_objects_without_project_file(binary: Path) -> Transcript:
    expected = {
        "MESSAGE": "a, [b] = {c}: #雪",
        "NUMBER": "42",
        "EMPTY": "",
        "URL": "https://example.com/a=b",
        "APOSTROPHE": "it's",
        "ESCAPE": 'say "go" \\ end',
        "APP.VERSION": "v1",
    }
    cases = (
        "{MESSAGE: 'a, [b] = {c}: #雪', NUMBER: '42', EMPTY: '', URL: https://example.com/a=b, "
        r"""APOSTROPHE: 'it''s', ESCAPE: "say \"go\" \\ end", APP.VERSION: v1}""",
        json.dumps(expected, separators=(",", ":")),
        "{MESSAGE = 'a, [b] = {c}: #雪', NUMBER = '42', EMPTY = '', URL = 'https://example.com/a=b', "
        r"""APOSTROPHE = "it's", ESCAPE = "say \"go\" \\ end", "APP.VERSION" = 'v1',}""",
    )
    records = []
    with TemporaryDirectory() as temporary:
        for value in cases:
            result = subprocess.run(
                [
                    binary,
                    "sandbox",
                    "--config",
                    f"environment={value}",
                    "--",
                    sys.executable,
                    "-c",
                    # fmt: python
                    code("""
                        import json
                        import os
                        import sys

                        print(json.dumps({key: os.environ[key] for key in sys.argv[1:]}))
                        """),
                    *expected,
                ],
                cwd=temporary,
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 0 and result.stderr == "", result
            actual = json.loads(result.stdout)
            assert actual == expected, actual
            records.append({"value": value, "environment": actual})
    return records


def test_overrides_precede_schema_validation(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        configure(workspace, {"environment": False, "python": ["invalid"]})
        overrides = (
            "-c",
            "environment.KEEP=first",
            "-c",
            "environment=null",
            "-c",
            "environment={FINAL: last}",
            "-c",
            "python=null",
        )
        with McpClient(
            binary,
            ("serve", "--no-sandbox", *overrides),
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            _, stderr = client.finish_with_standard_error()
            assert stderr == "", stderr
        assert json.loads((workspace / CONFIG).read_text()) == {
            "environment": False,
            "python": ["invalid"],
        }
    return [{"overrides": list(overrides), "initialized": True}]


def test_discovers_home_configuration_with_project_layering(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        home = root / "home"
        workspace = root / "project"
        home.mkdir()
        workspace.mkdir()
        environment = {**os.environ, "HOME": str(home)}
        if os.name == "nt":
            environment["USERPROFILE"] = str(home)
        environment.pop("MCP_CONSOLE_HOME", None)
        configure(home, {"unknown": "home"})

        def launch_error() -> str:
            result = subprocess.run(
                [binary, "serve", "--no-sandbox"],
                cwd=workspace,
                env=environment,
                input="",
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 1 and result.stdout == "", result
            return result.stderr

        assert str(home / CONFIG).replace("\\", "/") in launch_error().replace(
            "\\", "/"
        )

        fixture_workspace = root / "default-client"
        fixture_workspace.mkdir()
        with McpClient(
            binary,
            ("serve", "--no-sandbox"),
            environment=environment,
            current_directory=fixture_workspace,
        ) as client:
            client.initialize_and_list_tools()
            _, stderr = client.finish_with_standard_error()
            assert stderr == "", stderr

        opt_out_workspace = root / "opt-out-client"
        opt_out_workspace.mkdir()
        with McpClient(
            binary,
            ("serve", "--no-sandbox"),
            environment=environment,
            current_directory=opt_out_workspace,
            record_in_project=False,
        ) as client:
            client.initialize_and_list_tools()
            _, stderr = client.finish_with_standard_error()
            assert stderr == "", stderr
        assert not (opt_out_workspace / ".agents").exists()

        configure(workspace, {})
        # An empty project mapping preserves the invalid global setting.
        assert str(home / CONFIG) in launch_error()
        configure(home, {})
        with McpClient(
            binary,
            ("serve", "--no-sandbox"),
            environment=environment,
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            _, stderr = client.finish_with_standard_error()
            assert stderr == "", stderr

        configure(workspace, {"unknown": "project"})
        error = launch_error()
        assert CONFIG in error and str(home / CONFIG) in error, error

        (workspace / CONFIG).unlink()
        configure(home, {"unknown": "home"})
        assert str(home / CONFIG).replace("\\", "/") in launch_error().replace(
            "\\", "/"
        )
        configure(home, {})
        with McpClient(
            binary,
            ("serve", "--no-sandbox"),
            environment=environment,
            current_directory=workspace,
            record_in_project=False,
            use_home_configuration=True,
        ) as client:
            client.initialize_and_list_tools()
            _, stderr = client.finish_with_standard_error()
            assert stderr == "", stderr

    return [{"home_discovery": True, "project_layering": True}]


def test_discovers_explicit_console_home(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        home = root / "home"
        console_home = root / "console"
        workspace = root / "workspace"
        console_home.mkdir()
        workspace.mkdir()
        configure(home, {"unknown": "ambient home"})
        selected_config = console_home / "config.yaml"
        selected_config.write_text('{"unknown": "selected console home"}')
        environment = os.environ | {
            "HOME": str(home),
            "MCP_CONSOLE_HOME": str(console_home),
        }
        for arguments in (("serve", "--no-sandbox"), ("sandbox", "--", "unused")):
            result = subprocess.run(
                [binary, *arguments],
                cwd=workspace,
                env=environment,
                input="",
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 1 and result.stdout == "", result
            assert str(selected_config) in result.stderr, result.stderr
            assert str(home / CONFIG) not in result.stderr, result.stderr

        configure(workspace, {})
        result = subprocess.run(
            [binary, "serve", "--no-sandbox"],
            cwd=workspace,
            env=environment,
            input="",
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 1 and str(selected_config) in result.stderr, result
        selected_config.write_text("{}", encoding="utf-8")
        with McpClient(
            binary,
            ("serve", "--no-sandbox"),
            environment,
            workspace,
            record_in_project=False,
            use_home_configuration=True,
        ) as client:
            client.initialize_and_list_tools()
            _, stderr = client.finish_with_standard_error()
            assert stderr == "", stderr

        (workspace / CONFIG).unlink()
        selected_config.unlink()
        with McpClient(
            binary,
            ("serve", "--no-sandbox"),
            environment,
            workspace,
            record_in_project=False,
            use_home_configuration=True,
        ) as client:
            client.initialize_and_list_tools()
            _, stderr = client.finish_with_standard_error()
            assert stderr == "", stderr

        for invalid in ("", "relative/path"):
            result = subprocess.run(
                [binary, "serve", "--no-sandbox"],
                cwd=workspace,
                env=environment | {"MCP_CONSOLE_HOME": invalid},
                input="",
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 1 and result.stdout == "", result
            assert "MCP_CONSOLE_HOME must be an absolute path" in result.stderr, (
                result.stderr
            )
    return [
        {
            "console_home_override": True,
            "project_layering": True,
            "invalid_console_home_rejected": True,
        }
    ]


def test_rejects_malformed_overrides(binary: Path) -> Transcript:
    cases = (
        "sandbox",
        "=value",
        "sandbox..network=restricted",
        "sandbox.=restricted",
        "sandbox.network=",
        "sandbox={network:}",
        "sandbox={network restricted}",
        "sandbox={network: [restricted}",
        "sandbox={network: 'restricted}",
        "sandbox={network: restricted} trailing",
        "sandbox={network: [restricted,, enabled]}",
        "extends=.inf",
        "extends=first\nsecond",
    )
    records = []
    with TemporaryDirectory() as temporary:
        for override in cases:
            result = subprocess.run(
                [binary, "serve", "--no-sandbox", "-c", override],
                cwd=temporary,
                input="",
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 1 and result.stdout == "", result
            assert "configuration override" in result.stderr, result.stderr
            records.append({"override": override, "stderr": result.stderr})
    return records


@requires(POSIX)
def test_python_home_expansion_requires_absolute_home(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        configure(workspace, {"python": "~/.venv/bin/python"})
        for home in (None, "", "relative/home"):
            environment = dict(os.environ)
            environment["MCP_CONSOLE_HOME"] = str(workspace / "console-home")
            environment.pop("HOME", None)
            if home is not None:
                environment["HOME"] = home
            result = subprocess.run(
                [binary, "serve", "--no-sandbox"],
                cwd=workspace,
                env=environment,
                input="",
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 1 and result.stdout == "", result
            expected = (
                "HOME must be an absolute path"
                if home
                else "cannot expand '~' without a user home directory"
            )
            assert result.stderr == f"configured Python: {expected}\n", result
            records.append({"HOME": home, "stderr": result.stderr})
    return records


def test_validates_effective_configuration(binary: Path) -> Transcript:
    cases = (
        ("inherit_environment=wrong", "boolean"),
        ("inherit_environment=42", "integer"),
        ("inherit_environment=1.5", "floating point"),
        ("target.command=[far, faz]", "unknown field"),
        ("unknown={baz: [far, faz]}", "unknown field"),
        ("cache=relative-cache", "expected managed, host, or a directory mapping"),
        ("cache=/tmp/cache", "expected managed, host, or a directory mapping"),
        ("cache=42", "expected managed, host, or a directory mapping"),
        ("cache={directory: ''}", "cache.directory must not be empty"),
        ("cache={directory: 42}", "string"),
        ("cache={path: cache}", "expected managed, host, or a directory mapping"),
        (
            "cache={directory: cache, unknown: true}",
            "expected managed, host, or a directory mapping",
        ),
    )
    records = []
    with TemporaryDirectory() as temporary:
        for override, expected in cases:
            result = subprocess.run(
                [binary, "serve", "--no-sandbox", "-c", override],
                cwd=temporary,
                input="",
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 1 and result.stdout == "", result
            assert expected in result.stderr, result.stderr
            records.append({"override": override, "stderr": result.stderr})
    return records


def test_overrides_do_not_bypass_file_errors_or_explicit_inputs(
    binary: Path,
) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        config = workspace / CONFIG
        config.parent.mkdir(parents=True)
        config.write_text("sandbox: [")
        cases = (
            (["serve", "--no-sandbox", "-c", "sandbox={} "], CONFIG),
            (
                [
                    "-c",
                    "extends=:workspace",
                    "sandbox",
                    "--runner-config-env",
                    "TEST_POLICY",
                    "--",
                    str(binary),
                    "--version",
                ],
                "cannot be combined",
            ),
            (
                [
                    "sandbox",
                    "--settings-env",
                    "TEST_POLICY",
                    "-c",
                    "extends=:workspace",
                    "--",
                    str(binary),
                    "--version",
                ],
                "cannot be combined",
            ),
        )
        for arguments, expected in cases:
            result = subprocess.run(
                [binary, *arguments],
                cwd=workspace,
                input="",
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 1 and result.stdout == "", result
            assert expected in result.stderr, result.stderr
            records.append(
                {
                    "arguments": [
                        "<console>" if argument == str(binary) else argument
                        for argument in arguments
                    ],
                    "stderr": result.stderr,
                }
            )
    return records


def test_rejects_oversized_startup_before_launch(binary: Path) -> Transcript:
    limit = 32 * 1024

    def encoded_bytes(language: str, source: str) -> int:
        payload = json.dumps(
            {"language": language, "code": source},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return len(payload.encode("utf-8"))

    cases = []
    for language, padding in (
        ("r", "a"),
        ("python", "\n"),
        ("python", "\x01"),
        ("python", "雪"),
    ):
        remaining = limit + 1 - encoded_bytes(language, "#\npass")
        unit_bytes = encoded_bytes(language, padding) - encoded_bytes(language, "")
        count, remainder = divmod(remaining, unit_bytes)
        source = "#" + padding * count + "a" * remainder + "\npass"
        assert encoded_bytes(language, source) == limit + 1
        cases.append((language, repr(padding), source))
    cases.append(("python", "70,000 leading newlines", "\n" * 70_000 + "pass"))

    records = []
    with TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        for language, description, source in cases:
            configured_language = "r" if description == repr("\n") else language
            configure(
                workspace,
                {"startup": {"language": configured_language, "code": source}},
            )
            # Override after loading: validation must use the effective source.
            arguments = [binary, "serve", "--no-sandbox"]
            if configured_language != language:
                arguments.extend(["-c", "startup.language=python"])
            result = subprocess.run(
                arguments,
                cwd=workspace,
                input="",
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 1 and result.stdout == "", result
            expected = (
                f"startup encoded source is {encoded_bytes(language, source)} bytes; "
                f"maximum is {limit} bytes"
            )
            assert expected in result.stderr, result.stderr
            records.append(
                {
                    "source": description,
                    "error": result.stderr.replace(str(workspace), "<workspace>"),
                }
            )
    return records


def test_rejects_invalid_startup(binary: Path) -> Transcript:
    cases = (
        ("startup.language=sql", "startup.language"),
        ("startup.language=unknown", "startup.language"),
        ("startup.code=''", "startup.code must be nonempty"),
        ("startup.code=42", "startup.code"),
        ("startup.code=null", "startup.code"),
        ("startup.extra=true", "unknown field `extra`"),
        ("sql.provider=python", "unknown field `sql`"),
    )
    records = []
    with TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        configure(workspace, {"startup": {"language": "python", "code": "pass"}})
        for override, expected in cases:
            result = subprocess.run(
                [binary, "serve", "--no-sandbox", "-c", override],
                cwd=workspace,
                input="",
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 1 and result.stdout == "", result
            assert expected in result.stderr, result.stderr
            records.append({"override": override, "error": result.stderr})
        result = subprocess.run(
            [binary, "serve", "--no-sandbox", "--worker", "unused"],
            cwd=workspace,
            input="",
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 1 and result.stdout == "", result
        assert "startup requires the built-in worker" in result.stderr, result.stderr
        records.append({"custom_worker_error": result.stderr})
    return records


if __name__ == "__main__":
    run_this_suite(__file__)
