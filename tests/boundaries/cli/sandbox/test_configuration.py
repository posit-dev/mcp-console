#!/usr/bin/env -S uv run --script

import errno
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.normalization import code
from support.records import Transcript
from support.requirements import (
    LINUX_NATIVE,
    LINUX_SANDBOX,
    MACOS_SANDBOX,
    NESTED_NAMESPACES,
    POSIX,
    SANDBOX,
    command,
    requires,
)
from support.linux_sandbox import root_metadata_prefix
from support.suites import run_this_suite


def configuration() -> dict:
    return {
        "filesystem": {
            "kind": "restricted",
            "entries": [
                {
                    "path": {"type": "special", "value": {"kind": "root"}},
                    "access": "read",
                }
            ],
        },
        "network": "restricted",
    }


def invoke(binary: Path, config: dict | str, *command: str, launch_prefix=(), **kwargs):
    return subprocess.run(
        [
            *launch_prefix,
            binary,
            "sandbox",
            "--runner-config-env",
            "TEST_POLICY",
            "--",
            *command,
        ],
        env={
            **os.environ,
            "TEST_POLICY": config if isinstance(config, str) else json.dumps(config),
            "MCP_CONSOLE_SANDBOX_CONFIG": "unselected ambient value",
            "INHERITED": "from launcher",
        },
        capture_output=True,
        text=True,
        **kwargs,
    )


def test_rejects_unknown_native_policy_fields_before_execution(
    binary: Path,
) -> Transcript:
    transcript = []
    for value in (None, True, "unknown"):
        result = invoke(
            binary,
            configuration() | {"unsupported": value},
            sys.executable,
            "-c",
            "print('USER CODE RAN')",
        )
        assert result.returncode == 1 and result.stdout == "", result
        assert "invalid configuration JSON" in result.stderr, result
        transcript.append({"value": value, "stderr": result.stderr})
    return transcript


@requires(SANDBOX)
def test_explicit_policy_controls_filesystem_and_network(binary: Path) -> Transcript:
    # fmt: python
    script = code(r"""
        import errno
        import pathlib
        import socket
        import sys

        try:
            pathlib.Path("created").write_text("allowed", encoding="utf-8")
            print("write allowed")
        except OSError as error:
            assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS)
            print("write denied")
        try:
            with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=2):
                print("network allowed")
        except OSError:
            print("network denied")
        """)
    transcript = []
    with TemporaryDirectory() as directory, socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        for allowed, network in ((False, False), (True, True), (False, True)):
            config = configuration()
            if allowed:
                config["filesystem"]["entries"].append(
                    {
                        "path": {
                            "type": "path",
                            "path": str(Path(directory).resolve()),
                        },
                        "access": "write",
                    }
                )
            if network:
                config["network"] = "enabled"
            result = invoke(
                binary,
                config,
                sys.executable,
                "-c",
                script,
                str(listener.getsockname()[1]),
                cwd=directory,
            )
            assert result.returncode == 0, result.stderr
            assert result.stderr == ""
            permission = "allowed" if allowed else "denied"
            network_permission = "allowed" if network else "denied"
            assert (
                result.stdout == f"write {permission}\nnetwork {network_permission}\n"
            )
            assert (Path(directory) / "created").exists() == allowed
            if allowed:
                (Path(directory) / "created").unlink()
            transcript.append(
                {
                    "scenario": "network only"
                    if network and not allowed
                    else permission,
                    "stdout": result.stdout,
                }
            )
    return transcript


@requires(SANDBOX)
def test_unrestricted_and_external_policies_preserve_network_and_cleanup(
    binary: Path,
) -> Transcript:
    # fmt: python
    script = code("""
        import json
        import os
        from pathlib import Path
        import socket
        import sys

        assert "TEST_POLICY" not in os.environ
        assert "MCP_CONSOLE_SANDBOX_CONFIG" not in os.environ
        Path("created").write_text("allowed", encoding="utf-8")
        temporary = Path(os.environ["TMPDIR"])
        (temporary / "input").write_text(sys.stdin.read(), encoding="utf-8")
        try:
            with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=2):
                network = "allowed"
        except OSError:
            network = "denied"
        print(
            json.dumps(
                {
                    "network": network,
                    "temporary": str(temporary),
                    "input": (temporary / "input").read_text(encoding="utf-8"),
                }
            )
        )
        sys.exit(23)
        """)
    transcript = []
    with TemporaryDirectory() as directory, socket.socket() as listener:
        host = Path(directory).resolve()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        for kind in ("unrestricted", "external-sandbox"):
            for network in ("restricted", "enabled"):
                config = {
                    **configuration(),
                    "filesystem": {"kind": kind},
                    "network": network,
                    "lifecycle": {
                        "private_tmp": {"parent": str(host), "environment": ["TMPDIR"]}
                    },
                }
                result = invoke(
                    binary,
                    config,
                    sys.executable,
                    "-c",
                    script,
                    str(listener.getsockname()[1]),
                    cwd=host,
                    input="literal stdin\x00雪\n",
                )
                assert result.returncode == 23 and result.stderr == "", result
                output = json.loads(result.stdout)
                permission = (
                    "allowed"
                    if network == "enabled" or kind == "external-sandbox"
                    else "denied"
                )
                assert output["network"] == permission, (kind, network, output)
                assert output["input"] == "literal stdin\x00雪\n", output
                temporary = Path(output["temporary"])
                assert temporary.parent.parent == host, output
                assert not temporary.parent.exists(), output
                assert (host / "created").read_text(encoding="utf-8") == "allowed"
                (host / "created").unlink()
                transcript.append(
                    {
                        "filesystem": kind,
                        "network": network,
                        "exit_code": result.returncode,
                        "stdout": result.stdout.replace(
                            str(temporary), "<private data>"
                        ),
                        "private_storage_removed": True,
                    }
                )
    return transcript


@requires(SANDBOX)
def test_environment_overrides_and_arguments_are_literal(binary: Path) -> Transcript:
    value = "雪, café; 'quoted' \"double\" $() `literal` \\ newline\nend"
    # fmt: python
    script = code(r"""
        import json
        import os
        import sys

        assert "TEST_POLICY" not in os.environ
        assert "MCP_CONSOLE_SANDBOX_CONFIG" not in os.environ
        print(
            json.dumps(
                {
                    "inherited": os.environ.get("INHERITED"),
                    "marker": os.environ.get("MCP_CONSOLE_SANDBOX"),
                    "value": os.environ["VALUE"],
                    "arguments": sys.argv[1:],
                },
                ensure_ascii=False,
            )
        )
        """)
    transcript = []
    for inherit in (True, False):
        config = configuration()
        config["inherit_environment"] = inherit
        config["environment"] = {
            "VALUE": value,
            "MCP_CONSOLE_SANDBOX": value,
            "TEST_POLICY": "attempted reintroduction",
            "MCP_CONSOLE_SANDBOX_CONFIG": "attempted reintroduction",
        }
        arguments = [value, "--config-env", "ANOTHER_POLICY", "--bootstrap-fd", "3"]
        result = invoke(binary, config, sys.executable, "-c", script, *arguments)
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
        assert json.loads(result.stdout) == {
            "inherited": "from launcher" if inherit else None,
            "marker": value,
            "value": value,
            "arguments": arguments,
        }
        transcript.append({"inherit_environment": inherit, "stdout": result.stdout})
    return transcript


@requires(POSIX, SANDBOX)
def test_disabled_inheritance_ignores_non_utf8_host_environment(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        config = root / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps(
                {
                    "inherit_environment": False,
                    "environment": {"VALUE": "controlled"},
                }
            )
        )
        environment = os.environ.copy()
        environment[os.fsdecode(b"UNRELATED_NAME_\xff")] = "unused"
        environment["UNRELATED_VALUE"] = os.fsdecode(b"unused-\xff")
        result = subprocess.run(
            [
                binary,
                "sandbox",
                "--",
                sys.executable,
                "-c",
                "import os; print(os.environ['VALUE']); assert 'UNRELATED_VALUE' not in os.environ",
            ],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert result.stderr == "", result.stderr
        assert result.stdout == "controlled\n", result.stdout
        return [{"stdout": result.stdout}]


@requires(SANDBOX)
def test_rejects_malformed_and_conflicting_configuration(binary: Path) -> Transcript:
    transcript = []
    for name, value in (
        ("malformed JSON", "{"),
        ("file reference", "@config.json"),
        ("missing policy", "{}"),
        ("duplicate field", '{"network":"restricted","network":"restricted"}'),
        ("removed version", {**configuration(), "version": 2}),
        ("duplicate command", {**configuration(), "command": ["/bin/true"]}),
        ("duplicate cwd", {**configuration(), "cwd": "/"}),
        ("file include", {**configuration(), "include": ["policy.json"]}),
        (
            "transport export",
            {
                **configuration(),
                "lifecycle": {"private_tmp": {"environment": ["TEST_POLICY"]}},
            },
        ),
        (
            "invalid lifecycle",
            {**configuration(), "lifecycle": {"cleanup_timeout_ms": 0}},
        ),
        (
            "invalid environment",
            {**configuration(), "environment": "PRIVATE_ENVIRONMENT_SENTINEL"},
        ),
    ):
        result = invoke(binary, value, "/bin/echo", "executed")
        assert result.returncode != 0
        assert result.stdout == ""
        assert result.stderr
        assert "PRIVATE_ENVIRONMENT_SENTINEL" not in result.stderr
        transcript.append(
            {
                "scenario": name,
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
    for options in (
        ["--runner-config-env", "ABSENT_POLICY"],
        ["--runner-config-env", "TEST_POLICY", "--runner-config-env", "SECOND_POLICY"],
        ["--runner-config-env", "TEST_POLICY", "--exit-with-parent", str(os.getpid())],
    ):
        result = subprocess.run(
            [binary, "sandbox", *options, "--", "/bin/echo", "executed"],
            env={
                **os.environ,
                "TEST_POLICY": json.dumps(configuration()),
                "NO_COLOR": "1",
            },
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert result.stdout == ""
        transcript.append(
            {
                "scenario": " ".join(options[:3]),
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
    return transcript


@requires(SANDBOX)
def test_options_before_command_are_launcher_options(binary: Path) -> Transcript:
    transcript = []
    for option in ("--bootstrap-fd", "--unsupported-launch-option"):
        result = subprocess.run(
            [
                binary,
                "sandbox",
                "--runner-config-env",
                "TEST_POLICY",
                option,
                "3",
                "--",
                "/bin/echo",
                "executed",
            ],
            env={
                **os.environ,
                "TEST_POLICY": json.dumps(configuration()),
                "NO_COLOR": "1",
            },
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2, result.stderr
        assert result.stdout == ""
        assert f"unexpected argument '{option}'" in result.stderr
        transcript.append(
            {"option": option, "exit_code": result.returncode, "stderr": result.stderr}
        )
    return transcript


@requires(SANDBOX)
def test_large_environment_configuration_preserves_stdin(binary: Path) -> Transcript:
    config = json.dumps(configuration()) + " " * 65536
    result = invoke(binary, config, "/bin/cat", input="independent stdin\n\x00雪\n")
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout == "independent stdin\n\x00雪\n"
    # The OS rejects an oversized launch before frontend diagnostics can run.
    oversized = " " * (os.sysconf("SC_ARG_MAX") * 2)
    try:
        invoke(binary, oversized, "/bin/echo", "executed")
    except OSError as error:
        assert error.errno == errno.E2BIG
    else:
        raise AssertionError("oversized exec environment was accepted")
    return [
        {"configuration_padding_bytes": 65536, "stdout": result.stdout},
        {"oversized_launch_errno": "E2BIG"},
    ]


@requires(SANDBOX)
def test_target_cannot_replace_active_policy(binary: Path) -> Transcript:
    # fmt: python
    script = code(r"""
        import errno
        import json
        import os
        from pathlib import Path
        import subprocess
        import sys

        forbidden = Path(sys.argv[2])
        policy = json.loads(sys.argv[3])
        os.environ["TEST_POLICY"] = json.dumps(policy)
        os.environ["MCP_CONSOLE_SANDBOX_CONFIG"] = json.dumps(policy)
        temporary = Path(os.environ["TMPDIR"])
        for name in ("config.json", "sandbox.json", ".mcp-console.json"):
            (temporary / name).write_text(json.dumps(policy), encoding="utf-8")
        os.chdir(temporary)
        print(json.dumps(policy))  # stdout is target data, never host policy input.
        try:
            forbidden.write_text("escaped", encoding="utf-8")
        except OSError as error:
            assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS)
            print("active policy still denies writes")
        else:
            raise AssertionError("target environment or files changed policy")
        child = subprocess.run(
            [
                sys.argv[1],
                "sandbox",
                "--runner-config-env",
                "TEST_POLICY",
                "--",
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('escaped')",
                str(forbidden),
            ],
            env={**os.environ, "TEST_POLICY": json.dumps(policy)},
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert child.returncode != 0
        assert child.stdout == b""
        assert not forbidden.exists()
        print("nested sandbox cannot grant host writes")
        """)
    with TemporaryDirectory() as directory:
        forbidden = Path(directory).resolve() / "forbidden"
        permissive = configuration()
        permissive["filesystem"]["entries"].append(
            {"path": {"type": "path", "path": str(forbidden.parent)}, "access": "write"}
        )
        permissive["network"] = "enabled"
        restricted = configuration()
        restricted["lifecycle"] = {"private_tmp": {"environment": ["TMPDIR"]}}
        result = invoke(
            binary,
            restricted,
            sys.executable,
            "-c",
            script,
            str(binary),
            str(forbidden),
            json.dumps(permissive),
        )
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
        expected = (
            json.dumps(permissive)
            + """
active policy still denies writes
nested sandbox cannot grant host writes
"""
        )
        assert result.stdout == expected
        assert not forbidden.exists()
        return [
            {
                "stdout": result.stdout.replace(
                    str(forbidden.parent), "<host directory>"
                ),
                "transcript_normalization": {"host_directory": "omitted"},
            }
        ]


@requires(SANDBOX)
def test_default_sandbox_ignores_ambient_configuration(binary: Path) -> Transcript:
    result = subprocess.run(
        [
            binary,
            "sandbox",
            "--",
            sys.executable,
            "-c",
            "import os; assert 'MCP_CONSOLE_SANDBOX_CONFIG' not in os.environ; print('default policy')",
        ],
        env={**os.environ, "MCP_CONSOLE_SANDBOX_CONFIG": "malformed ambient policy"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "default policy\n"
    assert result.stderr == ""
    return [{"stdout": result.stdout}]


@requires(SANDBOX)
def test_launcher_marker_cannot_replace_selected_configuration(
    binary: Path,
) -> Transcript:
    config = configuration()
    config["environment"] = {"MCP_CONSOLE_SANDBOX": "attempted reintroduction"}
    result = subprocess.run(
        [
            binary,
            "sandbox",
            "--runner-config-env",
            "MCP_CONSOLE_SANDBOX",
            "--",
            sys.executable,
            "-c",
            "import os; assert 'MCP_CONSOLE_SANDBOX' not in os.environ; print('selected transport removed')",
        ],
        env={**os.environ, "MCP_CONSOLE_SANDBOX": json.dumps(config)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "selected transport removed\n"
    assert result.stderr == ""
    return [{"stdout": result.stdout}]


@requires(SANDBOX, command("Rscript"))
def test_child_specific_shell_python_and_processx_examples(binary: Path) -> Transcript:
    examples = Path(__file__).resolve().parents[4] / "examples"
    transcript = []
    for runtime, suffix in (
        ("/bin/sh", "sh"),
        (sys.executable, "py"),
        ("Rscript", "R"),
    ):
        result = subprocess.run(
            [runtime, str(examples / f"sandbox-config.{suffix}"), str(binary)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
        assert (
            result.stdout
            == 'value: café 雪, "quotes", $() and spaces\nliteral argument: * ; $(echo no)\n'
        )
        transcript.append(
            {"example": f"sandbox-config.{suffix}", "stdout": result.stdout}
        )
    return transcript


@requires(LINUX_NATIVE)
def test_rejects_removed_landlock_before_target_execution(binary: Path) -> Transcript:
    transcript = []
    for filesystem in (configuration()["filesystem"], {"kind": "unrestricted"}):
        config = {
            **configuration(),
            "filesystem": filesystem,
            "network": "enabled",
            "linux_backend": "landlock",
        }
        result = invoke(binary, config, "/bin/echo", "must not run")
        assert result.returncode == 1 and result.stdout == "", result
        # The pinned schema no longer parses the removed enum variant.
        # Keep its complete parse diagnostic and source location in the snapshot.
        assert "invalid configuration JSON (Data at line 1," in result.stderr, result
        transcript.append(
            {
                "filesystem": filesystem["kind"],
                "stderr": result.stderr,
                "exit_code": result.returncode,
            }
        )
    return transcript


def target_start_control(binary: Path) -> Transcript:
    # Stdout proves the marker works independently of file permissions.
    config = {
        **configuration(),
        "filesystem": {"kind": "unrestricted"},
        "network": "enabled",
    }
    result = invoke(binary, config, "/bin/echo", "target started")
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        "target started\n",
        "",
    ), result
    return [{"scenario": "full-write positive control", "stdout": result.stdout}]


@requires(LINUX_SANDBOX)
def test_supervised_linux_accepts_full_write_policies(binary: Path) -> Transcript:
    transcript = target_start_control(binary)
    root = {**configuration()["filesystem"]["entries"][0], "access": "write"}
    path = {"type": "path", "path": "/tmp"}
    for backend in (None, "bubblewrap"):
        for network in ("restricted", "enabled"):
            for name, filesystem in (
                ("unrestricted", {"kind": "unrestricted"}),
                ("external sandbox", {"kind": "external-sandbox"}),
                ("root write", {"kind": "restricted", "entries": [root]}),
                (
                    "shadowed read carveout",
                    {
                        "kind": "restricted",
                        "entries": [
                            root,
                            {"path": path, "access": "read"},
                            {"path": path, "access": "write"},
                        ],
                    },
                ),
            ):
                config = {
                    **configuration(),
                    "filesystem": filesystem,
                    "network": network,
                }
                if backend is not None:
                    config["linux_backend"] = backend
                result = invoke(binary, config, "/bin/echo", "target started")
                assert (result.returncode, result.stdout, result.stderr) == (
                    0,
                    "target started\n",
                    "",
                ), result
                transcript.append(
                    {
                        "backend": backend,
                        "network": network,
                        "filesystem": name,
                        "stdout": result.stdout,
                        "exit_code": result.returncode,
                    }
                )
    return transcript


@requires(LINUX_SANDBOX, NESTED_NAMESPACES)
def test_bubblewrap_enforces_root_write_carveouts(binary: Path) -> Transcript:
    # fmt: python
    script = code(r"""
        import errno
        from pathlib import Path
        import sys

        print("target started", flush=True)
        allowed, protected = map(Path, sys.argv[1:3])
        allowed.write_text("allowed")
        try:
            protected.write_text("escaped")
        except OSError as error:
            assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS)
            print("carveout write denied")
        else:
            raise AssertionError("carveout write succeeded")
        try:
            assert protected.read_text() == "protected"
            print("carveout read allowed")
        except OSError as error:
            assert error.errno in (errno.EPERM, errno.EACCES)
            print("carveout read denied")
        """)
    transcript = []
    with TemporaryDirectory() as directory:
        prefix = root_metadata_prefix(Path(directory))
        allowed = Path(directory).resolve() / "allowed"
        protected = Path(directory).resolve() / "protected"
        protected.write_text("protected")
        for backend in (None, "bubblewrap"):
            for access in ("read", "deny"):
                config = configuration()
                config["filesystem"]["entries"][0]["access"] = "write"
                config["filesystem"]["entries"].append(
                    {"path": {"type": "path", "path": str(protected)}, "access": access}
                )
                if backend is not None:
                    config["linux_backend"] = backend
                result = invoke(
                    binary,
                    config,
                    sys.executable,
                    "-c",
                    script,
                    str(allowed),
                    str(protected),
                    launch_prefix=prefix,
                )
                assert result.returncode == 0 and result.stderr == "", result
                permission = "allowed" if access == "read" else "denied"
                assert result.stdout == (
                    "target started\ncarveout write denied\n"
                    f"carveout read {permission}\n"
                ), result
                assert allowed.read_text() == "allowed"
                assert protected.read_text() == "protected"
                allowed.unlink()
                transcript.append(
                    {"backend": backend, "carveout": access, "stdout": result.stdout}
                )
    return transcript


@requires(MACOS_SANDBOX)
def test_linux_backend_is_rejected_on_macos(binary: Path) -> Transcript:
    result = invoke(
        binary,
        configuration() | {"linux_backend": "bubblewrap"},
        "/bin/echo",
        "must not run",
    )
    assert result.returncode == 1 and not result.stdout, result
    assert "linux_backend is supported only on Linux" in result.stderr
    return [{"stderr": result.stderr, "exit_code": result.returncode}]


if __name__ == "__main__":
    run_this_suite(__file__)
