#!/usr/bin/env -S uv run --script

import json
import copy
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text, last_tool_text
from support.client import McpClient
from support.normalization import code
from support.python import isolated_python
from support.records import McpTranscript, Transcript, TranscriptWithCompanions
from support.requirements import SANDBOX, requires
from support.sandbox_configuration import host_tcp_ports
from support.suites import run_this_suite
from boundaries.client_server.python.test_without_r import (
    environment as without_r_environment,
)

STATE = "codex/sandbox-state"
LEGACY_STATE = "codex/sandbox-state-meta"


def sandbox_state(root: Path, network: str = "restricted") -> dict:
    return {
        "permissionProfile": {
            "type": "managed",
            "file_system": {
                "type": "restricted",
                "entries": [
                    {
                        "path": {"type": "special", "value": {"kind": "root"}},
                        "access": "read",
                    },
                    {
                        "path": {"type": "special", "value": {"kind": "project_roots"}},
                        "access": "write",
                    },
                    {
                        "path": {"type": "path", "path": str(root / "additional")},
                        "access": "write",
                    },
                    {
                        "path": {
                            "type": "path",
                            "path": (root / "additional/private").as_uri(),
                        },
                        "access": "deny",
                    },
                ],
            },
            "network": network,
        },
        "sandboxCwd": (root / "project café 雪").as_uri(),
        "codexLinuxSandboxExe": "/must-not-be-executed",
        "useLegacyLandlock": True,
        "useMxc": False,
        "unknownFutureField": True,
    }


def send_with_state(
    client: McpClient, state: object, *, key: str = STATE, **arguments: object
) -> dict:
    return client.request(
        "tools/call",
        name="send",
        arguments={"timeout_ms": 60000, **arguments},
        _meta={} if state is None else {key: state},
    )


def finish(client: McpClient, root: Path, *, warning: str = "") -> Transcript:
    transcript, stderr = client.finish_with_standard_error()
    assert stderr == warning, stderr
    if stderr:
        transcript.append({"stderr": stderr})

    # Normalize native paths before JSON escaping, including Windows separators.
    # Native/URI permissions are asserted before this incidental presentation.
    def normalize(value: object) -> object:
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, str):
            if value.startswith(str(root)):
                return "<workspace>" + value[len(str(root)) :].replace(os.sep, "/")
            return value.replace(root.as_uri(), "file:///<workspace>").replace(
                str(root), "<workspace>"
            )
        return value

    return json.loads(json.dumps(normalize(transcript), ensure_ascii=False))


@requires(SANDBOX)
def test_profile_fallback_selects_one_base_before_local_overrides(
    binary: Path,
) -> TranscriptWithCompanions:
    # Host temporary grants must not also cover the workspace being tested.
    with TemporaryDirectory(dir=Path.home()) as temporary, host_tcp_ports() as ports:
        root = Path(temporary).resolve()
        for name in ("output", "private", "temp"):
            (root / name).mkdir()
        (root / "private/secret").write_text("secret")
        python, _ = isolated_python(root)
        config = root / "shared.json"
        config.write_text(
            json.dumps(
                {
                    "sandbox": {
                        "profile": {"inherit_codex": {"fallback": "workspace"}},
                        "filesystem": {"read_write": ["output"], "deny": ["private"]},
                        "network": "restricted",
                        "tempdir": "read_write",
                    },
                    "cache": "host",
                    "python": {"existing": str(python)},
                }
            ),
            encoding="utf-8",
        )
        state = {
            "permissionProfile": {
                "type": "managed",
                "file_system": {"type": "restricted", "entries": []},
                "network": "enabled",
            }
        }
        transcripts = {}
        # Connections share one file; only the connection with usable client
        # metadata selects the client base instead of the workspace fallback.
        for name, metadata in (
            ("codex", state),
            ("fallback", None),
            ("unsupported", {"permissionProfile": {"type": "future"}}),
        ):
            use_workspace = name != "codex"
            with McpClient(
                binary,
                ("serve", "--config-file", str(config)),
                {
                    **os.environ,
                    "TMPDIR": str(root / "temp"),
                    "MCP_CONSOLE_TEST_FALLBACK": str(use_workspace),
                    "MCP_CONSOLE_TEST_PORTS": json.dumps(ports),
                },
                root,
            ) as client:
                client.initialize_and_list_tools()
                send_with_state(
                    client,
                    metadata,
                    # fmt: python
                    python=code("""
                        import errno
                        import json
                        import os
                        import socket
                        from pathlib import Path

                        fallback = os.environ["MCP_CONSOLE_TEST_FALLBACK"] == "True"
                        _ = Path("output/created").write_text("explicit grant")
                        try:
                            _ = Path("workspace-file").write_text("workspace grant")
                        except OSError as error:
                            assert not fallback and error.errno in (errno.EACCES, errno.EPERM, errno.EROFS)
                        else:
                            assert fallback, "fallback workspace grants leaked into the client base"
                        try:
                            _ = Path("private/secret").read_text()
                        except OSError as error:
                            assert error.errno in (errno.EACCES, errno.EPERM)
                        else:
                            raise AssertionError("explicit denial was lost")
                        assert Path(os.environ["TMPDIR"]) == Path.cwd() / "temp"
                        assert os.environ["MCP_CONSOLE_MANAGED_TEMPDIR"] == "0"
                        for port in json.loads(os.environ["MCP_CONSOLE_TEST_PORTS"]):
                            try:
                                connection = socket.create_connection(("127.0.0.1", port), timeout=2)
                            except OSError:
                                pass
                            else:
                                connection.close()
                                raise AssertionError("explicit network restriction was lost")
                        print("workspace fallback" if fallback else "client base")
                        """),
                )
                assert last_tool_text(client) == (
                    "workspace fallback\n" if use_workspace else "client base\n"
                ), last_tool_text(client)
                transcripts[name] = finish(
                    client,
                    root,
                    warning=(
                        "Warning: could not parse Codex sandbox settings; falling back to workspace\n"
                        if name == "unsupported"
                        else ""
                    ),
                )
        return TranscriptWithCompanions(
            transcripts.pop("codex"),
            {
                f"{name}.yaml": McpTranscript(transcript)
                for name, transcript in transcripts.items()
            },
        )


@requires(SANDBOX)
def test_missing_metadata_silently_uses_default_read_only(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        python, _ = isolated_python(root)
        with McpClient(
            binary,
            (
                "serve",
                "-c",
                "sandbox.profile={inherit_codex: {}}",
                "-c",
                "cache=host",
                "-c",
                f"python.existing={python}",
            ),
            os.environ.copy(),
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "read_only fallback\n",
                # fmt: python
                python=code("""
                    import errno
                    import os
                    from pathlib import Path

                    assert os.environ["MCP_CONSOLE_SANDBOX"] == "1"
                    try:
                        _ = Path("created").write_text("denied")
                    except OSError as error:
                        assert error.errno in (errno.EACCES, errno.EPERM, errno.EROFS)
                    else:
                        raise AssertionError("default fallback allowed workspace writes")
                    print("read_only fallback")
                    """),
            )
            assert not (root / "created").exists()
            return finish(client, root)


@requires(SANDBOX)
def test_malformed_metadata_warns_and_uses_default_read_only(
    binary: Path,
) -> TranscriptWithCompanions:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        python, _ = isolated_python(root)
        malformed = {
            "null": None,
            "empty": {},
            "unknown_profile": {"permissionProfile": {"type": "future"}},
            "invalid_filesystem": {
                "permissionProfile": {"type": "managed", "file_system": []}
            },
        }
        malformed_entries = [
            ("missing_access", {"path": {"type": "path", "path": str(root)}}),
            (
                "invalid_path",
                {"path": {"type": "path", "path": False}, "access": "deny"},
            ),
            ("unknown_path", {"path": {"type": "future"}, "access": "deny"}),
            (
                "invalid_missing_path_behavior",
                {
                    "path": {"type": "path", "path": str(root / "secret")},
                    "access": "deny",
                    "missing_path_behavior": False,
                },
            ),
            (
                "invalid_subpath",
                {
                    "path": {
                        "type": "special",
                        "value": {"kind": "project_roots", "subpath": False},
                    },
                    "access": "deny",
                },
            ),
        ]
        malformed_entries.extend(
            (
                name,
                {
                    "path": {
                        "type": "special",
                        "value": {"kind": kind, "subpath": subpath},
                    },
                    "access": "write",
                },
            )
            for name, kind, subpath in (
                ("absolute_project_subpath", "project_roots", str(root)),
                ("parent_project_subpath", "project_roots", ".."),
                (
                    "parent_cwd_subpath",
                    "current_working_directory",
                    "nested/../outside",
                ),
                ("nul_cwd_subpath", "current_working_directory", "bad\0path"),
            )
        )
        for name, entry in malformed_entries:
            malformed[name] = {
                "permissionProfile": {
                    "type": "managed",
                    "network": "enabled",
                    "file_system": {
                        "type": "restricted",
                        "entries": [
                            {
                                "path": {"type": "path", "path": str(root)},
                                "access": "write",
                            },
                            entry,
                        ],
                    },
                },
                "sandboxCwd": root.as_uri(),
            }
        transcripts = {}
        for name, state in malformed.items():
            with McpClient(
                binary,
                (
                    "serve",
                    "-c",
                    "sandbox.profile=inherit_codex",
                    "-c",
                    "cache=host",
                    "-c",
                    f"python.existing={python}",
                ),
                os.environ.copy(),
                root,
            ) as client:
                client.initialize_and_list_tools()
                result = client.request(
                    "tools/call",
                    name="send",
                    arguments={
                        "timeout_ms": 60000,
                        # fmt: python
                        "python": code("""
                            import errno
                            import os
                            from pathlib import Path

                            assert os.environ["MCP_CONSOLE_SANDBOX"] == "1"
                            try:
                                _ = Path("created").write_text("denied")
                            except OSError as error:
                                assert error.errno in (errno.EACCES, errno.EPERM, errno.EROFS)
                            else:
                                raise AssertionError("malformed metadata allowed workspace writes")
                            print("read_only fallback")
                            """),
                    },
                    _meta={STATE: state},
                )
                assert result["result"]["isError"] is False, result
                assert last_tool_text(client) == "read_only fallback\n", result
                transcripts[name] = finish(
                    client,
                    root,
                    warning="Warning: could not parse Codex sandbox settings; falling back to read_only\n",
                )
        return TranscriptWithCompanions(
            transcripts.pop("null"),
            {
                f"{name}.yaml": McpTranscript(transcript)
                for name, transcript in transcripts.items()
            },
        )


@requires(SANDBOX)
def test_first_received_send_selects_authority_before_cancelled_handler(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        python, _ = isolated_python(root)
        with McpClient(
            binary,
            (
                "serve",
                "-c",
                "sandbox.profile=inherit_codex",
                "-c",
                "cache=host",
                "-c",
                f"python.existing={python}",
            ),
            os.environ.copy(),
            root,
        ) as client:
            client.transcript.append(
                {
                    "scenario": "The first received send selects permissions even if its handler is cancelled.",
                    "fixture": "Write two conflicting sends and cancellation together so all three frames are ready before handler dispatch.",
                }
            )
            client.initialize_and_list_tools()
            first, second, _ = client.send_messages(
                [
                    {
                        "jsonrpc": "2.0",
                        "id": 1000,
                        "method": "tools/call",
                        "params": {
                            "name": "send",
                            "arguments": {
                                "python": "raise AssertionError('cancelled first handler ran')"
                            },
                            "_meta": {
                                STATE: {
                                    "permissionProfile": {
                                        "type": "managed",
                                        "file_system": {
                                            "type": "restricted",
                                            "entries": [],
                                        },
                                        "network": "restricted",
                                    }
                                }
                            },
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 1001,
                        "method": "tools/call",
                        "params": {
                            "name": "send",
                            "arguments": {
                                "timeout_ms": 60000,
                                # fmt: python
                                "python": code("""
                                    from pathlib import Path

                                    _ = Path("later-policy-ran").write_text("unexpected")
                                    """),
                            },
                            "_meta": {
                                STATE: {"permissionProfile": {"type": "disabled"}}
                            },
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/cancelled",
                        "params": {"requestId": 1000},
                    },
                ]
            )
            client.receive(second)
            assert "result" not in first and "error" not in first, first
            assert second["result"]["isError"] is True, second
            assert "reconnect" in second["result"]["content"][0]["text"], second
            assert not (root / "later-policy-ran").exists()
            client.expect(
                "first received permissions retained\n",
                # fmt: python
                python=code("""
                    import errno
                    import os
                    from pathlib import Path

                    assert os.environ["MCP_CONSOLE_SANDBOX"] == "1"
                    try:
                        _ = Path("created").write_text("denied")
                    except OSError as error:
                        assert error.errno in (errno.EACCES, errno.EPERM, errno.EROFS)
                    else:
                        raise AssertionError("later metadata replaced the first permissions")
                    print("first received permissions retained")
                    """),
            )
            assert not (root / "created").exists()
            transcript = finish(client, root)
            projection = next(root.glob(".agents/console/sessions/*/transcript.qmd"))
            assert "cancelled first handler ran" not in projection.read_text(), (
                "the fixture must cancel the first handler before execution admission"
            )
            return transcript


@requires(SANDBOX)
def test_inherits_filesystem_and_network_before_first_worker(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as temporary, host_tcp_ports() as ports:
        root = Path(temporary).resolve()
        for name in (
            "project café 雪/nested",
            "additional/private",
            "local",
            "neighbor",
        ):
            (root / name).mkdir(parents=True)
        (root / "additional/private/secret").write_text("secret")
        python, _ = isolated_python(root)
        environment = {
            **os.environ,
            "MCP_CONSOLE_TEST_ROOT": str(root),
            "MCP_CONSOLE_TEST_PORTS": json.dumps(ports),
        }
        with McpClient(
            binary,
            (
                "serve",
                "-c",
                "sandbox.profile=inherit_codex",
                "-c",
                "sandbox.filesystem.read_write=[local]",
                "-c",
                "cache=host",
                "-c",
                f"python.existing={python}",
            ),
            environment,
            root,
        ) as client:
            client.initialize_and_list_tools()
            capabilities = client.transcript[0]["result"]["capabilities"]
            assert capabilities["experimental"][STATE] == {}, capabilities
            state = sandbox_state(root, "enabled")
            state["permissionProfile"]["file_system"]["entries"].append(
                {
                    "path": {
                        "type": "special",
                        "value": {
                            "kind": "current_working_directory",
                            "subpath": "nested",
                        },
                    },
                    "access": "write",
                }
            )
            entry = send_with_state(
                client,
                state,
                # fmt: python
                python=code("""
                    import errno
                    import json
                    import os
                    import socket
                    from pathlib import Path

                    root = Path(os.environ["MCP_CONSOLE_TEST_ROOT"])
                    assert Path.cwd() == root
                    assert os.environ["MCP_CONSOLE_SANDBOX"] == "1"
                    for name in ("project café 雪", "project café 雪/nested", "additional", "local"):
                        _ = (root / name / "created").write_text("allowed")
                    for path, operation in (
                        (root / "neighbor/created", lambda path: path.write_text("denied")),
                        (root / "additional/private/secret", lambda path: path.read_text()),
                    ):
                        try:
                            operation(path)
                        except OSError as error:
                            assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS)
                        else:
                            raise AssertionError(path)
                    with socket.create_connection(
                        ("127.0.0.1", json.loads(os.environ["MCP_CONSOLE_TEST_PORTS"])[0]), timeout=2
                    ):
                        pass
                    retained = 41
                    print("inherited grants and network; local grants retained")
                    """),
            )
            assert "error" not in entry, entry
            assert (
                last_tool_text(client)
                == "inherited grants and network; local grants retained\n"
            ), last_tool_text(client)
            # The legacy spelling carries the identical authority.
            reordered = copy.deepcopy(state)
            entries = reordered["permissionProfile"]["file_system"]["entries"]
            entries.reverse()
            entries.append(entries[-1])
            client.request(
                "tools/call",
                name="send",
                arguments={"python": "retained + 1"},
                _meta={LEGACY_STATE: reordered},
            )
            assert last_tool_text(client) == "42\n"
            changed = sandbox_state(root)
            rejected = send_with_state(client, changed, python="retained = -1")
            assert rejected["result"]["isError"] is True, rejected
            assert "reconnect" in last_result_text(client), rejected
            malformed = send_with_state(
                client,
                {"permissionProfile": {"type": "future"}},
                python="retained = -2",
            )
            assert malformed["result"]["isError"] is True, malformed
            assert "reconnect" in last_result_text(client), malformed
            client.send(python="retained")
            assert last_tool_text(client) == "41\n"
            send_with_state(
                client, state, control="restart", python="'retained' in globals()"
            )
            assert (
                last_tool_text(client)
                == "[worker stopped: in-memory state lost]\n[starting new worker]\nFalse\n[done]"
            ), last_tool_text(client)
            assert (root / "additional/created").read_text() == "allowed"
            assert not (root / "neighbor/created").exists()
            transcript = finish(
                client,
                root,
                warning="Warning: could not parse Codex sandbox settings; reconnect to apply the read_only fallback\n",
            )
            projection = next(root.glob(".agents/console/sessions/*/transcript.qmd"))
            assert "retained = -1" in projection.read_text(), (
                "rejected call was not recorded"
            )
            return transcript


@requires(SANDBOX)
def test_default_ignores_client_permissions(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary, host_tcp_ports() as ports:
        root = Path(temporary).resolve()
        (root / "local").mkdir()
        (root / "additional").mkdir()
        python, _ = isolated_python(root)
        with McpClient(
            binary,
            (
                "serve",
                "--writable-root",
                "local",
                "-c",
                "cache=host",
                "-c",
                f"python.existing={python}",
            ),
            {**os.environ, "MCP_CONSOLE_TEST_PORTS": json.dumps(ports)},
            root,
        ) as client:
            client.initialize_and_list_tools()
            assert "experimental" not in client.transcript[0]["result"]["capabilities"]
            send_with_state(
                client,
                {"permissionProfile": {"type": "disabled"}},
                # fmt: python
                python=code("""
                    import errno
                    import json
                    import os
                    import socket
                    from pathlib import Path

                    assert os.environ["MCP_CONSOLE_SANDBOX"] == "1"
                    _ = Path("local/created").write_text("allowed")
                    try:
                        _ = Path("additional/created").write_text("denied")
                    except OSError as error:
                        assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS)
                    else:
                        raise AssertionError("client disabled the sandbox without opt-in")
                    for port in json.loads(os.environ["MCP_CONSOLE_TEST_PORTS"]):
                        try:
                            connection = socket.create_connection(("127.0.0.1", port), timeout=2)
                        except OSError:
                            pass
                        else:
                            connection.close()
                            raise AssertionError("client enabled direct networking without opt-in")
                    print("local sandbox unchanged")
                    """),
            )
            assert last_tool_text(client) == "local sandbox unchanged\n", (
                last_tool_text(client)
            )
            assert not (root / "additional/created").exists()
            return finish(client, root)


def test_full_access_uses_host_permissions_and_caches(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary, host_tcp_ports() as ports:
        root = Path(temporary).resolve()
        (root / "host-cache").mkdir()
        python, _ = isolated_python(root)
        exported = subprocess.run(
            [
                binary,
                "config",
                "resolve",
                "--no-config",
                "--full",
                "--format",
                "json",
                "-c",
                "sandbox.profile=inherit_codex",
                "-c",
                "sandbox.filesystem.deny=[outside]",
                "-c",
                f"python.existing={python}",
            ],
            cwd=root,
            env={**os.environ, "MCP_CONSOLE_HOME": str(root / "console-home")},
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert exported.returncode == 0 and exported.stderr == "", exported
        config = root / "full.json"
        config.write_text(exported.stdout, encoding="utf-8")
        with McpClient(
            binary,
            ("serve", "--config-file", str(config)),
            {
                **without_r_environment(root),
                "XDG_CACHE_HOME": str(root / "host-cache"),
                "MCP_CONSOLE_TEST_PORT": str(ports[0]),
            },
            root,
        ) as client:
            client.initialize_and_list_tools()
            send_with_state(
                client,
                {"permissionProfile": {"type": "disabled"}},
                key=LEGACY_STATE,
                # fmt: python
                python=code("""
                    import os
                    import socket
                    from pathlib import Path

                    assert "MCP_CONSOLE_SANDBOX" not in os.environ
                    assert Path(os.environ["XDG_CACHE_HOME"]) == Path.cwd() / "host-cache"
                    _ = Path("outside").write_text("full access")
                    with socket.create_connection(
                        ("127.0.0.1", int(os.environ["MCP_CONSOLE_TEST_PORT"])), timeout=2
                    ):
                        pass
                    print("full access with host caches")
                    """),
            )
            assert last_tool_text(client) == "full access with host caches\n", (
                last_tool_text(client)
            )
            assert (root / "outside").read_text() == "full access"
            return finish(client, root)


@requires(SANDBOX)
def test_missing_and_unsupported_metadata_retain_local_policy(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        python, _ = isolated_python(root)
        with McpClient(
            binary,
            (
                "serve",
                "-c",
                "sandbox.profile={inherit_codex: {fallback: workspace}}",
                "-c",
                "cache=host",
                "-c",
                f"python.existing={python}",
            ),
            os.environ.copy(),
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "local sandbox\n",
                python="import os; assert os.environ['MCP_CONSOLE_SANDBOX'] == '1'; print('local sandbox')",
            )
            for state in (
                None,
                {},
                {"permissionProfile": {"type": "future"}},
                {"permissionProfile": {"type": "managed", "file_system": []}},
            ):
                send_with_state(
                    client,
                    state,
                    python="assert os.environ['MCP_CONSOLE_SANDBOX'] == '1'; print('local sandbox')",
                )
                assert last_tool_text(client) == "local sandbox\n", last_tool_text(
                    client
                )
            rejected = send_with_state(
                client,
                {
                    "permissionProfile": {
                        "type": "managed",
                        "file_system": {"type": "restricted", "entries": []},
                        "network": "restricted",
                    }
                },
                python="raise AssertionError('changed permissions executed code')",
            )
            assert rejected["result"][
                "isError"
            ] is True and "reconnect" in last_result_text(client), rejected
            client.expect("local sandbox\n", python="print('local sandbox')")
            return finish(
                client,
                root,
                warning="Warning: could not parse Codex sandbox settings; falling back to workspace\n"
                * 3,
            )


def test_discovery_and_eof_do_not_prepare_runtimes_when_inheriting(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        python, site = isolated_python(root)
        marker = root / "inspected"
        (site / "sitecustomize.py").write_text(
            f"from pathlib import Path\n_ = Path({str(marker)!r}).write_text('inspected')\n"
        )
        with McpClient(
            binary,
            (
                "serve",
                "-c",
                "sandbox.profile=inherit_codex",
                "-c",
                "sandbox.filesystem.read_write=[.]",
                "-c",
                "cache=host",
                "-c",
                f"python.existing={python}",
            ),
            os.environ.copy(),
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.request("ping")
            transcript = finish(client, root)
            assert not marker.exists(), "runtime inspection preceded first send"
            return transcript


@requires(SANDBOX)
def test_unrestricted_filesystem_retains_network_enforcement(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as temporary, host_tcp_ports() as ports:
        root = Path(temporary).resolve()
        (root / "secret").write_text("private")
        python, _ = isolated_python(root)
        with McpClient(
            binary,
            (
                "serve",
                "-c",
                "sandbox.profile=inherit_codex",
                "-c",
                "sandbox.filesystem.deny=[secret]",
                "-c",
                "cache=host",
                "-c",
                f"python.existing={python}",
            ),
            {**os.environ, "MCP_CONSOLE_TEST_PORTS": json.dumps(ports)},
            root,
        ) as client:
            client.initialize_and_list_tools()
            state = {
                "permissionProfile": {
                    "type": "managed",
                    "file_system": {"type": "unrestricted"},
                    "network": "restricted",
                }
            }
            send_with_state(
                client,
                state,
                # fmt: python
                python=code("""
                    import errno
                    import json
                    import os
                    import socket
                    from pathlib import Path

                    assert os.environ["MCP_CONSOLE_SANDBOX"] == "1"
                    _ = Path("created").write_text("unrestricted filesystem")
                    try:
                        _ = Path("secret").read_text()
                    except OSError as error:
                        assert error.errno in (errno.EACCES, errno.EPERM)
                    else:
                        raise AssertionError("explicit denial was lost on unrestricted base")
                    for port in json.loads(os.environ["MCP_CONSOLE_TEST_PORTS"]):
                        try:
                            connection = socket.create_connection(("127.0.0.1", port), timeout=2)
                        except OSError:
                            pass
                        else:
                            connection.close()
                            raise AssertionError("inherited network restriction was lost")
                    print("unrestricted filesystem with restricted networking")
                    """),
            )
            assert (
                last_tool_text(client)
                == "unrestricted filesystem with restricted networking\n"
            ), last_tool_text(client)
            # External enforcement maps to the same supported permissions.
            send_with_state(
                client,
                {"permissionProfile": {"type": "external", "network": "restricted"}},
                python="Path('created').read_text()",
            )
            assert last_tool_text(client) == "'unrestricted filesystem'\n", (
                last_tool_text(client)
            )
            assert (root / "created").read_text() == "unrestricted filesystem"
            return finish(client, root)


@requires(SANDBOX)
def test_inheritance_preserves_explicit_console_proxy(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        python, _ = isolated_python(root)
        with McpClient(
            binary,
            (
                "serve",
                "-c",
                "sandbox.profile=inherit_codex",
                "-c",
                "sandbox.network={proxy: {mode: limited, allow_upstream_proxy: true}}",
                "-c",
                "cache=host",
                "-c",
                f"python.existing={python}",
            ),
            os.environ.copy(),
            root,
        ) as client:
            client.initialize_and_list_tools()
            send_with_state(
                client,
                {
                    "permissionProfile": {
                        "type": "managed",
                        "file_system": {"type": "restricted", "entries": []},
                        "network": "enabled",
                    }
                },
                # fmt: python
                python=code("""
                    import http.client
                    import os
                    from urllib.parse import urlsplit

                    assert os.environ["CODEX_NETWORK_PROXY_ACTIVE"] == "1"
                    proxy = urlsplit(os.environ["HTTP_PROXY"])
                    connection = http.client.HTTPConnection(proxy.hostname, proxy.port, timeout=5)
                    connection.request("GET", "http://blocked.invalid/")
                    response = connection.getresponse()
                    assert response.status == 403, response.status
                    _ = response.read()
                    connection.close()
                    assert os.environ["ALL_PROXY"].startswith("http:")
                    print("configured limited proxy and destination restrictions retained")
                    """),
            )
            assert (
                last_tool_text(client)
                == "configured limited proxy and destination restrictions retained\n"
            ), last_tool_text(client)
            return finish(client, root)


if __name__ == "__main__":
    run_this_suite(__file__)
