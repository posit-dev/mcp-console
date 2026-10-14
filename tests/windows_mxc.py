"""Windows sandbox contracts, with native execution selected by the MXC probe."""

import json
import os
import subprocess
import sys
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from support.normalization import code
from support.sandbox_configuration import NATIVE_PROXY
from support.windows_mxc import mxc_requirement, requires_mxc
from windows_sandbox import BINARY, workspace


@unittest.skipUnless(os.name == "nt", "native Windows MXC")
class WindowsMxc(unittest.TestCase):
    def test_startup_failures_withhold_code_at_every_public_entry_point(self):
        from support.windows_faults import run_with_native_fault

        native_available = mxc_requirement(BINARY).available

        def mcp_probe(process):
            captured = []

            def request(identifier, method, params):
                process.stdin.write(
                    (
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": identifier,
                                "method": method,
                                "params": params,
                            }
                        )
                        + "\n"
                    ).encode()
                )
                process.stdin.flush()
                while line := process.stdout.readline():
                    captured.append(line)
                    response = json.loads(line)
                    if response.get("id") == identifier:
                        return response
                raise AssertionError("MCP closed before the startup failure response")

            request(
                1,
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "clientInfo": {"name": "MXC acceptance", "version": "1"},
                    "capabilities": {},
                },
            )
            process.stdin.write(
                b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
            )
            request(2, "tools/list", {})
            response = request(
                3,
                "tools/call",
                {
                    "name": "send",
                    "arguments": {
                        "python": "print('USER CODE RAN')",
                        "timeout_ms": 30000,
                    },
                },
            )
            self.assertTrue(response["result"].get("isError"), response)
            # Do not close input until failure was observed; EOF cancellation
            # alone cannot establish that startup withheld the submitted cell.
            remainder, stderr = process.communicate()
            return b"".join(captured) + remainder, stderr

        with workspace() as root:
            environment = os.environ | {
                "MCP_CONSOLE_HOME": str(root / "home"),
                "POLICY": json.dumps({"extends": ":read-only"}),
            }
            for fault, diagnostic in (
                ("mxc_unavailable", "WINDOWS.html#native-sandbox"),
                ("legacy_accounts", "WINDOWS.html#legacy-installation-cleanup"),
            ):
                for route, arguments in (
                    (
                        "session",
                        [
                            "serve",
                            "--no-config",
                            "-c",
                            "languages=[python]",
                            "-c",
                            "python=" + json.dumps(sys.executable),
                        ],
                    ),
                    (
                        "sandbox",
                        [
                            "sandbox",
                            "--no-config",
                            "--",
                            sys.executable,
                            "-c",
                            "print('USER CODE RAN')",
                        ],
                    ),
                    (
                        "complete",
                        [
                            "sandbox",
                            "--config-env",
                            "POLICY",
                            "--",
                            sys.executable,
                            "-c",
                            "print('USER CODE RAN')",
                        ],
                    ),
                ):
                    with self.subTest(fault=fault, route=route):
                        result = run_with_native_fault(
                            [str(BINARY), *arguments],
                            # Without native MXC, exercise the real startup
                            # rejection. Capable hosts inject that failure.
                            fault=(
                                fault
                                if fault != "mxc_unavailable" or native_available
                                else None
                            ),
                            cwd=root,
                            env=environment,
                            interaction=mcp_probe if route == "session" else None,
                        )
                        output = (result.stdout + result.stderr).decode(
                            errors="replace"
                        )
                        self.assertIn(diagnostic, output)
                        self.assertNotIn("USER CODE RAN", output)
                        self.assertNotIn("use elevated mode", output)
                        if route != "session":
                            self.assertEqual(result.returncode, 1, output)
                            self.assertEqual(result.stdout, b"", output)
                self.assertFalse((root / "home/.sandbox-secrets").exists())

    @requires_mxc(BINARY)
    def test_proxy_launch(self):
        requests = []
        # fmt: python
        program = code("""
            import http.client
            import os
            from pathlib import Path
            import socket
            import struct
            import sys
            from urllib.parse import urlsplit

            port = int(sys.argv[1])
            assert os.environ["CODEX_NETWORK_PROXY_ACTIVE"] == "1"
            assert os.environ["CODEX_NETWORK_ALLOW_LOCAL_BINDING"] == "1"
            assert "MCP_CONSOLE_SANDBOX_CONFIG" not in os.environ
            proxy = urlsplit(os.environ["HTTP_PROXY"])
            for host, allowed in (("127.0.0.1", True), ("localhost", False)):
                connection = http.client.HTTPConnection(proxy.hostname, proxy.port, timeout=10)
                connection.request("GET", f"http://{host}:{port}/http")
                response = connection.getresponse()
                assert response.status == (200 if allowed else 403), response.status
                body = response.read()
                if allowed:
                    assert body == b"MXC_ORIGIN", body
                connection.close()

                connection = http.client.HTTPConnection(proxy.hostname, proxy.port, timeout=10)
                connection.set_tunnel(host, port)
                try:
                    connection.request("GET", "/connect")
                    response = connection.getresponse()
                    assert allowed and response.status == 200
                    assert response.read() == b"MXC_ORIGIN"
                except OSError as error:
                    assert not allowed and "403" in str(error), error
                finally:
                    connection.close()

            socks = urlsplit(os.environ["ALL_PROXY"])
            for host, allowed in (("127.0.0.1", True), ("localhost", False)):
                with socket.create_connection(
                    (socks.hostname, socks.port), timeout=10
                ) as connection:
                    with connection.makefile("rb") as response:
                        connection.sendall(b"\\x05\\x01\\x00")
                        assert response.read(2) == b"\\x05\\x00"
                        hostname = host.encode()
                        connection.sendall(
                            b"\\x05\\x01\\x00\\x03"
                            + bytes([len(hostname)])
                            + hostname
                            + struct.pack("!H", port)
                        )
                        reply = response.read(4)
                        assert len(reply) == 4 and reply[0] == 5, reply
                        assert (reply[1] == 0) is allowed, reply
                        if allowed:
                            size = {1: 4, 4: 16}.get(reply[3])
                            if size is None:
                                assert reply[3] == 3
                                size = response.read(1)[0]
                            assert len(response.read(size + 2)) == size + 2
                            connection.sendall(
                                b"GET /socks HTTP/1.1\\r\\nHost: 127.0.0.1\\r\\nConnection: close\\r\\n\\r\\n"
                            )
                            reply = response.read()
                            assert reply.startswith(b"HTTP/1.0 200") and reply.endswith(
                                b"MXC_ORIGIN"
                            ), reply
            (Path(os.environ["TMP"]) / "probe.txt").write_text("private write")
            print("MXC proxy allowed and denied HTTP, CONNECT, and SOCKS")
            """)

        class Origin(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", "10")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b"MXC_ORIGIN")

            def log_message(self, *_):
                pass

        with (
            workspace() as root,
            ThreadingHTTPServer(("127.0.0.1", 0), Origin) as origin,
        ):
            thread = Thread(target=origin.serve_forever)
            thread.start()
            try:
                result = subprocess.run(
                    [
                        str(BINARY),
                        "sandbox",
                        "--no-config",
                        "-c",
                        "sandbox.network={proxy: {domains: {allow: [127.0.0.1], deny: [localhost]}}}",
                        "--",
                        sys.executable,
                        "-c",
                        program,
                        str(origin.server_port),
                    ],
                    cwd=root,
                    env=dict(os.environ, MCP_CONSOLE_HOME=str(root / "home")),
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    result.stdout.strip(),
                    "MXC proxy allowed and denied HTTP, CONNECT, and SOCKS",
                )
                self.assertEqual(requests, ["/http", "/connect", "/socks"])
            finally:
                origin.shutdown()
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())

    def test_complete_policy_cannot_disable_mxc(self):
        with workspace() as root:
            for policy, expected in (
                (
                    {"filesystem": {"kind": "external-sandbox"}, "network": "enabled"},
                    "requires MXC",
                ),
                (
                    {"extends": ":read-only", "proxy": NATIVE_PROXY},
                    "allow_local_binding=false",
                ),
            ):
                result = subprocess.run(
                    [
                        BINARY,
                        "sandbox",
                        "--config-env",
                        "POLICY",
                        "--",
                        sys.executable,
                        "-c",
                        "print('USER CODE RAN')",
                    ],
                    env=os.environ
                    | {
                        "POLICY": json.dumps(policy),
                        "MCP_CONSOLE_HOME": str(root / "home"),
                    },
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 1, result)
                self.assertEqual(result.stdout, "", result)
                self.assertIn(expected, result.stderr)

    def test_setup_command_is_removed(self):
        result = subprocess.run(
            [BINARY, "sandbox-setup", "--status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("unrecognized subcommand", result.stderr)


if __name__ == "__main__":
    unittest.main()
