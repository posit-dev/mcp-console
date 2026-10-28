#!/usr/bin/env -S uv run --script

import base64
import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import tomllib
import zipfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.records import Transcript
from support.installation import native_console
from support.requirements import POSIX, requires
from support.suites import run_this_suite


def environment(root: Path) -> dict[str, str]:
    return os.environ | {
        "CODEX_HOME": str(root / "codex-home"),
        "CLAUDE_CONFIG_DIR": str(root / "claude-home"),
        "MCP_CONSOLE_HOME": str(root / "console-home"),
        "NO_COLOR": "1",
    }


def configure(
    binary: Path,
    root: Path,
    workspace: Path,
    *arguments: str,
    extra_environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        # Registration-only cases leave background execution to test_configure_warmup.
        [binary, "configure-client", "--no-warmup", *arguments],
        cwd=workspace,
        env=environment(root) | (extra_environment or {}),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )


@contextmanager
def authenticated_wheel(wheel: Path, token: str) -> Iterator[str]:
    contents = wheel.read_bytes()
    route = f"/{wheel.name}?access_key={token}"

    class Origin(BaseHTTPRequestHandler):
        def respond(self, body: bool) -> None:
            if self.path != route:
                self.send_error(403)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(contents)))
            self.end_headers()
            if body:
                self.wfile.write(contents)

        def do_GET(self) -> None:
            self.respond(True)

        def do_HEAD(self) -> None:
            self.respond(False)

        def log_message(self, *_args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Origin) as origin:
        thread = threading.Thread(target=origin.serve_forever)
        thread.start()
        try:
            yield f"http://127.0.0.1:{origin.server_port}{route}"
        finally:
            origin.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive(), "fixture origin did not stop"


def test_multiple_clients_pins_names_and_serve_arguments(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        name = "console.test_custom-1:@/local"
        for pin in (None, "latest", "0.0.5", "v0.0.5"):
            arguments = [] if pin is None else ["--pin-version", pin]
            result = configure(
                binary,
                root,
                root,
                "codex",
                "claude",
                "codex",
                "--name",
                name,
                *arguments,
                "--",
                "--no-project-config",
                "--writable-root",
                "a path",
            )
            assert result.returncode == 0 and result.stderr == "", result
            assert result.stdout.count("Configuration:") == 3, result
            prefix = [
                "mcp-console" + (f"@{pin}" if pin else ""),
                "serve",
            ]
            serve_args = [
                "--no-project-config",
                "--writable-root",
                "a path",
            ]
            expected = {
                "codex": [
                    *prefix,
                    "--default-config",
                    "sandbox.profile=inherit_codex",
                    *serve_args,
                ],
                "claude": [*prefix, *serve_args],
            }
            for client, path in (
                ("codex", root / "codex-home/config.toml"),
                ("claude", root / "claude-home/.claude.json"),
            ):
                config = read_config(path, client)
                mapping = config["mcp_servers" if client == "codex" else "mcpServers"]
                assert list(mapping) == [name], config
                assert mapping[name]["command"] == "uvx"
                assert mapping[name]["args"] == expected[client], mapping[name]
                if client == "codex":
                    assert mapping[name]["tool_input_schema_max_bytes"] == 1_000_000
            records.append({"version_suffix": pin, "clients": 2, "args": expected})
    return records


def test_explicit_sandbox_profile_overrides_client_default(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for no_uvx in (False, True):
            result = configure(
                binary,
                root,
                root,
                "codex",
                "claude",
                *(["--no-uvx"] if no_uvx else []),
                "--",
                "-c",
                "sandbox.profile=workspace",
            )
            assert result.returncode == 0 and not result.stderr, result
            for client, path in (
                ("codex", root / "codex-home/config.toml"),
                ("claude", root / "claude-home/.claude.json"),
            ):
                entry = server(read_config(path, client), client)
                expected = [
                    "serve",
                    *(
                        ["--default-config", "sandbox.profile=inherit_codex"]
                        if client == "codex"
                        else []
                    ),
                    "-c",
                    "sandbox.profile=workspace",
                ]
                assert entry["args"] == (
                    expected if no_uvx else ["mcp-console", *expected]
                ), entry
                serve_index = entry["args"].index("serve")
                result = subprocess.run(
                    [
                        binary,
                        "config",
                        "resolve",
                        "--no-config",
                        "--format",
                        "json",
                        *entry["args"][serve_index + 1 :],
                    ],
                    cwd=root,
                    env=environment(root),
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                assert result.returncode == 0 and not result.stderr, result
                resolved = json.loads(result.stdout)
                assert resolved == {"sandbox": {"profile": "workspace"}}, resolved
                records.append(
                    {
                        "launcher": "direct" if no_uvx else "uvx",
                        "client": client,
                        "args": entry["args"],
                        "resolved": resolved,
                    }
                )
    return records


def test_project_profile_overrides_generated_client_default(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for no_uvx in (False, True):
            result = configure(
                binary,
                root,
                root,
                "codex",
                *(["--no-uvx"] if no_uvx else []),
            )
            assert result.returncode == 0 and not result.stderr, result
            entry = server(
                read_config(root / "codex-home/config.toml", "codex"), "codex"
            )
            arguments = entry["args"][entry["args"].index("serve") + 1 :]
            path = root / ".agents/console/config.yaml"
            path.parent.mkdir(parents=True, exist_ok=True)
            for profile in (None, "workspace", "read_only"):
                if profile is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_text(
                        json.dumps({"sandbox": {"profile": profile}}), encoding="utf-8"
                    )
                resolved = subprocess.run(
                    [binary, "config", "resolve", "--format", "json", *arguments],
                    cwd=root,
                    env=environment(root),
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                assert resolved.returncode == 0 and not resolved.stderr, resolved
                value = json.loads(resolved.stdout)
                expected = profile or "inherit_codex"
                assert value == {"sandbox": {"profile": expected}}, value
                records.append(
                    {
                        "launcher": "direct" if no_uvx else "uvx",
                        "project_profile": profile,
                        "selected_profile": expected,
                    }
                )
    return records


def test_all_clients_are_validated_before_changes(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        path = root / "claude-home/.claude.json"
        path.parent.mkdir()
        path.write_text("[]", encoding="utf-8")
        result = configure(binary, root, root, "codex", "claude")
        assert result.returncode == 1 and not result.stdout, result
        assert str(path) in result.stderr, result
        assert not (root / "codex-home").exists()
        assert path.read_text(encoding="utf-8") == "[]"
        result = configure(
            binary, root, root, "codex", "--no-uvx", "--pin-version", "latest"
        )
        assert result.returncode == 2, result
        assert "--no-uvx" in result.stderr and "--pin-version" in result.stderr, result
        assert not (root / "codex-home").exists()
    return [{"invalid_client": "no files written", "conflicting_options": "rejected"}]


def test_version_validation_and_current_pin(binary: Path) -> Transcript:
    version = subprocess.check_output([binary, "--version"], text=True).split()[1]
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for suffix in ("typo", ">=0.0.5", "v", "0.0.5@latest", ""):
            result = configure(
                binary,
                root,
                root,
                "codex",
                "claude",
                "--pin-version",
                suffix,
            )
            assert result.returncode == 2 and not result.stdout, result
            assert "--pin-version" in result.stderr, result
            assert (
                not (root / "codex-home").exists()
                and not (root / "claude-home").exists()
            )
        for suffix in ("current", "1!2.0rc1.post2.dev3+local"):
            expected = version if suffix == "current" else suffix
            case = root / suffix
            case.mkdir()
            result = configure(
                binary,
                case,
                case,
                "codex",
                "claude",
                "--pin-version",
                suffix,
                "--dry-run",
            )
            assert (
                result.returncode == 0 and f"mcp-console@{expected}" in result.stdout
            ), result
            assert (
                not (case / "codex-home").exists()
                and not (case / "claude-home").exists()
            )
            result = configure(
                binary,
                case,
                case,
                "codex",
                "claude",
                "--pin-version",
                suffix,
            )
            assert result.returncode == 0 and not result.stderr, result
            for client, path in (
                ("codex", case / "codex-home/config.toml"),
                ("claude", case / "claude-home/.claude.json"),
            ):
                assert server(read_config(path, client), client)["args"] == [
                    f"mcp-console@{expected}",
                    "serve",
                    *(
                        ["--default-config", "sandbox.profile=inherit_codex"]
                        if client == "codex"
                        else []
                    ),
                ]
    return [
        {
            "invalid_suffixes": "rejected before writes",
            "current": "expanded to the running version",
            "exact_versions": "PEP 440 syntax",
        }
    ]


def test_reserved_names_are_client_specific(binary: Path) -> Transcript:
    names = (
        "workspace",
        "claude-in-chrome",
        "computer-use",
        "Claude Preview",
        "Claude Browser",
    )
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for name in names:
            for clients in (("claude",), ("codex", "claude")):
                result = configure(binary, root, root, *clients, "--name", name)
                assert result.returncode == 1 and not result.stdout, result
                assert name in result.stderr and "reserved" in result.stderr, result
                assert (
                    not (root / "codex-home").exists()
                    and not (root / "claude-home").exists()
                )
        for clients in (("codex",), ("claude", "codex")):
            result = configure(
                binary,
                root,
                root,
                *clients,
                "--name",
                "codex_apps",
            )
            assert result.returncode == 1 and not result.stdout, result
            assert "codex_apps" in result.stderr and "reserved" in result.stderr, result
            assert not (root / "codex-home").exists()
            assert not (root / "claude-home").exists()
        result = configure(binary, root, root, "claude", "--name", "codex_apps")
        assert result.returncode == 0 and not result.stderr, result
        assert (
            "codex_apps"
            in read_config(root / "claude-home/.claude.json", "claude")["mcpServers"]
        )
        result = configure(binary, root, root, "codex", "--name", "workspace")
        assert result.returncode == 0, result
        assert (
            "workspace"
            in read_config(root / "codex-home/config.toml", "codex")["mcp_servers"]
        )
    return [
        {
            "reserved_names": list(names),
            "reserved_builtin_name": "rejected only for the affected client",
            "selected_clients_validated_before_writes": True,
            "other_clients": "same names remain available",
        }
    ]


def test_server_name_patterns_are_client_specific(binary: Path) -> Transcript:
    names = ("console custom", 'console"quoted', "console+extra", "cönsole")
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for name in names:
            for clients in (("codex",), ("claude", "codex")):
                result = configure(binary, root, root, *clients, "--name", name)
                assert result.returncode == 1 and not result.stdout, result
                assert "^[a-zA-Z0-9_:@/.-]+$" in result.stderr, result
                assert (
                    not (root / "codex-home").exists()
                    and not (root / "claude-home").exists()
                )
            result = configure(binary, root, root, "claude", "--name", name)
            assert result.returncode == 0 and not result.stderr, result
            config = read_config(root / "claude-home/.claude.json", "claude")
            assert name in config["mcpServers"], config
            shutil.rmtree(root / "claude-home")
    return [{"server_name_pattern": "client-specific validation before changes"}]


def test_existing_files_retain_identity_and_security_metadata(
    binary: Path,
) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        long_command = "old-" + "x" * 1024
        for client, source, relative in (
            (
                "codex",
                f'[mcp_servers.console]\ncommand = "{long_command}"\n',
                "codex-home/config.toml",
            ),
            (
                "claude",
                json.dumps({"mcpServers": {"console": {"command": long_command}}}),
                "claude-home/.claude.json",
            ),
        ):
            path = root / relative
            path.parent.mkdir()
            path.write_text(source, encoding="utf-8")
            alias = root / f"{client}-managed-file"
            os.link(path, alias)
            before = path.stat()
            result = configure(binary, root, root, client)
            assert result.returncode == 0 and not result.stderr, result
            assert path.samefile(alias), "The managed file was replaced"
            assert (
                alias.read_bytes() == path.read_bytes() and alias.read_text() != source
            )
            assert server(read_config(path, client), client)["command"] == "uvx"
            assert (path.stat().st_uid, path.stat().st_gid) == (
                before.st_uid,
                before.st_gid,
            )
            records.append(
                {
                    "client": client,
                    "file_identity_and_native_security_metadata": "retained",
                }
            )
    return records


def test_comments_on_updated_toml_fields_are_preserved(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        path = root / "codex-home/config.toml"
        path.parent.mkdir()
        source = (
            "[mcp_servers.console]\n"
            '# Managed locally.\ncommand = "old" # command annotation\n'
            'args = ["old"] # argument annotation\n'
            "enabled = false # enable annotation\n"
            "omit_tools_from = [] # discovery annotation\n"
            "tool_input_schema_max_bytes = 5000 # schema annotation\n"
        )
        path.write_text(source, encoding="utf-8")
        result = configure(binary, root, root, "codex")
        assert result.returncode == 0, result
        updated = path.read_text(encoding="utf-8")
        for comment in (
            "# Managed locally.",
            "# command annotation",
            "# argument annotation",
            "# enable annotation",
            "# discovery annotation",
            "# schema annotation",
        ):
            assert updated.count(comment) == 1, updated
        assert server(read_config(path, "codex"), "codex")["args"] == [
            "mcp-console",
            "serve",
            "--default-config",
            "sandbox.profile=inherit_codex",
        ]
        assert (
            server(read_config(path, "codex"), "codex")["tool_input_schema_max_bytes"]
            == 1_000_000
        )
        result = configure(binary, root, root, "codex")
        assert result.returncode == 0 and path.read_text(encoding="utf-8") == updated, (
            result
        )
    return [{"updated_fields": "comments preserved", "repeat": "unchanged"}]


def test_direct_executable_finds_itself(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        prefix = root / "custom location"
        executable = prefix / "bin" / native_console(binary).name
        executable.parent.mkdir(parents=True)
        shutil.copy2(native_console(binary), executable)
        # uv-created persistent virtualenvs have cache markers too.
        (prefix / "pyvenv.cfg").write_text("uv = fixture\n", encoding="utf-8")
        (prefix / "CACHEDIR.TAG").write_text(
            "Signature: 8a477f597d28d172789f06886806bc55\n", encoding="utf-8"
        )
        result = configure(
            executable,
            root,
            root,
            "codex",
            "claude",
            "--no-uvx",
            extra_environment={"PATH": str(root / "empty-path")},
        )
        assert result.returncode == 0 and result.stderr == "", result
        for client, path in (
            ("codex", root / "codex-home/config.toml"),
            ("claude", root / "claude-home/.claude.json"),
        ):
            entry = server(read_config(path, client), client)
            assert Path(entry["command"]).is_absolute()
            assert Path(entry["command"]).samefile(executable)
            assert entry["args"] == [
                "serve",
                *(
                    ["--default-config", "sandbox.profile=inherit_codex"]
                    if client == "codex"
                    else []
                ),
            ]
    return [{"launch": "absolute path to the running executable", "clients": 2}]


def test_ephemeral_uvx_installs_persistent_executable(binary: Path) -> Transcript:
    # A local binary-only wheel exercises real uv tool run/install without
    # registry access or a production-only test hook. Serving is not needed.
    uv = shutil.which("uv")
    assert uv is not None, "uv is required"
    version = subprocess.check_output([binary, "--version"], text=True).split()[1]
    with TemporaryDirectory() as temporary, ExitStack() as stack:
        root = Path(temporary).resolve()
        wheels = root / "wheels"
        wheels.mkdir()
        selected = root / "selected source"
        selected.mkdir()
        stem = f"mcp_console-{version}"
        filename = f"{stem}-py3-none-any.whl"
        for directory, origin in ((wheels, "index"), (selected, "selected")):
            with zipfile.ZipFile(directory / filename, "w") as archive:
                archive.write(
                    native_console(binary),
                    f"{stem}.data/scripts/{native_console(binary).name}",
                )
                archive.writestr(
                    f"{stem}.dist-info/METADATA",
                    f"Metadata-Version: 2.1\nName: mcp-console\nVersion: {version}\n",
                )
                archive.writestr(
                    f"{stem}.dist-info/WHEEL",
                    "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
                )
                archive.writestr(f"{stem}.dist-info/fixture-origin.txt", origin)
            # Reopen finalized entries before hashing: on Windows, the writer's
            # cached ZipInfo retains native separators in its original name.
            with zipfile.ZipFile(directory / filename, "a") as archive:
                record = io.StringIO()
                writer = csv.writer(record, lineterminator="\n")
                for name in archive.namelist():
                    content = archive.read(name)
                    digest = (
                        base64.urlsafe_b64encode(hashlib.sha256(content).digest())
                        .rstrip(b"=")
                        .decode("ascii")
                    )
                    writer.writerow((name, f"sha256={digest}", len(content)))
                writer.writerow((f"{stem}.dist-info/RECORD", "", ""))
                archive.writestr(f"{stem}.dist-info/RECORD", record.getvalue())
        extra = {
            "UV_CACHE_DIR": str(root / "uv-cache"),
            "UV_TOOL_DIR": str(root / "uv-tools"),
            "UV_TOOL_BIN_DIR": str(root / "uv-bin"),
            "UV_CONFIG_FILE": str(root / "uv.toml"),
            "UV_OFFLINE": "true",
            "UV_PYTHON": sys.executable,
            "UV_PYTHON_DOWNLOADS": "never",
        }
        config = Path(extra["UV_CONFIG_FILE"])
        config.write_text(
            f"no-index = true\nfind-links = [{json.dumps(str(wheels))}]\n",
            encoding="utf-8",
        )
        # Persistent tools must stay usable beneath another tool's tagged cache,
        # regardless of the current UV_TOOL_DIR or whether uv is still on PATH.
        tagged = root / "target"
        tagged.mkdir()
        (tagged / "CACHEDIR.TAG").write_text(
            "Signature: 8a477f597d28d172789f06886806bc55\n", encoding="utf-8"
        )
        for origin, requirement in (
            ("index", "mcp-console"),
            ("direct", str(selected / filename)),
        ):
            case = tagged / origin
            case.mkdir()
            tools = case / "tools"
            result = subprocess.run(
                [uv, "tool", "install", requirement],
                cwd=case,
                env=environment(case)
                | extra
                | {
                    "UV_TOOL_DIR": str(tools),
                    "UV_TOOL_BIN_DIR": str(case / "bin"),
                },
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
            assert result.returncode == 0, result
            executables = [
                path
                for path in tools.rglob(native_console(binary).name)
                if path.is_file()
            ]
            assert len(executables) == 1, executables
            executable = executables[0]
            result = configure(
                executable,
                case,
                case,
                "codex",
                "claude",
                "--no-uvx",
                extra_environment=extra | {"PATH": str(root / "empty-path")},
            )
            assert result.returncode == 0 and not result.stderr, result
            assert "tool install" not in result.stdout, result
            for client, path in (
                ("codex", case / "codex-home/config.toml"),
                ("claude", case / "claude-home/.claude.json"),
            ):
                entry = server(read_config(path, client), client)
                assert Path(entry["command"]).samefile(executable), entry
                assert entry["args"] == [
                    "serve",
                    *(
                        ["--default-config", "sandbox.profile=inherit_codex"]
                        if client == "codex"
                        else []
                    ),
                ], entry
        command = [
            uv,
            "tool",
            "run",
            "--isolated",
            "--no-cache",
            "--find-links",
            str(wheels),
            "mcp-console",
            "configure-client",
            "--no-warmup",
            "codex",
            "claude",
            "--no-uvx",
        ]

        def invoke(
            *args: str, cached: bool = False
        ) -> subprocess.CompletedProcess[str]:
            launch = command.copy()
            if cached:
                launch.remove("--no-cache")
            return subprocess.run(
                [*launch, *args],
                cwd=root,
                env=environment(root) | extra,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )

        # CLI-only resolver options are not inherited by uv tool install.
        # Neither an unavailable source nor a different same-version wheel in
        # configuration makes a version-only freeze safe to promote.
        for links in (None, selected):
            config.write_text(
                "no-index = true\n"
                + (f"find-links = [{json.dumps(str(links))}]\n" if links else ""),
                encoding="utf-8",
            )
            for arguments in (("--dry-run",), ()):
                result = invoke(*arguments)
                assert result.returncode == 1 and not result.stdout, result
                assert "index or --find-links source" in result.stderr, result
                assert "uv tool install" in result.stderr, result
                assert not Path(extra["UV_TOOL_DIR"]).exists()
                assert not Path(extra["UV_TOOL_BIN_DIR"]).exists()
                assert not (root / "codex-home").exists()
                assert not (root / "claude-home").exists()
        config.write_text(
            f"no-index = true\nfind-links = [{json.dumps(str(wheels))}]\n",
            encoding="utf-8",
        )
        token = "fixture-query-credential"
        source = stack.enter_context(authenticated_wheel(selected / filename, token))
        # Keep the outer uv run quiet; the Console command owns the install report.
        command.insert(1, "--quiet")
        extra["UV_OFFLINE"] = "false"
        command[command.index("mcp-console") : command.index("mcp-console")] = [
            "--from",
            source,
        ]
        for cached in (False, True):
            result = invoke("--dry-run", cached=cached)
            assert result.returncode == 0, result
            assert source.split("?")[0] in result.stdout, result
            assert token not in result.stdout + result.stderr, result
            assert not (root / "uv-bin").exists()
            assert (
                not (root / "codex-home").exists()
                and not (root / "claude-home").exists()
            )
        for name in ("invalid name", "codex_apps"):
            result = invoke("--name", name)
            assert result.returncode == 1 and not result.stdout, result
        assert not (root / "uv-bin").exists()
        assert (
            not (root / "codex-home").exists() and not (root / "claude-home").exists()
        )
        # Invalid configuration must also prevent a persistent installation.
        bad = root / "claude-home/.claude.json"
        bad.parent.mkdir()
        bad.write_text("[]", encoding="utf-8")
        result = invoke()
        assert result.returncode == 1 and not (root / "uv-bin").exists(), result
        bad.unlink()
        # An installation failure must leave both configurations untouched.
        blocked = root / "blocked-tools"
        blocked.write_text("not a directory", encoding="utf-8")
        extra["UV_TOOL_DIR"] = str(blocked)
        result = invoke()
        assert result.returncode != 0 and "uv tool install" in result.stdout, result
        assert token not in result.stdout + result.stderr, result
        assert not (root / "codex-home").exists() and not bad.exists()
        extra["UV_TOOL_DIR"] = str(root / "uv-tools")
        preview = invoke("--dry-run")
        assert preview.returncode == 0, preview
        result = invoke()
        assert result.returncode == 0, result
        assert token not in preview.stdout + preview.stderr, preview
        assert token not in result.stdout + result.stderr, result
        for client, path in (
            ("codex", root / "codex-home/config.toml"),
            ("claude", bad),
        ):
            entry = server(read_config(path, client), client)
            installed = Path(extra["UV_TOOL_BIN_DIR"]) / native_console(binary).name
            assert Path(entry["command"]) == installed and installed.is_file(), entry
            assert entry["args"] == [
                "serve",
                *(
                    ["--default-config", "sandbox.profile=inherit_codex"]
                    if client == "codex"
                    else []
                ),
            ]
            assert (
                subprocess.check_output([installed, "--version"], text=True).split()[1]
                == version
            )
        origins = list(Path(extra["UV_TOOL_DIR"]).rglob("fixture-origin.txt"))
        assert len(origins) == 1, origins
        assert origins[0].read_text() == "selected", origins[0].read_text()
        assert (
            source.split("?")[0] in preview.stdout
            and source.split("?")[0] in result.stdout
        )
        provenance = json.loads(origins[0].with_name("direct_url.json").read_text())
        assert provenance["url"] == source, provenance
        # Once installed, --no-uvx should use itself without calling uv again.
        result = configure(
            installed,
            root,
            root,
            "codex",
            "--no-uvx",
            extra_environment=extra,
        )
        assert result.returncode == 0 and "tool install" not in result.stdout, result
    return [
        {
            "fixture": "Local binary-only Console wheel, real uv, isolated tools and cache, no registry access",
            "temporary_run": "promoted to a persistent executable",
            "persistent_tools": "index and direct-source installations beneath a tagged parent retain their executable paths without uv",
            "cache_layouts": "custom cache and --no-cache environments are promoted",
            "source_identity": "selected wheel retained despite an index wheel with the same version",
            "index_source": "unrecorded resolver options rejected before installation or configuration writes",
            "credentials": "used for installation and omitted from both output streams",
            "dry_run": "no install or configuration writes",
            "installation_failure": "no configuration writes",
        }
    ]


def test_shared_cache_marker_keeps_direct_executables(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for layout in ("cargo", "python-venv"):
            case = root / layout
            cache = case / "target"
            prefix = cache if layout == "cargo" else cache / "venv"
            executable = (
                prefix
                / ("release" if layout == "cargo" else "bin")
                / native_console(binary).name
            )
            executable.parent.mkdir(parents=True)
            shutil.copy2(native_console(binary), executable)
            (cache / "CACHEDIR.TAG").write_text(
                "Signature: 8a477f597d28d172789f06886806bc55\n", encoding="utf-8"
            )
            if layout == "python-venv":
                (prefix / "pyvenv.cfg").write_text("home = fixture\n", encoding="utf-8")
            result = configure(
                executable,
                case,
                case,
                "codex",
                "claude",
                "--no-uvx",
                extra_environment={"PATH": str(root / "empty-path")},
            )
            assert result.returncode == 0 and not result.stderr, result
            assert "tool install" not in result.stdout, result
            for client, path in (
                ("codex", case / "codex-home/config.toml"),
                ("claude", case / "claude-home/.claude.json"),
            ):
                entry = server(read_config(path, client), client)
                assert Path(entry["command"]).is_absolute()
                assert Path(entry["command"]).samefile(executable)
                assert entry["args"] == [
                    "serve",
                    *(
                        ["--default-config", "sandbox.profile=inherit_codex"]
                        if client == "codex"
                        else []
                    ),
                ]
    return [
        {
            "shared_cache_marker": "Cargo and non-uv virtualenvs retain their executable paths"
        }
    ]


def read_config(path: Path, client: str) -> dict:
    if client == "codex":
        return tomllib.loads(path.read_text(encoding="utf-8"))
    return json.loads(path.read_text(encoding="utf-8"))


def server(config: dict, client: str) -> dict:
    return config["mcp_servers" if client == "codex" else "mcpServers"]["console"]


def test_send_approval_preserves_other_tool_settings(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for project in (False, True):
            name = "console.local" if project else "console"
            path = root / (
                ".codex/config.toml" if project else "codex-home/config.toml"
            )
            path.parent.mkdir(parents=True)
            source = (
                'mcp_servers = { "console.local" = { command = "old", '
                'tools = { send = { approval_mode = "prompt", output_token_limit = 5000 }, '
                'other = { approval_mode = "prompt" } } } }\n'
                if project
                else '[mcp_servers.console]\ncommand = "old"\n'
                'default_tools_approval_mode = "prompt"\n'
                "[mcp_servers.console.tools.send]\n"
                'approval_mode = "prompt" # Keep this comment.\noutput_token_limit = 5000\n'
                '[mcp_servers.console.tools.other]\napproval_mode = "prompt"\n'
            )
            path.write_text(source, encoding="utf-8")
            arguments = ["codex", "--name", name, "--no-auto-load"]
            if project:
                arguments += ["--directory", "."]
            preview = configure(binary, root, root, *arguments, "--dry-run")
            assert preview.returncode == 0 and not preview.stderr, preview
            assert path.read_text(encoding="utf-8") == source
            assert 'approval_mode = "approve"' in preview.stdout, preview
            result = configure(binary, root, root, *arguments)
            assert result.returncode == 0 and not result.stderr, result
            entry = read_config(path, "codex")["mcp_servers"][name]
            assert entry["tools"] == {
                "send": {"approval_mode": "approve", "output_token_limit": 5000},
                "other": {"approval_mode": "prompt"},
            }, entry
            if not project:
                assert entry["default_tools_approval_mode"] == "prompt"
                assert "# Keep this comment." in path.read_text(encoding="utf-8")
            written = path.read_bytes()
            again = configure(binary, root, root, *arguments)
            assert again.returncode == 0 and not again.stderr, again
            assert path.read_bytes() == written
            records.append(
                {"project": project, "server": name, "tools": entry["tools"]}
            )
    return records


def test_claude_send_permission_paths_names_and_preservation(
    binary: Path,
) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        target = root / "project"
        target.mkdir()
        for project in (False, True):
            name = "console.test_custom-1:@/local" if project else "console"
            rule = (
                "mcp__console_test_custom-1___local__send"
                if project
                else "mcp__console__send"
            )
            settings = (
                target / ".claude/settings.json"
                if project
                else root / "claude-home/settings.json"
            )
            registration = (
                target / ".mcp.json" if project else root / "claude-home/.claude.json"
            )
            settings.parent.mkdir(parents=True)
            original = {
                "model": "example",
                "permissions": {
                    "allow": ["Read", "mcp__other__send"],
                    "ask": ["Bash"],
                    "deny": ["Read(./.env)"],
                },
            }
            settings.write_text(json.dumps(original), encoding="utf-8")
            written = settings.read_bytes()
            arguments = ["claude", "claude", "--name", name, "--no-auto-load"]
            if project:
                arguments += ["--directory", str(target)]
            preview = configure(binary, root, workspace, *arguments, "--dry-run")
            assert preview.returncode == 0 and not preview.stderr, preview
            assert str(settings) in preview.stdout and rule in preview.stdout, preview
            assert settings.read_bytes() == written and not registration.exists()
            result = configure(binary, root, workspace, *arguments)
            assert result.returncode == 0 and not result.stderr, result
            assert str(settings) in result.stdout and rule in result.stdout, result
            expected = original | {
                "permissions": original["permissions"]
                | {"allow": [*original["permissions"]["allow"], rule]}
            }
            assert read_config(settings, "claude") == expected
            config = read_config(registration, "claude")
            assert "permissions" not in config and list(config["mcpServers"]) == [name]
            written = settings.read_bytes()
            again = configure(binary, root, workspace, *arguments)
            assert again.returncode == 0 and not again.stderr, again
            assert settings.read_bytes() == written
            assert not (workspace / ".claude").exists()
            records.append({"project": project, "server": name, "settings": expected})
    return records


def test_invalid_claude_permissions_prevent_all_writes(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        settings = root / "claude-home/settings.json"
        settings.parent.mkdir()
        for source in (
            "{",
            "[]",
            '{"permissions": []}',
            '{"permissions": {"allow": "Read"}}',
            '{"permissions": {"allow": [42]}}',
        ):
            settings.write_text(source, encoding="utf-8")
            for dry_run in (False, True):
                result = configure(
                    binary,
                    root,
                    root,
                    "codex",
                    "claude",
                    *(["--dry-run"] if dry_run else []),
                )
                assert result.returncode == 1 and not result.stdout, result
                assert str(settings) in result.stderr, result
                assert settings.read_text(encoding="utf-8") == source
                assert not (root / "codex-home").exists()
                assert not (root / "claude-home/.claude.json").exists()
        return [{"invalid_permissions": 5, "all_configuration_files": "unchanged"}]


def test_global_install_preserves_configuration_and_is_repeatable(
    binary: Path,
) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for client in ("codex", "claude"):
            home = root / f"{client}-home"
            home.mkdir()
            path = home / ("config.toml" if client == "codex" else ".claude.json")
            if client == "codex":
                source = (
                    '# Keep this comment and model setting.\nmodel = "example"\n\n'
                    '[mcp_servers.other]\ncommand = "other-server" # Keep this too.\n\n'
                    '[mcp_servers.console]\ncommand = "mcp-console"\nargs = ["serve"]\n'
                    "enabled = false\nstartup_timeout_sec = 30\n"
                    '[mcp_servers.console.env]\nKEEP = "value"\n'
                )
            else:
                source = json.dumps(
                    {
                        "theme": "dark",
                        "projects": {"example": {"allowedTools": []}},
                        "mcpServers": {
                            "other": {"command": "other-server"},
                            "console": {
                                "type": "stdio",
                                "command": "mcp-console",
                                "args": ["serve"],
                                "env": {"KEEP": "value"},
                            },
                        },
                    }
                )
            path.write_text(source, encoding="utf-8")
            before = read_config(path, client)
            result = configure(binary, root, root, client)
            assert result.returncode == 0 and result.stderr == "", result
            assert str(path) in result.stdout and "uvx" in result.stdout, result
            config = read_config(path, client)
            entry = server(config, client)
            assert entry["command"] == "uvx" and entry["args"] == [
                "mcp-console",
                "serve",
                *(
                    ["--default-config", "sandbox.profile=inherit_codex"]
                    if client == "codex"
                    else []
                ),
            ]
            assert entry["env"] == {"KEEP": "value"}
            if client == "codex":
                assert "[mcp_servers.console]\n" in result.stdout, result
                assert (
                    entry["omit_tools_from"] == ["deferred"]
                    and entry["enabled"] is True
                )
                assert entry["startup_timeout_sec"] == 30
                assert (
                    '# Keep this comment and model setting.\nmodel = "example"'
                    in path.read_text()
                )
                assert 'command = "other-server" # Keep this too.' in path.read_text()
                before["mcp_servers"]["console"] = entry
            else:
                assert entry["type"] == "stdio" and entry["alwaysLoad"] is True
                assert read_config(home / "settings.json", client) == {
                    "permissions": {"allow": ["mcp__console__send"]}
                }
                before["mcpServers"]["console"] = entry
            assert config == before
            written = path.read_bytes()
            again = configure(binary, root, root, client)
            assert again.returncode == 0 and again.stderr == "", again
            assert path.read_bytes() == written
            records.append(
                {
                    "client": client,
                    "console": entry,
                    "stdout": result.stdout.replace(str(path), "<CONFIG>").replace(
                        str(home / "settings.json"), "<SETTINGS>"
                    ),
                    "repeat": "unchanged",
                }
            )
    return records


def test_scope_defaults_and_explicit_overrides(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for client in ("codex", "claude"):
            project = root / client
            nested = project / "src" / "nested"
            nested.mkdir(parents=True)
            # Git worktrees use a .git file rather than a directory.
            (project / ".git").write_text("gitdir: fixture\n", encoding="utf-8")
            relative = ".codex/config.toml" if client == "codex" else ".mcp.json"
            project_config = project / relative
            global_config = (
                root
                / f"{client}-home"
                / ("config.toml" if client == "codex" else ".claude.json")
            )
            result = configure(binary, root, nested, client)
            assert result.returncode == 0 and result.stderr == "", result
            assert str(global_config) in result.stdout, result
            assert not project_config.exists() and not (nested / relative).exists()
            assert (
                server(read_config(global_config, client), client)["command"] == "uvx"
            )
            result = configure(binary, root, nested, client, "--directory", ".")
            assert result.returncode == 0 and result.stderr == "", result
            assert os.path.join(nested, ".", str(Path(relative))) in result.stdout, (
                result
            )
            assert not project_config.exists()
            assert (
                server(read_config(nested / relative, client), client)["command"]
                == "uvx"
            )
            outside = root / f"outside-{client}"
            outside.mkdir()
            result = configure(binary, root, outside, client)
            assert result.returncode == 0 and str(global_config) in result.stdout, (
                result
            )
            assert not (outside / relative).exists()
            result = configure(binary, root, outside, client, "--directory", ".")
            assert (
                result.returncode == 0
                and os.path.join(outside, ".", str(Path(relative))) in result.stdout
            ), result
            assert (
                server(read_config(outside / relative, client), client)["command"]
                == "uvx"
            )
            records.append(
                {
                    "client": client,
                    "default": "global",
                    "project": "current directory",
                }
            )
    return records


def test_project_directory_destinations(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        (root / ".git").mkdir()
        workspace = root / "workspace"
        workspace.mkdir()
        for kind in ("relative", "absolute"):
            target = (workspace if kind == "relative" else root) / f"{kind} project"
            target.mkdir()
            selector = target.name if kind == "relative" else str(target)
            arguments = (
                "codex",
                "claude",
                "--directory",
                selector,
            )
            preview = configure(binary, root, workspace, *arguments, "--dry-run")
            assert preview.returncode == 0 and preview.stderr == "", preview
            assert not list(target.iterdir())
            result = configure(binary, root, workspace, *arguments)
            assert result.returncode == 0 and result.stderr == "", result
            for client, relative in (
                ("codex", ".codex/config.toml"),
                ("claude", ".mcp.json"),
            ):
                path = target / relative
                assert str(path) in preview.stdout and str(path) in result.stdout
                assert server(read_config(path, client), client)["command"] == "uvx"
                if client == "claude":
                    assert read_config(target / ".claude/settings.json", client) == {
                        "permissions": {"allow": ["mcp__console__send"]}
                    }
                assert not (workspace / relative).exists()
                assert not (root / relative).exists()
            records.append({"directory": kind, "clients": ["codex", "claude"]})
        assert not (root / "codex-home").exists()
        assert not (root / "claude-home").exists()
    return records


def test_project_directory_rejects_invalid_targets_and_scopes(
    binary: Path,
) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        (root / ".git").mkdir()
        file = root / "file"
        file.write_text("not a directory", encoding="utf-8")
        cases = [
            (
                f"removed {scope} scope",
                ("--scope", scope),
                2,
                "unexpected argument '--scope' found",
            )
            for scope in ("global", "project", "auto")
        ] + [
            (
                "unsupported global flag",
                ("--global",),
                2,
                "unexpected argument '--global' found",
            ),
            *[
                (
                    name,
                    ("--directory", str(path)),
                    1,
                    "Project directory does not exist or is not a directory",
                )
                for name, path in (
                    ("missing directory", root / "missing"),
                    ("file", file),
                )
            ],
        ]
        before = set(root.rglob("*"))
        for name, arguments, exit_code, message in cases:
            for dry_run in (False, True):
                result = configure(
                    binary,
                    root,
                    root,
                    "codex",
                    "claude",
                    *arguments,
                    *(("--dry-run",) if dry_run else ()),
                )
                assert result.returncode == exit_code and result.stdout == "", result
                assert message in result.stderr, result
                assert set(root.rglob("*")) == before
                assert file.read_text(encoding="utf-8") == "not a directory"
                stderr = result.stderr
                if arguments[0] == "--directory":
                    stderr = stderr.replace(arguments[1], "<DIRECTORY>")
                records.append(
                    {
                        "case": name,
                        "dry_run": dry_run,
                        "exit_code": result.returncode,
                        "stderr": stderr,
                    }
                )
    return records


def test_global_registration_warns_about_legacy_project_entries(
    binary: Path,
) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for client in ("codex", "claude"):
            for marker in ("directory", "worktree"):
                project = root / f"{client}-{marker}"
                workspace = project / "src" / "nested"
                workspace.mkdir(parents=True)
                if marker == "directory":
                    (project / ".git").mkdir()
                    (project / ".git/HEAD").write_text("ref: refs/heads/main\n")
                else:
                    (project / ".git").write_text("gitdir: fixture\n")
                relative = ".codex/config.toml" if client == "codex" else ".mcp.json"
                legacy = configure(
                    binary, root, workspace, client, "--directory", str(project)
                )
                assert legacy.returncode == 0 and legacy.stderr == "", legacy
                project_path = project / relative
                previous = project_path.read_bytes()
                global_path = (
                    root
                    / f"{client}-home"
                    / ("config.toml" if client == "codex" else ".claude.json")
                )
                arguments = (
                    client,
                    "--pin-version",
                    "0.0.5",
                    "--no-auto-load",
                    "--",
                    "--writable-root",
                    "output",
                )
                global_before = (
                    global_path.read_bytes() if global_path.exists() else None
                )
                preview = configure(binary, root, workspace, client, "--dry-run")
                assert preview.returncode == 0, preview
                assert str(project_path) in preview.stderr, preview
                assert "may take precedence" in preview.stderr, preview
                assert (
                    global_path.read_bytes() if global_path.exists() else None
                ) == global_before
                result = configure(binary, root, workspace, *arguments)
                assert result.returncode == 0, result
                assert result.stderr == preview.stderr, result
                assert project_path.read_bytes() == previous
                entry = server(read_config(global_path, client), client)
                assert entry["args"][0] == "mcp-console@0.0.5"
                assert entry["args"][-2:] == ["--writable-root", "output"]
                assert (
                    entry["omit_tools_from"] == []
                    if client == "codex"
                    else not entry["alwaysLoad"]
                )
                records.append(
                    {
                        "client": client,
                        "git_marker": marker,
                        "stderr": result.stderr.replace(
                            str(project_path), "<PROJECT_CONFIG>"
                        ),
                        "project_entry": "unchanged",
                    }
                )
    return records


def test_project_shadow_warning_follows_client_discovery(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for client in ("codex", "claude"):
            for case in (
                "current directory",
                "closest ancestor",
                "unrelated server",
                "above Git root",
                "outside Git",
                "custom root marker",
                "disabled root search",
            ):
                home = root / f"{client}-{case}"
                project = home / "project"
                workspace = project / "src" / "nested"
                workspace.mkdir(parents=True)
                (project / ".git").write_text("gitdir: fixture\n")
                relative = ".codex/config.toml" if client == "codex" else ".mcp.json"
                expected = None
                if case == "current directory":
                    selected = workspace
                elif case == "closest ancestor":
                    selected = workspace.parent
                elif case in (
                    "above Git root",
                    "outside Git",
                    "custom root marker",
                    "disabled root search",
                ):
                    selected = home
                else:
                    selected = project
                if case in ("outside Git", "custom root marker"):
                    (project / ".git").unlink()
                if case == "custom root marker":
                    (home / ".project-root").touch()
                if client == "codex" and case in (
                    "custom root marker",
                    "disabled root search",
                ):
                    config = home / "codex-home/config.toml"
                    config.parent.mkdir()
                    config.write_text(
                        'project_root_markers = [".project-root"]\n'
                        if case == "custom root marker"
                        else "project_root_markers = []\n"
                    )
                name = "other" if case == "unrelated server" else "console"
                path = selected / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    f'[mcp_servers.{name}]\ncommand = "old"\n'
                    if client == "codex"
                    else json.dumps({"mcpServers": {name: {"command": "old"}}})
                )
                if case == "closest ancestor":
                    ancestor = project / relative
                    ancestor.parent.mkdir(parents=True, exist_ok=True)
                    ancestor.write_bytes(path.read_bytes())
                    unrelated = workspace / relative
                    unrelated.parent.mkdir(parents=True, exist_ok=True)
                    unrelated.write_text(
                        '[mcp_servers.other]\ncommand = "other"\n'
                        if client == "codex"
                        else json.dumps({"mcpServers": {"other": {"command": "other"}}})
                    )
                before = {
                    file: file.read_bytes()
                    for file in home.rglob("*")
                    if file.is_file()
                }
                if case != "unrelated server" and (
                    client == "claude"
                    or case
                    not in ("above Git root", "outside Git", "disabled root search")
                ):
                    expected = path
                result = configure(binary, home, workspace, client, "--dry-run")
                assert result.returncode == 0, result
                if expected is None:
                    assert result.stderr == "", result
                else:
                    assert str(expected) in result.stderr, result
                    assert result.stderr.count("Warning:") == 1, result
                assert {
                    file: file.read_bytes()
                    for file in home.rglob("*")
                    if file.is_file()
                } == before
                records.append(
                    {"client": client, "case": case, "warning": expected is not None}
                )
        workspace = root / "global-is-project"
        workspace.mkdir()
        for attempt in range(2):
            result = configure(
                binary,
                root,
                workspace,
                "codex",
                extra_environment={"CODEX_HOME": ".codex"},
            )
            assert result.returncode == 0 and result.stderr == "", result
        records.append({"global_file_in_cwd": "not reported as a project override"})
    return records


def test_global_path_aliases_are_not_project_overrides(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for alias in ("relative parent", "absolute parent"):
            project = root / alias
            workspace = project / "child"
            workspace.mkdir(parents=True)
            (project / ".git").write_text("gitdir: fixture\n")
            path = project / ".codex/config.toml"
            home = (
                "../.codex"
                if alias == "relative parent"
                else str(workspace / "../.codex")
            )
            environment = {"CODEX_HOME": home}
            first = configure(
                binary, root, workspace, "codex", extra_environment=environment
            )
            assert first.returncode == 0 and first.stderr == "", first
            previous = path.read_bytes()
            preview = configure(
                binary,
                root,
                workspace,
                "codex",
                "--no-auto-load",
                "--dry-run",
                extra_environment=environment,
            )
            assert preview.returncode == 0 and preview.stderr == "", preview
            assert path.read_bytes() == previous
            repeated = configure(
                binary,
                root,
                workspace,
                "codex",
                "--no-auto-load",
                extra_environment=environment,
            )
            assert repeated.returncode == 0 and repeated.stderr == "", repeated
            assert server(read_config(path, "codex"), "codex")["omit_tools_from"] == []
            project_entry = configure(
                binary, root, workspace, "codex", "--directory", "."
            )
            assert project_entry.returncode == 0 and project_entry.stderr == "", (
                project_entry
            )
            override = workspace / ".codex/config.toml"
            before = {file: file.read_bytes() for file in (path, override)}
            genuine_override = configure(
                binary,
                root,
                workspace,
                "codex",
                "--dry-run",
                extra_environment=environment,
            )
            assert genuine_override.returncode == 0, genuine_override
            assert str(override) in genuine_override.stderr, genuine_override
            assert genuine_override.stderr.count("Warning:") == 1, genuine_override
            assert {file: file.read_bytes() for file in before} == before
            records.append(
                {
                    "alias": alias,
                    "global_entry": "not a project override",
                    "separate_project_entry": "warns",
                }
            )
    return records


@requires(POSIX)
def test_global_symlink_aliases_are_not_project_overrides(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for client, alias in (
            ("codex", "directory"),
            ("codex", "file"),
            ("claude", "file"),
        ):
            project = root / f"{client}-{alias}"
            workspace = project / "child"
            workspace.mkdir(parents=True)
            (project / ".git").write_text("gitdir: fixture\n")
            first = configure(binary, root, project, client, "--directory", ".")
            assert first.returncode == 0 and first.stderr == "", first
            path = project / (
                ".codex/config.toml" if client == "codex" else ".mcp.json"
            )
            home = root / f"{client}-{alias}-home"
            if alias == "directory":
                link = home
                link.symlink_to(path.parent, target_is_directory=True)
            else:
                home.mkdir()
                link = home / ("config.toml" if client == "codex" else ".claude.json")
                link.symlink_to(path)
            environment = {
                "CODEX_HOME" if client == "codex" else "CLAUDE_CONFIG_DIR": str(home)
            }
            previous = path.read_bytes()
            preview = configure(
                binary,
                root,
                workspace,
                client,
                "--pin-version",
                "0.0.5",
                "--dry-run",
                extra_environment=environment,
            )
            assert preview.returncode == 0 and preview.stderr == "", preview
            assert path.read_bytes() == previous
            result = configure(
                binary,
                root,
                workspace,
                client,
                "--pin-version",
                "0.0.5",
                extra_environment=environment,
            )
            assert result.returncode == 0 and result.stderr == "", result
            assert link.is_symlink()
            assert (
                server(read_config(path, client), client)["args"][0]
                == "mcp-console@0.0.5"
            )
            records.append(
                {
                    "client": client,
                    "alias": f"{alias} symlink",
                    "global_entry": "not a project override",
                }
            )
    return records


def test_global_registration_rejects_malformed_project_configuration(
    binary: Path,
) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for client in ("codex", "claude"):
            workspace = root / client
            workspace.mkdir()
            path = workspace / (
                ".codex/config.toml" if client == "codex" else ".mcp.json"
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("malformed [", encoding="utf-8")
            before = {
                file: file.read_bytes() for file in root.rglob("*") if file.is_file()
            }
            result = configure(binary, root, workspace, "codex", "claude")
            assert result.returncode == 1 and result.stdout == "", result
            assert str(path) in result.stderr, result
            assert "Cannot inspect project configuration" in result.stderr, result
            assert {
                file: file.read_bytes() for file in root.rglob("*") if file.is_file()
            } == before
            records.append(
                {
                    "client": client,
                    "exit_code": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr.replace(str(path), "<PROJECT_CONFIG>"),
                }
            )
    return records


def test_dry_run_and_auto_load_opt_out(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for client in ("codex", "claude"):
            path = (
                root
                / f"{client}-home"
                / ("config.toml" if client == "codex" else ".claude.json")
            )
            result = configure(binary, root, root, client, "--dry-run")
            assert result.returncode == 0 and result.stderr == "", result
            assert str(path) in result.stdout and "uvx" in result.stdout, result
            assert not path.parent.exists()
            settings = path.parent / "settings.json"
            preview = result.stdout.replace(str(path), "<CONFIG>").replace(
                str(settings), "<SETTINGS>"
            )
            result = configure(binary, root, root, client)
            assert result.returncode == 0, result
            written = path.read_bytes()
            permissions = settings.read_bytes() if client == "claude" else None
            result = configure(
                binary,
                root,
                root,
                client,
                "--no-auto-load",
                "--dry-run",
            )
            assert result.returncode == 0 and path.read_bytes() == written, result
            if client == "claude":
                assert settings.read_bytes() == permissions
            result = configure(binary, root, root, client, "--no-auto-load")
            assert result.returncode == 0 and result.stderr == "", result
            entry = server(read_config(path, client), client)
            if client == "codex":
                assert entry["omit_tools_from"] == []
            else:
                assert entry["alwaysLoad"] is False
            records.append(
                {
                    "client": client,
                    "console": entry,
                    "dry_run": preview,
                    "opt_out": result.stdout.replace(str(path), "<CONFIG>").replace(
                        str(settings), "<SETTINGS>"
                    ),
                }
            )
    return records


def test_inline_toml_and_existing_project_json(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for client, source in (
            (
                "codex",
                '# Keep inline tables.\nmcp_servers = { other = { command = "other" }, '
                'console = { command = "old", env = { KEEP = "value" } } }\n',
            ),
            (
                "claude",
                '{"mcpServers": {"other": {"command": "other"}, '
                '"console": {"command": "old", "env": {"KEEP": "value"}}}}',
            ),
        ):
            path = root / (".codex/config.toml" if client == "codex" else ".mcp.json")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
            result = configure(binary, root, root, client, "--directory", ".")
            assert result.returncode == 0 and result.stderr == "", result
            config = read_config(path, client)
            entry = server(config, client)
            assert entry["command"] == "uvx" and entry["args"] == [
                "mcp-console",
                "serve",
                *(
                    ["--default-config", "sandbox.profile=inherit_codex"]
                    if client == "codex"
                    else []
                ),
            ]
            assert entry["env"] == {"KEEP": "value"}
            mapping = config["mcp_servers" if client == "codex" else "mcpServers"]
            assert mapping["other"] == {"command": "other"}
            records.append({"client": client, "console": entry})
    return records


def test_invalid_configuration_is_not_overwritten(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for client, invalid in (
            (
                "codex",
                (
                    "model = [",
                    'mcp_servers = "wrong"',
                    '[mcp_servers]\nconsole = "wrong"',
                    '[mcp_servers.console]\nurl = "https://example.com/mcp"',
                    "[mcp_servers.console]\ntools = []",
                    '[mcp_servers.console.tools]\nsend = "wrong"',
                ),
            ),
            (
                "claude",
                (
                    '{"mcpServers":',
                    "[]",
                    '{"mcpServers": []}',
                    '{"mcpServers": {"console": 1}}',
                    '{"mcpServers": {"console": {"type": "http", "url": "https://example.com/mcp"}}}',
                ),
            ),
        ):
            home = root / f"{client}-home"
            home.mkdir()
            path = home / ("config.toml" if client == "codex" else ".claude.json")
            for source in invalid:
                path.write_text(source, encoding="utf-8")
                result = configure(binary, root, root, client)
                assert result.returncode == 1 and result.stdout == "", result
                assert str(path) in result.stderr, result
                assert path.read_text(encoding="utf-8") == source
            records.append(
                {"client": client, "invalid_configurations_preserved": len(invalid)}
            )
    return records


@requires(POSIX)
def test_linked_configuration_preserves_target_and_permissions(
    binary: Path,
) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for client, source in (
            ("codex", "# Keep dotfile comments.\n"),
            ("claude", "{}\n"),
        ):
            home = root / f"{client}-home"
            home.mkdir()
            path = home / ("config.toml" if client == "codex" else ".claude.json")
            target = root / f"{client}-dotfile"
            target.write_text(source, encoding="utf-8")
            target.chmod(0o640)
            path.symlink_to(target)
            result = configure(binary, root, root, client)
            assert result.returncode == 0 and result.stderr == "", result
            assert path.is_symlink() and path.resolve() == target
            assert target.stat().st_mode & 0o777 == 0o640
            assert server(read_config(target, client), client)["command"] == "uvx"
            records.append(
                {"client": client, "symlink": "preserved", "permissions": "0640"}
            )
        # Both files parse before writes. The broken second link fails only
        # when writing, after the first client's update succeeds.
        target.unlink()
        result = configure(
            binary,
            root,
            root,
            "codex",
            "claude",
            "--pin-version",
            "latest",
        )
        first = root / "codex-home/config.toml"
        assert result.returncode == 1 and str(path) in result.stderr, result
        assert str(first) in result.stdout and str(path) not in result.stdout, result
        assert server(read_config(first, "codex"), "codex")["args"] == [
            "mcp-console@latest",
            "serve",
            "--default-config",
            "sandbox.profile=inherit_codex",
        ]
        assert path.is_symlink() and not target.exists()
        records.append({"later_write_failure": "earlier change remains reported"})
    return records


if __name__ == "__main__":
    run_this_suite(__file__)
