"""Cache selection and permissions through public local sessions."""

import json
import os
import shutil
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.python.test_without_r import environment
from support.assertions import wait_for_worker_ready
from support.client import McpClient
from support.checkpoints import FifoCheckpoint
from support.normalization import code
from support.python import write_test_wheel
from support.records import Transcript
from support.requirements import LINUX_SANDBOX, R, SANDBOX, command, requires
from support.r import r_test_environment
from support.resolvers import preseeded_duckdb_python, probing_uv


CACHE_VARIABLES = (
    "XDG_DATA_HOME",
    "UV_CACHE_DIR",
    "UV_PYTHON_INSTALL_DIR",
    "UV_PYTHON_BIN_DIR",
    "UV_TOOL_DIR",
    "UV_TOOL_BIN_DIR",
    "IR_CACHE_DIR",
    "IR_LIBRARY_ROOT",
    "R_USER_CACHE_DIR",
    "R_USER_DATA_DIR",
    "RENV_PATHS_ROOT",
    "RENV_PATHS_CACHE",
    "RENV_PATHS_SOURCE",
    "RENV_PATHS_BINARY",
    "PKG_CACHE_DIR",
    "R_PKG_CACHE_DIR",
    "MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY",
    "PYTHONPYCACHEPREFIX",
    "PYTHONUSERBASE",
)


@requires(LINUX_SANDBOX, command("uv"), command("rustc"))
def test_overlapping_resolvers_share_writable_cache(
    binary: Path,
) -> Transcript:
    for host in (True, False):
        with TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory).resolve()
            (root / "bin").mkdir()
            python = preseeded_duckdb_python(root)
            cache = root / "cache/dependencies"
            cache.mkdir(parents=True)
            probing_uv(
                root,
                python,
                # fmt: python
                code("""
                    import os
                    from pathlib import Path

                    if "MCP_CONSOLE_LOCAL_RUNTIME" not in os.environ:
                        state = Path(os.environ["CACHE_TEST_STATE"])
                        if not (state / "seen").exists():
                            (state / "seen").touch()
                            with (state / "started").open("wb", buffering=0) as ready:
                                ready.write(b"1")
                            with (state / "release").open("rb", buffering=0) as release:
                                assert release.read(1) == b"1"
                        cache = Path(os.environ["CACHE_TEST_SHARED"])
                        for name in (".git", ".agents", ".codex", ".aws"):
                            directory = cache / name
                            directory.mkdir(exist_ok=True)
                            directory.joinpath(state.name).write_text("prepared")
                        cache.joinpath("ordinary-cache-file").write_text("prepared")
                        temporary = Path(os.environ["TMPDIR"])
                        assert temporary.is_relative_to(os.environ["CACHE_TEST_TEMP"])
                        temporary.joinpath("temporary-file").write_text("private")
                    """),
            )
            clients = []
            for name in ("first", "second"):
                workspace = root / name
                workspace.mkdir()
                temporary = workspace / "temporary"
                temporary.mkdir()
                state = cache / name
                state.mkdir()
                started = FifoCheckpoint.create(state / "started")
                release = FifoCheckpoint.create(state / "release")
                stack.callback(started.close)
                stack.callback(release.close)
                env = environment(root / "bin")
                env.update({key: str(cache / "tools" / key) for key in CACHE_VARIABLES})
                env.update(
                    XDG_CACHE_HOME=str(cache),
                    UV_CACHE_DIR=str(cache),
                    TMPDIR=str(temporary),
                    CACHE_TEST_TEMP=str(temporary),
                    CACHE_TEST_STATE=str(state),
                    CACHE_TEST_SHARED=str(cache),
                )
                config = workspace / "config.yaml"
                config.write_text(
                    json.dumps(
                        {
                            "python": {"managed": {}},
                            "cache": "host"
                            if host
                            else {"directory": str(cache.parent)},
                            "resolver": {"sandbox": {"network": "enabled"}},
                        }
                    )
                )
                client = stack.enter_context(
                    McpClient(
                        binary,
                        ("serve", "--config-file", str(config)),
                        env,
                        workspace,
                    )
                )
                client.initialize_and_list_tools()
                started.wait("resolver reached shared cache", timeout=60, client=client)
                clients.append((client, release))
            for name in (".git", ".agents", ".codex", ".aws"):
                assert not (cache / name).exists(), f"sandbox created {name}"
            first, first_release = clients[0]
            second, second_release = clients[1]
            first_release.release()
            first.expect("ready\n", python="print('ready')")
            first.finish()
            # The second resolver is still inside the native sandbox. Retiring
            # the first must preserve its data and the survivor's full grant.
            for name in (".git", ".agents", ".codex", ".aws"):
                assert (cache / name / "first").read_text() == "prepared"
            second_release.release()
            second.expect("ready\n", python="print('ready')")
            second.finish()
            for name in (".git", ".agents", ".codex", ".aws"):
                for owner in ("first", "second"):
                    assert (cache / name / owner).read_text() == "prepared"
    return [{"overlapping_resolvers_retain_full_host_and_managed_cache_writes": True}]


@requires(SANDBOX, command("uv"), command("rustc"))
def test_default_caches_are_console_owned(binary: Path) -> Transcript:
    return cache_locations(
        binary, host=False, sources=("default", "platform", "isolated")
    )


@requires(SANDBOX, command("uv"), command("rustc"))
def test_absolute_xdg_cache_starts_without_home(binary: Path) -> Transcript:
    return cache_locations(binary, host=False, sources=("xdg_without_home",))


@requires(SANDBOX, command("uv"), command("rustc"))
def test_console_cache_directory_override(binary: Path) -> Transcript:
    return cache_locations(
        binary,
        host=False,
        sources=("default", "resolver", "isolated", "without_home", "explicit_entries"),
        cache_override=True,
    )


@requires(SANDBOX, command("uv"), command("rustc"))
def test_configured_cache_directory_overrides_environment(binary: Path) -> Transcript:
    return cache_locations(
        binary,
        host=False,
        sources=("path", "path_cli", "path_isolated"),
        cache_override=True,
    )


@requires(SANDBOX, command("uv"), command("rustc"))
def test_empty_console_cache_directory_uses_default(binary: Path) -> Transcript:
    return cache_locations(binary, host=False, sources=("empty",))


@requires(SANDBOX, command("uv"), command("rustc"))
def test_host_cache_ignores_console_cache_directory(binary: Path) -> Transcript:
    return cache_locations(
        binary,
        host=True,
        sources=("direct", "config", "cli"),
        cache_override=True,
    )


def test_console_cache_directory_requires_absolute_path(binary: Path) -> Transcript:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        result = subprocess.run(
            [binary, "serve", "--no-config"],
            cwd=root,
            env=dict(
                os.environ,
                MCP_CONSOLE_CACHE_DIR="relative-cache",
                MCP_CONSOLE_HOME=str(root / "home"),
            ),
            input="",
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 1, result
        assert result.stderr == "MCP_CONSOLE_CACHE_DIR must be an absolute path\n", (
            result
        )
        assert not result.stdout
        assert not (root / "relative-cache").exists()
    return [{"relative_console_cache_directory_rejected": True}]


@requires(SANDBOX, command("uv"), command("rustc"))
def test_console_root_is_created_with_explicit_entries(binary: Path) -> Transcript:
    return cache_locations(binary, host=False, sources=("explicit_entries",))


@requires(SANDBOX, command("uv"), command("rustc"))
def test_host_cache_opt_out(binary: Path) -> Transcript:
    return cache_locations(binary, host=True, sources=("direct", "config", "cli"))


@requires(SANDBOX, command("uv"), command("rustc"))
def test_host_matplotlib_cache_uses_platform_default(binary: Path) -> Transcript:
    return cache_locations(binary, host=True, sources=("platform",))


@requires(SANDBOX)
def test_selected_python_preserves_user_site_packages(binary: Path) -> Transcript:
    with TemporaryDirectory(prefix="console-user-site-", dir=Path.home()) as directory:
        root = Path(directory).resolve()
        selected = Path(sys._base_executable)
        env = environment(root)
        user_base = root / "user-base"
        env.update(
            PYTHONUSERBASE=str(user_base),
            XDG_CACHE_HOME=str(root / "cache"),
            UV_CACHE_DIR=str(root / "host-uv"),
        )
        env.pop("PYTHONNOUSERSITE", None)
        user_site = Path(
            subprocess.check_output(
                [selected, "-c", "import site; print(site.getusersitepackages())"],
                env=env,
                text=True,
            ).strip()
        )
        assert user_site.is_relative_to(user_base), user_site
        user_site.mkdir(parents=True)
        module = user_site / "console_user_site_package.py"
        module.write_text("answer = 42\n")
        workspace = root / "workspace"
        workspace.mkdir()
        config = workspace / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        for source in ("config", "environment"):
            settings = {}
            if source == "config":
                settings["python"] = str(selected)
                env.pop("RETICULATE_PYTHON", None)
            else:
                env["RETICULATE_PYTHON"] = str(selected)
            config.write_text(json.dumps(settings))
            for host in (True, False):
                expected_cache = (
                    root / "host-uv"
                    if host
                    else root / "cache/mcp-console/dependencies/uv/cache"
                )
                arguments = ["serve", "-c", "cache=host"] if host else ["serve"]
                with McpClient(binary, arguments, env, workspace) as client:
                    client.initialize_and_list_tools()
                    for restart in (False, True):
                        if restart:
                            client.send(control="restart")
                        client.expect(
                            "preinstalled user-site package retained\n",
                            # fmt: python
                            python=code(f"""
                                import errno
                                import os
                                import site
                                from pathlib import Path
                                import console_user_site_package as package

                                assert package.answer == 42
                                assert site.ENABLE_USER_SITE
                                assert Path(site.getusersitepackages()) == Path({str(user_site)!r})
                                assert os.environ["PYTHONUSERBASE"] == {str(user_base)!r}
                                assert Path(os.environ["UV_CACHE_DIR"]) == Path({str(expected_cache)!r})
                                try:
                                    Path(package.__file__).write_text("answer = -1\\n")
                                except OSError as error:
                                    assert error.errno in (errno.EACCES, errno.EPERM, errno.EROFS), error
                                else:
                                    raise AssertionError("worker wrote to the host user site")
                                print("preinstalled user-site package retained")
                                """),
                        )
                    client.finish()
                assert module.read_text() == "answer = 42\n"
    return [
        {"selected_python_user_site_retained": True, "host_user_site_read_only": True}
    ]


@requires(SANDBOX, command("uv"), command("rustc"))
def test_resolver_cannot_write_companion_build_cache(binary: Path) -> Transcript:
    cache_locations(binary, host=False, sources=("default", "platform"))
    cache_locations(binary, host=True, sources=("cli",))
    return [{"companion_build_cache_read_only": True}]


def test_console_cache_rejects_unsandboxed_execution(binary: Path) -> Transcript:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        result = subprocess.run(
            [binary, "serve", "--no-sandbox", "-c", "cache=console"],
            cwd=root,
            env=dict(
                os.environ,
                XDG_CACHE_HOME=str(root / "cache-base"),
                MCP_CONSOLE_HOME=str(root / "home"),
            ),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1, result
        assert (
            result.stderr
            == "Console caches require sandboxing; use cache: host with --no-sandbox\n"
        )
        assert not result.stdout
        assert not (root / "cache-base").exists()
    return [{"unsandboxed_console_cache_rejected": True}]


@requires(SANDBOX, R, command("ir"), command("uv"))
def test_uses_reticulate_managed_uv_inside_console_cache(binary: Path) -> Transcript:
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        env, _ = r_test_environment()
        env.update(
            XDG_CACHE_HOME=str(root / "cache-base"),
            RETICULATE_UV="managed",
            MCP_CONSOLE_LANGUAGES="r",
        )
        uv = root / "cache-base/mcp-console/dependencies/R/reticulate/uv/bin/uv"
        if sys.platform == "darwin":
            # macOS's system shell writes heredoc files outside TMPDIR. Seed the
            # tool here; Linux exercises the cold managed installer as well.
            uv.parent.mkdir(parents=True)
            shutil.copy2(shutil.which("uv"), uv)
        with McpClient(binary, ("serve",), env, root) as client:
            client.initialize_and_list_tools()
            wait_for_worker_ready(client, "reticulate managed uv readiness")
            client.expect("[prepared]", requirements={"r": ["DBI"]})
            client.expect(
                "managed uv is Console owned\n",
                # fmt: r
                r=code(r"""
                    uv <- get("uv_binary", asNamespace("reticulate"))()
                    root <- paste0(normalizePath(Sys.getenv("R_USER_CACHE_DIR")), "/")
                    stopifnot(startsWith(normalizePath(uv), root))
                    cat("managed uv is Console owned\n")
                    """),
            )
            client.finish()
        assert uv.is_file(), uv
    return [{"reticulate_managed_uv_is_console_owned": True}]


@requires(SANDBOX)
def test_managed_python_and_duckdb_stay_in_console_cache(binary: Path) -> Transcript:
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        tools = root / "bin"
        tools.mkdir()
        (tools / "uv").symlink_to(shutil.which("uv"))
        env = environment(tools)
        env.pop("UV_PYTHON", None)
        env["UV_PYTHON_PREFERENCE"] = "only-managed"
        env["XDG_CACHE_HOME"] = str(root / "cache-base")
        cache = root / "custom cache/dependencies"
        env["CACHE_TEST_ROOT"] = str(cache)
        config = root / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"cache": {"directory": str(cache.parent)}}))
        for name in CACHE_VARIABLES:
            env[name] = str(root / "host" / name)
        with McpClient(binary, ("serve",), env, root) as client:
            client.initialize_and_list_tools()
            for restart in (False, True):
                if restart:
                    client.send(control="restart")
                # fmt: python
                check = code("""
                    import os
                    import sys
                    from pathlib import Path

                    root = Path(os.environ["CACHE_TEST_ROOT"])
                    assert Path(sys.executable).resolve().is_relative_to(root)
                    assert Path(sys.base_prefix).resolve().is_relative_to(root)
                    assert Path(sys.pycache_prefix).is_relative_to(root)
                    assert Path(os.environ["PYTHONUSERBASE"]).is_relative_to(root)
                    import numpy, pandas, duckdb

                    for package in (numpy, pandas, duckdb):
                        assert Path(package.__file__).resolve().is_relative_to(root)
                    print("managed installation is Console owned")
                    """)
                client.expect("managed installation is Console owned\n", python=check)
                result = client.send(
                    sql="SELECT current_setting('extension_directory') AS directory"
                )
                assert not result.get("isError"), result
                assert str(cache / "duckdb/extensions") in json.dumps(result), result
            client.send(requirements={"python": ["six"]})
            client.expect("six\n", python="import six; print(six.__name__)")
            result = client.send(sql="LOAD sqlite")
            assert not result.get("isError"), result
            client.finish()
        assert list(
            (cache / "duckdb/extensions").glob("v*/**/sqlite_scanner.duckdb_extension")
        )
        assert not (root / "host").exists()
        assert not (root / "cache-base").exists()
    return [
        {
            "managed_python": True,
            "live_requirements": True,
            "duckdb": True,
            "host_caches_untouched": True,
        }
    ]


@requires(SANDBOX, command("uv"), command("rustc"))
def test_resolver_cache_roots_allow_metadata_writes(binary: Path) -> Transcript:
    for host, source in ((False, "environment"), (True, "config")):
        cache_locations(binary, host=host, sources=(source,), metadata=True)
    return [{"resolver_cache_root_metadata_writable": True}]


@requires(SANDBOX, command("git"), command("uv"), command("rustc"))
def test_uv_git_cache_works_without_root_metadata_grants(binary: Path) -> Transcript:
    with TemporaryDirectory(prefix="console-git-cache-", dir=Path.home()) as temporary:
        root = Path(temporary).resolve()
        source = root / "repository"
        source.mkdir()
        write_test_wheel(source, "console_git_cache_fixture", "answer = 42\n")
        (source / "pyproject.toml").write_text(
            '[build-system]\nrequires = []\nbuild-backend = "backend"\nbackend-path = ["."]\n'
        )
        # Build the Git dependency inside the selected cache, including metadata.
        backend = (
            # fmt: python
            code("""
                from pathlib import Path
                import os
                import shutil


                def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
                    assert (
                        Path.cwd().resolve().is_relative_to(Path(os.environ["UV_CACHE_DIR"]).resolve())
                    )
                    Path(".git/console-cache-probe").write_text("Git checkout metadata is writable")
                    wheel = next(Path("wheels").glob("*.whl"))
                    shutil.copyfile(wheel, Path(wheel_directory) / wheel.name)
                    return wheel.name
                """)
        )
        (source / "backend.py").write_text(backend)
        for arguments in (
            ("init", "--quiet"),
            ("add", "."),
            (
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "--quiet",
                "-m",
                "Git dependency fixture",
            ),
        ):
            subprocess.run(
                ["git", "-C", source, *arguments], check=True, capture_output=True
            )
        revision = subprocess.check_output(
            ["git", "-C", source, "rev-parse", "HEAD"], text=True
        ).strip()
        for host, source_kind in ((False, "environment"), (True, "config")):
            cache_locations(
                binary,
                host=host,
                sources=(source_kind,),
                git_dependency=f"git+{source.as_uri()}@{revision}",
            )
    return [{"console_and_host_caches": True, "uv_git_build_metadata_writable": True}]


def cache_locations(
    binary: Path,
    *,
    host: bool,
    sources: tuple[str, ...],
    metadata: bool = False,
    git_dependency: str | None = None,
    cache_override: bool = False,
) -> Transcript:
    for source in sources:
        # Host cache mode grants Darwin's user temp. Keep protected companion
        # artifacts outside that grant, as the actual host build cache is.
        with TemporaryDirectory(
            prefix="mcp-console-cache-locations-", dir=Path.home()
        ) as directory:
            root = Path(directory).resolve()
            tools = root / "bin"
            tools.mkdir()
            if git_dependency is not None:
                # Git's submodule helper needs these utilities; keep R hidden.
                for name in ("git", "basename", "sed", "uname"):
                    (tools / name).symlink_to(shutil.which(name))
                (tools / "uv").symlink_to(shutil.which("uv"))
            python = preseeded_duckdb_python(root)
            env = environment(tools)
            env.pop("MCP_CONSOLE_CACHE_DIR", None)
            env["XDG_CACHE_HOME"] = str(root / "cache-base")
            env["CACHE_TEST_ROOT"] = str(root)
            for name in CACHE_VARIABLES:
                env[name] = str(root / "host" / name)
            config = root / ".agents/console/config.yaml"
            config.parent.mkdir(parents=True)
            settings = {"python": {"managed": {}}}
            console_base = root / "cache-base/mcp-console"
            if source == "platform":
                env.pop("XDG_CACHE_HOME")
                home = root / "account"
                settings["resolver"] = {"environment": {"HOME": str(home)}}
                if host:
                    settings["cache"] = "host"
                    env.pop("MPLCONFIGDIR", None)
                    env["CACHE_TEST_MATPLOTLIB"] = str(
                        home
                        / (
                            ".matplotlib"
                            if sys.platform == "darwin"
                            else ".cache/matplotlib"
                        )
                    )
                console_base = home / (
                    "Library/Caches/mcp-console"
                    if sys.platform == "darwin"
                    else ".cache/mcp-console"
                )
            companion = console_base / "sandbox/fixture/revision/source"
            companion_files = (
                companion / ".git/config",
                companion / "target/release/mcp-console-sandbox",
            )
            for file in companion_files:
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text("trusted companion cache")
            env["CACHE_TEST_COMPANION_FILES"] = json.dumps(
                [str(file) for file in companion_files]
            )
            if cache_override:
                console_base = root / "custom cache"
                env["MCP_CONSOLE_CACHE_DIR"] = str(console_base)
                if source == "resolver":
                    settings["resolver"] = {
                        "environment": {"MCP_CONSOLE_CACHE_DIR": str(console_base)}
                    }
                    env["MCP_CONSOLE_CACHE_DIR"] = str(root / "ignored-cache")
                elif source in ("path", "path_cli", "path_isolated"):
                    settings["cache"] = {"directory": str(console_base)}
                    env["MCP_CONSOLE_CACHE_DIR"] = str(root / "ignored-cache")
            elif source == "empty":
                env["MCP_CONSOLE_CACHE_DIR"] = ""
            console_root = console_base / "dependencies"
            if source == "config":
                settings["cache"] = "host"
            if source in ("isolated", "path_isolated"):
                settings.update(inherit_environment=False, environment=dict(env))
            if source == "xdg_without_home":
                env.pop("HOME", None)
            if source == "without_home":
                for name in ("HOME", "USERPROFILE", "LOCALAPPDATA", "XDG_CACHE_HOME"):
                    env.pop(name, None)
            if source == "explicit_entries":
                settings["resolver"] = {
                    "sandbox": {
                        "filesystem": {
                            "read_only": ["/"],
                            "read_write": [str(console_root)],
                        }
                    }
                }
                assert not console_root.exists()
            config.write_text(json.dumps(settings))
            expected = {
                name: env[name] if host else str(console_root)
                for name in CACHE_VARIABLES
            }
            env["CACHE_TEST_EXPECTED"] = json.dumps(expected)
            if metadata:
                metadata_root = Path(env["UV_CACHE_DIR"]) if host else console_root
                env["CACHE_TEST_METADATA_ROOT"] = str(metadata_root)
                for name in (".git", ".agents", ".codex", ".aws"):
                    directory = metadata_root / name
                    directory.mkdir(parents=True)
                    (directory / "keep").write_text("cache contents")
            if source in ("isolated", "path_isolated"):
                settings["environment"]["CACHE_TEST_EXPECTED"] = env[
                    "CACHE_TEST_EXPECTED"
                ]
                config.write_text(json.dumps(settings))
            # Existing interpreter hooks use worker permissions. Probe trusted
            # preparation through uv, with the real DuckDB provider preseeded.
            # fmt: python
            probe = code(f"""
                import errno
                import json
                import os
                import subprocess
                import sys
                from pathlib import Path

                if "MCP_CONSOLE_LOCAL_RUNTIME" not in os.environ and "GIT_CACHE_PROBE_RUNNING" not in os.environ:
                    expected = json.loads(os.environ["CACHE_TEST_EXPECTED"])
                    for name, value in expected.items():
                        actual = Path(os.environ[name])
                        assert actual == Path(value) if {host!r} else actual.is_relative_to(value), (name, actual, value)
                    cache = Path(os.environ["UV_CACHE_DIR"])
                    cache.mkdir(parents=True, exist_ok=True)
                    if not {host!r}:
                        try:
                            Path(os.environ["CACHE_TEST_ROOT"]).joinpath("host-write").write_text("escaped")
                        except OSError as error:
                            assert error.errno in (errno.EACCES, errno.EPERM, errno.EROFS), error
                        else:
                            raise AssertionError("resolver wrote outside Console caches")
                    if {source != "direct"!r}:
                        for file in json.loads(os.environ["CACHE_TEST_COMPANION_FILES"]):
                            file = Path(file)
                            assert file.read_text() == "trusted companion cache"
                            try:
                                file.write_text("resolver overwrote companion cache")
                            except OSError as error:
                                assert error.errno in (errno.EACCES, errno.EPERM, errno.EROFS), error
                            else:
                                raise AssertionError("resolver wrote to companion build cache")
                    cache.joinpath("resolver-probe").write_text("prepared")
                    if {host and source == "platform"!r}:
                        matplotlib = Path(os.environ["CACHE_TEST_MATPLOTLIB"])
                        matplotlib.mkdir(parents=True, exist_ok=True)
                        matplotlib.joinpath("resolver-probe").write_text("prepared")
                    if {metadata!r}:
                        for name in (".git", ".agents", ".codex", ".aws"):
                            file = Path(os.environ["CACHE_TEST_METADATA_ROOT"]) / name / "keep"
                            assert file.read_text() == "cache contents"
                            file.write_text("updated")
                            assert file.read_text() == "updated"
                            file.write_text("cache contents")
                            nested = file.parent / ".git"
                            nested.mkdir(exist_ok=True)
                            nested.joinpath("new").write_text("created")
                            nested.joinpath("new").unlink()
                        cache.joinpath("metadata-check-passed").write_text("checked")
                    if {git_dependency is not None!r}:
                        result = subprocess.run(
                            [
                                "uv", "--no-config", "--cache-dir", str(cache),
                                "run", "--no-project", "--python", sys.executable,
                                "--with", {git_dependency!r}, "--", "python", "-c",
                                "import console_git_cache_fixture; print(console_git_cache_fixture.answer)",
                            ],
                            env={{**os.environ, "GIT_CACHE_PROBE_RUNNING": "1", "UV_NO_CACHE": "false"}},
                            capture_output=True, text=True,
                        )
                        cache.joinpath("git-output").write_text(result.stdout + result.stderr)
                        assert result.returncode == 0, result
                        assert result.stdout == "42\\n", result
                        cache.joinpath("git-check-passed").write_text("checked")
                """)
            probing_uv(root, python, probe)
            arguments = ["serve"]
            if source == "direct":
                arguments.append("--no-sandbox")
            elif source == "cli":
                arguments.extend(["-c", "cache=host"])
            elif source == "path_cli":
                settings["cache"] = "host"
                config.write_text(json.dumps(settings))
                arguments.extend(
                    ["-c", f"cache.directory={json.dumps(str(console_base))}"]
                )
            with McpClient(binary, arguments, env, root) as client:
                client.initialize_and_list_tools()
                if not host:
                    assert console_root.is_dir(), console_root
                for restart in (False, True):
                    if restart:
                        config.write_text(
                            "cache: host\n" if not host else "cache: console\n"
                        )
                        client.send(control="restart")
                    # fmt: python
                    check = code(f"""
                        import json
                        import os
                        from pathlib import Path

                        expected = json.loads(os.environ["CACHE_TEST_EXPECTED"])
                        # Extension preparation has its own cache coverage.
                        expected.pop("MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY")
                        for name, value in expected.items():
                            actual = Path(os.environ[name])
                            assert actual == Path(value) if {host!r} else actual.is_relative_to(value), (name, actual, value)
                        assert Path(os.environ["UV_CACHE_DIR"]).joinpath("resolver-probe").read_text() == "prepared"
                        if {metadata!r}:
                            assert Path(os.environ["UV_CACHE_DIR"]).joinpath("metadata-check-passed").read_text() == "checked"
                        if {git_dependency is not None!r}:
                            assert Path(os.environ["UV_CACHE_DIR"]).joinpath("git-check-passed").exists(), Path(os.environ["UV_CACHE_DIR"]).joinpath("git-output").read_text()
                            assert Path(os.environ["UV_CACHE_DIR"]).joinpath("git-check-passed").read_text() == "checked"
                        print("cache selection retained")
                        """)
                    client.expect("cache selection retained\n", python=check)
                client.finish()
            assert not (root / "host-write").exists()
            if metadata:
                for name in (".git", ".agents", ".codex", ".aws"):
                    assert (
                        metadata_root / name / "keep"
                    ).read_text() == "cache contents"
            if host and source == "platform":
                assert (
                    Path(env["CACHE_TEST_MATPLOTLIB"])
                    .joinpath("resolver-probe")
                    .read_text()
                    == "prepared"
                )
            for file in companion_files:
                assert file.read_text() == "trusted companion cache", file
            if cache_override:
                assert not (root / "ignored-cache").exists()
                if host:
                    assert not console_base.exists()
                else:
                    assert not (root / "cache-base/mcp-console/dependencies").exists()
            if not host:
                assert not (root / "host").exists()
    return [
        {
            "host_caches" if host else "console_caches": True,
            "restart_retains_selection": True,
        }
    ]
