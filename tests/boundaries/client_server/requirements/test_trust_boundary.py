"""Worker-controlled resolver inputs remain within the captured native policy."""

import json
import os
from collections.abc import Iterator
from contextlib import chdir, contextmanager
from pathlib import Path
import shlex
import shutil
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.normalization import code
from support.records import Transcript
from support.r import r_test_environment
from support.resolvers import ir_cache_directory
from support.requirements import POSIX, R, SANDBOX, command, requires
from boundaries.client_server.python.test_without_r import environment


PROBE = Path(__file__).resolve().parents[3] / "fixtures/resolver/trust_probe.py"
EVIDENCE = {
    "host_read": True,
    "direct_write_denied": True,
    "symlink_write_denied": True,
    "download_denied": True,
}


def selected_resolver(name: str) -> str:
    executable = shutil.which(name)
    assert executable is not None
    return str(Path(executable).resolve())


@contextmanager
def relative_resolver_path(root: Path, name: str) -> Iterator[None]:
    tools = root / "caller-bin"
    tools.mkdir()
    (tools / name).symlink_to(selected_resolver(name))
    with (
        chdir(root),
        patch.dict(
            os.environ,
            PATH=os.pathsep.join((tools.name, os.environ["PATH"])),
        ),
    ):
        yield


def workspace(root: Path) -> tuple[Path, dict[str, str]]:
    working = root / "workspace"
    tools = working / "bin"
    tools.mkdir(parents=True)
    uv = selected_resolver("uv")
    (tools / "uv").symlink_to(uv)
    protected = root / "protected"
    protected.mkdir()
    (protected / "canary").write_text("preserved")
    # These probes own uv configuration. Exclude its whole inherited namespace
    # so new uv settings cannot silently become fixture inputs.
    env = dict(
        {
            name: value
            for name, value in environment(tools).items()
            if not name.startswith("UV_") and "proxy" not in name.lower()
        },
        XDG_CACHE_HOME=str(root / "cache"),
        MCP_CONSOLE_TEST_PROTECTED=str(protected),
        MCP_CONSOLE_TEST_ALIAS=str(working / "escape"),
        MCP_CONSOLE_TEST_RESOLVER=uv,
        UV_HTTP_TIMEOUT="37",
        UV_INDEX_URL="https://pypi.org/simple",
        UV_NO_CONFIG="1",
        UV_PYTHON_PREFERENCE="only-managed",
    )
    return working, env


def replacement_resolver(working: Path, name: str) -> None:
    python = working / "python with spaces"
    python.symlink_to(sys.executable)
    program = working / f"{name}.py"
    program.write_text(PROBE.read_text())
    replacement = working / name
    replacement.write_text(
        f'#!/bin/sh\nexec {shlex.quote(str(python))} {shlex.quote(str(program))} "$@"\n'
    )
    replacement.chmod(0o755)


@requires(POSIX, SANDBOX, command("uv"))
def test_worker_replaces_selected_uv_wrapper(binary: Path) -> Transcript:
    # A home-relative fixture stays outside macOS's writable user temp directory.
    with (
        TemporaryDirectory(prefix="resolver-trust-", dir=Path.home()) as directory,
        relative_resolver_path(Path(directory), "uv"),
        patch.dict(
            os.environ,
            MCP_CONSOLE_HOME=str(Path(directory) / "caller-console"),
            # Representative hostile inputs; the unknown sentinel checks the
            # namespace rule without maintaining a catalog of uv settings.
            UV_ENV_FILE=str(Path(directory) / "missing.env"),
            UV_NO_ENV_FILE="0",
            UV_EXCLUDE_NEWER="2000-01-01",
            UV_MCP_CONSOLE_TEST_INHERITED="caller setting",
            UV_PYTHON_INSTALL_MIRROR=(
                Path(directory) / "missing-python-mirror"
            ).as_uri(),
        ),
    ):
        root = Path(directory).resolve()
        caller_config = root / "caller-console/config.yaml"
        caller_config.parent.mkdir()
        caller_config.write_text("{")
        working, env = workspace(root)
        replacement_resolver(working, "replacement-uv")
        with McpClient(
            binary, ("serve", "--writable-root", str(working)), env, working
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "worker replaced selected uv\n",
                # fmt: python
                python=code("""
                    import os
                    from pathlib import Path

                    Path("escape").symlink_to(os.environ["MCP_CONSOLE_TEST_PROTECTED"])
                    selected = Path("bin/uv")
                    selected.unlink()
                    selected.symlink_to(Path("replacement-uv").resolve())
                    os.environ["UV_HTTP_TIMEOUT"] = "1"
                    os.environ["UV_CONFIG_FILE"] = "/missing/worker-config"
                    print("worker replaced selected uv")
                    """),
            )
            prepared = client.send(control="restart", requirements={"python": ["six"]})
            assert not prepared.get("isError"), prepared
            client.expect(
                "prepared after replacement\n",
                python="import six; print('prepared after replacement')",
            )
            client.finish()
        assert (
            json.loads(next((root / "cache").rglob("trust-probe.json")).read_text())
            == EVIDENCE
        )
        assert not (root / "protected/escaped").exists()
    return [{"worker_replaced_selected_wrapper": True, **EVIDENCE}]


@requires(POSIX, SANDBOX, command("uv"))
def test_worker_replaces_uv_configuration_and_wheel(binary: Path) -> Transcript:
    with (
        TemporaryDirectory(prefix="resolver-trust-", dir=Path.home()) as directory,
        patch.dict(os.environ, UV_NO_BINARY_PACKAGE="mcp-console-trust-probe"),
    ):
        root = Path(directory).resolve()
        working, env = workspace(root)
        (working / "probe-source.py").write_text(PROBE.read_text())
        config = working / "uv.toml"
        config.write_text("")
        env.pop("UV_NO_CONFIG")
        env["UV_CONFIG_FILE"] = str(config)
        with McpClient(
            binary, ("serve", "--writable-root", str(working)), env, working
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "worker supplied package startup code\n",
                # fmt: python
                python=code("""
                    import json
                    import os
                    from pathlib import Path
                    import zipfile

                    Path("escape").symlink_to(os.environ["MCP_CONSOLE_TEST_PROTECTED"])
                    wheels = Path("wheels")
                    wheels.mkdir()
                    dist = "mcp_console_trust_probe-1.0.0.dist-info"
                    entries = {
                        "mcp_console_trust_probe.py": Path("probe-source.py").read_text(),
                        "mcp_console_trust_probe.pth": "import mcp_console_trust_probe\\n",
                        f"{dist}/METADATA": "Metadata-Version: 2.3\\nName: mcp-console-trust-probe\\nVersion: 1.0.0\\n",
                        f"{dist}/WHEEL": "Wheel-Version: 1.0\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n",
                    }
                    entries[f"{dist}/RECORD"] = "\\n".join(f"{entry},," for entry in entries) + "\\n"
                    with zipfile.ZipFile(
                        wheels / "mcp_console_trust_probe-1.0.0-py3-none-any.whl", "w"
                    ) as wheel:
                        for name, contents in entries.items():
                            wheel.writestr(name, contents)
                    Path("uv.toml").write_text(
                        "find-links = [" + json.dumps(str(wheels.resolve())) + "]\\n"
                    )
                    os.environ["UV_CONFIG_FILE"] = "/missing/worker-config"
                    print("worker supplied package startup code")
                    """),
            )
            prepared = client.send(
                control="restart",
                requirements={"python": ["mcp-console-trust-probe==1.0.0"]},
            )
            assert not prepared.get("isError"), prepared
            client.expect(
                "worker package prepared\n",
                python="import mcp_console_trust_probe; print('worker package prepared')",
            )
            client.finish()
        assert (
            json.loads(next((root / "cache").rglob("trust-probe.json")).read_text())
            == EVIDENCE
        )
        assert not (root / "protected/escaped").exists()
    return [{"worker_replaced_configuration_and_wheel": True, **EVIDENCE}]


@requires(POSIX, SANDBOX, command("uv"))
def test_worker_poisons_explicitly_writable_python_cache(binary: Path) -> Transcript:
    with TemporaryDirectory(prefix="resolver-trust-", dir=Path.home()) as directory:
        root = Path(directory).resolve()
        working, env = workspace(root)
        (working / "probe-source.py").write_text(PROBE.read_text())
        caches = root / "cache/mcp-console/dependencies"
        caches.mkdir(parents=True)
        env["MCP_CONSOLE_TEST_ALIAS"] = str(caches / "uv/cache/escape")
        with McpClient(
            binary,
            ("serve", "--writable-root", str(working), "--writable-root", str(caches)),
            env,
            working,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "worker poisoned explicitly writable Python cache\n",
                # fmt: python
                python=code("""
                    import os
                    from pathlib import Path
                    import sys
                    import sysconfig

                    cache = Path(os.environ["UV_CACHE_DIR"])
                    assert Path(sys.base_prefix).is_relative_to(cache.parent)
                    (cache / "escape").symlink_to(os.environ["MCP_CONSOLE_TEST_PROTECTED"])
                    site = Path(
                        sysconfig.get_path(
                            "purelib", vars={"base": sys.base_prefix, "platbase": sys.base_prefix}
                        )
                    )
                    (site / "sitecustomize.py").write_text(Path("probe-source.py").read_text())
                    print("worker poisoned explicitly writable Python cache")
                    """),
            )
            prepared = client.send(control="restart", requirements={"python": ["six"]})
            assert not prepared.get("isError"), prepared
            client.expect(
                "poisoned cache remained contained\n",
                python="import six; print('poisoned cache remained contained')",
            )
            client.finish()
        assert (
            json.loads(next((root / "cache").rglob("trust-probe.json")).read_text())
            == EVIDENCE
        )
        assert not (root / "protected/escaped").exists()
    return [
        {
            "explicit_worker_cache_write_grant": True,
            "poisoned_python_startup": True,
            **EVIDENCE,
        }
    ]


@requires(POSIX, SANDBOX, command("uv"))
def test_worker_supplies_package_build_backend(binary: Path) -> Transcript:
    with (
        TemporaryDirectory(prefix="resolver-trust-", dir=Path.home()) as directory,
        patch.dict(os.environ, UV_NO_BUILD="1"),
    ):
        root = Path(directory).resolve()
        working, env = workspace(root)
        for name in ("trust_probe.py", "build_backend.py"):
            (working / name).write_text(PROBE.with_name(name).read_text())
        config = working / "uv.toml"
        config.write_text("")
        env.pop("UV_NO_CONFIG")
        env["UV_CONFIG_FILE"] = str(config)
        with McpClient(
            binary, ("serve", "--writable-root", str(working)), env, working
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "worker supplied build backend\n",
                # fmt: python
                python=code("""
                    import io
                    import json
                    import os
                    from pathlib import Path
                    import tarfile

                    Path("escape").symlink_to(os.environ["MCP_CONSOLE_TEST_PROTECTED"])
                    sources = Path("sources")
                    sources.mkdir()
                    entries = {
                        "pyproject.toml": '[build-system]\\nrequires = []\\nbuild-backend = "build_backend"\\nbackend-path = ["."]\\n',
                        "trust_probe.py": Path("trust_probe.py").read_text(),
                        "build_backend.py": Path("build_backend.py").read_text(),
                        "PKG-INFO": "Metadata-Version: 2.3\\nName: mcp-console-build-probe\\nVersion: 1.0.0\\n",
                    }
                    with tarfile.open(sources / "mcp_console_build_probe-1.0.0.tar.gz", "w:gz") as archive:
                        for name, contents in entries.items():
                            payload = contents.encode()
                            member = tarfile.TarInfo("mcp_console_build_probe-1.0.0/" + name)
                            member.size = len(payload)
                            archive.addfile(member, io.BytesIO(payload))
                    Path("uv.toml").write_text(
                        "find-links = [" + json.dumps(str(sources.resolve())) + "]\\n"
                    )
                    print("worker supplied build backend")
                    """),
            )
            prepared = client.send(
                control="restart",
                requirements={"python": ["mcp-console-build-probe==1.0.0"]},
            )
            assert not prepared.get("isError"), prepared
            client.expect(
                "42\n",
                python="import mcp_console_build_probe; print(mcp_console_build_probe.answer)",
            )
            client.finish()
        assert (
            json.loads(next((root / "cache").rglob("trust-probe.json")).read_text())
            == EVIDENCE
        )
        assert not (root / "protected/escaped").exists()
    return [{"real_package_build_backend": True, **EVIDENCE}]


@requires(POSIX, SANDBOX, R, command("ir"), command("uv"))
def test_worker_replaces_selected_r_resolver(binary: Path) -> Transcript:
    with (
        TemporaryDirectory(prefix="resolver-trust-", dir=Path.home()) as directory,
        relative_resolver_path(Path(directory), "ir"),
    ):
        root = Path(directory).resolve()
        working, env = workspace(root)
        r_env, _ = r_test_environment()
        tools = working / "bin"
        ir = selected_resolver("ir")
        (tools / "ir").symlink_to(ir)
        env.update(
            R_HOME=r_env["R_HOME"],
            PATH=os.pathsep.join((str(tools), r_env["PATH"])),
            IR_CACHE_DIR=ir_cache_directory(r_env),
            UV_CACHE_DIR=str(root / "cache/uv"),
            RETICULATE_PYTHON=sys.executable,
            RETICULATE_UV=selected_resolver("uv"),
            MCP_CONSOLE_TEST_RESOLVER=ir,
        )
        env.update(
            {
                name: r_env[name]
                for name in (
                    "R_ENVIRON",
                    "R_ENVIRON_USER",
                    "R_PROFILE",
                    "R_PROFILE_USER",
                )
            }
        )
        replacement_resolver(working, "replacement-ir")
        with McpClient(
            binary,
            ("serve", "-c", "cache=host", "--writable-root", str(working)),
            env,
            working,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "worker replaced selected ir\n",
                # fmt: r
                r=code(r"""
                    stopifnot(file.symlink(Sys.getenv("MCP_CONSOLE_TEST_PROTECTED"), "escape"))
                    unlink("bin/ir")
                    stopifnot(file.symlink(normalizePath("replacement-ir"), "bin/ir"))
                    writeLines("stop('worker profile reached resolver')", ".Rprofile")
                    Sys.setenv(UV_HTTP_TIMEOUT = "1")
                    cat("worker replaced selected ir\n")
                    """),
            )
            prepared = client.send(control="restart", requirements={"r": ["utf8"]})
            assert not prepared.get("isError"), prepared
            client.expect(
                "TRUE\n", r='cat(utf8::utf8_valid("prepared"), "\\n", sep = "")'
            )
            client.finish()
        assert json.loads((root / "cache/uv/trust-probe.json").read_text()) == EVIDENCE
        assert not (root / "protected/escaped").exists()
    return [
        {
            "worker_replaced_selected_r_resolver": True,
            "worker_r_profile_excluded": True,
            **EVIDENCE,
        }
    ]
