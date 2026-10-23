#!/usr/bin/env -S uv run --script

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.snapshots import execution_snapshots
from support.assertions import last_result_text, wait_for_evaluation_output
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.requirements import (
    FRAMEWORK_PYTHON,
    PYTHON_FRAMEWORK,
    R,
    POSIX,
    SQL,
    requires,
)
from support.r import install_r_startup, r_test_environment
from support.resolvers import bare_runtime_environment
from support.python import isolated_python
from support.linux_sandbox import retain_system_bwrap
from support.suites import run_this_suite
from boundaries.client_server.python.test_without_r import (
    environment as without_r_environment,
)


def selected_python(directory: Path, python: Path) -> dict[str, str]:
    retain_system_bwrap(directory)
    environment = bare_runtime_environment(os.environ.copy(), directory / "r-library")
    environment["RETICULATE_PYTHON"] = str(python)
    return environment


def startup_input(binary: Path, execution: Execution, hook: str) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        python, site = isolated_python(root)
        # Discovery runs without input; embedded startup uses Console's bridge.
        # fmt: python
        source = code("""
            import builtins
            import sys

            if sys.argv[0] != "-c":
                builtins.startup_attempts = getattr(builtins, "startup_attempts", 0) + 1
                builtins.startup_input = input("startup> ")
                if builtins.startup_attempts == 1:
                    raise KeyboardInterrupt("retry startup")
            """)
        (site / f"{hook}.py").write_text(source)
        if hook != "sitecustomize":
            (site / "console-startup.pth").write_text(f"import {hook}\n")
        environment = selected_python(root, python)
        environment["PYTHONPATH"] = str(site)
        environment["PYTHONNODEBUGRANGES"] = "1"
        environment["RETICULATE_PYTHONPATH"] = str(site)
        with McpClient(binary, execution.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            client.send(python="never_run = True")
            assert last_result_text(client) == (
                '[input requested: "startup> "]\n[waiting for stdin]'
            ), last_result_text(client)
            interrupted = wait_for_evaluation_output(
                client,
                lambda output: "KeyboardInterrupt" in output,
                "interrupted Python startup input",
                stdin="retry\n",
                timeout_ms=0,
            )
            result = client.transcript[-1]["result"]
            result["content"][0]["text"] = re.sub(
                r'(File "<frozen site>", line )\d+', r"\1<line>", interrupted
            )
            client.send(
                # fmt: python
                python=code("""
                    import builtins

                    assert "never_run" not in globals()
                    assert builtins.startup_attempts == 2
                    builtins.startup_input
                    """)
            )
            assert last_result_text(client) == (
                '[input requested: "startup> "]\n[waiting for stdin]'
            ), last_result_text(client)
            wait_for_evaluation_output(
                client,
                "'caf\u00e9\\x00tail'\n",
                "retried Python startup input",
                stdin="caf\u00e9\0tail\n",
                timeout_ms=0,
            )
            return json.loads(
                json.dumps(client.finish()).replace(
                    json.dumps(str(site) + os.sep)[1:-1], "<site-packages>/"
                )
            )


@executions(DIRECT, SANDBOXED)
def test_manages_sitecustomize_input_across_retries(
    binary: Path, execution: Execution
) -> list:
    return startup_input(binary, execution, "sitecustomize")


@executions(DIRECT, SANDBOXED)
def test_manages_pth_input_across_retries(binary: Path, execution: Execution) -> list:
    return startup_input(binary, execution, "console_startup")


@executions(DIRECT, SANDBOXED)
@requires(PYTHON_FRAMEWORK)
def test_embeds_framework_python(binary: Path, execution: Execution) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = selected_python(root, FRAMEWORK_PYTHON)
        with McpClient(binary, execution.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            client.send(
                # fmt: python
                python=code("""
                    answer = 41
                    answer + 1
                    """)
            )
            assert last_result_text(client) == "42\n", last_result_text(client)
            return client.finish()


@requires(R)
@executions(DIRECT, SANDBOXED)
@requires(POSIX)
def test_preserves_broken_pipe_errors_after_r_startup(
    binary: Path, execution: Execution
) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        python, _ = isolated_python(root)
        with McpClient(
            binary, execution.serve(), selected_python(root, python), root
        ) as client:
            client.initialize_and_list_tools()
            client.send(
                # fmt: python
                python=code("""
                    import os

                    reader, writer = os.pipe()
                    os.close(reader)
                    try:
                        os.write(writer, b"closed reader")
                    except BrokenPipeError:
                        print("caught Python broken pipe")
                    finally:
                        os.close(writer)
                    """)
            )
            assert last_result_text(client) == "caught Python broken pipe\n", (
                last_result_text(client)
            )
            client.send(r="1L + 1L")
            assert last_result_text(client) == "[1] 2\n", last_result_text(client)
            client.finish()
            return [{"broken_pipe_errors_survive_r_startup": True}]


@execution_snapshots
@executions(DIRECT, SANDBOXED)
def test_processes_initial_site_directories_once(
    binary: Path, execution: Execution
) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        python, site = isolated_python(root)
        # Executable .pth lines run each time the site directory is processed.
        # fmt: python
        source = code("""
            import builtins; builtins.console_startups = getattr(builtins, "console_startups", 0) + 1
            """)
        (site / "console-startup.pth").write_text(source)
        expected = int(
            subprocess.check_output(
                [python, "-c", "import builtins; print(builtins.console_startups)"],
                text=True,
            )
        )
        with McpClient(
            binary,
            execution.serve(
                *(("--writable-root", str(root)) if execution == SANDBOXED else ())
            ),
            selected_python(root, python),
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.send(
                # fmt: python
                python=code(f"""
                    import builtins
                    import sys

                    assert sys.flags.no_site == 0
                    builtins.console_startups == {expected}
                    """)
            )
            assert last_result_text(client) == "True\n", last_result_text(client)
            client.send(python=f"builtins.console_startups == {expected}")
            assert last_result_text(client) == "True\n"
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_retries_after_sql_runtime_installation_interrupt(
    binary: Path, execution: Execution
) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        python, site = isolated_python(root)
        # Interrupt after Console has installed its import hook, when the SQL
        # connection selector is being installed. The audit hook fires once.
        # fmt: python
        source = code("""
            import sys

            interrupted = False

            def interrupt_sql(event, arguments):
                global interrupted
                if event == "compile" and not interrupted:
                    if b"def sql_connection(" in arguments[0]:
                        interrupted = True
                        raise KeyboardInterrupt

            if sys.argv[0] != "-c":
                sys.addaudithook(interrupt_sql)
            """)
        (site / "sitecustomize.py").write_text(source)
        environment = selected_python(root, python)
        environment["PYTHONPATH"] = str(site)
        with McpClient(binary, execution.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            client.send(python="never_run = True")
            assert "KeyboardInterrupt" in last_result_text(client), last_result_text(
                client
            )
            client.send(
                # fmt: python
                python=code("""
                    import json

                    assert "never_run" not in globals()
                    assert callable(_console.sql_connection)
                    json.loads("42")
                    """)
            )
            assert last_result_text(client) == "42\n", last_result_text(client)
            return json.loads(
                json.dumps(client.finish()).replace(
                    json.dumps(str(site) + os.sep)[1:-1], "<site-packages>/"
                )
            )


def interrupted_initialization(
    binary: Path,
    execution: Execution,
    *,
    hook: str = "sitecustomize",
    r_first: bool = False,
    language: str = "python",
) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        python, site = isolated_python(root)
        marker = root / "startup.pid"
        worker_identity = root / "worker.pid"
        # The managed input notice acknowledges that the embedded hook is live.
        # SIGINT may reach any native thread once R has initialized.
        # fmt: python
        source = code(f"""
            import builtins
            import os
            import sys
            from pathlib import Path

            if sys.argv[0] != "-c" and (
                {not r_first!r}
                or (Path({str(worker_identity)!r}).exists()
                    and os.getpid() == int(Path({str(worker_identity)!r}).read_text()))
            ):
                builtins.startup_attempts = getattr(builtins, "startup_attempts", 0) + 1
                marker = Path({str(marker)!r})
                if not marker.exists():
                    marker.write_text(str(os.getpid()))
                    input("startup interrupt> ")
            """)
        (site / f"{hook}.py").write_text(source)
        if hook != "sitecustomize":
            (site / "console-startup.pth").write_text(f"import {hook}\n")
        environment = selected_python(root, python)
        if r_first:
            # Select Python from an R startup package, after R has initialized.
            # The controller has no explicit Python hint, so eager bootstrap
            # follows the unresolved R selection path before entering Python.
            r_environment, _ = r_test_environment()
            library = install_r_startup(
                root,
                r_environment,
                # fmt: r
                code(f"""
                Sys.setenv(RETICULATE_PYTHON = {
                  json.dumps(str(python))
                })
                writeLines(as.character(Sys.getpid()), {
                  json.dumps(str(worker_identity))
                })
                startup_state <- 41L
                    """),
            )
            environment = bare_runtime_environment(r_environment, library)
            if os.name == "nt":
                environment["PATH"] = os.pathsep.join(
                    entry
                    for entry in environment["PATH"].split(os.pathsep)
                    if not (Path(entry) / "python.exe").exists()
                )
        if language == "sql":
            commands = root / "no-r-commands"
            commands.mkdir()
            environment = dict(
                without_r_environment(commands), RETICULATE_PYTHON=str(python)
            )
        environment["PYTHONPATH"] = str(site)
        environment["PYTHONNODEBUGRANGES"] = "1"
        environment["RETICULATE_PYTHONPATH"] = str(site)
        with McpClient(
            binary,
            execution.serve(
                *(("--writable-root", str(root)) if execution == SANDBOXED else ())
            ),
            environment,
            root,
        ) as client:
            client.initialize_and_list_tools()
            cell = "never_run = True" if language == "python" else "SELECT 42"
            # Interpreter discovery/bootstrap precedes arrival at the hook.
            # The input notice, rather than the first response cut, admits SIGINT.
            client.expect(
                '[input requested: "startup interrupt> "]\n[waiting for stdin]',
                **{language: cell},
                timeout_ms=10_000,
            )
            worker = int(marker.read_text())
            if r_first:
                assert worker == int(worker_identity.read_text())
            client.send(control="interrupt", timeout_ms=10_000)
            interrupted = last_result_text(client)
            assert "KeyboardInterrupt" in interrupted, interrupted
            assert "[running; poll with an empty send]" not in interrupted, interrupted
            client.transcript[-1]["result"]["content"][0]["text"] = re.sub(
                r'(File "<frozen site>", line )\d+',
                r"\1<line>",
                interrupted,
            )
            client.send(
                # fmt: python
                python=code(f"""
                    import builtins
                    import os

                    assert os.getpid() == {worker}
                    assert builtins.startup_attempts == 2
                    "never_run" in globals()
                    """)
            )
            assert last_result_text(client) == "False\n", last_result_text(client)
            client.transcript[-1]["send"]["python"] = client.transcript[-1]["send"][
                "python"
            ].replace(f"== {worker}", "== <worker pid>")
            client.send(python="builtins.startup_attempts")
            assert last_result_text(client) == "2\n", last_result_text(client)
            if r_first:
                client.send(r="startup_state + 1L")
                assert last_result_text(client) == "[1] 42\n", last_result_text(client)
            transcript = json.dumps(client.finish())
            transcript = transcript.replace(
                json.dumps(str(site) + os.sep)[1:-1], "<site-packages>/"
            )
            transcript = transcript.replace(str(root), "<workspace>")
            return json.loads(transcript)


@execution_snapshots
@executions(DIRECT, SANDBOXED)
def test_interrupts_embedded_sitecustomize(binary: Path, execution: Execution) -> list:
    return interrupted_initialization(binary, execution)


@execution_snapshots
@executions(DIRECT, SANDBOXED)
def test_interrupts_embedded_pth(binary: Path, execution: Execution) -> list:
    return interrupted_initialization(binary, execution, hook="console_startup")


@execution_snapshots
@executions(DIRECT, SANDBOXED)
@requires(R)
def test_interrupts_embedded_startup_after_r(
    binary: Path, execution: Execution
) -> list:
    return interrupted_initialization(binary, execution, r_first=True)


@execution_snapshots
@executions(DIRECT, SANDBOXED)
@requires(SQL)
def test_interrupts_sql_first_embedded_startup(
    binary: Path, execution: Execution
) -> list:
    return interrupted_initialization(binary, execution, language="sql")


if __name__ == "__main__":
    run_this_suite(__file__)
