import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from support.assertions import last_result_text, wait_for_evaluation_output
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import SANDBOXED, Execution
from support.native import LOADER_VARIABLE, build_interposer
from support.normalization import code
from support.processes import ProcessIdentity, child_process_identities
from support.r import r_test_environment
from support.requirements import R
from support.python import virtualenv_python

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PYTHON_DOWNLOAD_URL = "https://example.invalid/python.tar.zst"

# Callers of recording/checkpoint fixtures select cache=host explicitly: their
# writable state is granted through fixture-owned UV_TOOL_DIR overrides. Real
# default Console cache coverage lives in requirements/test_cache_locations.py.


def expose_uv(directory: Path) -> Path:
    executable = shutil.which("uv")
    assert executable is not None, "real uv is required"
    target = directory / ("uv.exe" if os.name == "nt" else "uv")
    if os.name == "nt":
        shutil.copyfile(executable, target)
    else:
        target.symlink_to(executable)
    return target


def preseeded_duckdb_python(directory: Path) -> Path:
    """Prepare the real extension provider before testing resolver permissions."""
    venv = directory / "provider"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", venv],
        check=True,
        capture_output=True,
    )
    python = virtualenv_python(venv)
    subprocess.run(
        [shutil.which("uv"), "pip", "install", "--python", python, "duckdb"],
        check=True,
        capture_output=True,
    )
    # The no-HOME cache case supplies absolute XDG storage. Give its real
    # DuckDB fixture an explicit home instead of requiring ambient HOME.
    site = (
        venv / "Lib/site-packages"
        if os.name == "nt"
        else next(venv.glob("lib/python*/site-packages"))
    )
    (site / "sitecustomize.py").write_text(
        # fmt: python
        code(f"""
            import os

            if "HOME" not in os.environ:
                import duckdb

                connect = duckdb.connect
                duckdb.connect = lambda *args, config=None, **kwargs: connect(
                    *args, config={{"home_directory": {str(directory)!r}, **(config or {{}})}}, **kwargs
                )
            """)
    )
    return python


def probing_uv(directory: Path, python: Path, probe: str) -> None:
    """Run a cache/environment probe in trusted preparation, then use seeded Python."""
    source = directory / "preparation-probe.py"
    source.write_text(probe, encoding="utf-8")
    executable = directory / "bin" / ("uv.exe" if os.name == "nt" else "uv")
    executable.unlink(missing_ok=True)
    executable.with_suffix(".python").write_text(sys.executable, encoding="utf-8")
    executable.with_suffix(".py").write_text(
        # fmt: python
        code(f"""
            import json
            import os
            import runpy
            import subprocess
            import sys

            def delegate(executable, arguments):
                if os.name == "nt":
                    # execv neither quotes Windows argv nor waits for its child.
                    raise SystemExit(subprocess.call([executable, *arguments]))
                os.execv(executable, [executable, *arguments])

            arguments = sys.argv[1:]
            if arguments[0] == "--no-config":
                delegate({shutil.which("uv")!r}, arguments)
            if arguments[:2] == ["python", "list"]:
                major, minor, patch = sys.version_info[:3]
                print(json.dumps([{{"version": f"{{major}}.{{minor}}.{{patch}}", "version_parts": {{"major": major, "minor": minor, "patch": patch}}, "variant": "default", "implementation": "cpython"}}]))
            else:
                assert arguments[:2] == ["tool", "run"] and "--isolated" in arguments, arguments
                runpy.run_path({str(source)!r})
                command = arguments[arguments.index("--") + 1:]
                assert command[0] == "python", command
                delegate({str(python)!r}, command[1:])
            """),
        encoding="utf-8",
    )
    subprocess.run(
        [
            "rustc",
            "--edition=2024",
            str(FIXTURES / "resolver/uv_probe.rs"),
            "-o",
            str(executable),
        ],
        check=True,
    )


def local_resolver_owner(server: ProcessIdentity, binary: Path) -> ProcessIdentity:
    # Native sandbox launchers own the resolver beneath the server's children.
    pending = list(child_process_identities(server))
    owners = []
    while pending:
        child = pending.pop()
        arguments = subprocess.run(
            ["/bin/ps", "-ww", "-o", "args=", "-p", str(child[0])],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if arguments == f"{binary} resolve":
            owners.append(child)
        else:
            pending.extend(child_process_identities(child))
    assert len(owners) == 1, owners
    return owners[0]


def recording_ir_environment(
    directory: Path,
    *,
    fail_requirement: str | None = None,
    failure_output: str | None = None,
) -> tuple[dict[str, str], Path]:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    # Fixture records and checkpoints live in a granted resolver cache.
    environment["UV_TOOL_DIR"] = str(directory)
    real_ir = shutil.which("ir")
    assert real_ir is not None, "real `ir` is required"
    fake_bin = directory / "bin"
    fake_bin.mkdir()
    (fake_bin / "ir").symlink_to(FIXTURES / "record_ir")
    path = environment.get("PATH")
    assert path is not None, "PATH is required"
    environment["PATH"] = os.pathsep.join((str(fake_bin), path))
    record = directory / "ir.jsonl"
    environment["MCP_CONSOLE_TEST_REAL_IR"] = real_ir
    environment["MCP_CONSOLE_TEST_IR_RECORD"] = str(record)
    if fail_requirement is not None:
        environment["MCP_CONSOLE_TEST_IR_FAIL_REQUIREMENT"] = fail_requirement
    if failure_output is not None:
        failure = directory / "ir-failure-output"
        failure.write_text(failure_output, encoding="utf-8")
        environment["MCP_CONSOLE_TEST_IR_FAILURE_OUTPUT"] = str(failure)
    return environment, record


def ir_run_records(record: Path) -> list[dict[str, object]]:
    if not record.exists():
        return []
    records = [
        json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()
    ]
    return [entry for entry in records if entry["arguments"][0] == "run"]


def ir_requirements(record: dict[str, object]) -> list[str]:
    arguments = record["arguments"]
    assert isinstance(arguments, list), arguments
    return [
        arguments[index + 1]
        for index, argument in enumerate(arguments[:-1])
        if argument == "--with"
    ]


def checkpoint_uv_environment(
    temporary: Path,
    argument: str,
    *,
    reuse_resolved_python_for: tuple[str, ...] = (),
    provide_python_module: tuple[str, str] | None = None,
) -> tuple[dict[str, str], FifoCheckpoint, FifoCheckpoint]:
    assert all(reuse_resolved_python_for)
    assert provide_python_module is None or (
        provide_python_module[0] in reuse_resolved_python_for
        and provide_python_module[1].isidentifier()
    )
    real_uv = shutil.which("uv")
    assert real_uv is not None, "real uv is required"
    started = FifoCheckpoint.create(temporary / "uv-started")
    release = FifoCheckpoint.create(temporary / "uv-release")
    environment = os.environ.copy()
    # Fixture records and checkpoints live in a granted resolver cache.
    environment["UV_TOOL_DIR"] = str(temporary)
    environment["RETICULATE_UV"] = str(FIXTURES / "checkpoint_uv")
    environment["MCP_CONSOLE_TEST_REAL_UV"] = real_uv
    environment["MCP_CONSOLE_TEST_UV_CHECKPOINT_ARGUMENT"] = argument
    environment["MCP_CONSOLE_TEST_UV_CHECKPOINT_CLAIM"] = str(temporary / "uv-claimed")
    environment["MCP_CONSOLE_TEST_UV_STARTED"] = str(started.path)
    environment["MCP_CONSOLE_TEST_UV_RELEASE"] = str(release.path)
    if reuse_resolved_python_for:
        environment["MCP_CONSOLE_TEST_UV_REUSE_PYTHON"] = str(
            temporary / "resolved-python"
        )
        environment["MCP_CONSOLE_TEST_UV_REUSE_REQUIREMENTS"] = os.pathsep.join(
            reuse_resolved_python_for
        )
        environment["MCP_CONSOLE_TEST_UV_REUSE_RECORD"] = str(
            temporary / "uv-reuse-record"
        )
    if provide_python_module is not None:
        requirement, module = provide_python_module
        modules = temporary / "python-modules"
        modules.mkdir()
        environment["PYTHONPATH"] = str(modules)
        environment["MCP_CONSOLE_TEST_UV_PROVIDE_REQUIREMENT"] = requirement
        environment["MCP_CONSOLE_TEST_UV_PROVIDE_MODULE"] = str(
            modules / f"{module}.py"
        )
    return environment, started, release


def _isolate_r_package_cache(environment: dict[str, str], directory: Path) -> None:
    # A different case must not retire package-cache metadata while the native
    # runner is protecting a writable cache mount.
    cache = directory / "r-cache"
    environment["R_USER_CACHE_DIR"] = str(cache)
    environment["R_PKG_CACHE_DIR"] = str(cache)
    environment["PKG_CACHE_DIR"] = str(cache / "R/pkgcache")


def record_resolved_r_library(environment: dict[str, str], directory: Path) -> None:
    real_ir = shutil.which("ir", path=environment.get("PATH"))
    assert real_ir is not None, "ir is required"
    environment.update(r_library_cache_environment(environment))
    _isolate_r_package_cache(environment, directory)
    # Record the result inside an explicitly granted resolver cache.
    environment["UV_TOOL_DIR"] = str(directory)
    identity = directory / "resolved-r-library"
    fake_bin = directory / "fixture-r-bin"
    fake_bin.mkdir()
    ir = fake_bin / "ir"
    ir.write_text(
        code(r"""
            #!/bin/sh

            set -eu
            if [ "$#" -eq 1 ] && [ "$1" = "--version" ]; then
              exec "$MCP_CONSOLE_TEST_REAL_IR" "$@"
            fi
            if [ -n "${MCP_CONSOLE_TEST_R_RESOLUTION_FAILURE:-}" ] &&
              [ -e "$MCP_CONSOLE_TEST_R_RESOLUTION_FAILURE" ]; then
              printf 'fixture R resolver failed\n' >&2
              exit 1
            fi
            library=$("$MCP_CONSOLE_TEST_REAL_IR" "$@")
            printf '%s' "$library" > "$MCP_CONSOLE_TEST_R_LIBRARY_IDENTITY"
            printf '%s' "$library"
            """),
        encoding="utf-8",
    )
    ir.chmod(0o755)
    path = environment.get("PATH")
    assert path is not None, "PATH is required"
    environment["PATH"] = os.pathsep.join((str(fake_bin), path))
    environment["MCP_CONSOLE_TEST_REAL_IR"] = real_ir
    environment["MCP_CONSOLE_TEST_R_LIBRARY_IDENTITY"] = str(identity)


def resolver_interrupt_permission_environment(
    temporary_path: Path,
    execution: Execution,
) -> tuple[dict[str, str], FifoCheckpoint, FifoCheckpoint, Path, Path, Path]:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    fake_bin = temporary_path / "bin"
    fake_bin.mkdir()
    fake_ir = fake_bin / "ir"
    fake_ir.write_text(
        code(r"""
            #!/bin/sh

            set -eu
            if [ "$#" -eq 1 ] && [ "$1" = "--version" ]; then
              printf 'ir 0.4.0\n'
              exit 0
            fi
            exec 3< "$MCP_CONSOLE_TEST_RESOLVER_LIFETIME"
            printf '%s\n' "$$" > "$MCP_CONSOLE_TEST_RESOLVER_GROUP"
            printf 1 > "$MCP_CONSOLE_TEST_RESOLVER_STARTED"
            IFS= read -r _ <&3
            """),
        encoding="utf-8",
    )
    fake_ir.chmod(0o755)

    path = environment.get("PATH")
    assert path is not None, "PATH is required"
    environment["PATH"] = os.pathsep.join((str(fake_bin), path))
    environment["TMPDIR"] = str(temporary_path)
    environment["UV_TOOL_DIR"] = str(temporary_path)
    denied_interrupt = temporary_path / "resolver-sigint-denied"
    resolver_watches = temporary_path / "resolver-watches"
    resolver_watches.mkdir()
    resolver_group = temporary_path / "resolver-group"
    resolver_started = FifoCheckpoint.create(temporary_path / "resolver-started")
    resolver_lifetime = FifoCheckpoint.create(temporary_path / "resolver-lifetime")
    environment["MCP_CONSOLE_TEST_DENIED_SIGINT"] = str(denied_interrupt)
    environment["MCP_CONSOLE_TEST_RESOLVER_WATCHES"] = str(resolver_watches)
    environment["MCP_CONSOLE_TEST_RESOLVER_GROUP"] = str(resolver_group)
    environment["MCP_CONSOLE_TEST_RESOLVER_STARTED"] = str(resolver_started.path)
    environment["MCP_CONSOLE_TEST_RESOLVER_LIFETIME"] = str(resolver_lifetime.path)
    # Load the hook in the server and resolver owner, including inside the
    # native sandbox. The owner removes it before launching ir or the worker.
    environment[LOADER_VARIABLE] = str(
        build_interposer(temporary_path, "killpg_denial_interposer")
    )
    config = temporary_path / ".agents/console/config.yaml"
    config.parent.mkdir(parents=True)
    resolver = {"environment": {LOADER_VARIABLE: environment[LOADER_VARIABLE]}}
    if execution == SANDBOXED:
        # This resolver is a checkpoint fixture, not an installer. Avoid
        # mounting shared caches whose Git metadata can change during
        # concurrent real dependency preparation.
        resolver["sandbox"] = {
            "filesystem": {
                "read_only": ["/"],
                "read_write": [str(temporary_path)],
            }
        }
    config.write_text(json.dumps({"resolver": resolver}))
    return (
        environment,
        resolver_started,
        resolver_lifetime,
        resolver_group,
        denied_interrupt,
        resolver_watches,
    )


def fake_ir_environment(root: Path, libraries: list[Path]) -> dict[str, str]:
    environment, _ = r_test_environment()
    _isolate_r_package_cache(environment, root)
    environment["IR_CACHE_DIR"] = str(root / "ir-cache")
    # Keep fixture records and checkpoints in a granted resolver cache.
    environment["UV_TOOL_DIR"] = str(root)
    fake_bin = root / "bin"
    fake_bin.mkdir()
    fixture = FIXTURES / "ordered_retirement_ir"
    (fake_bin / "ir").symlink_to(fixture)
    path = environment.get("PATH")
    assert path is not None, "PATH is required"
    environment["PATH"] = os.pathsep.join((str(fake_bin), path))
    environment["MCP_CONSOLE_TEST_IR_COUNTER"] = str(root / "ir-counter")
    environment["MCP_CONSOLE_TEST_IR_LIBRARIES"] = os.pathsep.join(map(str, libraries))
    return environment


def named_requirement_error(requirement: str) -> str:
    return (
        f"Python requirement `{requirement}` is not accepted: host-side managed "
        "resolution accepts named package requirements only"
    )


def python_version_constraint_error(constraint: str) -> str:
    return (
        f"Python version constraint `{constraint}` is not accepted: host-side managed "
        "resolution accepts version numbers and supported PEP 440 version specifiers only"
    )


def normalize_duckdb_resolution_error(error: str, extension: str) -> str:
    detail = next(
        line.strip().removeprefix("! ")
        for line in error.splitlines()
        if f'Failed to download extension "{extension}"' in line
    )
    # DuckDB releases differ in whether the HTTP error has an Invalid wrapper.
    detail = detail.removeprefix("Invalid Error: ")
    return detail.partition(' at URL "')[0]


def ir_cache_directory(environment: dict[str, str]) -> str:
    ir = shutil.which("ir", path=environment.get("PATH"))
    assert ir is not None, "ir is required"
    cache = subprocess.run(
        [ir, "cache", "dir"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    ).stdout.strip()
    assert cache and Path(cache).is_absolute(), (
        f"ir returned invalid cache directory: {cache}"
    )
    return cache


def r_library_cache_environment(environment: dict[str, str]) -> dict[str, str]:
    """Capture ir libraries and their renv targets before redirecting cache homes."""
    caches = {"IR_CACHE_DIR": ir_cache_directory(environment)}
    renv_cache = environment.get("RENV_PATHS_CACHE")
    if not renv_cache:
        renv_root = environment.get("RENV_PATHS_ROOT")
        if not renv_root:
            r_home = environment.get("R_HOME")
            rscript = (
                Path(r_home) / "bin" / ("Rscript.exe" if os.name == "nt" else "Rscript")
                if r_home
                else shutil.which("Rscript", path=environment.get("PATH"))
            )
            assert rscript is not None, "Rscript is required"
            renv_root = subprocess.run(
                [rscript, "--vanilla", "-e", "cat(tools::R_user_dir('renv', 'cache'))"],
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            assert Path(renv_root).is_absolute(), renv_root
        renv_cache = str(Path(renv_root) / "cache")
    # A shared ir library must not retain links into a deleted fixture HOME.
    return caches | {"RENV_PATHS_CACHE": renv_cache}


def matplotlib_test_environment(cache_home: Path) -> dict[str, str]:
    environment = os.environ.copy()
    if R.available:
        environment.update(r_library_cache_environment(environment))
    environment["XDG_CACHE_HOME"] = str(cache_home)
    return environment


def bare_runtime_environment(
    environment: dict[str, str], library: Path
) -> dict[str, str]:
    environment = environment.copy()
    environment["PATH"] = os.pathsep.join(
        entry
        for entry in environment["PATH"].split(os.pathsep)
        if not any(
            (Path(entry) / (name + (".exe" if os.name == "nt" else ""))).exists()
            for name in ("ir", "uv", "uvx")
        )
    )
    environment.pop("RETICULATE_UV", None)
    environment.pop("RETICULATE_PYTHON", None)
    for name in ("R_LIBS", "R_LIBS_SITE", "R_LIBS_USER"):
        environment[name] = str(library)
    return environment


def python_inventory_client(
    binary: Path,
    execution: Execution,
    directory: Path,
    *,
    preference: str | None = None,
    install_directory: Path | None = None,
    resolver_python: Path | None = None,
    resolver_record: Path | None = None,
    extra_environment: dict[str, str] | None = None,
    retain_initialization: bool = False,
) -> tuple[McpClient, Path, Path]:
    real_uv = shutil.which("uv")
    assert real_uv is not None, "real uv is required"
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    environment.pop("UV_PYTHON_PREFERENCE", None)
    environment["UV_TOOL_DIR"] = str(directory)
    environment["RETICULATE_UV"] = str(FIXTURES / "record_uv_environment")
    environment["MCP_CONSOLE_TEST_REAL_UV"] = real_uv
    environment["MCP_CONSOLE_TEST_UV_RECORD"] = str(directory / "uv.jsonl")
    # These cases resolve synthetic Python inventories without embedding their
    # interpreter selections. R can query version constraints before selection.
    environment["MCP_CONSOLE_LANGUAGES"] = "r"
    arguments = directory / "uv-arguments.jsonl"
    environment["MCP_CONSOLE_TEST_UV_ARGUMENTS_RECORD"] = str(arguments)
    inventories = directory / "uv-python-inventories.json"
    environment["MCP_CONSOLE_TEST_UV_PYTHON_INVENTORIES"] = str(inventories)
    if preference is not None:
        environment["UV_PYTHON_PREFERENCE"] = preference
    if install_directory is not None:
        environment["UV_PYTHON_INSTALL_DIR"] = str(install_directory)
    if resolver_python is not None:
        environment["MCP_CONSOLE_TEST_UV_PYTHON"] = str(resolver_python)
    if resolver_record is not None:
        environment["MCP_CONSOLE_TEST_UV_RESOLVER_RECORD"] = str(resolver_record)
    if extra_environment is not None:
        environment.update(extra_environment)
    client = McpClient(
        binary,
        execution.serve("-c", "cache=host", "-c", "python.managed.resolution=implicit"),
        environment,
        current_directory=directory,
    )
    client.initialize_and_list_tools()
    if not retain_initialization:
        client.transcript.clear()
    client.send(requirements={"r": ["DBI"]})
    assert last_result_text(client) == "[prepared]", client.transcript[-1]
    arguments.write_text("", encoding="utf-8")
    if resolver_record is not None:
        resolver_record.write_text("", encoding="utf-8")
    return client, inventories, arguments


def uv_python_row(
    version: str,
    *,
    path: str | Path | None = None,
    url: str | None = PYTHON_DOWNLOAD_URL,
    variant: str = "default",
    implementation: str = "cpython",
) -> dict[str, object]:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version)
    assert match is not None, version
    major, minor, patch = (int(part) for part in match.groups())
    return {
        "key": f"{implementation}-{version}-macos-aarch64-none",
        "version": version,
        "version_parts": {"major": major, "minor": minor, "patch": patch},
        "path": None if path is None else str(path),
        "symlink": None,
        "url": url,
        "variant": variant,
        "implementation": implementation,
    }


def write_uv_python_inventories(path: Path, inventories: dict[str, object]) -> None:
    path.write_text(json.dumps(inventories), encoding="utf-8")


def recorded_python_preferences(arguments: Path) -> list[str]:
    invocations = [
        json.loads(line) for line in arguments.read_text(encoding="utf-8").splitlines()
    ]
    return [
        invocation[invocation.index("--python-preference") + 1]
        for invocation in invocations
        if invocation[:2] == ["python", "list"]
    ]


def recorded_tool_run_pythons(arguments: Path) -> list[str]:
    invocations = [
        json.loads(line) for line in arguments.read_text(encoding="utf-8").splitlines()
    ]
    return [
        invocation[invocation.index("--python") + 1]
        for invocation in invocations
        if invocation[:2] == ["tool", "run"] and "--python" in invocation
    ]


def read_uv_resolver_records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def resolve_public_python_version(
    client: McpClient,
    constraints: list[str],
) -> str:
    constraints_r = (
        "character()"
        if not constraints
        else f"c({', '.join(json.dumps(value) for value in constraints)})"
    )
    # fmt: r
    r = code(rf"""
        reticulate::py_require(
          python_version = {
            constraints_r
          },
          action = "set"
        )
        result <- tryCatch(
          reticulate::py_write_requirements(
            NULL,
            NULL,
            freeze = FALSE,
            python = NULL
          )$python_version,
          error = conditionMessage
        )
        cat(result, "\n", sep = "")
        """)
    client.send(r=r)
    return last_result_text(client)


def write_python_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def recording_uv_environment(
    directory: Path,
    *,
    fail_requirement: str | None = None,
    substitute_requirement: tuple[str, str] | None = None,
) -> tuple[dict[str, str], Path]:
    real_uv = shutil.which("uv")
    assert real_uv is not None, "real uv is required"
    environment = os.environ.copy()
    # Fixture records and checkpoints live in a granted resolver cache.
    environment["UV_TOOL_DIR"] = str(directory)
    environment.pop("RETICULATE_PYTHON", None)
    environment["RETICULATE_UV"] = str(FIXTURES / "record_uv_environment")
    environment["MCP_CONSOLE_TEST_REAL_UV"] = real_uv
    environment["MCP_CONSOLE_TEST_UV_RECORD"] = str(directory / "uv-environment.jsonl")
    arguments_record = directory / "uv-arguments.jsonl"
    environment["MCP_CONSOLE_TEST_UV_ARGUMENTS_RECORD"] = str(arguments_record)
    if fail_requirement is not None:
        failure_marker = directory / "uv-failure"
        failure_marker.touch()
        environment["MCP_CONSOLE_TEST_UV_FAILURE_MARKER"] = str(failure_marker)
        environment["MCP_CONSOLE_TEST_UV_FAILURE_ARGUMENT"] = fail_requirement
    if substitute_requirement is not None:
        substitute, replacement = substitute_requirement
        environment["MCP_CONSOLE_TEST_UV_SUBSTITUTE_REQUIREMENT"] = substitute
        environment["MCP_CONSOLE_TEST_UV_REPLACEMENT_REQUIREMENT"] = replacement
    return environment, arguments_record


def uv_tool_run_requirements(record: Path) -> list[list[str]]:
    if not record.exists():
        return []
    arguments = [
        json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()
    ]
    requirements = []
    for invocation in arguments:
        if invocation[:2] != ["tool", "run"]:
            continue
        separator = invocation.index("--")
        manifest = [
            invocation[index + 1]
            for index, argument in enumerate(invocation[:separator])
            if argument == "--with"
        ]
        requirements.append(manifest)
    return requirements


def initialize_python_and_record_baseline(client: McpClient, record: Path) -> int:
    client.send(python="None")
    assert last_result_text(client) == "[done]"
    return len(uv_tool_run_requirements(record))


def resolve_managed_python(
    binary: Path,
    execution: Execution,
    directory: Path,
    *,
    environment: dict[str, str] | None = None,
) -> Path:
    workspace = directory / "managed-python"
    workspace.mkdir()
    environment = os.environ.copy() if environment is None else environment.copy()
    environment.pop("RETICULATE_PYTHON", None)
    environment.pop("UV_PYTHON", None)
    with McpClient(
        binary,
        execution.serve(),
        environment,
        current_directory=workspace,
    ) as client:
        client.initialize_and_list_tools()
        client.send(
            # fmt: python
            python=code("""
                import sys

                print(f"managed-python={sys.executable}")
                """),
        )
        output = last_result_text(client)
        client.finish()
    executable = Path(
        next(
            line for line in output.splitlines() if line.startswith("managed-python=")
        ).split("=", 1)[1]
    ).absolute()
    assert executable.is_file(), executable
    return executable


def send_and_collect_runtime_python_resolution(
    client: McpClient,
    **arguments: object,
) -> str:
    return wait_for_evaluation_output(
        client,
        None,
        "automatic Python resolution",
        expected_error=None,
        completion_timeout_seconds=client.response_timeout,
        **arguments,
    )
