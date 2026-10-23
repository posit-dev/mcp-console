#!/usr/bin/env -S uv run --script

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from yaml12 import parse_yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.snapshots import platform_snapshots
from support.client import McpClient
from support.linux_sandbox import retain_system_bwrap
from support.normalization import code
from support.records import Transcript, TranscriptEntry
from support.requirements import SANDBOX, requires
from support.suites import run_this_suite


CONFIG = ".agents/console/config.yaml"


def accepted(binary: Path, host: Path, *arguments: str) -> TranscriptEntry:
    with McpClient(
        binary,
        arguments or ("serve", "--worker", "unused-worker"),
        current_directory=host,
        record_in_project=False,
    ) as client:
        client.initialize_and_list_tools()
        _, stderr = client.finish_with_standard_error()
        assert stderr == "", stderr
        return {
            "command": ["mcp-console", *client.process.args[1:]],
            "initialized": True,
            "exit_status": client.process.returncode,
            "stderr": stderr,
        }


def invoke(binary: Path, host: Path, *arguments: str):
    return subprocess.run(
        [binary, *(arguments or ("serve", "--worker", "unused-worker"))],
        cwd=host,
        env={**os.environ, "MCP_CONSOLE_SANDBOX_SETTINGS": "invalid ambient settings"},
        input="",
        capture_output=True,
        text=True,
    )


def test_null_clears_requested_policies(binary: Path) -> Transcript:
    with TemporaryDirectory() as directory:
        host = Path(directory)
        config = host / CONFIG
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps({"sandbox": {}, "resolver": {"sandbox": {}}}),
            encoding="utf-8",
        )
        return [
            accepted(
                binary,
                host,
                "serve",
                "--worker",
                "unused-worker",
                "--no-sandbox",
                "-c",
                "sandbox=null",
                "-c",
                "resolver.sandbox=null",
            )
        ]


@requires(SANDBOX)
def test_duplicate_keys_use_last_value(binary: Path) -> Transcript:
    cases = (
        "sandbox: invalid\nsandbox: {}",
        "sandbox: {network: invalid, network: restricted}",
        "sandbox: {network: {proxy: {domains: {allow: [example.com], allow: []}}}}",
    )
    with TemporaryDirectory() as directory:
        host = Path(directory)
        config = host / CONFIG
        config.parent.mkdir(parents=True)
        for yaml in cases:
            config.write_text(yaml, encoding="utf-8")
            accepted(binary, host)
    return [{"yaml": yaml, "initialized": True} for yaml in cases]


def test_rejects_invalid_project_configuration(binary: Path) -> Transcript:
    cases = (
        ("invalid tagged scalar", "sandbox: {network: !!int enabled}", "YAML"),
        ("empty", "", "one mapping document"),
        ("sequence", "[]", "mapping"),
        ("tagged sequence", "!custom []", "mapping"),
        ("tagged scalar", "!custom scalar", "mapping"),
        (
            "multiple documents",
            """---
{}
---
{}""",
            "one mapping document",
        ),
        ("malformed", "sandbox: [", "line"),
        ("top-level field", "profile: default", "profile"),
        ("tagged unknown field", "!custom {profile: default}", "profile"),
        ("sandbox type", "sandbox: false", "sandbox"),
        ("tagged sandbox type", "sandbox: !custom false", "sandbox"),
        ("tagged nested sequence", "sandbox: !custom [!args [42]]", "sandbox"),
        ("sandbox sequence", "sandbox: []", "sandbox"),
        ("owned protocol", "sandbox: {version: 2}", "version"),
        (
            "owned lifetime",
            "sandbox: {lifecycle: {parent_pid: null}}",
            "lifecycle",
        ),
        (
            "non-string key",
            "sandbox: {proxy: {enabled: true, domains: {1: allow}}}",
            "key",
        ),
        ("non-finite number", "sandbox: {future: .inf}", "non-finite"),
    )
    transcript = []
    with TemporaryDirectory() as directory:
        host = Path(directory).resolve()
        config = host / CONFIG
        config.parent.mkdir(parents=True)
        for name, yaml, diagnostic in cases:
            config.write_text(yaml, encoding="utf-8")
            for arguments in ((), ("sandbox", "--", "/bin/echo", "workload started")):
                result = invoke(binary, host, *arguments)
                assert result.returncode == 1, (name, result)
                assert result.stdout == "", (name, result)
                assert CONFIG in result.stderr, (name, result)
                assert diagnostic in result.stderr, (name, result)
                transcript.append(
                    {
                        "case": name,
                        "command": arguments[0] if arguments else "serve",
                        "stderr": result.stderr.replace(str(host), "<project>"),
                    }
                )
    return transcript


def test_discovers_only_launch_directory_configuration(binary: Path) -> Transcript:
    transcript = []
    with TemporaryDirectory() as directory:
        host = Path(directory).resolve()
        accepted(binary, host)
        assert not (host / ".agents").exists()
        transcript.append({"case": "absent", "initialized": True})
        for location in (".agents", ".agents/console"):
            metadata = host / location
            metadata.parent.mkdir(parents=True, exist_ok=True)
            metadata.write_text("not a metadata directory", encoding="utf-8")
            accepted(binary, host)
            transcript.append({"case": f"{location} is a file", "initialized": True})
            metadata.unlink()

        for location in (".mcp-console/config.yaml", ".agents/mcp-console.yaml"):
            old = host / location
            old.parent.mkdir(parents=True, exist_ok=True)
            old.write_text("invalid: [", encoding="utf-8")
            accepted(binary, host)
            transcript.append({"case": f"ignored {location}", "initialized": True})

        config = host / CONFIG
        config.parent.mkdir()
        config.write_text("{}", encoding="utf-8")
        accepted(binary, host)
        transcript.append({"case": "only console config used", "initialized": True})

        config.write_text("unknown: true", encoding="utf-8")
        for arguments in ((), ("sandbox", "--", "/bin/echo", "workload started")):
            result = invoke(binary, host, *arguments)
            assert result.returncode == 1 and result.stdout == "", result
            assert CONFIG in result.stderr and "unknown" in result.stderr, result
            transcript.append(
                {
                    "case": CONFIG,
                    "command": arguments[0] if arguments else "serve",
                    "stderr": result.stderr.replace(str(host), "<project>"),
                }
            )

        child = host / "child"
        child.mkdir()
        accepted(binary, child)
        transcript.append({"case": "ancestors ignored", "initialized": True})

        config.unlink()
        config.mkdir()
        result = invoke(binary, host)
        assert result.returncode == 1 and "must be a regular file" in result.stderr, (
            result
        )
        transcript.append(
            {
                "case": "unreadable existing path",
                "stderr": result.stderr.replace(str(host), "<project>"),
            }
        )
    return transcript


def test_tool_parameter_settings_are_validated_before_launch(
    binary: Path,
) -> Transcript:
    cases = (
        ("unknown", {"missing": {"enabled": False}}, "tool.parameters.missing"),
        ("unavailable", {"r": {"description": "unused"}}, "tool.parameters.r"),
        (
            "unknown nested",
            {"requirements": {"parameters": {"missing": {"enabled": False}}}},
            "tool.parameters.requirements.parameters.missing",
        ),
        (
            "scalar children",
            {"timeout_ms": {"parameters": {"missing": {"enabled": False}}}},
            "tool.parameters.timeout_ms.parameters",
        ),
    )
    transcript = []
    with TemporaryDirectory() as directory:
        host = Path(directory).resolve()
        config = host / CONFIG
        config.parent.mkdir(parents=True)
        for name, parameters, diagnostic in cases:
            for visibility in ("configuration", "environment"):
                settings = {"tool": {"parameters": parameters}}
                if visibility == "configuration":
                    settings["languages"] = ["python"]
                config.write_text(json.dumps(settings), encoding="utf-8")
                for arguments in (
                    (
                        "sandbox",
                        "--",
                        sys.executable,
                        "-c",
                        "print('workload started')",
                    ),
                    ("serve", "--worker", "unused-worker"),
                ):
                    result = subprocess.run(
                        [binary, *arguments],
                        cwd=host,
                        env={**os.environ, "MCP_CONSOLE_LANGUAGES": "python"},
                        input="",
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    assert result.returncode == 1 and result.stdout == "", result
                    assert CONFIG in result.stderr and diagnostic in result.stderr, (
                        result
                    )
                    transcript.append(
                        {
                            "case": name,
                            "visibility": visibility,
                            "command": arguments[0],
                            "stderr": result.stderr.replace(str(host), "<project>"),
                        }
                    )
    return transcript


@requires(SANDBOX)
def test_accepts_supported_project_settings(binary: Path) -> Transcript:
    cases = (
        "{}",
        "sandbox: {}",
        "sandbox: {network: {proxy: {}}}",
        "sandbox: {filesystem: {read_write: [./out]}}",
        "sandbox: {filesystem: {read_write: ['./future café 雪']}}",
        "sandbox: {network: {proxy: {mode: limited, allow_upstream_proxy: true, domains: {allow: ['*.example.com'], deny: [blocked.example.com]}}, allow_local_binding: true}}",
    )
    transcript = []
    with TemporaryDirectory() as directory:
        host = Path(directory).resolve()
        config = host / CONFIG
        config.parent.mkdir(parents=True)
        for yaml in cases:
            config.write_text(yaml, encoding="utf-8")
            launch = accepted(
                binary,
                host,
                "serve",
                "--worker",
                "unused-worker",
                "--writable-root",
                "future CLI",
            )
            assert not (host / "future café 雪").exists()
            assert not (host / "future CLI").exists()
            # Decode only the known fixture input, preserving literal diagnostics.
            transcript.append({"configuration": parse_yaml(yaml), **launch})
    return transcript


def test_no_sandbox_rejects_explicit_permissions(binary: Path) -> Transcript:
    with TemporaryDirectory() as directory:
        host = Path(directory)
        config = host / CONFIG
        config.parent.mkdir(parents=True)
        config.write_text("sandbox: {network: restricted}")
        result = invoke(
            binary, host, "serve", "--no-sandbox", "--worker", "unused-worker"
        )
        assert result.returncode == 1 and "require sandboxing" in result.stderr, result
    return [{"stderr": result.stderr}]


@requires(SANDBOX)
def test_explicit_policy_bypasses_project_configuration(binary: Path) -> Transcript:
    # fmt: python
    script = code("""
        import os
        import sys

        assert "MCP_CONSOLE_SANDBOX_SETTINGS" not in os.environ
        assert "TEST_POLICY" not in os.environ
        print(sys.stdin.read())
        """)
    policy = {
        "filesystem": {
            "kind": "restricted",
            "entries": [
                {
                    "path": {"type": "special", "value": {"kind": "root"}},
                    "access": "read",
                },
            ],
        },
        "network": "restricted",
    }
    transcript = []
    with TemporaryDirectory() as directory:
        host = Path(directory)
        config = host / CONFIG
        config.parent.mkdir(parents=True)
        config.write_text("invalid: [", encoding="utf-8")
        # Also exercise a selected complete policy whose name is reserved for
        # internal application settings. Only the explicit selector owns it.
        for name in ("TEST_POLICY", "MCP_CONSOLE_SANDBOX_SETTINGS"):
            result = subprocess.run(
                [
                    binary,
                    "sandbox",
                    "--runner-config-env",
                    name,
                    "--",
                    sys.executable,
                    "-c",
                    script,
                ],
                cwd=host,
                env={
                    **os.environ,
                    "MCP_CONSOLE_SANDBOX_SETTINGS": "invalid ambient settings",
                    name: json.dumps(policy),
                },
                input="unchanged workload stdin",
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0 and result.stderr == "", result
            assert result.stdout == "unchanged workload stdin\n", result
            transcript.append({"selected": name, "stdout": result.stdout})
    return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
