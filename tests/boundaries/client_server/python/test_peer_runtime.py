"""One Python cell contract with optional R, followed by bridge-only checks."""

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from functools import cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.python.test_setup import deferred_selection_client

from support.progress import without_elapsed
from support.requirements import NATIVE_FIXTURES, POSIX, R, SQL, command, requires
from support.assertions import (
    assert_result_content,
    last_result_text,
    wait_for_evaluation_output,
)
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import DIRECT, RUNTIME, SANDBOXED, Execution, executions
from support.linux_sandbox import retain_system_bwrap
from support.normalization import code
from support.native import build_interposer
from support.records import (
    McpTranscript,
    ToolResult,
    Transcript,
    TranscriptWithCompanions,
)
from support.resolvers import (
    checkpoint_uv_environment,
    send_and_collect_runtime_python_resolution,
)
from support.r import isolated_r_home, r_test_environment, reference_plots
from support.python import virtualenv_python, write_test_wheel
from support.snapshots import execution_snapshots


# fmt: python
CLI_SOURCE = code("""
    import json
    import os
    import sys
    import numpy as np


    def main() -> None:
        print(
            json.dumps(
                {
                    "module": __name__,
                    "executable": sys.executable,
                    "prefix": sys.prefix,
                    "virtualenv": os.environ.get("VIRTUAL_ENV"),
                    "values": np.arange(3).tolist(),
                }
            )
        )
    """)

# fmt: r
CLI_CHECK = code("""
    check_cli <- function(command, module) {
      selected <- reticulate::py_config()$python
      prefix <- reticulate::import("sys")$prefix
      stopifnot(identical(
        normalizePath(unname(Sys.which(command))),
        normalizePath(file.path(
          dirname(selected),
          paste0(command, if (.Platform$OS.type == "windows") ".exe" else "")
        ))
      ))
      output <- system2(command, stdout = TRUE)
      stopifnot(is.null(attr(output, "status")))
      child <- jsonlite::fromJSON(output)
      # Installers may use bin/python or bin/python3 in their shebang.
      # Check executable identity and the virtualenv independently.
      stopifnot(
        identical(child$module, module),
        identical(normalizePath(child$executable), normalizePath(selected)),
        identical(normalizePath(child$prefix), normalizePath(prefix)),
        identical(child$virtualenv, Sys.getenv("VIRTUAL_ENV")),
        identical(child$values, 0:2)
      )
      cat("installed CLI uses the selected Python environment\\n")
    }
    """)


# fmt: python
DEFER_R_STARTUP = code("""
    import builtins
    import sys

    if sys.argv[0] == "" and "_mcp_console_services" in sys.modules:
        if not getattr(builtins, "peer_bootstrap_interrupted", False):
            builtins.peer_bootstrap_interrupted = True
            input("defer R startup> ")
        builtins.peer_startups = getattr(builtins, "peer_startups", 0) + 1
    """)


def defer_r_bootstrap(client: McpClient) -> None:
    # Interrupt Python's retryable site setup before eager bootstrap enters R.
    # The next Python cell completes setup; R still initializes only at demand.
    initialized = client.transcript.copy()
    wait_for_evaluation_output(
        client,
        '[input requested: "defer R startup> "]\n[waiting for stdin]',
        "Python bootstrap before R attachment",
        python="raise AssertionError('interrupted bootstrap ran fixture cell')",
        timeout_ms=0,
        completion_timeout_seconds=client.response_timeout,
    )
    client.send(control="interrupt", timeout_ms=10_000)
    output = last_result_text(client)
    assert "KeyboardInterrupt" in output, output
    assert "AssertionError" not in output, output
    assert "[running; poll with an empty send]" not in output, output
    client.transcript[:] = initialized


def without_r(environment: dict[str, str], root: Path) -> None:
    path = root / "empty-path"
    path.mkdir()
    retain_system_bwrap(path, environment.get("PATH"))
    environment["PATH"] = str(path)
    for name in ("R_HOME", "R_LIBS", "R_LIBS_USER", "RETICULATE_UV"):
        environment.pop(name, None)


def exercise_late_r(client: McpClient, trigger: str = "python-access") -> None:
    # Inspect libR's actual state, not cell order or PID. A loader
    # re-exec after this cell would lose the objects below.
    # fmt: python
    source = code("""
        import builtins
        import ctypes
        import io
        import os
        import sqlite3
        import sys
        import numpy as np

        def r_initialized():
            try:
                if os.name == "nt":
                    kernel = ctypes.WinDLL("kernel32")
                    kernel.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
                    kernel.GetModuleHandleW.restype = ctypes.c_void_p
                    handle = kernel.GetModuleHandleW("R.dll")
                    if not handle:
                        return False
                    library = ctypes.CDLL(None, handle=handle)
                else:
                    library = ctypes.CDLL(None)
                return bool(ctypes.c_void_p.in_dll(library, "R_GlobalEnv").value)
            except ValueError:
                return False

        assert not r_initialized(), "R initialized before Python demand"
        assert builtins.peer_startups == 1
        persistent = object()
        connection = sqlite3.connect(":memory:")
        connection.execute("create table peer(value integer)")
        connection.execute("insert into peer values (42)")
        _console.sql_connection(connection)
        np.set_printoptions(linewidth=73)
        before = (id(persistent), id(connection), sys.executable, sys.prefix,
                  os.environ.get("MPLCONFIGDIR"), os.environ.get("XDG_CACHE_HOME"))
        print("Python live; R absent")
        original_streams = (sys.stdout, sys.stderr)
        redirected_stdout = io.StringIO()
        redirected_stderr = io.StringIO()
        sys.stdout, sys.stderr = redirected_stdout, redirected_stderr
        """)
    client.expect("Python live; R absent\n", python=source)
    if trigger == "r-cell":
        client.expect(
            "[done]",
            r="peer_from_r <- 41L; stopifnot(reticulate::py_eval('persistent is not None'))",
        )
    else:
        client.expect(
            "[done]",
            python="assert 3 < r.pi < 4; assert int(r['sum(c(20, 21))']) == 41",
        )
        assert last_result_text(client) == "[done]", client.transcript[-1]
    # fmt: python
    source = code("""
        assert r_initialized()
        assert sys.stdout is redirected_stdout
        assert sys.stderr is redirected_stderr
        print("redirected stdout")
        print("redirected stderr", file=sys.stderr)
        assert redirected_stdout.getvalue() == "redirected stdout\\n"
        assert redirected_stderr.getvalue() == "redirected stderr\\n"
        sys.stdout, sys.stderr = original_streams
        assert builtins.peer_startups == 1
        assert before == (
            id(persistent),
            id(connection),
            sys.executable,
            sys.prefix,
            os.environ.get("MPLCONFIGDIR"),
            os.environ.get("XDG_CACHE_HOME"),
        )
        assert connection.execute("select value from peer").fetchone() == (42,)
        assert np.get_printoptions()["linewidth"] == 73


        def nested_peer():
            return int(r["sum(c(40, 2))"])


        print("Python state and services retained")
        """)
    client.expect("Python state and services retained\n", python=source)
    client.send(sql="select value from peer")
    assert "42" in last_result_text(client), client.transcript[-1]
    client.expect(r="stopifnot(identical(reticulate::py$nested_peer(), 42L))")


@requires(SQL)
@requires(R)
@executions(DIRECT, SANDBOXED)
def test_late_r_preserves_python_runtime(
    binary: Path, execution: Execution
) -> Transcript:
    records = None
    for trigger in ("r-cell", "python-access"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            modules = root / "modules"
            modules.mkdir()
            (modules / "sitecustomize.py").write_text(DEFER_R_STARTUP)
            environment = dict(
                os.environ,
                RETICULATE_PYTHON=sys.executable,
                RETICULATE_PYTHONPATH=str(modules),
            )
            with McpClient(binary, execution.serve(), environment, root) as client:
                client.initialize_and_list_tools()
                defer_r_bootstrap(client)
                exercise_late_r(client, trigger)
                current = client.finish()[3:]
                if records is None:
                    records = current
    assert records is not None
    return records


@requires(R, command("uv"))
@executions(DIRECT, SANDBOXED)
def test_late_attachment_preserves_environment_metadata(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        modules = root / "modules"
        modules.mkdir()
        (modules / "sitecustomize.py").write_text(DEFER_R_STARTUP)
        executable = Path(sys._base_executable)
        prefix = root / "virtualenv"
        for kind in ("base", "virtualenv", "conda-marker"):
            if kind == "virtualenv":
                subprocess.run(
                    [sys.executable, "-m", "venv", "--without-pip", str(prefix)],
                    check=True,
                )
                executable = virtualenv_python(prefix)
                # Explicit environments still need reticulate's default
                # NumPy declaration satisfied before bridge attachment.
                subprocess.run(
                    ["uv", "pip", "install", "--python", str(executable), "numpy"],
                    check=True,
                    capture_output=True,
                )
            elif kind == "conda-marker":
                # Exercise both values of reticulate's Conda metadata marker in
                # a test-owned environment, without installing a Conda manager.
                (prefix / "conda-meta").mkdir()
            environment = dict(
                os.environ,
                RETICULATE_PYTHON=str(executable),
                RETICULATE_PYTHONPATH=str(modules),
                MCP_CONSOLE_TEST_PYTHON=str(executable),
                MCP_CONSOLE_TEST_ENVIRONMENT_KIND=kind,
                MCP_CONSOLE_TEST_VIRTUALENV="" if kind == "base" else str(prefix),
            )
            with McpClient(binary, execution.serve(), environment, root) as client:
                client.initialize_and_list_tools()
                defer_r_bootstrap(client)
                client.expect(
                    python="peer_object = object(); peer_identity = id(peer_object)",
                )
                # fmt: r
                client.expect(
                    "environment metadata retained\n",
                    r=code("""
                    config <- reticulate::py_config()
                    sys <- reticulate::import("sys")
                    stopifnot(
                      identical(config$python, Sys.getenv("MCP_CONSOLE_TEST_PYTHON")),
                      identical(normalizePath(config$prefix), normalizePath(sys$prefix)),
                      identical(normalizePath(config$exec_prefix), normalizePath(sys$exec_prefix)),
                      identical(normalizePath(config$base_prefix), normalizePath(sys$base_prefix)),
                      identical(
                        normalizePath(config$base_exec_prefix),
                        normalizePath(sys$base_exec_prefix)
                      ),
                      !isTRUE(config$ephemeral),
                      identical(
                        config$conda,
                        Sys.getenv("MCP_CONSOLE_TEST_ENVIRONMENT_KIND") == "conda-marker"
                      ),
                      identical(
                        normalizePath(config$virtualenv, mustWork = FALSE),
                        normalizePath(Sys.getenv("MCP_CONSOLE_TEST_VIRTUALENV"), mustWork = FALSE)
                      ),
                      identical(config$virtualenv_activate, "")
                    )
                    cat("environment metadata retained\\n")
                    """),
                )
                client.expect(
                    "same interpreter\n",
                    python="assert id(peer_object) == peer_identity; print('same interpreter')",
                )
                records.extend([{"environment": kind}, *client.finish()[3:]])
    return records


@requires(POSIX)
@requires(R)
@executions(DIRECT, SANDBOXED)
def test_interrupt_wakes_input_before_and_after_attachment(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        modules = root / "modules"
        modules.mkdir()
        (modules / "sitecustomize.py").write_text(DEFER_R_STARTUP)
        release = FifoCheckpoint.create(root / "interrupt")
        environment = dict(
            os.environ,
            RETICULATE_PYTHON=sys.executable,
            RETICULATE_PYTHONPATH=str(modules),
        )
        serve = (
            execution.serve("--writable-root", str(root))
            if execution == SANDBOXED
            else execution.serve()
        )
        try:
            with McpClient(binary, serve, environment, root) as client:
                client.initialize_and_list_tools()
                defer_r_bootstrap(client)
                for attached in (False, True):
                    if attached:
                        client.expect(
                            "[done]",
                            python="assert int(r['42L']) == 42",
                        )
                        assert last_result_text(client) == "[done]", client.transcript[
                            -1
                        ]
                    client.expect(
                        '[input requested: "interrupt gate> "]\n[waiting for stdin]',
                        python=code("""
                        import signal
                        import threading

                        def interrupt_from_background():
                            with open("interrupt", "rb", buffering=0) as gate:
                                assert gate.read(1) == b"1"
                            signal.pthread_kill(threading.get_ident(), signal.SIGINT)

                        sender = threading.Thread(target=interrupt_from_background)
                        sender.start()
                        try:
                            input("interrupt gate> ")
                        except KeyboardInterrupt:
                            print("input cancelled once")
                        else:
                            raise AssertionError("input was not interrupted")
                        sender.join()
                        """),
                    )
                    # The public input request proves the main thread is blocked.
                    # SIGINT goes to another thread, so only the self-pipe wakes it.
                    release.release()
                    wait_for_evaluation_output(
                        client, "input cancelled once\n", "background-thread interrupt"
                    )
                    client.expect(
                        "no duplicate interrupt\n",
                        python="print('no duplicate interrupt')",
                    )
                return client.finish()[3:]
        finally:
            release.close()


@requires(POSIX)
@requires(R, command("uv"))
@executions(DIRECT, SANDBOXED)
def test_idle_preparation_keeps_r_uninitialized(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        modules = root / "modules"
        modules.mkdir()
        (modules / "sitecustomize.py").write_text(DEFER_R_STARTUP)
        environment = dict(os.environ, RETICULATE_PYTHONPATH=str(modules))
        environment.pop("RETICULATE_PYTHON", None)
        environment["UV_TOOL_DIR"] = str(root)
        arguments = root / "uv-arguments"
        environment.update(
            RETICULATE_UV=str(
                Path(__file__).parents[3] / "fixtures/record_uv_environment"
            ),
            MCP_CONSOLE_TEST_REAL_UV=shutil.which("uv"),
            MCP_CONSOLE_TEST_UV_RECORD=str(root / "uv-environment"),
            MCP_CONSOLE_TEST_UV_ARGUMENTS_RECORD=str(arguments),
        )
        if execution == SANDBOXED:
            # Own the resolver cache and its mount targets for this session.
            # Other concurrent fixtures can mutate the shared host cache.
            environment["XDG_CACHE_HOME"] = str(root / "cache")
            serve = execution.serve(
                "--writable-root",
                str(root),
                "-c",
                "resolver="
                + json.dumps(
                    {
                        "sandbox": {
                            "filesystem": {
                                "read_only": ["/"],
                                "read_write": [str(root)],
                            }
                        }
                    }
                ),
            )
        else:
            serve = execution.serve("-c", "cache=host")
        # Keep the cold cache, but prepare only the required R runtime packages.
        serve += ("-c", "r.packages=[]")
        with McpClient(binary, serve, environment, root) as client:
            client.initialize_and_list_tools()
            defer_r_bootstrap(client)
            client.expect(
                python="import sys; from pathlib import Path; sentinel = object(); original = sentinel; _ = Path('running-python').write_text(sys.executable)",
            )
            arguments.write_text("")
            client.expect("[prepared]", requirements={"python": ["packaging"]})
            calls = [json.loads(line) for line in arguments.read_text().splitlines()]
            resolution = next(call for call in calls if call[:2] == ["tool", "run"])
            assert (
                resolution[resolution.index("--python") + 1]
                == (root / "running-python").read_text()
            ), resolution
            assert not any(call[:2] == ["python", "list"] for call in calls), calls
            arguments.write_text("")
            rejected = client.send(requirements={"python": ["NumPy>=0"]})
            assert rejected["isError"], rejected
            assert "changes already-declared `numpy`" in last_result_text(client), (
                client.transcript[-1]
            )
            assert arguments.read_text() == "", (
                "incompatible live declaration reached resolver"
            )
            # fmt: python
            source = code("""
                import ctypes
                import packaging

                try:
                    initialized = bool(ctypes.c_void_p.in_dll(ctypes.CDLL(None), "R_GlobalEnv").value)
                except ValueError:
                    initialized = False
                assert not initialized, "idle Python preparation initialized R"
                assert sentinel is original
                print("idle preparation uses the Python owner")
                """)
            client.expect("idle preparation uses the Python owner\n", python=source)
            client.expect(
                r='stopifnot("packaging" %in% reticulate::py_require()$packages, isTRUE(reticulate::py_config()$ephemeral))',
            )
            return client.finish()[3:]


@executions(RUNTIME)
def test_standalone_python_contract(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        environment = dict(
            os.environ,
            RETICULATE_PYTHON=sys.executable,
            MCP_CONSOLE_TEST_PYTHON=sys.executable,
            MCP_CONSOLE_TEST_PYTHON_PREFIX=sys.prefix,
        )
        without_r(environment, root)
        with McpClient(binary, execution.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            exercise_python(client)
            return client.finish()[3:]


@requires(R, command("uv"))
@executions(RUNTIME)
def test_shared_module_configuration(binary: Path, execution: Execution) -> Transcript:
    records = None
    for with_r in (False, True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = dict(os.environ)
            environment.pop("RETICULATE_PYTHON", None)
            if not with_r:
                uv = shutil.which("uv")
                assert uv is not None
                without_r(environment, root)
                shutil.copyfile(
                    uv, Path(environment["PATH"]) / "uv.exe"
                ) if os.name == "nt" else (Path(environment["PATH"]) / "uv").symlink_to(
                    uv
                )
            with McpClient(binary, execution.serve(), environment, root) as client:
                client.initialize_and_list_tools()
                client.expect("[prepared]", requirements={"python": ["matplotlib"]})
                # Defaults apply at the first import, not at a later cell or
                # bridge attachment. Each module keeps subsequent user choices.
                # fmt: python
                source = code("""
                    import numpy as np
                    import pandas as pd
                    import matplotlib.pyplot as plt

                    assert np.get_printoptions()["linewidth"] == 200
                    assert pd.get_option("display.width") == 200
                    np.set_printoptions(linewidth=73)
                    pd.set_option("display.width", 79)
                    custom_show = lambda *args, **kwargs: "user show"
                    plt.show = custom_show
                    print("shared module defaults installed")
                    """)
                client.expect("shared module defaults installed\n", python=source)
                if with_r:
                    client.expect(
                        "[1] TRUE\n",
                        r="reticulate::py_eval(\"id(custom_show) == id(__import__('matplotlib.pyplot', fromlist=['show']).show)\")",
                    )
                client.expect(
                    "user module options retained\n",
                    python='assert np.get_printoptions()["linewidth"] == 73; assert pd.get_option("display.width") == 79; assert plt.show is custom_show; print("user module options retained")',
                )
                current = client.finish()[3:]
                if records is None:
                    records = current
    assert records is not None
    return records


def exercise_python(client: McpClient) -> tuple[str, ...]:
    # Every configuration exercises the same evaluator and NumPy availability
    # without referring to the R/Python bridge.
    # fmt: python
    first = code("""
        import os
        import sys
        import numpy as np
        assert np.arange(3).tolist() == [0, 1, 2]
        assert os.path.samefile(sys.executable, os.environ["MCP_CONSOLE_TEST_PYTHON"])
        assert os.path.realpath(sys.prefix) == os.path.realpath(
            os.environ["MCP_CONSOLE_TEST_PYTHON_PREFIX"]
        )
        peer_value = 40
        print("shared stdout")
        sys.stderr.write("shared stderr\\n")
        peer_value + 1
        """)
    # fmt: python
    failure = code("""
        peer_value += 2
        raise ValueError("peer runtime sentinel")
        """)
    # fmt: python
    recovery = code("""
        (
            peer_value,
            "_mcp_console" not in globals(),
            "_mcp_console" in sys.modules,
        )
        """)
    # fmt: python
    shadow = code("""
        exec = None
        eval = None
        compile = None
        peer_value
        """)
    # fmt: python
    after_shadow = code("""
        peer_value += 1
        peer_value
        """)
    output = []
    for source in (first, failure, recovery, shadow, after_shadow):
        response = client.send(python=source)
        assert not response.get("isError"), response
        output.append(last_result_text(client))
        if len(output) == 1:
            assert output[0] == "shared stdout\nshared stderr\n41\n", output[0]
    assert "<mcp-console:python:e2>" in output[1], output[1]
    assert output[1].endswith("ValueError: peer runtime sentinel\n"), output[1]
    assert output[2:] == ["(42, True, True)\n", "42\n", "43\n"], output[2:]
    return tuple(output)


@requires(R)
@executions(RUNTIME)
def test_python_contract_with_and_without_r(
    binary: Path, execution: Execution
) -> Transcript:
    # The host must have R so the same test can compare both configurations.
    # Both orders compose the same evaluator; the late-R case above probes
    # initialization itself and checks in-memory continuity across attachment.
    reference = None
    records = None
    for mode in ("without-r", "r-first", "python-first"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            config = workspace / ".agents/console/config.yaml"
            config.parent.mkdir(parents=True)
            # Keep the exact same interpreter in all three configurations.
            # An explicit selection bypasses managed Python package resolution.
            config.write_text(json.dumps({"python": sys.executable}), encoding="utf-8")
            environment = dict(
                os.environ,
                MCP_CONSOLE_TEST_PYTHON=sys.executable,
                MCP_CONSOLE_TEST_PYTHON_PREFIX=sys.prefix,
            )
            environment.pop("RETICULATE_PYTHON", None)
            if mode == "without-r":
                without_r(environment, root)
            with McpClient(binary, execution.serve(), environment, workspace) as client:
                client.initialize_and_list_tools()
                if mode == "r-first":
                    # R execution alone does not require reticulate bridge attachment.
                    client.expect(
                        r="stopifnot(!reticulate::py_available(initialize = FALSE))",
                    )
                actual = exercise_python(client)
                if reference is None:
                    reference = actual
                else:
                    # Preserve and compare complete error text, not summaries.
                    assert actual == reference, (mode, actual, reference)
                if mode != "without-r":
                    # Only the mixed configurations add cross-language checks.
                    # The Python state above must remain the same state seen by R.
                    # fmt: r
                    r = code("""
                        stopifnot(identical(reticulate::py_eval("peer_value"), 43L))
                        peer_from_r <- 44L
                        """)
                    client.expect(r=r)
                    client.expect(
                        "(44, 43)\n", python="(int(r.peer_from_r), peer_value)"
                    )
                transcript = client.finish()[3:]
                if mode == "without-r":
                    records = transcript
    # One representative transcript; all common Python outputs were asserted
    # byte-for-byte equal above. Bridge checks assert their distinct results.
    assert records is not None
    return records


@requires(R, command("uv"))
@executions(DIRECT, SANDBOXED)
def test_shared_virtualenv_bootstrap(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        venv = root / "selected environment"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True
        )
        executable = virtualenv_python(venv)
        other = root / "other environment"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(other)], check=True
        )
        assert (
            executable.read_bytes() == virtualenv_python(other).read_bytes()
            if os.name == "nt"
            else executable.samefile(virtualenv_python(other))
        )
        # Reticulate declares NumPy by default, including for explicit Python
        # selections. Satisfy that declaration before exercising the bridge.
        index = write_test_wheel(
            root, "mcp_console_test_cli", CLI_SOURCE, command="peer-cli"
        )
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(executable),
                "--index",
                index.as_uri(),
                "numpy",
                "mcp-console-test-cli",
            ],
            check=True,
            capture_output=True,
        )
        modules = root / "modules"
        modules.mkdir()
        # Inspection runs isolated; this hook observes the embedded interpreter
        # and ordinary children after Console has applied the shared contract.
        # fmt: python
        hook = code("""
            import builtins
            import os
            import shutil
            import sys

            builtins.peer_bootstrap_count = getattr(builtins, "peer_bootstrap_count", 0) + 1
            builtins.peer_bootstrap = (sys.prefix, os.environ.get("VIRTUAL_ENV"), shutil.which("python"))
            """)
        (modules / "sitecustomize.py").write_text(hook)
        (modules / "peer_module.py").write_text("value = 41\n")
        # CPython's pyvenv.cfg is the environment owner. A bridge must not run
        # an activation script again after the interpreter is already live.
        executable.with_name("activate_this.py").write_text(
            "raise AssertionError('bridge reactivated the selected interpreter')\n"
        )
        reference = None
        records = None
        for mode in (
            "without-r",
            "python-first",
            "python-first-relative",
            "python-first-command",
            "r-first",
        ):
            workspace = root if mode == "python-first-relative" else root / mode
            if workspace != root:
                workspace.mkdir()
            (workspace / "after-startup").mkdir()
            selection = (
                os.path.relpath(executable, workspace)
                if mode == "python-first-relative"
                else str(executable)
            )
            environment = dict(
                os.environ,
                RETICULATE_PYTHON=selection,
                MCP_CONSOLE_TEST_PYTHON=str(workspace.resolve() / selection),
                MCP_CONSOLE_TEST_PYTHON_PREFIX=str(venv),
                MCP_CONSOLE_TEST_OTHER_PYTHON=str(virtualenv_python(other)),
                PYTHONPATH=str(root / "unselected-modules"),
                RETICULATE_PYTHONPATH=str(modules),
            )
            if mode == "python-first-command":
                environment["RETICULATE_PYTHON"] = "python"
                environment["PATH"] = os.pathsep.join(
                    (str(executable.parent), environment["PATH"])
                )
            if mode == "without-r":
                without_r(environment, workspace)
            with McpClient(binary, execution.serve(), environment, workspace) as client:
                client.initialize_and_list_tools()
                if mode == "r-first":
                    client.expect(
                        r='reticulate::py_run_string("peer_from_r = object()")',
                    )
                # fmt: python
                source = code("""
                    import os
                    import sys
                    import json
                    import subprocess
                    import builtins
                    import peer_module

                    assert peer_module.value == 41
                    assert os.path.samefile(
                        peer_module.__file__,
                        os.path.join(os.environ["RETICULATE_PYTHONPATH"], "peer_module.py"),
                    )
                    assert os.path.samefile(sys.executable, os.environ["MCP_CONSOLE_TEST_PYTHON"])
                    assert os.path.realpath(sys.prefix) == os.path.realpath(
                        os.environ["MCP_CONSOLE_TEST_PYTHON_PREFIX"]
                    )
                    assert os.environ["PYTHONPATH"] == os.environ["RETICULATE_PYTHONPATH"]
                    assert builtins.peer_bootstrap_count == 1
                    assert all(
                        os.path.samefile(a, b)
                        for a, b in zip(
                            builtins.peer_bootstrap, (sys.prefix, sys.prefix, sys.executable), strict=True
                        )
                    ), builtins.peer_bootstrap
                    expected = [
                        sys.executable,
                        sys.prefix,
                        sys.exec_prefix,
                        sys.base_prefix,
                        sys.base_exec_prefix,
                    ]
                    assert sys.executable == os.environ["MCP_CONSOLE_TEST_PYTHON"], (
                        sys.executable,
                        os.environ["MCP_CONSOLE_TEST_PYTHON"],
                    )
                    assert os.path.dirname(sys.executable) not in sys.path, sys.path
                    assert os.environ["VIRTUAL_ENV"] == sys.prefix
                    program = "import sys, json; print(json.dumps([sys.executable, sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix]))"
                    for command in (sys.executable, "python"):
                        child = json.loads(subprocess.check_output([command, "-c", program], text=True))
                        assert all(os.path.samefile(a, b) for a, b in zip(child, expected, strict=True)), (
                            child,
                            expected,
                        )
                    print("selected environment retained by interpreter and children")
                    """)
                client.send(python=source)
                actual = last_result_text(client)
                assert (
                    actual
                    == "selected environment retained by interpreter and children\n"
                ), actual
                if reference is None:
                    reference = actual
                    records = client.finish()[3:]
                else:
                    assert actual == reference
                    client.expect(
                        python="peer_object = object(); peer_id = id(peer_object); os.chdir('after-startup')",
                    )
                    client.expect(r=CLI_CHECK)
                    if mode == "python-first-relative":
                        # A genuinely changed selection still fails before
                        # attachment, and restoring the original hint retries.
                        client.expect(
                            r=code("""
                            original <- Sys.getenv("RETICULATE_PYTHON")
                            for (selection in c("/incompatible-python", "managed", Sys.getenv("MCP_CONSOLE_TEST_OTHER_PYTHON"))) {
                              Sys.setenv(RETICULATE_PYTHON = selection)
                              failure <- tryCatch(reticulate::py_config(), error = conditionMessage)
                              stopifnot(identical(failure, "Python is already initialized with another selection; restart required"))
                            }
                            Sys.setenv(RETICULATE_PYTHON = original)
                            """),
                        )
                    client.expect(
                        r=code("""
                        requested <- Sys.getenv("MCP_CONSOLE_TEST_OTHER_PYTHON")
                        failure <- tryCatch(
                          reticulate::use_python(requested, required = TRUE),
                          error = conditionMessage
                        )
                        stopifnot(identical(failure, "Python is already initialized with another selection; restart required"))
                        # Executable aliases within the selected environment remain valid.
                        suppressWarnings(reticulate::use_python(
                          file.path(dirname(Sys.getenv("MCP_CONSOLE_TEST_PYTHON")), if (.Platform$OS.type == "windows") "python.exe" else "python3"),
                          required = TRUE
                        ))
                        """),
                    )
                    client.expect(
                        "installed CLI uses the selected Python environment\n",
                        r='check_cli("peer-cli", "mcp_console_test_cli")',
                    )
                    # fmt: r
                    client.expect(
                        r=code("""
                        actual <- reticulate::py_config()$virtualenv_activate
                        expected <- file.path(
                          dirname(Sys.getenv("MCP_CONSOLE_TEST_PYTHON")),
                          "activate_this.py"
                        )
                        stopifnot(identical(
                          normalizePath(actual, mustWork = TRUE),
                          normalizePath(expected, mustWork = TRUE)
                        ))
                        """),
                    )
                    client.expect(
                        python="assert id(peer_object) == peer_id; assert builtins.peer_bootstrap_count == 1",
                    )
                    client.finish()
        assert records is not None
        return records


@requires(R, NATIVE_FIXTURES)
@executions(DIRECT, SANDBOXED)
def test_r_does_not_initialize_python(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        probe = build_interposer(root, "python_initialized")
        # Use the public interrupted-selection state, before CPython loads.
        # R evaluation must not independently retry the optional Python setup.
        with deferred_selection_client(binary, execution.serve()) as client:
            # fmt: r
            source = code("""
                probe <- dyn.load(PROBE_PATH)
                r_value <- new.env()
                r_value$answer <- 42L
                r_answer <- 42L
                python_initialized <- function() {
                  .C(
                    getNativeSymbolInfo(
                      "mcp_console_probe_python_initialized",
                      PACKAGE = probe
                    ),
                    value = 0L
                  )$value
                }
                stopifnot(python_initialized() == -1L)
                stopifnot(!reticulate::py_available(initialize = FALSE))
                r_value$answer
                """).replace("PROBE_PATH", json.dumps(str(probe)))
            client.expect("[1] 42\n", r=source)
            client.transcript[-1]["send"]["r"] = source.replace(
                str(probe), "<Python initialization probe>"
            )
            client.expect(
                "42\n",
                python="peer_object = object(); peer_identity = id(peer_object); int(r.r_answer)",
            )
            client.expect(
                "[1] 42\n",
                r="stopifnot(python_initialized() == 1L); r_value$answer",
            )
            client.expect(
                "runtime objects retained\n",
                python="assert id(peer_object) == peer_identity; print('runtime objects retained')",
            )
            return client.finish()[3:]


@requires(R, command("uv"))
@executions(DIRECT, SANDBOXED)
def test_shared_managed_bootstrap_and_replacement(
    binary: Path, execution: Execution
) -> Transcript:
    records = None
    for mode in ("without-r", "python-first", "r-first"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            index = write_test_wheel(root, "mcp_console_test_added", "answer = 42\n")
            environment = dict(
                os.environ, UV_INDEX=index.as_uri(), UV_INDEX_STRATEGY="first-index"
            )
            environment.pop("RETICULATE_PYTHON", None)
            if mode == "python-first":
                environment["RETICULATE_PYTHON"] = "managed"
            with_r = mode != "without-r"
            if not with_r:
                uv = shutil.which("uv")
                assert uv is not None
                without_r(environment, root)
                shutil.copyfile(
                    uv, Path(environment["PATH"]) / "uv.exe"
                ) if os.name == "nt" else (Path(environment["PATH"]) / "uv").symlink_to(
                    uv
                )
            with McpClient(
                binary,
                execution.serve("-c", "python.managed.resolution=implicit"),
                environment,
                root,
            ) as client:
                client.initialize_and_list_tools()
                defaults = client.send(requirements={"action": "get"})[
                    "structuredContent"
                ]["requirements"]["python"]
                assert "numpy" in defaults, defaults
                # Inspect the complete public response before recording the Python fields
                # owned by this case; provider inventories have separate coverage.
                assert (
                    json.loads(last_result_text(client))
                    == client.transcript[-1]["result"]["structuredContent"]
                )
                client.transcript[-1]["result"] = {
                    "default_python_requirements": defaults
                }
                if mode == "r-first":
                    client.expect(
                        r="stopifnot(!reticulate::py_available(initialize = FALSE))",
                    )
                client.send(control="restart", requirements={"python": ["py-yaml12"]})
                assert not client.transcript[-1]["result"].get("isError"), (
                    client.transcript[-1]
                )
                # fmt: python
                source = code("""
                    import json
                    import os
                    import subprocess
                    import sys
                    import yaml12
                    import numpy as np

                    assert np.arange(3).tolist() == [0, 1, 2]

                    peer_object = object()
                    peer_id = id(peer_object)
                    peer_library = sys.base_prefix
                    expected = [
                        sys.executable,
                        sys.prefix,
                        sys.exec_prefix,
                        sys.base_prefix,
                        sys.base_exec_prefix,
                    ]
                    program = "import sys, json; print(json.dumps([sys.executable, sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix]))"
                    child = json.loads(subprocess.check_output([sys.executable, "-c", program], text=True))
                    assert all(os.path.samefile(a, b) for a, b in zip(child, expected, strict=True)), (
                        child,
                        expected,
                    )
                    assert os.environ["VIRTUAL_ENV"] == sys.prefix
                    print("managed identity and child environment agree")
                    """)
                client.expect(
                    "managed identity and child environment agree\n",
                    python=source,
                )
                if with_r:
                    client.expect(
                        r="stopifnot(isTRUE(reticulate::py_config()$ephemeral))",
                    )
                client.expect(
                    "live import retained objects\n",
                    python="import mcp_console_test_added; assert mcp_console_test_added.answer == 42; assert id(peer_object) == peer_id; assert os.path.samefile(sys.base_prefix, peer_library); print('live import retained objects')",
                )
                client.send(python="raise ValueError('after accepted activation')")
                assert last_result_text(client).endswith(
                    "ValueError: after accepted activation\n"
                ), client.transcript[-1]
                client.send(control="restart")
                client.expect(
                    "accepted additions survived restart\n",
                    python="import yaml12, mcp_console_test_added; assert mcp_console_test_added.answer == 42; print('accepted additions survived restart')",
                )
                client.send(python="import os; os._exit(47)")
                client.expect(
                    "accepted additions survived crash\n",
                    python="import yaml12, mcp_console_test_added; assert mcp_console_test_added.answer == 42; print('accepted additions survived crash')",
                )
                current = client.finish()[3:]
                if not with_r:
                    records = current
    assert records is not None
    return records


@requires(R, command("uv"))
@executions(DIRECT, SANDBOXED)
def test_r_commands_follow_managed_python_activation(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        index = write_test_wheel(
            root, "mcp_console_test_cli", CLI_SOURCE, command="peer-cli"
        )
        write_test_wheel(
            root, "mcp_console_test_cli_added", CLI_SOURCE, command="peer-cli-added"
        )
        environment = dict(
            os.environ, UV_INDEX=index.as_uri(), UV_INDEX_STRATEGY="first-index"
        )
        environment.pop("RETICULATE_PYTHON", None)
        with McpClient(binary, execution.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            client.expect(
                "[prepared]", requirements={"python": ["mcp-console-test-cli"]}
            )
            client.expect(
                python="import numpy as np; peer_object = object(); peer_id = id(peer_object)",
            )
            client.expect(r=CLI_CHECK)
            client.expect(
                "installed CLI uses the selected Python environment\n",
                r='check_cli("peer-cli", "mcp_console_test_cli"); stopifnot(Sys.which("peer-cli-added") == "")',
            )
            client.expect(
                "[prepared]",
                requirements={"python": ["mcp-console-test-cli-added"]},
            )
            client.expect(
                python="assert id(peer_object) == peer_id; assert np.arange(3).tolist() == [0, 1, 2]",
            )
            for command, module in (
                ("peer-cli", "mcp_console_test_cli"),
                ("peer-cli-added", "mcp_console_test_cli_added"),
            ):
                client.expect(
                    "installed CLI uses the selected Python environment\n",
                    r=f'check_cli("{command}", "{module}")',
                )
            client.send(control="restart")
            client.expect(r=CLI_CHECK)
            client.expect(
                "installed CLI uses the selected Python environment\n",
                r='check_cli("peer-cli-added", "mcp_console_test_cli_added")',
            )
            return client.finish()[3:]


@requires(POSIX)
@requires(R)
@executions(DIRECT, SANDBOXED)
def test_r_startup_uses_initialized_python(
    binary: Path, execution: Execution
) -> Transcript:
    return r_startup_with_python(binary, execution, managed=False)


@requires(POSIX)
@requires(R, command("uv"))
@executions(DIRECT, SANDBOXED)
def test_managed_import_after_r_startup(
    binary: Path, execution: Execution
) -> Transcript:
    return r_startup_with_python(binary, execution, managed=True)


@requires(POSIX)
@requires(R)
@executions(DIRECT, SANDBOXED)
def test_late_r_startup_preserves_plot_capture(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    def startup(trigger: str) -> Transcript:
        return r_startup_with_python(
            binary,
            execution,
            managed=False,
            python_first=True,
            plot_after_startup=True,
            trigger=trigger,
        )

    return TranscriptWithCompanions(
        startup("r-cell"),
        {"python-access.yaml": McpTranscript(startup("python-access"))},
    )


@requires(POSIX)
@requires(R)
@executions(DIRECT, SANDBOXED)
@execution_snapshots
def test_system_default_packages_survive_late_r_startup(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    return TranscriptWithCompanions(
        r_startup_with_python(
            binary, execution, managed=False, system_default_packages=True
        ),
        {
            "python-first.yaml": McpTranscript(
                r_startup_with_python(
                    binary,
                    execution,
                    managed=False,
                    python_first=True,
                    system_default_packages=True,
                )
            )
        },
    )


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_unusable_installed_numpy_metadata_does_not_block_startup(
    binary: Path, execution: Execution
) -> Transcript:
    return unusable_numpy_metadata(binary, execution, configured_path=False)


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_unusable_configured_numpy_metadata_does_not_block_attachment(
    binary: Path, execution: Execution
) -> Transcript:
    return unusable_numpy_metadata(binary, execution, configured_path=True)


def unusable_numpy_metadata(
    binary: Path, execution: Execution, *, configured_path: bool
) -> Transcript:
    for metadata in (
        b"Name: numpy\n",
        b"\xff",
        b"Name: numpy\nVersion: invalid\n",
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = root / "venv"
            subprocess.run(
                ["uv", "venv", "--python", sys.executable, str(selected)],
                capture_output=True,
                check=True,
            )
            executable = virtualenv_python(selected)
            if configured_path:
                modules = root / "configured modules"
                modules.mkdir()
            else:
                modules = Path(
                    subprocess.check_output(
                        [
                            executable,
                            "-I",
                            "-c",
                            "import sysconfig; print(sysconfig.get_path('purelib'))",
                        ],
                        text=True,
                    ).strip()
                )
            package = modules / "numpy"
            package.mkdir()
            package.joinpath("__init__.py").write_text(
                "raise RuntimeError('metadata collection imported optional NumPy')\n"
            )
            distribution = modules / "numpy-0.0.0.dist-info"
            distribution.mkdir()
            distribution.joinpath("METADATA").write_bytes(metadata)
            environment, _ = r_test_environment()
            environment.pop("PYTHONPATH", None)
            environment.pop("RETICULATE_PYTHONPATH", None)
            environment.update(
                RETICULATE_PYTHON=str(executable),
                MCP_CONSOLE_LANGUAGES="r,python",
                RETICULATE_CHECK_REQUIRED_PACKAGES="false",
            )
            if configured_path:
                environment["PYTHONPATH"] = str(modules)
            arguments = execution.serve(
                *(("--writable-root", str(root)) if execution == SANDBOXED else ())
            )
            with McpClient(binary, arguments, environment, root) as client:
                client.initialize_and_list_tools()
                client.expect(
                    "unusable optional NumPy metadata absent\n",
                    # fmt: r
                    r=code("""
                        stopifnot(is.null(reticulate::py_config()$numpy))
                        stopifnot(reticulate::py_eval("'numpy' not in __import__('sys').modules"))
                        cat("unusable optional NumPy metadata absent\\n")
                        """),
                )
                client.expect(
                    "Python remains available\n",
                    python="print('Python remains available')",
                )
                client.finish()
    return [{"unusable_numpy_metadata_is_absent": True}]


@requires(POSIX)
@requires(R)
@executions(DIRECT, SANDBOXED)
def test_conversion_metadata_matches_configured_import_paths(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        selected = root / "venv"
        modules = root / "configured modules"
        extra = root / "startup modules"
        modules.mkdir()
        extra.mkdir()
        subprocess.run(
            ["uv", "venv", "--python", sys.executable, str(selected)],
            capture_output=True,
            check=True,
        )
        executable = virtualenv_python(selected)
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(executable),
                "--target",
                str(modules),
                "numpy",
            ],
            capture_output=True,
            check=True,
        )
        (modules / "sitecustomize.py").write_text(
            code(f"""
                import sys
                if "_mcp_console_services" in sys.modules:
                    sys.path.append({str(extra)!r})
                """)
        )
        worker = root / "worker"
        worker.write_text(
            "#!/bin/sh\nexec " + shlex.join([str(binary), "worker"]) + "\n"
        )
        worker.chmod(0o755)
        for variable, lazy in (
            ("PYTHONPATH", False),
            ("RETICULATE_PYTHONPATH", False),
            ("PYTHONPATH", True),
            ("RETICULATE_PYTHONPATH", True),
        ):
            environment, _ = r_test_environment()
            environment.pop("RETICULATE_PYTHONPATH", None)
            environment.update(
                RETICULATE_PYTHON=str(executable),
                MCP_CONSOLE_LANGUAGES="r,python",
                # NumPy intentionally lives on configured paths, outside the
                # virtualenv that reticulate's package probe inspects.
                RETICULATE_CHECK_REQUIRED_PACKAGES="false",
                TMPDIR=str(root),
            )
            environment[variable] = str(modules)
            arguments = execution.serve(
                *(("--worker", str(worker)) if lazy else ()),
                *(("--writable-root", str(root)) if execution == SANDBOXED else ()),
            )
            with McpClient(binary, arguments, environment, root) as client:
                client.initialize_and_list_tools()
                client.expect(
                    "live conversion metadata retained\n",
                    # fmt: r
                    r=code("""
                        config <- reticulate::py_config()
                        sys <- reticulate::import("sys")
                        numpy <- reticulate::import("numpy")
                        stopifnot(
                          identical(config$pythonpath, paste(sys$path, collapse = .Platform$path.sep)),
                          identical(
                            normalizePath(config$numpy$path),
                            normalizePath(numpy$`__path__`[[1L]])
                          ),
                          identical(as.character(config$numpy$version), numpy$`__version__`)
                        )
                        cat("live conversion metadata retained\\n")
                        """),
                )
                client.finish()
        return [
            {
                "configured_import_paths": ["PYTHONPATH", "RETICULATE_PYTHONPATH"],
                "live_numpy_metadata": True,
            }
        ]


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_conversion_metadata_does_not_import_shadowed_numpy(
    binary: Path, execution: Execution
) -> Transcript:
    for package in (False, True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = root / "venv"
            subprocess.run(
                ["uv", "venv", "--python", sys.executable, str(selected)],
                capture_output=True,
                check=True,
            )
            executable = virtualenv_python(selected)
            subprocess.run(
                ["uv", "pip", "install", "--python", str(executable), "numpy"],
                capture_output=True,
                check=True,
            )
            shadow = root / "numpy.py"
            if package:
                (root / "numpy").mkdir()
                shadow = root / "numpy/__init__.py"
            marker = root / "numpy-imported"
            shadow.write_text(
                # fmt: python
                code(f"""
                    import sys
                    if "_mcp_console_services" in sys.modules:
                        from pathlib import Path
                        Path({str(marker)!r}).touch()
                        raise RuntimeError("workspace NumPy imported during metadata collection")
                    __version__ = "0.0.0"
                    """)
            )
            environment, _ = r_test_environment()
            environment.update(
                RETICULATE_PYTHON=str(executable),
                MCP_CONSOLE_LANGUAGES="r,python",
                RETICULATE_CHECK_REQUIRED_PACKAGES="false",
            )
            arguments = execution.serve(
                *(("--writable-root", str(root)) if execution == SANDBOXED else ())
            )
            with McpClient(binary, arguments, environment, root) as client:
                client.initialize_and_list_tools()
                client.expect(
                    "shadowed NumPy metadata absent\n",
                    # fmt: r
                    r=code("""
                        stopifnot(is.null(reticulate::py_config()$numpy))
                        stopifnot(reticulate::py_eval("'numpy' not in __import__('sys').modules"))
                        cat("shadowed NumPy metadata absent\\n")
                        """),
                )
                client.expect(
                    "Python remains available\n",
                    python="print('Python remains available')",
                )
                client.finish()
            assert not marker.exists(), (
                "metadata executed the workspace NumPy candidate"
            )
    return [{"shadowed_numpy_module_and_package_remain_unimported": True}]


@requires(POSIX)
@requires(R)
@executions(DIRECT, SANDBOXED)
def test_attaches_to_reticulate_initialized_by_r_startup(
    binary: Path, execution: Execution
) -> Transcript:
    _, library = installed_early_python_library()
    environment, _ = r_test_environment()
    environment.update(
        MCP_CONSOLE_LANGUAGES="r",
        R_LIBS=os.pathsep.join(filter(None, (str(library), environment.get("R_LIBS")))),
        R_DEFAULT_PACKAGES="datasets,utils,grDevices,graphics,stats,methods,mcpconsoleearlypython",
        RETICULATE_PYTHON=sys.executable,
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        environment["TMPDIR"] = str(root)
        worker = root / "worker"
        worker.write_text(
            "#!/bin/sh\nexec " + shlex.join([str(binary), "worker"]) + "\n"
        )
        worker.chmod(0o755)
        with McpClient(
            binary, execution.serve("--worker", str(worker)), environment, root
        ) as client:
            client.initialize_and_list_tools()
            # Custom workers retain lazy startup. The first R cell lets its startup
            # package initialize reticulate before Console installs its adapter.
            client.expect(
                "attached to startup Python\n",
                # fmt: r
                r=code("""
                    reticulate::py_run_string(
                      "assert id(early_object) == early_identity; assert sys.executable == early_executable; assert (sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix) == early_prefixes"
                    )
                    stopifnot(identical(reticulate::py_eval("{'answer': 42}"), list(answer = 42L)))
                    cat("attached to startup Python\\n")
                    """),
            )
            client.expect(
                "same interpreter retained\n",
                r="stopifnot(reticulate::py_eval('id(early_object) == early_identity')); cat('same interpreter retained\\n')",
            )
            client.finish()
            return [
                {"startup_interpreter_attached": True, "conversion_available": True}
            ]


@cache
def installed_early_python_library() -> tuple[tempfile.TemporaryDirectory, Path]:
    # Reuse only immutable package files across this case's execution modes.
    # The real load/attach hooks still run in each fresh Console worker.
    temporary = tempfile.TemporaryDirectory(prefix="mcp-console-early-python-")
    library = Path(temporary.name)
    environment, rscript = r_test_environment()
    fixture = Path(__file__).resolve().parents[3] / "fixtures/early_python"
    try:
        subprocess.run(
            [
                rscript.with_name("R"),
                "CMD",
                "INSTALL",
                "--no-test-load",
                f"--library={library}",
                fixture,
            ],
            check=True,
            capture_output=True,
            env=environment,
        )
    except BaseException:
        temporary.cleanup()
        raise
    return temporary, library


def r_startup_with_python(
    binary: Path,
    execution: Execution,
    *,
    managed: bool,
    python_first: bool = False,
    plot_after_startup: bool = False,
    system_default_packages: bool = False,
    trigger: str = "r-cell",
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory, ExitStack() as checkpoints:
        root = Path(directory)
        _, library = installed_early_python_library()
        environment, rscript = r_test_environment()
        # fmt: r
        plots = code("""
            graphics::plot(1:3)
            graphics::plot(3:1)
            """)
        expected_plots = (
            reference_plots(
                rscript,
                environment,
                plots,
                width=800 / 96,
                height=600 / 96,
                dpi=96,
                pages=2,
            )
            if plot_after_startup
            else []
        )
        environment.update(
            R_LIBS=os.pathsep.join(
                filter(None, (str(library), environment.get("R_LIBS")))
            ),
            R_DEFAULT_PACKAGES="datasets,utils,grDevices,graphics,stats,methods,mcpconsoleearlypython",
            RETICULATE_PYTHON=sys.executable,
            MCP_CONSOLE_TEST_PYTHON=sys.executable,
            MCP_CONSOLE_TEST_PYTHON_PREFIX=sys.prefix,
        )
        if system_default_packages:
            home = isolated_r_home(root, environment)
            with (home / "etc/Renviron").open("a") as stream:
                stream.write(
                    "\nR_DEFAULT_PACKAGES=" + environment["R_DEFAULT_PACKAGES"] + "\n"
                )
            environment["R_DEFAULT_PACKAGES"] = "NULL"
            ready = FifoCheckpoint.create(root / "startup-ready")
            release = FifoCheckpoint.create(root / "startup-release")
            checkpoints.callback(ready.close)
            checkpoints.callback(release.close)
            environment["MCP_CONSOLE_TEST_STARTUP_READY"] = str(ready.path)
            environment["MCP_CONSOLE_TEST_STARTUP_RELEASE"] = str(release.path)
        if managed:
            environment.pop("RETICULATE_PYTHON")
            # Eager bootstrap selects the prepared managed interpreter before
            # R packages run. The package attaches to that same identity.
            version = "==" + ".".join(map(str, sys.version_info[:3]))
        if python_first:
            modules = root / "modules"
            modules.mkdir()
            (modules / "sitecustomize.py").write_text(DEFER_R_STARTUP)
            environment["RETICULATE_PYTHONPATH"] = str(modules)
        serve = (
            execution.serve("--writable-root", str(root))
            if system_default_packages and execution == SANDBOXED
            else execution.serve()
        )
        if managed:
            serve += ("-c", "python.managed.resolution=implicit")
        with McpClient(binary, serve, environment, root) as client:
            client.initialize_and_list_tools()
            if managed:
                client.expect(
                    None, control="restart", requirements={"python_version": [version]}
                )
            if python_first:
                defer_r_bootstrap(client)
                collected = send_and_collect_runtime_python_resolution(
                    client,
                    python="before_r = object(); before_r_identity = id(before_r)",
                )
                assert collected == "[done]", client.transcript[-1]
            # The startup package attaches to the interpreter selected by
            # bootstrap, including after an interrupted Python-first setup.
            if system_default_packages:
                # Eager R startup can be observed before admission. Python-first
                # setup deliberately defers R until this cell reaches it.
                try:
                    if not python_first:
                        ready.wait("R startup package", timeout=client.response_timeout)
                        inspected = client.send(requirements={"action": "get"})
                        assert (
                            not inspected["isError"]
                            and "structuredContent" in inspected
                        )
                    startup = client.start_send(r="invisible(NULL)", timeout_ms=0)
                    if python_first:
                        ready.wait("R startup package", timeout=client.response_timeout)
                    client.receive(startup)
                    assert without_elapsed(last_result_text(client)) == (
                        "\n[running; poll with an empty send]"
                    ), client.transcript[-1]
                finally:
                    release.release()
                client.send(session="default")
            elif plot_after_startup:
                # Check identity in the startup cell so plotting remains the
                # first follow-up cell through the R graphics lifecycle.
                identity = (
                    "id(before_r) == before_r_identity and "
                    "id(early_object) == early_identity and "
                    "sys.executable == early_executable"
                )
                if trigger == "python-access":
                    client.send(
                        # fmt: python
                        python=code("""
                            assert 3 < r.pi < 4
                            assert IDENTITY
                            """).replace("IDENTITY", identity)
                    )
                else:
                    client.send(
                        r=f"stopifnot(reticulate::py_eval({json.dumps(identity)}))"
                    )
            else:
                client.send(r="invisible(NULL)")
            assert last_result_text(client) == "[done]", client.transcript[-1]
            if plot_after_startup:
                client.send(r=plots)
                assert_result_content(client, expected_plots)
            client.expect(
                # fmt: r
                r=code("""
                    stopifnot(
                      "mcpconsoleearlypython" %in% getOption("defaultPackages"),
                      identical(isTRUE(reticulate::py_config()$ephemeral), MANAGED_PYTHON),
                      identical(search()[[2L]], "tools:mcp-console"),
                      identical(find("py")[[1L]], "tools:mcp-console"),
                      identical(find(".console")[[1L]], "tools:mcp-console")
                    )
                    """).replace("MANAGED_PYTHON", "TRUE" if managed else "FALSE"),
            )
            if python_first:
                client.expect(
                    "startup package attached to running Python\n",
                    python="assert id(before_r) == before_r_identity; assert early_executable == sys.executable; print('startup package attached to running Python')",
                )
                client.expect(
                    # fmt: r
                    r=code("""
                        incompatible <- tryCatch(
                          reticulate::use_python("/incompatible-python", required = TRUE),
                          error = conditionMessage
                        )
                        stopifnot(identical(
                          incompatible,
                          "Python is already initialized with another selection; restart required"
                        ))
                        """),
                )
            client.expect(
                "attached to the existing interpreter\n",
                # fmt: python
                python=code("""
                    import json
                    import subprocess

                    if not MANAGED_PYTHON:
                        assert os.path.samefile(sys.executable, os.environ["MCP_CONSOLE_TEST_PYTHON"])
                        assert os.path.realpath(sys.prefix) == os.path.realpath(
                            os.environ["MCP_CONSOLE_TEST_PYTHON_PREFIX"]
                        )
                    assert id(early_object) == early_identity
                    assert sys.executable == early_executable
                    assert (
                        sys.prefix,
                        sys.exec_prefix,
                        sys.base_prefix,
                        sys.base_exec_prefix,
                    ) == early_prefixes
                    assert os.environ["PATH"] == early_path
                    child = json.loads(
                        subprocess.check_output([sys.executable, "-c", early_child_program], text=True)
                    )
                    assert child == early_child, (child, early_child)
                    print("attached to the existing interpreter")
                    """).replace("MANAGED_PYTHON", str(managed)),
            )
            client.expect(
                "[1] TRUE\n",
                r='reticulate::py_eval("id(early_object) == early_identity")',
            )
            if managed:
                client.expect(
                    "[resolved PyPI distribution 'py-yaml12' for Python import 'yaml12']\nadopted interpreter resolved import\n",
                    python="import yaml12; assert id(early_object) == early_identity; print('adopted interpreter resolved import')",
                )
                accepted = client.send(requirements={"action": "get"})[
                    "structuredContent"
                ]["requirements"]
                assert "py-yaml12" in accepted["python"], accepted
                assert accepted["python_version"] == [version], accepted
                client.send(control="restart")
                # The startup package chooses its environment again before
                # Console attaches. Server-retained declarations survive that
                # adoption; it does not replay activation into the live runtime.
                retained = client.send(requirements={"action": "get"})[
                    "structuredContent"
                ]["requirements"]
                assert retained == accepted, (retained, accepted)
            records = client.finish()
            if not (system_default_packages or plot_after_startup):
                records = records[3:]
            if managed:
                records = json.loads(
                    json.dumps(records).replace(version, "<selected Python version>")
                )
            return records


@requires(POSIX)
@requires(R, command("uv"))
@executions(DIRECT, SANDBOXED)
def test_shared_managed_import_failures(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    from boundaries.client_server.python.test_without_r import (
        automatic_activation_failure_requires_restart,
        automatic_resolution_failure_and_cancel_keep_accepted_state,
    )

    # The owning no-R cases already run these exact helpers with with_r=False.
    # Keep each R-enabled invocation as its own transcript rather than recording
    # another no-R copy and discarding the R-enabled evidence.
    failures = automatic_resolution_failure_and_cancel_keep_accepted_state(
        binary, execution, with_r=True
    )
    activation = automatic_activation_failure_requires_restart(
        binary, execution, with_r=True
    )
    return TranscriptWithCompanions(
        [
            {
                "scenario": "Recover from failed and cancelled automatic Python preparation.",
                "fixture": (
                    "R is available. A wrapper around real uv fails one resolution, "
                    "then blocks another behind FIFOs so the client can interrupt it. "
                    "The accepted state is checked before a successful retry."
                ),
            }
        ]
        + failures,
        {
            "activation.yaml": McpTranscript(
                [
                    {
                        "scenario": "Require a restart after automatic Python activation fails.",
                        "fixture": (
                            "R is available. The uv fixture injects an activation failure "
                            "after preparation; the existing worker remains usable, but "
                            "further requirement changes require a restart."
                        ),
                    }
                ]
                + activation
            )
        },
    )


@requires(POSIX)
@requires(R, command("uv"))
@executions(DIRECT, SANDBOXED)
def test_shared_managed_tool_activation_failure(
    binary: Path, execution: Execution
) -> Transcript:
    from boundaries.client_server.python.test_without_r import (
        live_python_activation_failure_requires_restart,
    )

    # The no-R owner runs the same helper without R; do not repeat that session.
    transcript = live_python_activation_failure_requires_restart(
        binary, execution, with_r=True
    )
    return [
        {
            "scenario": "Withhold submitted code when live Python activation fails.",
            "fixture": (
                "R is available. The uv fixture injects an activation failure while "
                "fd 2 is redirected to a temporary file. The case checks sideband "
                "diagnostic delivery and preservation of accepted requirements."
            ),
        }
    ] + transcript
