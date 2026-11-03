#!/usr/bin/env -S uv run --script

import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient
from support.native import LOADER_VARIABLE, build_interposer
from support.normalization import code
from support.records import TranscriptWithCompanions
from support.requirements import NATIVE_FIXTURES, R, SANDBOX, requires
from support.resolvers import send_and_collect_runtime_python_resolution
from support.sandbox_configuration import NATIVE_PROXY, host_tcp_ports
from support.suites import run_this_suite


def _snapshot_survives_replacement(
    binary: Path, configured: bool
) -> TranscriptWithCompanions:
    # fmt: python
    exercise = code(r"""
        import errno
        import json
        import os
        from pathlib import Path
        import socket
        from urllib.parse import urlsplit

        host = Path(os.environ["MCP_CONSOLE_TEST_PROJECT"])
        configured = os.environ["MCP_CONSOLE_TEST_CONFIGURED"] == "1"
        assert "MCP_CONSOLE_SANDBOX_SETTINGS" not in os.environ
        assert "MCP_CONSOLE_SANDBOX_CONFIG" not in os.environ
        assert (os.environ.get("CODEX_NETWORK_PROXY_ACTIVE") == "1") == configured
        _ = (Path(os.environ["TMPDIR"]) / "private").write_text("private storage")
        _ = (host / "CLI cache" / "persistent").write_text("CLI grant")
        for name, allowed in (("output café 雪", configured), ("neighbor", False)):
            try:
                _ = (host / name / "created").write_text("project grant")
            except OSError as error:
                assert not allowed and error.errno in (errno.EPERM, errno.EACCES, errno.EROFS)
            else:
                assert allowed, name
        # A proxy listener can reuse a host port in its network namespace.
        proxy_ports = {
            urlsplit(os.environ.get(key, "")).port
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
        }
        ports = json.loads(os.environ["MCP_CONSOLE_TEST_PORTS"])
        port = next(port for port in ports if port not in proxy_ports)
        try:
            socket.create_connection(("127.0.0.1", port), timeout=2)
        except OSError:
            pass
        else:
            raise AssertionError("direct network unexpectedly allowed")
        os.chdir(host / "CLI cache")
        print("captured grants, proxy selection, and restricted network verified")
        """)
    with TemporaryDirectory() as directory, host_tcp_ports() as ports:
        host = Path(directory).resolve()
        for name in ("output café 雪", "CLI cache", "neighbor"):
            (host / name).mkdir()
        config = host / ".agents/console/config.yaml"
        global_config = host / "console-home/config.yaml"
        config.parent.mkdir(parents=True)
        if configured:
            global_config.parent.mkdir()
            global_config.write_text(
                json.dumps(
                    {"sandbox": {"filesystem": {"read_write": ["./output café 雪"]}}}
                ),
                encoding="utf-8",
            )
            config.write_text(
                json.dumps(
                    {
                        "sandbox": {
                            "network": {"proxy": {"domains": {"allow": ["127.0.0.1"]}}},
                        }
                    }
                ),
                encoding="utf-8",
            )
        capture = host / "payloads.jsonl"
        resolver_capture = host / "resolver-payloads.jsonl"
        environment = {
            **os.environ,
            LOADER_VARIABLE: str(build_interposer(host, "runner_configuration")),
            "MCP_CONSOLE_TEST_RUNNER_CONFIGURATION": str(capture),
            "MCP_CONSOLE_TEST_RESOLVER_CONFIGURATION": str(resolver_capture),
            "MCP_CONSOLE_TEST_PROJECT": str(host),
            "MCP_CONSOLE_TEST_CONFIGURED": str(int(configured)),
            "MCP_CONSOLE_SANDBOX_SETTINGS": "invalid ambient settings",
            "MCP_CONSOLE_HOME": str(global_config.parent),
        }
        environment.pop("RETICULATE_PYTHON", None)
        environment["MCP_CONSOLE_TEST_PORTS"] = json.dumps(ports)
        expected = "captured grants, proxy selection, and restricted network verified\n"
        with McpClient(
            binary,
            ("serve", "--writable-root", "CLI cache", "-c", "cache=host"),
            environment,
            host,
            use_home_configuration=True,
        ) as client:
            client.initialize_and_list_tools()
            # Even the first worker uses the snapshot taken before MCP readiness.
            config.write_text("sandbox: {network: enabled}\n", encoding="utf-8")
            send_and_collect_runtime_python_resolution(client, python=exercise)
            assert last_tool_text(client).endswith(expected), last_tool_text(client)

            config.write_text("invalid: [", encoding="utf-8")
            if configured:
                global_config.write_text("invalid: [", encoding="utf-8")
            client.send(control="restart")
            send_and_collect_runtime_python_resolution(client, python=exercise)
            assert last_tool_text(client).endswith(expected), last_tool_text(client)

            config.unlink()
            if configured:
                global_config.unlink()
            client.send(python="os._exit(23)")
            send_and_collect_runtime_python_resolution(client, python=exercise)
            assert last_tool_text(client).endswith(expected), last_tool_text(client)

            client.send(
                control="restart",
                requirements={"python": ["py-yaml12"]},
            )
            assert (
                last_tool_text(client)
                == "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
            ), last_tool_text(client)
            send_and_collect_runtime_python_resolution(client, python=exercise)
            assert last_tool_text(client).endswith(expected), last_tool_text(client)
            client.send(python="import yaml12; print(yaml12.__name__)")
            assert last_tool_text(client) == "yaml12\n", last_tool_text(client)
            tool = client.transcript[2]["result"]["tools"][0]
            if configured:
                assert (
                    json.dumps(str(host / "output café 雪"), ensure_ascii=False)
                    in tool["description"]
                )
            transcript = client.finish()
            tool["description"] = tool["description"].replace(
                json.dumps(str(host), ensure_ascii=False)[1:-1], "<workspace>"
            )

        payloads = [json.loads(line) for line in capture.read_text().splitlines()]
        assert len(payloads) == 4, len(payloads)
        resolvers = [
            json.loads(line) for line in resolver_capture.read_text().splitlines()
        ]
        assert len(resolvers) == 1, resolvers
        resolver = resolvers[0]
        assert resolver["proxy"]["enabled"] is True
        assert resolver["proxy"]["domains"]["pypi.org"] == "allow"
        assert all(payload == payloads[0] for payload in payloads), payloads
        payload = payloads[0]
        assert payload["network"] == "restricted"
        assert (payload.get("proxy") is not None) == configured
        expected_roots = (
            ["output café 雪", "CLI cache"] if configured else ["CLI cache"]
        )
        assert payload["filesystem"]["entries"][1:] == [
            {"path": {"type": "path", "path": str(host / name)}, "access": "write"}
            for name in expected_roots
        ], payload
        assert payload["lifecycle"] == {
            "parent_pid": client.process.pid,
            "sigterm": "retire",
            "private_tmp": {"environment": ["TMPDIR"]},
        }, payload
        assert not (host / "neighbor/created").exists()
        return TranscriptWithCompanions(
            transcript,
            {
                "settings.yaml": [
                    {
                        "initially_configured": configured,
                        "validation_launches": 0,
                        "identical_worker_launches": len(payloads),
                        "writable_roots": expected_roots,
                        "network": payload["network"],
                        "proxy": payload.get("proxy"),
                    }
                ]
            },
        )


# The final restart prepares live requirements, which currently requires R.
@requires(SANDBOX, NATIVE_FIXTURES, R)
def test_retains_project_settings_after_edits(binary: Path) -> TranscriptWithCompanions:
    return _snapshot_survives_replacement(binary, configured=True)


@requires(SANDBOX, NATIVE_FIXTURES, R)
def test_retains_defaults_after_config_creation(
    binary: Path,
) -> TranscriptWithCompanions:
    return _snapshot_survives_replacement(binary, configured=False)


if __name__ == "__main__":
    run_this_suite(__file__)
