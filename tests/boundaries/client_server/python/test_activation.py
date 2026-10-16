#!/usr/bin/env -S uv run --script

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from contextlib import ExitStack

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.progress import without_elapsed
from support.snapshots import execution_snapshots
from support.assertions import last_result_text
from support.client import McpClient
from support.checkpoints import FifoCheckpoint
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.linux_sandbox import retain_system_bwrap
from support.normalization import code
from support.native import LOADER_VARIABLE, build_interposer
from support.processes import host_process_id, process_exists
from support.r import r_test_environment
from support.suites import run_this_suite
from support.requirements import NATIVE_FIXTURES, POSIX, R, SQL, command, requires
from support.python import write_test_wheel
from boundaries.client_server.server.test_no_r import no_r_environment


def managed_environments(root: Path, *, interrupt_site: bool = False) -> dict[str, str]:
    for name in ("initial", "candidate"):
        environment = root / name
        subprocess.run(
            [
                "uv",
                "venv",
                "--no-project",
                "--python",
                sys.executable,
                str(environment),
            ],
            check=True,
            capture_output=True,
        )
        site = Path(
            subprocess.check_output(
                [
                    environment / "bin/python",
                    "-c",
                    "import site; print(site.getsitepackages()[0])",
                ],
                text=True,
            ).strip()
        )
        extra = root / f"{name}-external"
        extra.mkdir()
        (site / "console-path.pth").write_text(str(extra) + "\n")
        (extra / "console_unloaded.py").write_text(
            # fmt: python
            code(f"""
                origin = {name!r}
                """)
        )
        if name == "initial":
            (extra / "console_retired.py").touch()
        else:
            metadata = site / "console_activation_fixture-1.0.dist-info"
            metadata.mkdir()
            (metadata / "METADATA").write_text(
                "Name: console-activation-fixture\nVersion: 1.0\n"
            )
            (metadata / "top_level.txt").write_text("console_unloaded\n")
            (site / "console-identity.pth").write_text("import console_identity\n")
            (site / "console_identity.py").write_text(
                # fmt: python
                code("""
                    import sys

                    identity = sys.prefix, sys.exec_prefix, sys.executable
                    if sys.flags.no_site:
                        import atexit

                        print("candidate startup output")
                        atexit.register(print, "candidate exit output")
                    """)
            )
        if name == "candidate" and interrupt_site:
            (site / "console-interrupt.pth").write_text("import console_interrupt\n")
            (site / "console_interrupt.py").write_text(
                # fmt: python
                code("""
                    import builtins
                    import signal
                    import sys

                    if sys.argv[0] != "-c" and not getattr(builtins, "site_interrupted", False):
                        builtins.site_interrupted = True
                        signal.raise_signal(signal.SIGINT)
                    """)
            )
    uv = root / "uv"
    # Use test-owned environments with real site processing and discovery.
    # Preserve uv's version discovery and execute its environment probe unchanged.
    real_uv = shutil.which("uv")
    assert real_uv is not None, "uv is required"
    # fmt: python
    source = code(f"""
        import os
        import sys

        if sys.argv[1:3] == ["tool", "run"]:
            name = "candidate" if "console-activation-fixture" in sys.argv else "initial"
            python = os.path.join({str(root)!r}, name, "bin", "python")
            command = sys.argv[sys.argv.index("--") + 2:]
            os.execv(python, [python, *command])
        uv = {real_uv!r}
        os.execv(uv, [uv, *sys.argv[1:]])
        """)
    uv.write_text(f"#!{sys.executable}\n" + source)
    uv.chmod(0o755)
    environment, _ = r_test_environment()
    environment.pop("RETICULATE_PYTHON", None)
    environment["RETICULATE_UV"] = str(uv)
    environment["RETICULATE_CHECK_REQUIRED_PACKAGES"] = "true"
    return environment


def initialize_managed_client(client: McpClient) -> None:
    client.initialize_and_list_tools()
    # These environments omit the NumPy/pandas seed. Replace the unused
    # prewarmed worker through the public API before arranging runtime state.
    client.send(requirements={"action": "set", "python": []})
    assert last_result_text(client) == "[prepared]", last_result_text(client)


def write_distribution(root: Path, name: str, module: str, version: str) -> None:
    source = Path(*module.split(".")).with_suffix(".py")
    (root / source).parent.mkdir(parents=True, exist_ok=True)
    (root / source).write_text(
        # fmt: python
        code(f"""
            __version__ = {version!r}
            value = object()
            """)
    )
    metadata = root / f"{name.replace('-', '_')}-{version}.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(f"Name: {name}\nVersion: {version}\n")
    (metadata / "top_level.txt").write_text(module.partition(".")[0] + "\n")
    (metadata / "RECORD").write_text(f"{source.as_posix()},,\n")


@requires(SQL, command("uv"))
@executions(DIRECT, SANDBOXED)
def test_unchanged_distribution_needs_no_file_inventory(
    binary: Path, execution: Execution
) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        package = "mcp_console_test_loaded"
        addition = "mcp_console_test_compatible_addition"
        incompatible = "mcp_console_test_incompatible_addition"
        index = write_test_wheel(
            root, package, "version = '1.0.0'\nmarker = object()\n"
        )
        write_test_wheel(
            root, addition, "answer = 42\n", requires=(f"{package}==1.0.0",)
        )
        environment = no_r_environment(root)
        environment.update(UV_INDEX=index.as_uri(), UV_INDEX_STRATEGY="first-index")
        with McpClient(
            binary,
            execution.serve("-c", f"python.managed.packages=[duckdb,{package}]"),
            environment,
            root,
        ) as client:
            client.initialize_and_list_tools()

            def accepted_requirements() -> dict:
                result = client.send(requirements={"action": "get"})
                assert (
                    json.loads(last_result_text(client)) == result["structuredContent"]
                )
                declaration = result["structuredContent"]["requirements"]
                # Provider inventories have separate coverage; compare the full
                # declaration before recording this case's Python requirements.
                result.clear()
                result["accepted_python_requirements"] = declaration["python"]
                return declaration

            client.expect(
                # fmt: python
                python=code("""
                    import importlib.metadata
                    import os
                    from pathlib import Path
                    import sys
                    import mcp_console_test_loaded as loaded

                    marker = loaded.marker
                    worker_pid = os.getpid()
                    record = Path(
                        importlib.metadata.distribution("mcp-console-test-loaded").locate_file(
                            "mcp_console_test_loaded-1.0.0.dist-info/RECORD"
                        )
                    )
                    block_record = False


                    def guard_inventory(event, arguments):
                        if (
                            block_record
                            and event == "open"
                            and isinstance(arguments[0], (str, bytes, os.PathLike))
                            and Path(os.fsdecode(arguments[0])) == record
                        ):
                            raise RuntimeError("unchanged distribution file inventory was read")


                    sys.addaudithook(guard_inventory)
                    """),
            )
            accepted = accepted_requirements()
            # An additive declaration can upgrade a transitive dependency. Keep
            # real resolution and reach the worker's loaded-version protection.
            write_test_wheel(
                root, package, "version = '2.0.0'\nmarker = object()\n", version="2.0.0"
            )
            write_test_wheel(
                root, incompatible, "answer = 7\n", requires=(f"{package}>=2.0.0",)
            )
            result = client.send(
                python="rejected_cell_ran = True",
                requirements={"python": [incompatible]},
            )
            assert result["isError"] is True, result
            assert last_result_text(client) == (
                "Cannot replace loaded mcp-console-test-loaded 1.0.0 with 2.0.0. "
                "Restart with compatible requirements; the running interpreter and objects are unchanged."
            ), result
            assert accepted_requirements() == accepted
            client.expect(
                "incompatible changes rejected without mutation\n",
                # fmt: python
                python=code("""
                    assert "rejected_cell_ran" not in globals()
                    assert os.getpid() == worker_pid and loaded.marker is marker
                    assert loaded.version == "1.0.0"
                    block_record = True
                    print("incompatible changes rejected without mutation")
                    """),
            )
            client.expect("[prepared]", requirements={"python": [addition]})
            client.expect(
                "unchanged loaded distribution retained\n",
                # fmt: python
                python=code("""
                    block_record = False
                    import mcp_console_test_compatible_addition as addition

                    assert addition.answer == 42
                    assert loaded.version == "1.0.0" and loaded.marker is marker
                    assert os.getpid() == worker_pid
                    print("unchanged loaded distribution retained")
                    """),
            )
            return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_removes_previous_environment_pth_paths(
    binary: Path, execution: Execution
) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = managed_environments(root)
        with McpClient(binary, execution.serve(), environment, root) as client:
            initialize_managed_client(client)
            client.send(
                # fmt: python
                python=code("""
                    import importlib.util
                    import sys

                    original_prefix = sys.prefix
                    original_paths = {path for path in sys.path if path.startswith(original_prefix)}
                    sys.path.insert(0, "user-added-path")
                    importlib.util.find_spec("console_retired") is not None
                    """)
            )
            assert last_result_text(client) == "True\n", last_result_text(client)
            client.send(requirements={"python": ["console-activation-fixture"]})
            assert last_result_text(client) == "[prepared]", last_result_text(client)
            client.send(
                # fmt: python
                python=code("""
                    import console_unloaded

                    assert "user-added-path" in sys.path
                    assert not original_paths.intersection(sys.path)
                    assert importlib.util.find_spec("console_retired") is None
                    console_unloaded.origin
                    """)
            )
            assert last_result_text(client) == "'candidate'\n", last_result_text(client)
            return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_site_hooks_observe_candidate_identity(
    binary: Path, execution: Execution
) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = managed_environments(root)
        with McpClient(binary, execution.serve(), environment, root) as client:
            initialize_managed_client(client)
            client.send(
                # fmt: python
                python=code("""
                    import sys

                    original_identity = sys.prefix, sys.exec_prefix, sys.executable
                    """)
            )
            assert last_result_text(client) == "[done]", last_result_text(client)
            client.send(requirements={"python": ["console-activation-fixture"]})
            assert last_result_text(client) == "[prepared]", last_result_text(client)
            client.send(
                # fmt: python
                python=code("""
                    import console_identity
                    import os

                    assert console_identity.identity != original_identity
                    assert console_identity.identity == (sys.prefix, sys.exec_prefix, sys.executable)
                    assert os.environ["VIRTUAL_ENV_PROMPT"] == "candidate"
                    assert sys.real_prefix == original_identity[0]
                    """)
            )
            assert last_result_text(client) == "[done]", last_result_text(client)
            return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_rolls_back_interrupted_python_site_activation(
    binary: Path, execution: Execution
) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = managed_environments(root, interrupt_site=True)
        with McpClient(binary, execution.serve(), environment, root) as client:
            initialize_managed_client(client)
            client.send(
                # fmt: python
                python=code("""
                    import os
                    import sys

                    original_environment = dict(os.environ)
                    original_paths = list(sys.path)
                    original_prefixes = sys.prefix, sys.exec_prefix, sys.executable
                    original_real_prefix = getattr(sys, "real_prefix", None)
                    """)
            )
            assert last_result_text(client) == "[done]", last_result_text(client)
            result = client.send(
                requirements={"python": ["console-activation-fixture"]}
            )
            assert result["isError"] is True, result
            assert "KeyboardInterrupt" in last_result_text(client), last_result_text(
                client
            )
            client.send(
                # fmt: python
                python=code("""
                    assert dict(os.environ) == original_environment
                    assert sys.path == original_paths
                    assert (sys.prefix, sys.exec_prefix, sys.executable) == original_prefixes
                    assert getattr(sys, "real_prefix", None) == original_real_prefix
                    """)
            )
            assert last_result_text(client) == "[done]", last_result_text(client)
            client.send(
                r='"console-activation-fixture" %in% reticulate::py_require()$packages'
            )
            assert last_result_text(client) == "[1] FALSE\n", last_result_text(client)
            client.send(requirements={"python": ["console-activation-fixture"]})
            assert last_result_text(client) == "[prepared]", last_result_text(client)
            client.send(
                # fmt: python
                python=code("""
                    import console_unloaded

                    console_unloaded.origin
                    """)
            )
            assert last_result_text(client) == "'candidate'\n", last_result_text(client)
            return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_preserves_r_interrupt_during_python_site_activation(
    binary: Path, execution: Execution
) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = managed_environments(root, interrupt_site=True)
        with McpClient(
            binary,
            execution.serve("-c", "python.managed.resolution=implicit"),
            environment,
            root,
        ) as client:
            initialize_managed_client(client)
            client.send(
                # fmt: r
                r=code("""
                    before_config <- reticulate::py_config()
                    before_requirements <- reticulate::py_require()
                    worker_pid <- Sys.getpid()
                    """)
            )
            assert last_result_text(client) == "[done]", last_result_text(client)
            client.send(
                # fmt: python
                python=code("""
                    import os
                    import sys

                    original_environment = dict(os.environ)
                    original_paths = list(sys.path)
                    original_prefixes = sys.prefix, sys.exec_prefix, sys.executable
                    original_real_prefix = getattr(sys, "real_prefix", None)
                    marker = object()
                    marker_id = id(marker)
                    """)
            )
            client.send(
                # fmt: r
                r=code("""
                    outcome <- tryCatch(
                      {
                        reticulate::py_require("console-activation-fixture")
                        "activated"
                      },
                      interrupt = function(condition) {
                        stopifnot(identical(class(condition), c("interrupt", "condition")))
                        "interrupt"
                      },
                      error = function(condition) paste("error:", conditionMessage(condition))
                    )
                    print(outcome)
                    stopifnot(
                      identical(reticulate::py_require(), before_requirements),
                      identical(reticulate::py_config(), before_config),
                      identical(Sys.getpid(), worker_pid)
                    )
                    """)
            )
            assert last_result_text(client) == '[1] "interrupt"\n', last_result_text(
                client
            )
            client.send(
                # fmt: python
                python=code("""
                    assert dict(os.environ) == original_environment
                    assert sys.path == original_paths
                    assert (sys.prefix, sys.exec_prefix, sys.executable) == original_prefixes
                    assert getattr(sys, "real_prefix", None) == original_real_prefix
                    assert id(marker) == marker_id
                    """)
            )
            assert last_result_text(client) == "[done]", last_result_text(client)
            client.send(r='reticulate::py_require("console-activation-fixture")')
            assert last_result_text(client) == "[done]", last_result_text(client)
            client.send(python="import console_unloaded; console_unloaded.origin")
            assert last_result_text(client) == "'candidate'\n", last_result_text(client)
            return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_interrupts_publication_after_committing_python(
    binary: Path, execution: Execution
) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = managed_environments(root)
        with McpClient(binary, execution.serve(), environment, root) as client:
            initialize_managed_client(client)
            client.send(
                # fmt: python
                python=code("""
                    import signal
                    import sys

                    old_prefix = sys.prefix
                    marker = object()
                    marker_id = id(marker)


                    def interrupt_publication(frame, event, function):
                        if (
                            event == "c_call"
                            and getattr(function, "__name__", None) == "publish_python_activation"
                        ):
                            sys.setprofile(None)
                            assert sys.prefix != old_prefix
                            signal.raise_signal(signal.SIGINT)


                    sys.setprofile(interrupt_publication)
                    """)
            )
            assert last_result_text(client) == "[done]", last_result_text(client)
            result = client.send(
                requirements={"python": ["console-activation-fixture"]}
            )
            assert result["isError"] is True, result
            assert last_result_text(client) == "KeyboardInterrupt", last_result_text(
                client
            )
            client.send(python="sys.prefix != old_prefix, id(marker) == marker_id")
            assert last_result_text(client) == "(True, True)\n", last_result_text(
                client
            )
            client.send(control="restart")
            client.send(python="import console_unloaded; console_unloaded.origin")
            assert last_result_text(client) == "'candidate'\n", last_result_text(client)
            return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_preserves_suspended_r_interrupt_during_publication(
    binary: Path, execution: Execution
) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = managed_environments(root)
        with McpClient(
            binary,
            execution.serve("-c", "python.managed.resolution=implicit"),
            environment,
            root,
        ) as client:
            initialize_managed_client(client)
            client.send(
                # fmt: python
                python=code("""
                    import signal
                    import _mcp_console_services as services

                    original_publish = services.publish_python_activation


                    def interrupted_publish(activation):
                        signal.raise_signal(signal.SIGINT)
                        original_publish(activation)


                    services.publish_python_activation = interrupted_publish
                    """)
            )
            assert last_result_text(client) == "[done]", last_result_text(client)
            client.send(
                # fmt: r
                r=code(r"""
                    tryCatch(
                      {
                        suspendInterrupts({
                          reticulate::py_require("console-activation-fixture")
                          cat("activation returned\n")
                        })
                        Sys.sleep(0) # Check R interrupts while the handler is installed.
                      },
                      interrupt = function(condition) cat("R accepted deferred interrupt\n")
                    )
                    """)
            )
            assert last_result_text(client) == (
                "activation returned\nR accepted deferred interrupt\n"
            ), last_result_text(client)
            client.send(python="import console_unloaded; console_unloaded.origin")
            assert last_result_text(client) == "'candidate'\n", last_result_text(client)
            client.send(control="restart")
            client.send(python="import console_unloaded; console_unloaded.origin")
            assert last_result_text(client) == "'candidate'\n", last_result_text(client)
            return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_rejects_replacement_of_loaded_distribution(
    binary: Path, execution: Execution
) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = managed_environments(root)
        for name, version in (("initial", "1.0"), ("candidate", "2.0")):
            write_distribution(
                root / f"{name}-external", "console-loaded", "console_loaded", version
            )
        with McpClient(binary, execution.serve(), environment, root) as client:
            initialize_managed_client(client)
            client.send(
                # fmt: python
                python=code("""
                    import console_loaded
                    import os
                    import sys

                    marker = console_loaded.value
                    identity = sys.prefix, sys.exec_prefix, sys.executable, list(sys.path)
                    process_environment = dict(os.environ)
                    worker_pid = os.getpid()
                    print(console_loaded.__version__)
                    """)
            )
            assert last_result_text(client) == "1.0\n", last_result_text(client)
            result = client.send(
                python="cell_was_run = True",
                requirements={"python": ["console-activation-fixture"]},
            )
            assert result["isError"] is True, result
            assert last_result_text(client) == (
                "Cannot replace loaded console-loaded 1.0 with 2.0. "
                "Restart with compatible requirements; the running interpreter and objects are unchanged."
            ), last_result_text(client)
            client.send(
                # fmt: python
                python=code("""
                    assert identity == (sys.prefix, sys.exec_prefix, sys.executable, sys.path)
                    assert process_environment == dict(os.environ)
                    assert console_loaded.value is marker and os.getpid() == worker_pid
                    assert "cell_was_run" not in globals()
                    print(console_loaded.__version__)
                    """)
            )
            assert last_result_text(client) == "1.0\n", last_result_text(client)
            client.send(control="restart")
            client.send(
                # fmt: python
                python=code("""
                    import console_loaded
                    import console_unloaded

                    assert console_unloaded.origin == "initial"
                    print(console_loaded.__version__)
                    """)
            )
            assert last_result_text(client) == "1.0\n", last_result_text(client)
            return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_allows_changes_to_unloaded_namespace_distributions(
    binary: Path, execution: Execution
) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = managed_environments(root)
        for name, version in (("initial", "1.0"), ("candidate", "2.0")):
            extra = root / f"{name}-external"
            write_distribution(extra, "console-auth", "console_namespace.auth", "1.0")
            write_distribution(
                extra, "console-cloud", "console_namespace.cloud", version
            )
        write_distribution(
            root / "initial-external",
            "console-storage",
            "console_namespace.storage",
            "1.0",
        )
        with McpClient(binary, execution.serve(), environment, root) as client:
            initialize_managed_client(client)
            client.send(
                # fmt: python
                python=code("""
                    import console_namespace.auth
                    import os

                    marker = console_namespace.auth.value
                    worker_pid = os.getpid()
                    print(console_namespace.auth.__version__)
                    """)
            )
            assert last_result_text(client) == "1.0\n", last_result_text(client)
            client.send(requirements={"python": ["console-activation-fixture"]})
            assert last_result_text(client) == "[prepared]", last_result_text(client)
            client.send(
                # fmt: python
                python=code("""
                    import console_namespace.cloud
                    import importlib.util

                    assert console_namespace.auth.value is marker
                    assert os.getpid() == worker_pid
                    assert importlib.util.find_spec("console_namespace.storage") is None
                    print(console_namespace.auth.__version__)
                    print(console_namespace.cloud.__version__)
                    """)
            )
            assert last_result_text(client) == "1.0\n2.0\n", last_result_text(client)
            return client.finish()


def cancelled_candidate_probe(
    binary: Path, execution: Execution, control: str, *, r_transition: bool = False
) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = managed_environments(root)
        environment["TMPDIR"] = str(root)
        checkpoints = []
        try:
            # Declare the empty baseline before prewarming; replacing a
            # default worker can otherwise retire R during native startup.
            with McpClient(
                binary,
                execution.serve(
                    "-c",
                    "python.managed.packages=[]",
                    *(
                        ("-c", "python.managed.resolution=implicit")
                        if r_transition
                        else ()
                    ),
                ),
                environment,
                root,
            ) as client:
                client.initialize_and_list_tools()
                client.send(
                    # fmt: r
                    r=code(r"""
                        cat(
                          tempfile("probe-ready-"),
                          tempfile("probe-release-"),
                          tempfile("probe-pid-"),
                          sep = "\n"
                        )
                        """)
                )
                paths = last_result_text(client).splitlines()
                assert len(paths) == 3, paths
                ready, release = [
                    FifoCheckpoint.create(Path(path)) for path in paths[:2]
                ]
                checkpoints.extend((ready, release))
                pid_path = Path(paths[2])
                client.transcript[-1]["result"]["content"][0]["text"] = (
                    "<probe ready>\n<probe release>\n<probe pid>"
                )
                candidate_site = next(
                    (root / "candidate/lib").glob("python*/site-packages")
                )
                (candidate_site / "console-probe.pth").write_text(
                    "import console_probe_gate\n"
                )
                (candidate_site / "console_probe_gate.py").write_text(
                    # fmt: python
                    code(f"""
                        import os
                        import sys
                        from pathlib import Path

                        if sys.flags.no_site:
                            Path({str(pid_path)!r}).write_text(str(os.getpid()))
                            with open({str(ready.path)!r}, "wb", buffering=0) as ready:
                                ready.write(b"1")
                            with open({str(release.path)!r}, "rb", buffering=0) as release:
                                assert release.read(1) == b"1"
                        """)
                )
                client.send(python="marker = object(); marker_id = id(marker)")
                assert last_result_text(client) == "[done]", last_result_text(client)
                if r_transition:
                    client.send(r="before <- reticulate::py_require()")
                    preparation = client.start_send(
                        # fmt: r
                        r=code(r"""
                            tryCatch(
                              reticulate::py_require("console-activation-fixture"),
                              interrupt = function(condition) cat("caught R interrupt\n"),
                              error = function(condition) {
                                cat("caught R error:", conditionMessage(condition), "\n")
                              }
                            )
                            stopifnot(identical(reticulate::py_require(), before))
                            """)
                    )
                else:
                    preparation = client.start_send(
                        requirements={"python": ["console-activation-fixture"]}
                    )
                ready.wait("candidate probe")
                child_pid = host_process_id(
                    int(pid_path.read_text()), client.process.pid
                )
                cancellation = client.start_send(control=control)
                client.receive_many([preparation, cancellation])
                assert preparation["result"]["isError"] is (not r_transition), (
                    preparation
                )
                if r_transition:
                    expected = "caught R interrupt\n"
                else:
                    expected = (
                        "Python preparation cancelled by restart"
                        if control == "restart"
                        else "KeyboardInterrupt"
                    )
                assert preparation["result"]["content"] == [
                    {"type": "text", "text": expected}
                ], preparation
                assert not process_exists(child_pid), child_pid
                client.send(
                    python='"marker" not in globals()'
                    if control == "restart"
                    else "id(marker) == marker_id"
                )
                assert last_result_text(client) == "True\n", last_result_text(client)
                (candidate_site / "console-probe.pth").unlink()
                if r_transition:
                    client.send(
                        r='reticulate::py_require("console-activation-fixture")'
                    )
                    assert last_result_text(client) == "[done]", last_result_text(
                        client
                    )
                    client.send(
                        python="import console_unloaded; console_unloaded.origin"
                    )
                    assert last_result_text(client) == "'candidate'\n", (
                        last_result_text(client)
                    )
                else:
                    client.send(requirements={"python": ["console-activation-fixture"]})
                    assert last_result_text(client) == "[prepared]", last_result_text(
                        client
                    )
                return client.finish()
        finally:
            for checkpoint in checkpoints:
                checkpoint.close()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_cancels_candidate_probe_and_reaps_its_child(
    binary: Path, execution: Execution
) -> list:
    return cancelled_candidate_probe(binary, execution, "interrupt")


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_restarts_during_candidate_probe(binary: Path, execution: Execution) -> list:
    return cancelled_candidate_probe(binary, execution, "restart")


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_preserves_r_interrupt_during_candidate_probe(
    binary: Path, execution: Execution
) -> list:
    return cancelled_candidate_probe(binary, execution, "interrupt", r_transition=True)


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_retains_previous_candidate_after_lazy_projection_failure(
    binary: Path, execution: Execution
) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = managed_environments(root)
        # R-only bootstrap leaves Python selection lazy for R interoperability.
        environment["MCP_CONSOLE_LANGUAGES"] = "r,sql"
        # Prepare the fixture baseline at startup. This case owns a later
        # projection failure, after the first R cell has finished bootstrap.
        with McpClient(
            binary,
            execution.serve(
                "-c",
                "python.managed.resolution=implicit",
                "-c",
                "python.managed.packages=[console-initial-fixture]",
            ),
            environment,
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.send(
                # fmt: r
                r=code("""
                    before <- reticulate::py_require()
                    worker_pid <- Sys.getpid()
                    stopifnot(!reticulate::py_available(initialize = FALSE))
                    lockBinding("python_requirements", reticulate:::.globals)
                    """)
            )
            assert last_result_text(client) == "[done]", last_result_text(client)
            result = client.send(
                requirements={"python": ["console-activation-fixture"]}
            )
            assert result["isError"] is True, result
            assert "cannot change value of locked binding" in last_result_text(
                client
            ), result
            client.send(
                # fmt: r
                r=code("""
                    unlockBinding("python_requirements", reticulate:::.globals)
                    stopifnot(
                      !reticulate::py_available(initialize = FALSE),
                      identical(reticulate::py_require(), before),
                      identical(Sys.getpid(), worker_pid)
                    )
                    """)
            )
            assert last_result_text(client) == "[done]", last_result_text(client)
            client.send(
                r='reticulate::py_run_string("import console_unloaded; print(console_unloaded.origin)")'
            )
            assert last_result_text(client) == "initial\n", last_result_text(client)
            client.send(
                # fmt: r
                r=code("""
                    stopifnot(
                      identical(reticulate::py_require(), before),
                      identical(Sys.getpid(), worker_pid)
                    )
                    """)
            )
            assert last_result_text(client) == "[done]", last_result_text(client)
            client.send(requirements={"python": ["console-activation-fixture"]})
            assert last_result_text(client) == "[prepared]", last_result_text(client)
            return client.finish()[3:]


@execution_snapshots
@requires(POSIX, NATIVE_FIXTURES)
@executions(DIRECT, SANDBOXED)
def test_retries_interrupted_startup_with_prepared_candidate_without_r(
    binary: Path, execution: Execution
) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = managed_environments(root)
        for name in ("initial", "candidate"):
            subprocess.run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(root / name / "bin/python"),
                    "duckdb",
                ],
                check=True,
                capture_output=True,
            )
        retain_system_bwrap(root, environment["PATH"])
        environment["PATH"] = str(root)
        for name in ("R_HOME", "R_LIBS", "R_LIBS_USER", "RETICULATE_UV"):
            environment.pop(name, None)
        arguments = ("--writable-root", str(root)) if execution == SANDBOXED else ()
        ready = FifoCheckpoint.create(root / "ready")
        release = FifoCheckpoint.create(root / "release")
        launching = FifoCheckpoint.create(root / "launching")
        launch = FifoCheckpoint.create(root / "launch")
        armed = root / "launch-armed"
        environment.update(
            MCP_CONSOLE_TEST_SPAWN_LIBRARY=str(
                build_interposer(root, "resolver_spawn_interposer")
            ),
            MCP_CONSOLE_TEST_SPAWN_ARMED=str(armed),
            MCP_CONSOLE_TEST_SPAWN_ORDINAL="1",
            MCP_CONSOLE_TEST_SPAWN_STARTED=str(launching.path),
            MCP_CONSOLE_TEST_SPAWN_RELEASE=str(launch.path),
        )
        # Hold launch before transport readiness until the observation expires.
        # The checkpoint must be reached before that deadline; a zero deadline
        # can instead project progress before the launch owner becomes busy.
        # fmt: python
        server = code(f"""
            import os
            import sys

            os.environ["MCP_CONSOLE_TEST_SPAWN_SERVER"] = str(os.getpid())
            os.environ[{LOADER_VARIABLE!r}] = os.environ.pop("MCP_CONSOLE_TEST_SPAWN_LIBRARY")
            os.execv(sys.argv[1], sys.argv[1:])
            """)
        try:
            with (
                McpClient(
                    Path(sys.executable),
                    ("-c", server, str(binary), *execution.serve(*arguments)),
                    environment,
                    root,
                ) as client,
                ExitStack() as completion,
            ):
                completion.callback(launch.release)
                completion.callback(release.release)
                initialize_managed_client(client)
                client.send(requirements={"python": ["console-activation-fixture"]})
                assert last_result_text(client) == "[prepared]", last_result_text(
                    client
                )
                retained = client.send(requirements={"action": "get"})[
                    "structuredContent"
                ]
                site = next((root / "candidate/lib").glob("python*/site-packages"))
                hook = site / "startup-probe.pth"
                hook.write_text("import startup_probe\n")
                (site / "startup_probe.py").write_text(
                    # fmt: python
                    code(f"""
                        import os
                        import sys
                        from pathlib import Path

                        if sys.flags.no_site:
                            Path({str(root / "probe-pid")!r}).write_text(str(os.getpid()))
                            with open({str(ready.path)!r}, "wb", buffering=0) as ready:
                                ready.write(b"1")
                            with open({str(release.path)!r}, "rb", buffering=0) as release:
                                assert release.read(1) == b"1"
                        else:
                            Path({str(root / "worker-pid")!r}).write_text(str(os.getpid()))
                        """)
                )
                armed.touch()
                evaluation = client.start_send(
                    python="print('initialized')", timeout_ms=30_000
                )
                launching.wait(
                    "prepared candidate launch is held before transport ready"
                )
                client.receive(evaluation)
                assert without_elapsed(last_result_text(client)) == (
                    "\n[running; poll with an empty send]"
                )
                # Launch holds the lifecycle owner. Disposable phase observations
                # may be unavailable there; the gate proves startup is unfinished.
                launch.release()
                ready.wait("prepared environment startup probe")
                child_pid = host_process_id(
                    int((root / "probe-pid").read_text()), client.process.pid
                )
                client.send(timeout_ms=0)
                assert (
                    without_elapsed(last_result_text(client))
                    == "\n[running; poll with an empty send]"
                )
                # Transport is ready; the embedded startup probe is worker-owned.
                assert "phase:" not in last_result_text(client)
                client.send(control="interrupt", timeout_ms=30_000)
                assert not process_exists(child_pid), (child_pid, client.transcript[-1])
                hook.unlink()
                assert (
                    client.send(requirements={"action": "get"})["structuredContent"]
                    == retained
                )
                client.send(
                    # fmt: python
                    python=code("""
                        import os
                        import console_unloaded
                        from pathlib import Path

                        assert os.getpid() == int(Path("worker-pid").read_text())
                        print(console_unloaded.origin)
                        """)
                )
                assert last_result_text(client) == "candidate\n", last_result_text(
                    client
                )
                client.send(control="restart")
                client.send(
                    python="import console_unloaded; print(console_unloaded.origin)"
                )
                assert last_result_text(client) == "candidate\n", last_result_text(
                    client
                )
                return client.finish()
        finally:
            launching.close()
            launch.close()
            ready.close()
            release.close()


@requires(R, POSIX, SQL)
@executions(DIRECT, SANDBOXED)
def test_retries_interrupted_startup_probe_with_live_r_and_sql(
    binary: Path, execution: Execution
) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = managed_environments(root)
        environment["MCP_CONSOLE_LANGUAGES"] = "r,sql"
        environment["TMPDIR"] = str(root)
        checkpoints = []
        try:
            with McpClient(binary, execution.serve(), environment, root) as client:
                initialize_managed_client(client)
                client.send(
                    # fmt: r
                    r=code(r"""
                        startup_worker_pid <- Sys.getpid()
                        startup_r_state <- 42L
                        stopifnot(!reticulate::py_available(initialize = FALSE))
                        cat(tempfile("ready-"), tempfile("release-"), tempfile("pid-"), sep = "\n")
                        """)
                )
                paths = last_result_text(client).splitlines()
                assert len(paths) == 3, paths
                ready, release = [
                    FifoCheckpoint.create(Path(path)) for path in paths[:2]
                ]
                checkpoints.extend((ready, release))
                pid_path = Path(paths[2])
                client.transcript[-1]["result"]["content"][0]["text"] = (
                    "<probe ready>\n<probe release>\n<probe pid>"
                )
                client.send(sql="CREATE TABLE startup_state AS SELECT 42 AS answer")
                site = next((root / "initial/lib").glob("python*/site-packages"))
                hook = site / "startup-probe.pth"
                hook.write_text("import startup_probe\n")
                (site / "startup_probe.py").write_text(
                    # fmt: python
                    code(f"""
                        import os
                        import sys
                        from pathlib import Path

                        if sys.flags.no_site:
                            Path({str(pid_path)!r}).write_text(str(os.getpid()))
                            with open({str(ready.path)!r}, "wb", buffering=0) as ready:
                                ready.write(b"1")
                            with open({str(release.path)!r}, "rb", buffering=0) as release:
                                assert release.read(1) == b"1"
                        """)
                )
                evaluation = client.start_send(
                    r="invisible(reticulate::py_config())", timeout_ms=0
                )
                ready.wait("initial environment probe")
                child_pid = host_process_id(
                    int(pid_path.read_text()), client.process.pid
                )
                client.receive(evaluation)
                assert (
                    without_elapsed(last_result_text(client))
                    == "\n[running; poll with an empty send]"
                )
                # The zero-timeout response can precede the probe and still
                # report preparation. Observe again at the held probe, then
                # retain the settled progress cut for the submitted cell.
                client.send(timeout_ms=0)
                assert (
                    without_elapsed(last_result_text(client))
                    == "\n[running; poll with an empty send]"
                )
                assert "phase:" not in last_result_text(client)
                evaluation["result"] = client.transcript.pop()["result"]
                client.send(control="interrupt", timeout_ms=30_000)
                assert not process_exists(child_pid), (child_pid, client.transcript[-1])
                hook.unlink()
                assert last_result_text(client) == "\n", repr(last_result_text(client))
                client.send(
                    # fmt: r
                    r=code("""
                        stopifnot(
                          identical(Sys.getpid(), startup_worker_pid),
                          identical(startup_r_state, 42L)
                        )
                        """)
                )
                assert last_result_text(client) == "[done]", last_result_text(client)
                client.send(sql="SELECT answer FROM startup_state")
                assert last_result_text(client).splitlines()[-1].split() == ["1", "42"]
                client.send(r="reticulate::py_run_string(\"print('initialized')\")")
                assert last_result_text(client) == "initialized\n", last_result_text(
                    client
                )
                return client.finish()[3:]
        finally:
            for checkpoint in checkpoints:
                checkpoint.close()


if __name__ == "__main__":
    run_this_suite(__file__)
