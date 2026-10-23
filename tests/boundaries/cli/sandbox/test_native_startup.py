"""Native containment precedes execution of target loader constructors."""

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.records import Transcript
from support.requirements import (
    NATIVE_FIXTURES,
    NATIVE_LOADER_INSPECTION,
    SANDBOX,
    requires,
)


def constructor(
    binary: Path, *, restricted_reads: bool, denied_alias: bool = False
) -> Transcript:
    with TemporaryDirectory(prefix="native-startup-", dir=Path.home()) as directory:
        root = Path(directory).resolve()
        working = root / "workspace"
        working.mkdir()
        protected = root / "protected"
        protected.write_text("preserved")
        target = working / "target"
        fixture = (
            Path(__file__).resolve().parents[3] / "fixtures/native/target_constructor.c"
        )
        subprocess.run(
            ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-o", target, fixture],
            check=True,
            capture_output=True,
        )
        if restricted_reads or denied_alias:
            # ELF's interpreter and libraries may use /lib or /lib64 aliases.
            reads = (
                ["/usr", "/bin", "/lib", "/lib64", str(root)]
                if restricted_reads
                else ["/"]
            )
            if sys.platform == "darwin":
                reads.extend(["/System", "/Library"])
            elif restricted_reads:
                dependencies = subprocess.run(
                    ["ldd", target],
                    env={**os.environ, "LC_ALL": "C"},
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                # Include both the target's interpreter and ldd's tracer mapping.
                # Toolchains may place them and shared libraries outside /usr/lib.
                paths = [
                    Path(path)
                    for line in dependencies.stdout.splitlines()
                    for path in line.split(" (", 1)[0].strip().split(" => ")
                    if path.startswith("/")
                ]
                assert paths, dependencies
                reads.extend(str(path.parent) for path in paths)
            config = working / ".agents/console/config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps(
                    {
                        "sandbox": {
                            "filesystem": {
                                "read_only": reads,
                                "deny": [str(working / "secret")]
                                if denied_alias
                                else [],
                            }
                        }
                    }
                )
            )
            if restricted_reads:
                alias = working / "target-alias"
                alias.symlink_to(target)
                target = alias
        env = {
            **os.environ,
            "MCP_CONSOLE_HOME": str(root / "console-home"),
            "MCP_CONSOLE_TEST_PROTECTED_FILE": str(protected),
        }
        if denied_alias:
            (working / "secret").write_text("must not be readable")
            alias = working / "secret-alias"
            alias.symlink_to(working / "secret")
            env["MCP_CONSOLE_TEST_DENIED_ALIAS"] = str(alias)
        result = subprocess.run(
            [binary, "sandbox", "--", str(target)],
            cwd=working,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result
        assert result.stderr == "", result
        assert result.stdout == "target constructor observed native enforcement\n", (
            result
        )
        assert protected.read_text() == "preserved"
    evidence = [
        {
            "target_constructor_contained": True,
            "restricted_system_read_grants": restricted_reads,
        }
    ]
    if denied_alias:
        evidence[0]["denied_read_through_alias"] = True
    return evidence


@requires(SANDBOX, NATIVE_FIXTURES)
def test_contains_target_loader_before_main(binary: Path) -> Transcript:
    return constructor(binary, restricted_reads=False)


@requires(SANDBOX, NATIVE_FIXTURES, NATIVE_LOADER_INSPECTION)
def test_restricted_read_aliases_preserve_loader_and_containment(
    binary: Path,
) -> Transcript:
    return constructor(binary, restricted_reads=True)


@requires(SANDBOX, NATIVE_FIXTURES)
def test_denials_apply_through_aliases_before_main(binary: Path) -> Transcript:
    return constructor(binary, restricted_reads=False, denied_alias=True)


@requires(SANDBOX)
def test_rejects_native_external_resolver_mode(binary: Path) -> Transcript:
    with (
        TemporaryDirectory() as directory,
        patch.dict(os.environ, MCP_CONSOLE_HOME=str(Path(directory) / "caller-home")),
    ):
        root = Path(directory).resolve()
        caller_config = root / "caller-home/config.yaml"
        caller_config.parent.mkdir()
        caller_config.write_text("{")
        config = root / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(
            "resolver:\n  sandbox:\n    filesystem:\n      kind: external-sandbox\n"
        )
        result = subprocess.run(
            [binary, "serve"],
            cwd=root,
            env={**os.environ, "MCP_CONSOLE_HOME": str(root / "console-home")},
            input="",
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 1, result
        assert result.stdout == "", result
        assert "unknown field `kind`" in result.stderr, result
        return [
            {
                "exit_code": result.returncode,
                "stderr": result.stderr.replace(str(root), "<workspace>"),
            }
        ]
