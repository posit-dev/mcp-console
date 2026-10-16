"""Public configuration compiled at the real runner launch boundary."""

import json
import os
import subprocess
import sys
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.public_configuration import CONFIGURATION_EXAMPLES, INVALID_CONFIGURATIONS
from support.native import LOADER_VARIABLE, build_interposer
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.records import Transcript, TranscriptWithCompanions
from support.snapshots import platform_snapshots
from support.requirements import (
    MACOS_SANDBOX,
    NATIVE_FIXTURES,
    POSIX,
    SANDBOX,
    command,
    requires,
)
from support.suites import run_this_suite
from support.resolvers import preseeded_duckdb_python, probing_uv


def configure(root: Path, document: dict) -> None:
    path = root / ".agents/console/config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document))


def observe(binary: Path, root: Path, document: dict, overrides=()) -> dict:
    configure(root, document)
    temporary = root / "host-temp"
    temporary.mkdir(exist_ok=True)
    capture = root / "policies.jsonl"
    capture.unlink(missing_ok=True)
    result = subprocess.run(
        [binary, "sandbox", *overrides, "--", sys.executable, "-c", "pass"],
        cwd=root,
        env={
            **os.environ,
            **{name: str(temporary) for name in ("TMPDIR", "TEMP", "TMP")},
            LOADER_VARIABLE: str(build_interposer(root, "runner_configuration")),
            "MCP_CONSOLE_TEST_RUNNER_CONFIGURATION": str(capture),
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0 and result.stderr == "", result
    payload = json.loads(capture.read_text().splitlines()[-1])
    return payload


@requires(SANDBOX, NATIVE_FIXTURES)
def test_user_to_native_policies(binary: Path) -> TranscriptWithCompanions:
    examples = CONFIGURATION_EXAMPLES
    companions = {}
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        (root / "data").mkdir()
        (root / "secrets").mkdir()
        for name, document in examples.items():
            native = observe(binary, root, document)
            assert native["filesystem"]["kind"] == "restricted", native
            profile = document["sandbox"].get("profile")
            workspace = profile == "workspace" or (
                isinstance(profile, dict)
                and profile["inherit_codex"].get("fallback", "read_only") == "workspace"
            )
            if workspace:
                assert native["extends"] == ":workspace", native
                assert native["workspace_options"] == {
                    "exclude_tmpdir_env_var": True,
                    "exclude_slash_tmp": True,
                }, native
            else:
                assert "extends" not in native, native
            managed = document["sandbox"].get("tempdir", "managed") == "managed"
            assert ("private_tmp" in native["lifecycle"]) is managed, native
            assert "tempdir" not in native, native
            if isinstance(document["sandbox"].get("network"), dict):
                proxy = native["proxy"]
                assert native["network"] == "restricted" and proxy["enabled"]
                limited = name == "limited"
                assert proxy["enableSocks5"] is (not limited)
                assert proxy["enableSocks5Udp"] is False
                assert proxy["allowUpstreamProxy"] is False
                assert proxy["dangerouslyAllowAllUnixSockets"] is False
                assert proxy["unixSockets"] == {}
                assert proxy["allowLocalBinding"] is False
            else:
                assert "proxy" not in native
            # Keep every transport field, normalizing only incidental paths.
            native = json.loads(json.dumps(native).replace(str(root), "<workspace>"))
            if sys.platform == "darwin":
                companions[f"{name}.darwin.yaml"] = [document, native]
    return TranscriptWithCompanions([{"native_launches": list(examples)}], companions)


@requires(SANDBOX, NATIVE_FIXTURES)
def test_layered_variants(binary: Path) -> Transcript:
    transcript = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for first, last in (
            ("restricted", {"proxy": {}}),
            ({"proxy": {}}, "enabled"),
            ("enabled", "restricted"),
        ):
            document = {"sandbox": {"network": first}}
            native = observe(
                binary, root, document, ("-c", "sandbox.network=" + json.dumps(last))
            )
            assert ("proxy" in native) is isinstance(last, dict), native
            transcript.append(
                {
                    "initial": first,
                    "override": last,
                    "network": native["network"],
                    "proxy": native.get("proxy"),
                }
            )
        for selector, expected in (
            ("disabled", (False, False)),
            ("tcp", (True, False)),
        ):
            native = observe(
                binary, root, {"sandbox": {"network": {"proxy": {"socks5": selector}}}}
            )
            proxy = native["proxy"]
            assert (proxy["enableSocks5"], proxy["enableSocks5Udp"]) == expected
            transcript.append({"socks5": selector, "native": proxy})
    return transcript


def test_rejects_ineffective_and_removed_settings(binary: Path) -> Transcript:
    transcript = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for path, value in INVALID_CONFIGURATIONS:
            result = subprocess.run(
                [
                    binary,
                    "sandbox",
                    *(
                        ("-c", "sandbox.network.proxy={}")
                        if path.startswith("sandbox.network.sockets")
                        else ()
                    ),
                    "-c",
                    path + "=" + json.dumps(value),
                    "--",
                    "unused-workload",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 1 and not result.stdout, (path, result)
            assert "configuration" in result.stderr, (path, result)
            assert path.split(".")[0] in result.stderr, (path, result)
            assert "secret sentinel" not in result.stderr
            assert "654987123" not in result.stderr
            transcript.append({"path": path, "stderr": result.stderr})
        for selector in ("disabled", "tcp", "tcp_udp"):
            for scope in ("sandbox", "resolver.sandbox"):
                result = subprocess.run(
                    [
                        binary,
                        "sandbox",
                        "-c",
                        f"{scope}.network.proxy={{mode: full, socks5: {selector}}}",
                        "-c",
                        f"{scope}.network.proxy.mode=limited",
                        "--",
                        "unused-workload",
                    ],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                assert (
                    result.returncode == 1
                    and "socks5" in result.stderr
                    and "limited" in result.stderr
                ), result
                transcript.append(
                    {"scope": scope, "selector": selector, "stderr": result.stderr}
                )
    return transcript


@platform_snapshots(
    "win32",
    reason="Windows rejects explicit resolver permissions before nested profile validation; Unix rejects session profiles in resolver scope.",
)
def test_rejects_session_profiles_in_resolver(binary: Path) -> Transcript:
    transcript = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for profile in ("inherit_codex", {"inherit_codex": {"fallback": "workspace"}}):
            result = subprocess.run(
                [
                    binary,
                    "config",
                    "resolve",
                    "--no-config",
                    "-c",
                    "resolver.sandbox.profile=" + json.dumps(profile),
                ],
                cwd=root,
                env=os.environ | {"MCP_CONSOLE_HOME": str(root / "console")},
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 1 and not result.stdout, result
            transcript.append({"profile": profile, "stderr": result.stderr})
    return transcript


@requires(SANDBOX, NATIVE_FIXTURES)
def test_resolver_defaults_and_replacement(binary: Path) -> TranscriptWithCompanions:
    variants = {
        "omitted": {},
        "restricted": {"sandbox": {"network": "restricted"}},
        "enabled": {"sandbox": {"network": "enabled"}},
        "limited": {"sandbox": {"network": {"proxy": {"mode": "limited"}}}},
        "mapping_to_enabled": {"sandbox": {"network": {"proxy": {}}}},
        "mapping_to_restricted": {"sandbox": {"network": {"proxy": {}}}},
        "scalar_to_limited": {"sandbox": {"network": "enabled"}},
        "empty_domains": {
            "sandbox": {"network": {"proxy": {"domains": {"allow": []}}}}
        },
        "read_only_filesystem": {"sandbox": {"filesystem": {"read_only": ["/"]}}},
        "empty_filesystem": {"sandbox": {"filesystem": {}}},
    }
    companions = {}
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        tools = root / "bin"
        tools.mkdir()
        (tools / "uv").symlink_to(shutil.which("uv"))
        captures = {
            "worker": root / "worker.jsonl",
            "resolver": root / "resolver.jsonl",
        }
        environment = {
            **os.environ,
            "PATH": str(tools),
            LOADER_VARIABLE: str(build_interposer(root, "runner_configuration")),
            "MCP_CONSOLE_TEST_RUNNER_CONFIGURATION": str(captures["worker"]),
            "MCP_CONSOLE_TEST_RESOLVER_CONFIGURATION": str(captures["resolver"]),
        }
        environment.pop("R_HOME", None)
        environment.pop("RHOME", None)
        # Record process defaults independently of CI's tool-cache overrides.
        for key in list(environment):
            if key.startswith(("XDG_", "UV_", "IR_", "RENV_")) or key in (
                "R_USER_CACHE_DIR",
                "R_USER_DATA_DIR",
                "R_PKG_CACHE_DIR",
                "PKG_CACHE_DIR",
                "MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY",
                "MPLCONFIGDIR",
                "PYTHONPYCACHEPREFIX",
                "PYTHONUSERBASE",
            ):
                environment.pop(key)
        for name, resolver in variants.items():
            for path in captures.values():
                path.unlink(missing_ok=True)
            document = {
                "cache": "host",
                "python": sys.executable,
                "languages": ["python"],
                "resolver": resolver,
            }
            configure(root, document)
            overrides = {
                "mapping_to_enabled": ("-c", "resolver.sandbox.network=enabled"),
                "mapping_to_restricted": ("-c", "resolver.sandbox.network=restricted"),
                "scalar_to_limited": (
                    "-c",
                    "resolver.sandbox.network.proxy.mode=limited",
                ),
            }.get(name, ())
            with McpClient(binary, ("serve", *overrides), environment, root) as client:
                client.initialize_and_list_tools()
                if name == "empty_filesystem":
                    result = client.send(python="print('must not run')")
                    assert result.get("isError") and "must not run" not in json.dumps(
                        result
                    ), result
                else:
                    client.expect("ready\n", python="print('ready')")
                if name == "empty_filesystem":
                    client.finish_with_standard_error(expected_exit_status=1)
                else:
                    client.finish()
            # Discovery owns the first resolver launch. Later explicit Python
            # inspection has the same private transport with worker permissions.
            native = json.loads(captures["resolver"].read_text().splitlines()[0])
            proxy = native.get("proxy")
            if name in {
                "restricted",
                "enabled",
                "mapping_to_restricted",
                "mapping_to_enabled",
            }:
                assert proxy is None, native
                assert native["network"] == name.removeprefix("mapping_to_")
            else:
                assert proxy["allowLocalBinding"] is True
                assert proxy["enableSocks5"] is (
                    name not in {"limited", "scalar_to_limited"}
                )
                assert proxy["enableSocks5Udp"] is False
                assert (
                    proxy["domains"] == {}
                    if name == "empty_domains"
                    else proxy["domains"]["pypi.org"] == "allow"
                )
            if name == "read_only_filesystem":
                assert native["filesystem"]["entries"] == [
                    {"path": {"type": "path", "path": "/"}, "access": "read"}
                ]
            elif name == "empty_filesystem":
                assert native["filesystem"]["entries"] == [], native
            if sys.platform == "darwin":
                native["lifecycle"]["parent_pid"] = "<caller pid>"
                host_temporary = (
                    subprocess.check_output(
                        ["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"], text=True
                    )
                    .strip()
                    .rstrip("/")
                )
                for original, placeholder in (
                    (str(root), "<workspace>"),
                    (sys.executable, "<selected Python>"),
                    (os.environ["HOME"], "<home>"),
                    (host_temporary, "<host temporary directory>"),
                ):
                    native = json.loads(
                        json.dumps(native).replace(original, placeholder)
                    )
                shown = {**document, "python": "<selected Python>"}
                companions[f"{name}.darwin.yaml"] = [
                    shown,
                    *([{"overrides": list(overrides)}] if overrides else []),
                    native,
                ]
    return TranscriptWithCompanions([{"resolver_launches": list(variants)}], companions)


@requires(SANDBOX, NATIVE_FIXTURES)
def test_socket_choices(binary: Path) -> Transcript:
    transcript = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for initial, final in [
            ("dangerously_allow_all", []),
            ([], "dangerously_allow_all"),
        ]:
            document = {
                "sandbox": {
                    "network": {"proxy": {}, "sockets": {"unix_sockets": initial}}
                }
            }
            native = observe(
                binary,
                root,
                document,
                ("-c", "sandbox.network.sockets.unix_sockets=" + json.dumps(final)),
            )
            proxy = native["proxy"]
            assert proxy["dangerouslyAllowAllUnixSockets"] is (
                final == "dangerously_allow_all"
            )
            assert proxy["unixSockets"] == (
                {path: "allow" for path in final} if isinstance(final, list) else {}
            )
            transcript.append({"initial": initial, "override": final, "native": proxy})
        # Windows rejects Unix sockets as a platform capability. Unix validates
        # individual paths, so keep this assertion with the Unix socket cases.
        result = subprocess.run(
            [
                binary,
                "sandbox",
                "--no-config",
                "-c",
                "sandbox.network.proxy={}",
                "-c",
                'sandbox.network.sockets.unix_sockets=["relative.sock"]',
                "--",
                "unused-workload",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 1 and not result.stdout, result
        assert result.stderr == (
            "configuration with CLI overrides: "
            "sandbox.network.sockets.unix_sockets[0]: "
            "socket paths must be literal absolute paths\n"
        ), result
    return transcript


@requires(MACOS_SANDBOX, NATIVE_FIXTURES)
def test_socket_allowlist_clears_allow_all(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        native = observe(
            binary,
            root,
            {
                "sandbox": {
                    "network": {
                        "proxy": {},
                        "sockets": {"unix_sockets": "dangerously_allow_all"},
                    }
                }
            },
            ("-c", "sandbox.network.sockets.unix_sockets=[/tmp/service.sock]"),
        )
        proxy = native["proxy"]
        assert proxy["dangerouslyAllowAllUnixSockets"] is False
        assert proxy["unixSockets"] == {"/tmp/service.sock": "allow"}
        return [
            {
                "initial": "dangerously_allow_all",
                "override": ["/tmp/service.sock"],
                "native": proxy,
            }
        ]


@requires(SANDBOX)
def test_rejects_pinned_udp_transport(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        result = subprocess.run(
            [
                binary,
                "sandbox",
                "-c",
                "sandbox.network={proxy: {socks5: tcp_udp}, allow_local_binding: true}",
                "--",
                "unused",
            ],
            cwd=temporary,
            capture_output=True,
            text=True,
        )
        assert (
            result.returncode == 1
            and "sandbox.network.proxy.socks5" in result.stderr
            and "unsupported" in result.stderr
        ), result
        if sys.platform == "linux":
            for override in (
                "sandbox.network={proxy: {}, sockets: {unix_sockets: [/tmp/service.sock]}}",
                "resolver.sandbox.network={proxy: {}, sockets: {unix_sockets: [/tmp/service.sock]}}",
            ):
                unsupported = subprocess.run(
                    [binary, "sandbox", "-c", override, "--", "unused"],
                    cwd=temporary,
                    capture_output=True,
                    text=True,
                )
                assert (
                    unsupported.returncode == 1
                    and "unix_sockets" in unsupported.stderr
                    and "Linux" in unsupported.stderr
                    and not unsupported.stdout
                ), unsupported
        return [{"stderr": result.stderr}]


@requires(SANDBOX)
def test_native_domain_literals(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        hosts = [
            "127.0.0.1",
            "::1",
            "[::1]",
            "fe80::1%lo0",
            "[fe80::1%25lo0]",
            "fe80::1%3",
            "fe80::1%25",
            "[fe80::1%253]",
            "api[0-9].example.org",
        ]
        result = subprocess.run(
            [
                binary,
                "sandbox",
                "-c",
                "sandbox.network.proxy.domains.allow=" + json.dumps(hosts),
                "--",
                sys.executable,
                "-c",
                "print('accepted')",
            ],
            cwd=temporary,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0 and result.stderr == "", result
        return [{"domains": hosts, "stdout": result.stdout}]


@executions(DIRECT, SANDBOXED)
@requires(POSIX, command("uv"))
def test_shared_environment_survives_restart(
    binary: Path, execution: Execution
) -> Transcript:
    transcript = []
    for inherit, resolver_inherit in (
        (True, None),
        (False, None),
        (False, True),
        (True, False),
    ):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            tools = root / "bin"
            tools.mkdir()
            python = preseeded_duckdb_python(root)
            probing_uv(
                root,
                python,
                """
import json
import os
from pathlib import Path
if "MCP_CONSOLE_LOCAL_RUNTIME" not in os.environ:
    cache = Path(os.environ["UV_CACHE_DIR"])
    cache.mkdir(parents=True, exist_ok=True)
    cache.joinpath("environment.json").write_text(json.dumps({name: os.environ.get(name) for name in ("ROLE", "SHARED", "INHERITED", "UV_CACHE_DIR")}))
""",
            )
            common = {
                "HOME": os.environ["HOME"],
                "PATH": str(tools),
                "ROLE": "worker",
                "SHARED": "configured",
                "XDG_CACHE_HOME": str(root / "cache"),
                "UV_CACHE_DIR": str(root / "host-cache"),
                "MCP_CONSOLE_DYNAMIC_ENVIRONMENT_RESOLUTION": "project",
                "TMPDIR": str(root)
                if execution is DIRECT
                else "/invalid/configured-temp",
            }
            configure(
                root,
                {
                    "cache": "host" if execution is DIRECT else "console",
                    "python": {"managed": {}},
                    "languages": ["python"],
                    "inherit_environment": inherit,
                    "environment": common,
                    "resolver": {
                        "environment": {
                            "ROLE": "resolver",
                            **({"TMPDIR": str(root)} if execution is DIRECT else {}),
                        },
                        **(
                            {"inherit_environment": resolver_inherit}
                            if resolver_inherit is not None
                            else {}
                        ),
                    },
                },
            )
            environment = {**os.environ, "INHERITED": "launch"}
            environment.pop("R_HOME", None)
            environment.pop("RHOME", None)
            with McpClient(binary, execution.serve(), environment, root) as client:
                client.initialize_and_list_tools()
                expected = (
                    "worker configured " + ("launch" if inherit else "absent") + "\n"
                )
                for generation in range(2):
                    if generation:
                        client.send(control="restart")
                    client.expect(
                        expected,
                        python="import os; print(os.environ['ROLE'], os.environ['SHARED'], os.environ.get('INHERITED', 'absent'))",
                    )
                    client.expect(
                        "temporary storage selected\n",
                        python="from pathlib import Path; assert os.environ['MCP_CONSOLE_DYNAMIC_ENVIRONMENT_RESOLUTION'] == '0'; "
                        + f"assert (os.environ['UV_CACHE_DIR'] == {common['UV_CACHE_DIR']!r}) is {execution is DIRECT!r}; "
                        + f"assert (os.environ['TMPDIR'] == {common['TMPDIR']!r}) is {execution is DIRECT!r}; Path(os.environ['TMPDIR'], 'probe').write_text('temporary'); print('temporary storage selected')",
                    )
                    client.send(
                        python="os.environ['ROLE'] = 'mutated'; os.environ['SHARED'] = 'mutated'"
                    )
                client.finish()
            prepared = json.loads(next(root.rglob("environment.json")).read_text())
            assert (
                prepared["ROLE"] == "resolver" and prepared["SHARED"] == "configured"
            ), prepared
            assert prepared["INHERITED"] == (
                "launch"
                if (inherit if resolver_inherit is None else resolver_inherit)
                else None
            )
            assert (prepared["UV_CACHE_DIR"] == common["UV_CACHE_DIR"]) is (
                execution is DIRECT
            )
            transcript.append(
                {
                    "inherit_environment": inherit,
                    "resolver_inherit_environment": resolver_inherit,
                    "worker": expected.strip(),
                    "resolver": {
                        key: prepared[key] for key in ("ROLE", "SHARED", "INHERITED")
                    },
                    "console_adjustments_retained": True,
                }
            )
    return transcript


@requires(SANDBOX)
def test_isolated_host_cache_requires_resolver_home(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        configure(
            root,
            {
                "cache": "host",
                "python": sys.executable,
                "languages": ["python"],
                "inherit_environment": False,
                "resolver": {"sandbox": {"filesystem": {"read_only": ["/"]}}},
            },
        )
        with McpClient(binary, ("serve",), dict(os.environ), root) as client:
            client.initialize_and_list_tools()
            result = client.send(python="print('must not run')")
            assert result.get("isError"), result
            text = result["content"][0]["text"]
            assert "resolver.environment" in text and "HOME" in text, result
            assert "panic" not in text and "must not run" not in text, result
            client.finish_with_standard_error(expected_exit_status=1)
        cache = root / "cache"
        cache.mkdir()
        tools = root / "bin"
        tools.mkdir()
        (tools / "uv").symlink_to(shutil.which("uv"))
        configure(
            root,
            {
                "cache": "host",
                "python": sys.executable,
                "languages": ["python"],
                "inherit_environment": False,
                "environment": {
                    "PATH": str(tools),
                    "UV_CACHE_DIR": str(cache),
                    "MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY": str(cache / "duckdb"),
                },
                "resolver": {
                    "sandbox": {
                        "filesystem": {"read_only": ["/"], "read_write": [str(cache)]}
                    }
                },
            },
        )
        environment = {**os.environ, "PATH": str(tools)}
        environment.pop("R_HOME", None)
        environment.pop("RHOME", None)
        with McpClient(binary, ("serve",), environment, root) as client:
            client.initialize_and_list_tools()
            client.expect("ready\n", python="print('ready')")
            client.finish()
        return [
            {"preparation_error": text},
            {"explicit_duckdb_directory_without_home": True},
        ]


if __name__ == "__main__":
    run_this_suite(__file__)
