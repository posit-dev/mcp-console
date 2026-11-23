"""Trusted limits constrain the final policy independently of user configuration."""

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.normalization import code
from support.records import Transcript
from support.requirements import LINUX_SANDBOX, SANDBOX, requires
from support.sandbox_configuration import host_tcp_ports
from support.suites import run_this_suite


def test_invalid_limits_fail_before_launch(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for name, payload in (
            ("missing variable", None),
            ("invalid YAML", "filesystem: ["),
            ("unknown field", '{"filesystem":{"allow":["."]}}'),
            ("path glob", '{"filesystem":{"deny":["./private/*"]}}'),
            ("empty path", '{"filesystem":{"deny":[""]}}'),
            ("domain URL", '{"network":{"domains":{"allow":["https://example.com"]}}}'),
            ("binding type", '{"network":{"allow_local_binding":"auto"}}'),
        ):
            environment = os.environ | {"MCP_CONSOLE_HOME": str(root / "home")}
            environment.pop("TEST_LIMITS", None)
            if payload is not None:
                environment["TEST_LIMITS"] = payload
            result = subprocess.run(
                [
                    binary,
                    "sandbox",
                    "--no-config",
                    "--limits-env",
                    "TEST_LIMITS",
                    "--",
                    sys.executable,
                    "-c",
                    "print('workload started')",
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert result.returncode != 0 and result.stdout == "", (name, result)
            assert "limits" in result.stderr.lower(), (name, result)
            records.append(
                {"case": name, "exit_code": result.returncode, "stderr": result.stderr}
            )
    return records


def test_limits_reject_launch_paths_that_cannot_preserve_them(
    binary: Path,
) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = os.environ | {
            "MCP_CONSOLE_HOME": str(root / "home"),
            "TEST_LIMITS": '{"filesystem":{"deny":["./private"]}}',
            "TEST_RUNNER": "{}",
        }
        for arguments in (
            ["serve", "--no-sandbox"],
            ["config", "resolve"],
            ["warmup"],
            [
                "sandbox",
                "--runner-config-env",
                "TEST_RUNNER",
                "--",
                sys.executable,
                "-c",
                "print('workload started')",
            ],
        ):
            result = subprocess.run(
                [binary, "--limits-env", "TEST_LIMITS", *arguments],
                cwd=root,
                env=environment,
                input="",
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert result.returncode != 0 and result.stdout == "", (arguments, result)
            assert "limits" in result.stderr.lower(), (arguments, result)
            records.append(
                {
                    "arguments": arguments[:-3] if "--" in arguments else arguments,
                    "exit_code": result.returncode,
                    "stderr": result.stderr,
                }
            )
    return records


def test_limits_reject_reserved_transport_and_trusted_warmup(
    binary: Path,
) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for name, variable, extra in (
            ("reserved transport", "MCP_CONSOLE_LIMITS", {}),
            ("trusted warmup", "TEST_LIMITS", {"MCP_CONSOLE_TRUSTED_WARMUP": "1"}),
        ):
            result = subprocess.run(
                [binary, "serve", "--no-config", "--limits-env", variable],
                cwd=root,
                env=os.environ
                | {
                    "MCP_CONSOLE_HOME": str(root / "home"),
                    variable: '{"filesystem":{"deny":["./private"]}}',
                }
                | extra,
                input="",
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert result.returncode != 0 and result.stdout == "", (name, result)
            assert "limits" in result.stderr.lower(), (name, result)
            records.append(
                {"case": name, "exit_code": result.returncode, "stderr": result.stderr}
            )
    return records


@requires(SANDBOX)
def test_empty_limits_preserve_access_and_consume_the_transport(
    binary: Path,
) -> Transcript:
    # fmt: python
    script = code(r"""
        import os
        from pathlib import Path

        assert "TEST_LIMITS" not in os.environ
        assert os.environ["ORDINARY_VALUE"] == "retained"
        assert Path("ordinary").read_text() == "before"
        Path("ordinary").write_text("after")
        print("ordinary access retained; limits transport absent")
        """)
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        (root / "ordinary").write_text("before")
        result = subprocess.run(
            [
                binary,
                "sandbox",
                "--no-config",
                "--limits-env",
                "TEST_LIMITS",
                "-c",
                "sandbox.profile=workspace",
                "-c",
                "sandbox.network=enabled",
                "-c",
                "inherit_environment=false",
                "-c",
                'environment={TEST_LIMITS: "reintroduced", ORDINARY_VALUE: "retained"}',
                "--",
                sys.executable,
                "-c",
                script,
            ],
            cwd=root,
            env=os.environ
            | {"MCP_CONSOLE_HOME": str(root / "home"), "TEST_LIMITS": "{}"},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert (result.returncode, result.stderr) == (0, ""), result
        assert result.stdout == "ordinary access retained; limits transport absent\n", (
            result
        )
        assert (root / "ordinary").read_text() == "after"
        return [{"stdout": result.stdout}]


@requires(SANDBOX)
def test_limits_close_descendant_grants_and_retain_user_denials(
    binary: Path,
) -> Transcript:
    # fmt: python
    script = code(r"""
        import os
        from pathlib import Path

        assert "TEST_LIMITS" not in os.environ
        assert "TEST_CONFIG" not in os.environ
        Path("public/created").write_text("permitted")
        for name in ("protected/child/secret", "user-secret"):
            path = Path(name)
            for operation in (path.read_text, lambda: path.write_text("escaped")):
                try:
                    operation()
                except OSError:
                    pass
                else:
                    raise AssertionError(f"denied operation succeeded: {name}")
        print("descendant grants capped; user denial retained; public write allowed")
        """)
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        (root / "protected/child").mkdir(parents=True)
        (root / "protected/child/secret").write_text("protected")
        (root / "user-secret").write_text("user denial")
        (root / "public").mkdir()
        config = {
            "environment": {"TEST_LIMITS": "ordinary settings cannot reintroduce it"},
            "sandbox": {
                "profile": "workspace",
                # This case isolates filesystem composition; network caps have
                # their own regression below.
                "network": "enabled",
                "filesystem": {
                    "read_write": ["./protected/child", "./public"],
                    "read_only": ["./protected/child/secret"],
                    "deny": ["./user-secret"],
                },
            },
        }
        selected = root / "ordinary.json"
        selected.write_text(json.dumps(config))
        for source, arguments in (
            ("file", ["--config-file", str(selected)]),
            ("environment", ["--config-env", "TEST_CONFIG"]),
        ):
            environment = os.environ | {
                "MCP_CONSOLE_HOME": str(root / "home"),
                "TEST_LIMITS": "filesystem:\n  deny: [./protected]\n",
            }
            environment.pop("TEST_CONFIG", None)
            if source == "environment":
                environment["TEST_CONFIG"] = json.dumps(config)
            result = subprocess.run(
                [
                    binary,
                    "sandbox",
                    *arguments,
                    "--limits-env",
                    "TEST_LIMITS",
                    "--writable-root",
                    "protected/child",
                    "-c",
                    "sandbox.filesystem.read_write=[protected/child, public]",
                    "--",
                    sys.executable,
                    "-c",
                    script,
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert (result.returncode, result.stderr) == (0, ""), result
            assert result.stdout == (
                "descendant grants capped; user denial retained; public write allowed\n"
            ), result
            assert (root / "protected/child/secret").read_text() == "protected"
            assert (root / "user-secret").read_text() == "user denial"
            assert (root / "public/created").read_text() == "permitted"
            records.append({"source": source, "stdout": result.stdout})
    return records


@requires(SANDBOX)
def test_limits_follow_symlink_grants_into_protected_subtrees(
    binary: Path,
) -> Transcript:
    # fmt: python
    script = code(r"""
        from pathlib import Path

        for name in ("protected/child/secret", "alias/child/secret"):
            path = Path(name)
            for operation in (path.read_text, lambda: path.write_text("escaped")):
                try:
                    operation()
                except OSError:
                    pass
                else:
                    raise AssertionError(f"alias reopened protected path: {name}")
        print("direct and symlink access remain denied")
        """)
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        (root / "protected/child").mkdir(parents=True)
        (root / "protected/child/secret").write_text("protected")
        (root / "alias").symlink_to(root / "protected", target_is_directory=True)
        result = subprocess.run(
            [
                binary,
                "sandbox",
                "--no-config",
                "--limits-env",
                "TEST_LIMITS",
                "--writable-root",
                "alias/child",
                "-c",
                "sandbox.filesystem.read_only=[alias/child/secret]",
                "-c",
                "sandbox.network=enabled",
                "--",
                sys.executable,
                "-c",
                script,
            ],
            cwd=root,
            env=os.environ
            | {
                "MCP_CONSOLE_HOME": str(root / "home"),
                "TEST_LIMITS": '{"filesystem":{"deny":["./protected"]}}',
            },
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert (result.returncode, result.stderr) == (0, ""), result
        assert result.stdout == "direct and symlink access remain denied\n", result
        assert (root / "protected/child/secret").read_text() == "protected"
        return [{"stdout": result.stdout}]


@requires(LINUX_SANDBOX)
def test_binding_limit_denies_servers_and_preserves_outbound_connections(
    binary: Path,
) -> Transcript:
    # fmt: python
    script = code(r"""
        import errno
        import os
        import socket
        import sys

        assert "TEST_LIMITS" not in os.environ
        for operation in (
            lambda sock: sock.bind(("127.0.0.1", 0)),
            lambda sock: sock.listen(1),
        ):
            with socket.socket() as sock:
                try:
                    operation(sock)
                except OSError as error:
                    assert error.errno == errno.EPERM, error
                else:
                    raise AssertionError("mandatory binding limit was reopened")
        with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=3):
            pass
        print("bind and listen denied; ordinary outbound connection retained")
        """)
    with TemporaryDirectory() as temporary, host_tcp_ports() as ports:
        root = Path(temporary).resolve()
        result = subprocess.run(
            [
                binary,
                "sandbox",
                "--no-config",
                "--limits-env",
                "TEST_LIMITS",
                "-c",
                "sandbox.network=enabled",
                "--",
                sys.executable,
                "-c",
                script,
                str(ports[0]),
            ],
            cwd=root,
            env=os.environ
            | {
                "MCP_CONSOLE_HOME": str(root / "home"),
                "TEST_LIMITS": "network: {allow_local_binding: false}",
            },
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert (result.returncode, result.stderr) == (0, ""), result
        assert result.stdout == (
            "bind and listen denied; ordinary outbound connection retained\n"
        ), result
        return [{"stdout": result.stdout}]


@requires(LINUX_SANDBOX)
def test_network_limits_force_filtering_without_enabling_network(
    binary: Path,
) -> Transcript:
    # fmt: python
    script = code(r"""
        import http.client
        import json
        import os
        import socket
        import sys
        from urllib.parse import urlsplit

        assert "TEST_LIMITS" not in os.environ
        filtered = sys.argv[1] == "filtered"
        assert (os.environ.get("CODEX_NETWORK_PROXY_ACTIVE") == "1") == filtered
        proxy_ports = set()
        if filtered:
            proxy = urlsplit(os.environ["HTTP_PROXY"])
            proxy_ports = {
                urlsplit(os.environ[key]).port
                for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
            }
            connection = http.client.HTTPConnection(proxy.hostname, proxy.port, timeout=5)
            connection.request("GET", "http://blocked.example.invalid/")
            response = connection.getresponse()
            assert response.status == 403, (response.status, response.read())
            connection.close()
        port = next(port for port in json.loads(sys.argv[2]) if port not in proxy_ports)
        try:
            socket.create_connection(("127.0.0.1", port), timeout=2)
        except OSError:
            pass
        else:
            raise AssertionError("direct connection bypassed network limits")
        print(
            "filtered denial; direct route closed" if filtered else "network remains disabled"
        )
        """)
    records = []
    with TemporaryDirectory() as temporary, host_tcp_ports() as ports:
        root = Path(temporary).resolve()
        for name, network, domains, filtered in (
            ("enabled with empty cap", "enabled", {"allow": []}, True),
            (
                "enabled with deny",
                "enabled",
                {"deny": ["blocked.example.invalid"]},
                True,
            ),
            (
                "empty requested allowlist",
                {"proxy": {"domains": {"allow": []}}},
                {"allow": ["*"]},
                True,
            ),
            ("disabled stays disabled", "restricted", {"allow": ["*"]}, False),
        ):
            environment = {
                key: value
                for key, value in os.environ.items()
                if "proxy" not in key.lower() and not key.startswith("CODEX_NETWORK_")
            }
            environment.update(
                {
                    "MCP_CONSOLE_HOME": str(root / "home"),
                    "TEST_LIMITS": json.dumps({"network": {"domains": domains}}),
                }
            )
            result = subprocess.run(
                [
                    binary,
                    "sandbox",
                    "--no-config",
                    "--limits-env",
                    "TEST_LIMITS",
                    "-c",
                    f"sandbox.network={json.dumps(network)}",
                    "--",
                    sys.executable,
                    "-c",
                    script,
                    "filtered" if filtered else "disabled",
                    json.dumps(ports),
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert (result.returncode, result.stderr) == (0, ""), (name, result)
            expected = (
                "filtered denial; direct route closed\n"
                if filtered
                else "network remains disabled\n"
            )
            assert result.stdout == expected, (name, result)
            records.append({"case": name, "stdout": result.stdout})
    return records


if __name__ == "__main__":
    run_this_suite(__file__)
