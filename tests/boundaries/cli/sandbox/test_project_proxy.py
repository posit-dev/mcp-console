#!/usr/bin/env -S uv run --script

import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.native import LOADER_VARIABLE, build_interposer
from support.normalization import code
from support.records import Transcript
from support.requirements import MACOS_SANDBOX, NATIVE_FIXTURES, SANDBOX, requires
from support.sandbox_configuration import host_tcp_ports
from support.suites import run_this_suite


class Origin(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "7")
        self.end_headers()
        self.wfile.write(b"allowed")

    do_POST = do_GET

    def log_message(self, *_):
        pass


def _enforces_native_proxy_settings(binary: Path) -> Transcript:
    # fmt: python
    script = code(r"""
        import errno
        import http.client
        import json
        import os
        import socket
        import sys
        from urllib.parse import urlsplit

        assert "MCP_CONSOLE_SANDBOX_SETTINGS" not in os.environ
        assert "MCP_CONSOLE_SANDBOX_CONFIG" not in os.environ
        assert os.environ["CODEX_NETWORK_PROXY_ACTIVE"] == "1"
        proxy = urlsplit(os.environ["HTTP_PROXY"])
        connection = http.client.HTTPConnection(proxy.hostname, proxy.port, timeout=5)
        connection.request(sys.argv[2], sys.argv[1])
        response = connection.getresponse()
        assert response.status == int(sys.argv[3]), (response.status, response.read())
        if response.status == 200:
            assert response.read() == b"allowed"
        connection.close()
        assert os.environ["ALL_PROXY"].startswith(sys.argv[4])
        if os.environ["CODEX_NETWORK_ALLOW_LOCAL_BINDING"] == "0":
            # A proxy listener can reuse a host port in its network namespace.
            proxy_ports = {
                urlsplit(os.environ[key]).port
                for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
            }
            port = next(port for port in json.loads(sys.argv[5]) if port not in proxy_ports)
            try:
                socket.create_connection(("127.0.0.1", port), timeout=2)
            except OSError as error:
                # Linux isolates the host listener in a separate network namespace.
                assert error.errno in (errno.EPERM, errno.EACCES, errno.ECONNREFUSED)
                print("direct network denied")
            else:
                raise AssertionError("direct network unexpectedly allowed")
        print(f"native proxy returned {response.status}")
        """)
    cases = (
        ("omitted domains", {"proxy": {}}, "GET", 403),
        ("empty domains", {"proxy": {"domains": {}}}, "GET", 403),
        (
            "allow literal",
            {"proxy": {"domains": {"allow": ["127.0.0.1"]}}},
            "POST",
            200,
        ),
        (
            "deny overrides allow",
            {"proxy": {"domains": {"allow": ["127.0.0.1"], "deny": ["127.0.0.1"]}}},
            "GET",
            403,
        ),
        (
            "native deny pattern overrides exact allow",
            {"proxy": {"domains": {"allow": ["127.0.0.1"], "deny": ["127.*"]}}},
            "GET",
            403,
        ),
        (
            "limited GET",
            {"proxy": {"mode": "limited", "domains": {"allow": ["127.0.0.1"]}}},
            "GET",
            200,
        ),
        (
            "limited POST",
            {"proxy": {"mode": "limited", "domains": {"allow": ["127.0.0.1"]}}},
            "POST",
            403,
        ),
        (
            "limited CONNECT",
            {"proxy": {"mode": "limited", "domains": {"allow": ["127.0.0.1"]}}},
            "CONNECT",
            403,
        ),
        (
            "HTTP transport",
            {"proxy": {"socks5": "disabled", "domains": {"allow": ["127.0.0.1"]}}},
            "GET",
            200,
        ),
    )
    transcript = []
    with (
        TemporaryDirectory() as directory,
        ThreadingHTTPServer(("127.0.0.1", 0), Origin) as origin,
        host_tcp_ports() as ports,
    ):
        thread = threading.Thread(target=origin.serve_forever)
        thread.start()
        try:
            host = Path(directory).resolve()
            config = host / ".agents/console/config.yaml"
            config.parent.mkdir(parents=True)
            capture = host / "payloads.jsonl"
            environment = {
                key: value
                for key, value in os.environ.items()
                if "proxy" not in key.lower()
            }
            environment.update(
                {
                    LOADER_VARIABLE: str(
                        build_interposer(host, "runner_configuration")
                    ),
                    "MCP_CONSOLE_TEST_RUNNER_CONFIGURATION": str(capture),
                    "MCP_CONSOLE_SANDBOX_SETTINGS": "unselected ambient value",
                }
            )
            for name, options, method, status in cases:
                config.write_text(
                    json.dumps({"sandbox": {"network": options}}),
                    encoding="utf-8",
                )
                result = subprocess.run(
                    [
                        binary,
                        "sandbox",
                        "--",
                        sys.executable,
                        "-c",
                        script,
                        f"127.0.0.1:{origin.server_port}"
                        if method == "CONNECT"
                        else f"http://127.0.0.1:{origin.server_port}/",
                        method,
                        str(status),
                        "http:"
                        if options["proxy"].get("socks5") == "disabled"
                        or options["proxy"].get("mode") == "limited"
                        else "socks5h:",
                        json.dumps(ports),
                    ],
                    cwd=host,
                    env=environment,
                    capture_output=True,
                    text=True,
                )
                assert result.returncode == 0 and result.stderr == "", (name, result)
                payload = json.loads(capture.read_text().splitlines()[-1])
                assert payload["network"] == "restricted", payload
                transcript.append(
                    {
                        "case": name,
                        "proxy": payload["proxy"],
                        "network": payload["network"],
                        "stdout": result.stdout,
                    }
                )
        finally:
            origin.shutdown()
            thread.join()
    return transcript


@requires(SANDBOX, NATIVE_FIXTURES)
def test_normalizes_and_enforces_native_proxy_settings(binary: Path) -> Transcript:
    return _enforces_native_proxy_settings(binary)


@requires(SANDBOX)
def test_project_network_enabled_allows_direct_connection(binary: Path) -> Transcript:
    # fmt: python
    script = code("""
        import errno
        import socket
        import sys
        from pathlib import Path

        socket.create_connection(("127.0.0.1", int(sys.argv[1])))
        try:
            Path("still-read-only").write_text("denied")
        except OSError as error:
            assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS), error
        else:
            raise AssertionError("network selection granted filesystem writes")
        print("network allowed")
        """)
    with TemporaryDirectory() as directory, socket.socket() as listener:
        host = Path(directory)
        config = host / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text("sandbox:\n  network: enabled\n", encoding="utf-8")
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        result = subprocess.run(
            [
                binary,
                "sandbox",
                "--",
                sys.executable,
                "-c",
                script,
                str(listener.getsockname()[1]),
            ],
            cwd=host,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0 and result.stderr == "", result
        return [{"stdout": result.stdout}]


@requires(SANDBOX)
def test_socket_allow_all_is_independent_of_limited_methods(binary: Path) -> Transcript:
    transcript = []
    with TemporaryDirectory() as temporary, socket.socket(socket.AF_UNIX) as listener:
        root = Path(temporary).resolve()
        path = str(root / "service.sock")
        listener.bind(path)
        listener.listen()
        listener.settimeout(5)
        config = root / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        for selector in ([], "dangerously_allow_all"):
            config.write_text(
                json.dumps(
                    {
                        "sandbox": {
                            "network": {
                                "proxy": {"mode": "limited"},
                                "sockets": {"unix_sockets": selector},
                            }
                        }
                    }
                )
            )
            result = subprocess.run(
                [
                    binary,
                    "sandbox",
                    "--",
                    sys.executable,
                    "-c",
                    # fmt: python
                    code("""
                        import errno
                        import socket
                        import sys

                        allowed = sys.argv[2] == "allowed"
                        try:
                            with socket.socket(socket.AF_UNIX) as peer:
                                peer.connect(sys.argv[1])
                                peer.sendall(b"arbitrary socket payload")
                        except OSError as error:
                            assert not allowed and error.errno in (errno.EPERM, errno.EACCES), error
                            print("Unix connection denied")
                        else:
                            assert allowed
                            print("Unix payload sent")
                        """),
                    path,
                    "allowed" if selector else "denied",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert result.returncode == 0 and result.stderr == "", result
            if selector:
                with listener.accept()[0] as peer:
                    assert peer.recv(256) == b"arbitrary socket payload"
            else:
                listener.setblocking(False)
                try:
                    listener.accept()
                except BlockingIOError:
                    pass
                else:
                    raise AssertionError("denied socket connection reached listener")
                listener.settimeout(5)
            transcript.append({"unix_sockets": selector, "stdout": result.stdout})
    return transcript


@requires(MACOS_SANDBOX)
def test_limited_local_and_socket_grants_allow_direct_connections(
    binary: Path,
) -> Transcript:
    with (
        TemporaryDirectory() as temporary,
        socket.socket() as tcp,
        socket.socket(socket.AF_UNIX) as unix,
    ):
        root = Path(temporary).resolve()
        tcp.bind(("127.0.0.1", 0))
        tcp.listen()
        path = str(root / "service.sock")
        unix.bind(path)
        unix.listen()
        config = root / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps(
                {
                    "sandbox": {
                        "network": {
                            "proxy": {"mode": "limited"},
                            "allow_local_binding": True,
                            "sockets": {"unix_sockets": [path]},
                        }
                    }
                }
            )
        )
        result = subprocess.run(
            [
                binary,
                "sandbox",
                "--",
                sys.executable,
                "-c",
                # fmt: python
                code("""
                    import socket
                    import sys

                    with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=5):
                        pass
                    with socket.socket(socket.AF_UNIX) as peer:
                        peer.connect(sys.argv[2])
                    print("direct local and Unix connections allowed independently of limited proxy")
                    """),
                str(tcp.getsockname()[1]),
                path,
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0 and result.stderr == "", result
        return [{"stdout": result.stdout}]


@requires(MACOS_SANDBOX)
def test_pinned_native_udp_reply_returns_to_origin(binary: Path) -> Transcript:
    # Exercise the unchanged complete-native-policy transport to record why
    # the public tcp_udp request is unsupported on the current pin.
    from support.sandbox_configuration import NATIVE_PROXY

    with (
        TemporaryDirectory() as temporary,
        socket.socket(type=socket.SOCK_DGRAM) as origin,
    ):
        root = Path(temporary).resolve()
        origin.bind(("127.0.0.1", 0))
        origin.settimeout(5)
        native = {
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
            "proxy": {
                **NATIVE_PROXY,
                "enableSocks5Udp": True,
                "allowLocalBinding": True,
                "domains": {"127.0.0.1": "allow"},
            },
        }
        # fmt: python
        script = code(r"""
            import os
            import socket
            import struct
            import sys
            from urllib.parse import urlsplit

            proxy = urlsplit(os.environ["ALL_PROXY"])
            with socket.socket(type=socket.SOCK_DGRAM) as peer:
                peer.bind(("127.0.0.1", 0))
                with socket.create_connection((proxy.hostname, proxy.port), timeout=5) as control:
                    stream = control.makefile("rb")
                    control.sendall(b"\x05\x01\x00")
                    assert stream.read(2) == b"\x05\x00"
                    control.sendall(
                        b"\x05\x03\x00\x01"
                        + socket.inet_aton("127.0.0.1")
                        + struct.pack("!H", peer.getsockname()[1])
                    )
                    head = stream.read(4)
                    assert head[:3] == b"\x05\x00\x00" and head[3] == 1, head
                    relay = socket.inet_ntoa(stream.read(4))
                    port = struct.unpack("!H", stream.read(2))[0]
                    assert relay in ("0.0.0.0", proxy.hostname), relay
                    packet = (
                        b"\x00\x00\x00\x01"
                        + socket.inet_aton("127.0.0.1")
                        + struct.pack("!H", int(sys.argv[1]))
                        + b"request"
                    )
                    peer.sendto(packet, (proxy.hostname, port))
                    assert sys.stdin.readline() == "reply returned to origin\n"
                    peer.setblocking(False)
                    try:
                        peer.recvfrom(1024)
                    except BlockingIOError:
                        print("native UDP reply returned to origin, not client")
                    else:
                        raise AssertionError("unexpected client reply")
                    stream.close()
            """)
        with subprocess.Popen(
            [
                binary,
                "sandbox",
                "--runner-config-env",
                "NATIVE_UDP_POLICY",
                "--",
                sys.executable,
                "-c",
                script,
                str(origin.getsockname()[1]),
            ],
            cwd=root,
            env={**os.environ, "NATIVE_UDP_POLICY": json.dumps(native)},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as process:
            first, sender = origin.recvfrom(1024)
            assert first == b"request", first
            origin.sendto(b"reply", sender)
            second, second_sender = origin.recvfrom(1024)
            assert second == b"reply" and second_sender == sender, (
                second,
                second_sender,
            )
            stdout, stderr = process.communicate(
                "reply returned to origin\n", timeout=10
            )
            assert process.returncode == 0 and stderr == "", (stdout, stderr)
        return [
            {
                "native_proxy": native["proxy"],
                "origin_received": [first.decode(), second.decode()],
                "stdout": stdout,
            }
        ]


if __name__ == "__main__":
    run_this_suite(__file__)
