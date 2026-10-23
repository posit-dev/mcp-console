#!/usr/bin/env -S uv run --script

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.native import LOADER_VARIABLE, build_interposer
from support.client import McpClient
from support.normalization import code
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, SANDBOX, requires
from support.sandbox_configuration import NATIVE_PROXY
from support.suites import run_this_suite


CONFIG = ".agents/console/config.yaml"


@requires(SANDBOX, NATIVE_FIXTURES)
def test_omits_absent_proxy_and_native_lifecycle_defaults(binary: Path) -> Transcript:
    transcript = []
    with TemporaryDirectory() as directory:
        host = Path(directory).resolve()
        config = host / CONFIG
        config.parent.mkdir(parents=True)
        capture = host / "payloads.jsonl"
        environment = {
            **os.environ,
            LOADER_VARIABLE: str(build_interposer(host, "runner_configuration")),
            "MCP_CONSOLE_TEST_RUNNER_CONFIGURATION": str(capture),
        }
        for name, settings in (
            ("absent file", None),
            ("omitted", {}),
            ("explicit restricted", {"network": "restricted"}),
        ):
            if settings is not None:
                config.write_text(json.dumps({"sandbox": settings}), encoding="utf-8")
            result = subprocess.run(
                [binary, "sandbox", "--", "/usr/bin/true"],
                cwd=host,
                env=environment,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0 and result.stdout == result.stderr == "", (
                result
            )
            payloads = [json.loads(line) for line in capture.read_text().splitlines()]
            for payload in payloads:
                assert "proxy" not in payload, payload
                assert "cleanup_timeout_ms" not in payload["lifecycle"], payload
            assert payloads[-1]["lifecycle"] == {
                "private_tmp": {"environment": ["TMPDIR"]}
            }, payloads
            capture.unlink()
            transcript.append(
                {"case": name, "proxy_omitted": True, "native_cleanup_default": True}
            )
    return transcript


@requires(SANDBOX, NATIVE_FIXTURES)
def test_augments_both_restricted_representations(binary: Path) -> Transcript:
    transcript = []
    with TemporaryDirectory() as directory:
        host = Path(directory).resolve()
        (host / "input").write_text("host read", encoding="utf-8")
        output = host / "output"
        output.mkdir()
        config = host / CONFIG
        config.parent.mkdir(parents=True)
        capture = host / "payloads.jsonl"
        environment = {
            **os.environ,
            LOADER_VARIABLE: str(build_interposer(host, "runner_configuration")),
            "MCP_CONSOLE_TEST_RUNNER_CONFIGURATION": str(capture),
        }
        for kind in (None, {}):
            for writable in (False, True):
                config.write_text(
                    json.dumps(
                        {"sandbox": {} if kind is None else {"filesystem": kind}}
                    ),
                    encoding="utf-8",
                )
                result = subprocess.run(
                    [
                        binary,
                        "sandbox",
                        *(["--writable-root", "output"] if writable else []),
                        "--",
                        sys.executable,
                        "-c",
                        # fmt: python
                        code("""
                            from pathlib import Path
                            import sys

                            print(Path("input").read_text())
                            if sys.argv[1] == "1":
                                Path("output/result").write_text("CLI grant")
                            """),
                        str(int(writable)),
                    ],
                    cwd=host,
                    env=environment,
                    capture_output=True,
                    text=True,
                )
                assert result.returncode == 0 and result.stderr == "", result
                assert result.stdout == "host read\n", result
                payloads = [
                    json.loads(line) for line in capture.read_text().splitlines()
                ]
                assert len(payloads) == 2, payloads
                for payload in payloads:
                    assert payload["filesystem"]["kind"] == "restricted", payload
                    assert payload["filesystem"]["entries"][0] == {
                        "path": {"type": "special", "value": {"kind": "root"}},
                        "access": "read",
                    }, payload
                    if sys.platform == "darwin":
                        assert payload["macos_seatbelt_profile_extension"], payload
                if writable:
                    assert (output / "result").read_text() == "CLI grant"
                    (output / "result").unlink()
                capture.unlink()
                transcript.append(
                    {"kind": kind, "writable_root": writable, "stdout": result.stdout}
                )
    return transcript


@requires(SANDBOX, NATIVE_FIXTURES)
def test_preserves_application_marker_in_target_environment(binary: Path) -> Transcript:
    transcript = []
    with TemporaryDirectory() as directory:
        host = Path(directory).resolve()
        config = host / CONFIG
        config.parent.mkdir(parents=True)
        capture = host / "payloads.jsonl"
        environment = {
            **os.environ,
            "MCP_CONSOLE_SANDBOX": "ambient marker",
            LOADER_VARIABLE: str(build_interposer(host, "runner_configuration")),
            "MCP_CONSOLE_TEST_RUNNER_CONFIGURATION": str(capture),
        }
        for inherit in (False, True):
            for target in (
                None,
                {"MCP_CONSOLE_SANDBOX": "project marker", "PROJECT_VALUE": "retained"},
            ):
                settings = {"inherit_environment": inherit}
                if target is not None:
                    settings["environment"] = target
                config.write_text(json.dumps(settings), encoding="utf-8")
                result = subprocess.run(
                    [
                        binary,
                        "sandbox",
                        "--",
                        sys.executable,
                        "-c",
                        "import os; print(os.environ.get('MCP_CONSOLE_SANDBOX', 'absent'))",
                    ],
                    cwd=host,
                    env=environment,
                    capture_output=True,
                    text=True,
                )
                assert result.returncode == 0 and result.stderr == "", result
                assert result.stdout == "1\n", result
                payloads = [
                    json.loads(line) for line in capture.read_text().splitlines()
                ]
                assert len(payloads) == 2, payloads
                expected = dict(target or {})
                if inherit:
                    expected.pop("MCP_CONSOLE_SANDBOX", None)
                else:
                    expected["MCP_CONSOLE_SANDBOX"] = "1"
                for payload in payloads:
                    if inherit and target is None:
                        assert "environment" not in payload, payload
                    else:
                        assert payload["environment"] == expected, payload
                capture.unlink()
                transcript.append({"settings": settings, "stdout": result.stdout})
    return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
