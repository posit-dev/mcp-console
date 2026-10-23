#!/usr/bin/env -S uv run --script

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.normalization import code
from support.records import Transcript
from support.requirements import SANDBOX, requires
from support.suites import run_this_suite


@requires(SANDBOX)
def test_environment_matches_public_configuration_file(binary: Path) -> Transcript:
    # fmt: python
    script = code("""
        import errno
        import os
        from pathlib import Path
        import sys

        assert "TEST_CONFIG" not in os.environ
        assert os.environ["MESSAGE"] == 'value: café 雪, "quotes", $() and spaces'
        Path("output/created").write_text("allowed")
        try:
            Path("blocked").write_text("denied")
        except OSError as error:
            assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS)
        else:
            raise AssertionError("read-only profile allowed a workspace write")
        temporary = Path(os.environ["TMPDIR"])
        (temporary / "created").write_text("managed temporary storage")
        print(sys.stdin.read(), end="")
        print("public environment, relative grants, and managed tempdir")
        """)
    yaml = """
environment:
  MESSAGE: 'value: café 雪, "quotes", $() and spaces'
sandbox:
  profile: read_only
  filesystem:
    read_write: [./output]
"""
    configuration = {
        "environment": {"MESSAGE": 'value: café 雪, "quotes", $() and spaces'},
        "sandbox": {
            "profile": "read_only",
            "filesystem": {"read_write": ["./output"]},
        },
    }
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        (root / "output").mkdir()
        project = root / ".agents/console/config.yaml"
        project.parent.mkdir(parents=True)
        project.write_text("invalid: [", encoding="utf-8")
        home = root / "home"
        home.mkdir()
        (home / "config.yaml").write_text("invalid: [", encoding="utf-8")
        selected = root / "selected.yaml"
        selected.write_text(yaml, encoding="utf-8")
        for source, options, payload in (
            ("file", ["--config-file", str(selected)], None),
            ("YAML environment", ["--config-env", "TEST_CONFIG"], yaml),
            (
                "JSON environment",
                ["--config-env", "TEST_CONFIG"],
                json.dumps(configuration),
            ),
        ):
            environment = os.environ | {"MCP_CONSOLE_HOME": str(home)}
            environment.pop("TEST_CONFIG", None)
            if payload is not None:
                environment["TEST_CONFIG"] = payload
            result = subprocess.run(
                [binary, "sandbox", *options, "--", sys.executable, "-c", script],
                cwd=root,
                env=environment,
                input="unchanged stdin: 雪\n",
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert (result.returncode, result.stderr) == (0, ""), result
            assert result.stdout == (
                "unchanged stdin: 雪\n"
                "public environment, relative grants, and managed tempdir\n"
            ), result
            assert (root / "output/created").read_text() == "allowed"
            (root / "output/created").unlink()
            records.append({"source": source, "stdout": result.stdout})
    return records


@requires(SANDBOX)
def test_environment_layers_defaults_overrides_and_writable_roots(
    binary: Path,
) -> Transcript:
    # fmt: python
    script = code("""
        import os
        from pathlib import Path

        assert os.environ["DEFAULT"] == "retained"
        assert os.environ["MESSAGE"] == "last override"
        assert "TEST_CONFIG" not in os.environ
        Path("output/created").write_text("writable root")
        print("defaults < environment < ordered overrides; writable root retained")
        """)
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "output").mkdir()
        result = subprocess.run(
            [
                binary,
                "--default-config",
                "environment={DEFAULT: retained, MESSAGE: default}",
                "-c",
                "environment.MESSAGE=first override",
                "sandbox",
                "--config-env",
                "TEST_CONFIG",
                "-c",
                "environment.MESSAGE=last override",
                "--writable-root",
                "output",
                "--",
                sys.executable,
                "-c",
                script,
            ],
            cwd=root,
            env=os.environ
            | {
                "TEST_CONFIG": "environment: {MESSAGE: configured, TEST_CONFIG: consumed}\n",
                "MCP_CONSOLE_HOME": str(root / "home"),
            },
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert (result.returncode, result.stderr) == (0, ""), result
        assert result.stdout == (
            "defaults < environment < ordered overrides; writable root retained\n"
        ), result
        return [{"stdout": result.stdout}]


def test_environment_rejects_invalid_public_configuration(binary: Path) -> Transcript:
    records = []
    for case, payload, diagnostic in (
        ("absent", None, "cannot read configuration environment 'TEST_CONFIG'"),
        ("empty", "", "expected one mapping document"),
        ("sequence", "[]", "expected one mapping document"),
        ("malformed", "sandbox: [", "line"),
        ("native policy", '{"filesystem": {"kind": "unrestricted"}}', "unknown field"),
        (
            "invalid sandbox",
            "sandbox: {profile: unknown}",
            "sandbox: profile: unknown variant",
        ),
        ("invalid environment", "environment: {MESSAGE: 123}", "environment"),
    ):
        environment = os.environ.copy()
        environment.pop("TEST_CONFIG", None)
        if payload is not None:
            environment["TEST_CONFIG"] = payload
        result = subprocess.run(
            [
                binary,
                "sandbox",
                "--config-env",
                "TEST_CONFIG",
                "--",
                sys.executable,
                "-c",
                "print('workload started')",
            ],
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 1 and result.stdout == "", result
        assert "TEST_CONFIG" in result.stderr and diagnostic in result.stderr, result
        records.append({"case": case, "stderr": result.stderr})
    return records


def test_public_and_native_environment_selectors_are_exclusive(
    binary: Path,
) -> Transcript:
    result = subprocess.run(
        [
            binary,
            "sandbox",
            "--config-env",
            "PUBLIC_CONFIG",
            "--runner-config-env",
            "NATIVE_CONFIG",
            "--",
            "unused-command",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=os.environ | {"NO_COLOR": "1"},
    )
    assert result.returncode == 2 and result.stdout == "", result
    assert "cannot be used with" in result.stderr, result
    assert "--config-env" in result.stderr and "--runner-config-env" in result.stderr
    return [{"stderr": result.stderr}]


if __name__ == "__main__":
    run_this_suite(__file__)
