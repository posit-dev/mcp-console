#!/usr/bin/env -S uv run --script

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.progress import without_elapsed_result
from support.requirements import POSIX, PROCESS_EVENTS, R_EVENT_LOOP, requires
from support.assertions import (
    last_tool_text,
    release_worker_callback_gate,
    wait_for_idle_output,
)
from support.checkpoints import FifoCheckpoint
from support.client import McpClient, stop_client
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code, normalize_python_resolution_error
from support.processes import host_process_id, process_group_exists, stop_process_group
from support.r import r_test_environment
from support.events import Events
from support.records import Transcript
from support.python import write_test_wheel
from boundaries.client_server.python.test_peer_runtime import without_r
from boundaries.client_server.requirements.test_actions import inspect
from support.resolvers import (
    checkpoint_uv_environment,
    matplotlib_test_environment,
    named_requirement_error,
    recording_uv_environment,
    send_and_collect_runtime_python_resolution,
    uv_tool_run_requirements,
)
from support.suites import run_this_suite


@executions(DIRECT, SANDBOXED)
def test_declares_imports_and_uses_python_packages(
    binary: Path, execution: Execution
) -> Transcript:
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    with McpClient(
        binary,
        execution.serve(),
        environment,
    ) as client:
        client.initialize_and_list_tools()
        client.expect(r='reticulate::py_require("py-yaml12")')
        client.expect(r='yaml12 <- reticulate::import("yaml12")')
        client.send(r='yaml12$parse_yaml("answer: 42")$answer')
        assert last_tool_text(client) == "[1] 42\n"
        client.send(
            # fmt: python
            python=code(r"""
                import yaml12

                yaml12.parse_yaml("answer: 42")["answer"]
                """),
        )
        assert last_tool_text(client) == "42\n"
        client.expect(r='reticulate::py_require("more-itertools")')
        client.expect(
            "[done]",
            # fmt: python
            python=code(r"""
                import more_itertools

                assert yaml12 is __import__("yaml12")
                assert more_itertools.__name__ == "more_itertools"
                """),
        )
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_prepares_initial_python_requirements(
    binary: Path, execution: Execution
) -> Transcript:
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    client = McpClient(binary, execution.serve(), environment)
    client.initialize_and_list_tools()
    tools = client.transcript[-1]["result"]
    client.expect("[prepared]", requirements={"python": ["py-yaml12"]})
    # The preparation receipt, rather than requirements inspection, owns this
    # schema-stability check. Validate the complete result before compacting it.
    assert client.request("tools/list")["result"] == tools
    client.transcript.pop()
    invalid = "not a valid requirement !!!"

    client.send(
        requirements={"python": [invalid]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    recorded_error = named_requirement_error(invalid)
    assert result["content"][0]["text"] == recorded_error
    client.send(
        requirements={"python": [invalid]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == recorded_error
    client.send(
        requirements={"python": ["numpy\npandas"]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == named_requirement_error("numpy\npandas")
    # fmt: r
    r = code(r"""
        seed <- tail(reticulate::py_require()$history, 1L)[[1L]]
        printed_requirements <- capture.output(print(reticulate::py_require()))
        stopifnot(
          identical(seed$requested_from, "mcp-console"),
          identical(seed$action, "set"),
          isFALSE(seed$exclude_newer_supplied),
          identical(
            seed$packages,
            c("numpy", "pandas", "matplotlib", "plotnine", "py-yaml12")
          ),
          length(printed_requirements) > 0L
        )
        """)
    client.expect(r=r)
    # fmt: python
    python = code("""
        import yaml12

        yaml12.__name__
        """)
    client.send(python=python)
    assert last_tool_text(client) == "'yaml12'\n"
    client.expect("[prepared]", requirements={"python": ["py-yaml12"]})
    return client.finish()


@executions(DIRECT)
def test_preserves_python_requirement_values(
    binary: Path, execution: Execution
) -> Transcript:
    environment = dict(os.environ, MCP_CONSOLE_LANGUAGES="r")
    client = McpClient(
        binary,
        execution.serve("-c", "python.managed.resolution=implicit"),
        environment,
    )
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        initial <- reticulate::py_require()
        stopifnot(
          identical(class(initial), "python_requirements"),
          identical(names(initial), c("packages", "history")),
          !reticulate::py_available(initialize = FALSE)
        )
        packages <- c(second = "py-yaml12", first = "numpy", repeated = "numpy")
        result <- withVisible(reticulate::py_require(
          packages,
          python_version = c(">=3.10", "<4"),
          exclude_newer = "2026-01-01",
          action = "set"
        ))
        expected <- initial
        expected$packages <- packages
        expected$python_version <- c(">=3.10", "<4")
        expected$exclude_newer <- "2026-01-01"
        expected$history <- c(
          initial$history,
          list(list(
            requested_from = "R_GlobalEnv",
            env_is_package = FALSE,
            packages = packages,
            python_version = c(">=3.10", "<4"),
            exclude_newer = "2026-01-01",
            exclude_newer_supplied = TRUE,
            action = "set"
          ))
        )
        stopifnot(
          identical(result, list(value = NULL, visible = FALSE)),
          identical(reticulate::py_require(), expected)
        )
        detached <- reticulate::py_require()
        detached$packages[1L] <- "changed"
        detached$history[[1L]]$packages <- "changed"
        invisible(gc())
        stopifnot(identical(reticulate::py_require(), expected))
        """)
    client.expect(r=r)
    # fmt: r
    r = code(r"""
        error <- tryCatch(
          reticulate::py_require(exclude_newer = "2026-02-01"),
          error = conditionMessage
        )
        stopifnot(
          identical(
            error,
            paste0(
              "`exclude_newer` is already set to '2026-01-01', ",
              "use `action = 'set'` to override"
            )
          ),
          identical(reticulate::py_require(), expected)
        )
        reticulate::py_require(
          "numpy",
          python_version = "<4",
          exclude_newer = "2026-01-01",
          action = "remove"
        )
        removed <- reticulate::py_require()
        stopifnot(
          identical(removed$packages, "py-yaml12"),
          identical(removed$python_version, ">=3.10"),
          identical(removed["exclude_newer"], list(exclude_newer = NULL)),
          identical(names(removed), names(expected)),
          identical(head(removed$history, -1L), expected$history)
        )
        reticulate::py_require(
          character(),
          python_version = character(),
          action = "set"
        )
        empty <- reticulate::py_require()
        stopifnot(
          identical(empty$packages, character()),
          identical(empty$python_version, character()),
          identical(empty["exclude_newer"], list(exclude_newer = NULL)),
          identical(names(empty), names(expected)),
          identical(head(empty$history, -1L), removed$history),
          !reticulate::py_available(initialize = FALSE)
        )
        """)
    client.expect(r=r)
    # fmt: r
    r = code(r"""
        initial <- reticulate::py_require()
        latin1 <- rawToChar(as.raw(0xe9))
        Encoding(latin1) <- "latin1"
        bytes <- rawToChar(as.raw(0xff))
        Encoding(bytes) <- "bytes"
        packages <- structure(
          c("zeta", "alpha", NA_character_, "alpha", "", latin1, bytes),
          names = c("last", "first", "missing", "again", "empty", latin1, bytes),
          class = "AsIs",
          comment = "package request",
          detail = list(values = c(1L, NA_integer_), nested = list("original"))
        )
        cutoff <- structure(
          "2026-01-01",
          names = "cutoff",
          class = "AsIs",
          detail = list("original")
        )
        reticulate::py_require(packages, exclude_newer = cutoff, action = "set")
        expected <- reticulate::py_require()
        stopifnot(
          identical(expected$packages, packages),
          identical(expected$exclude_newer, cutoff),
          identical(Encoding(expected$packages), Encoding(packages)),
          identical(tail(expected$history, 1L)[[1L]]$packages, packages),
          identical(tail(expected$history, 1L)[[1L]]$exclude_newer, cutoff),
          identical(head(expected$history, -1L), initial$history)
        )
        packages[1L] <- "changed input"
        names(packages)[2L] <- "changed name"
        attr(packages, "detail")$nested[[1L]] <- "changed input attribute"
        cutoff[1L] <- "2026-02-01"
        attr(cutoff, "detail")[[1L]] <- "changed cutoff attribute"
        invisible(gc())
        stopifnot(identical(reticulate::py_require(), expected))
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"
    # fmt: r
    r = code(r"""
        detached <- reticulate::py_require()
        detached$packages[1L] <- "changed result"
        attr(detached$packages, "detail")$nested[[1L]] <- "changed result attribute"
        attr(detached$exclude_newer, "detail")[[1L]] <- "changed cutoff result"
        attr(detached$history[[length(detached$history)]]$packages, "detail") <- NULL
        detached$history[[length(detached$history)]]$packages[1L] <- "changed history"
        invisible(gc())
        stopifnot(identical(reticulate::py_require(), expected))
        empty <- structure(character(), class = "AsIs", detail = list(1L))
        reticulate::py_require(empty, exclude_newer = "", action = "set")
        cleared <- reticulate::py_require()
        stopifnot(
          identical(cleared$packages, empty),
          identical(cleared["exclude_newer"], list(exclude_newer = NULL)),
          identical(names(cleared), names(expected)),
          identical(head(cleared$history, -1L), expected$history),
          identical(tail(cleared$history, 1L)[[1L]]$packages, empty),
          !reticulate::py_available(initialize = FALSE)
        )
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"
    return client.finish()[3:]


@executions(DIRECT, SANDBOXED)
def test_materializes_lazy_python_requirements_without_initializing(
    binary: Path, execution: Execution
) -> Transcript:
    environment = dict(os.environ, MCP_CONSOLE_LANGUAGES="r")
    client = McpClient(
        binary,
        execution.serve("-c", "python.managed.resolution=implicit"),
        environment,
    )
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        worker_pid <- Sys.getpid()
        reticulate::py_require("humanize")
        reticulate::py_require("humanize", action = "remove")
        reticulate::py_require("py-yaml12")
        reticulate::py_require(python_version = ">=3.10")
        reticulate::py_require(python_version = ">=3.10", action = "remove")
        reticulate::py_require(exclude_newer = "2026-09-01")
        reticulate::py_require(exclude_newer = NA, action = "set")
        stopifnot(!reticulate::py_available(initialize = FALSE))
        """)
    client.expect(r=r)
    client.expect("[prepared]", requirements={"python": ["packaging"]})
    # fmt: r
    r = code(r"""
        stopifnot(
          identical(Sys.getpid(), worker_pid),
          !reticulate::py_available(initialize = FALSE),
          all(c("py-yaml12", "packaging") %in% reticulate::py_require()$packages),
          !"humanize" %in% reticulate::py_require()$packages,
          identical(reticulate::py_require()$python_version, character()),
          is.null(reticulate::py_require()$exclude_newer)
        )
        """)
    client.expect(r=r)
    client.send(control="restart")
    assert last_tool_text(client) == (
        "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
    )
    # fmt: r
    r = code(r"""
        stopifnot(
          !reticulate::py_available(initialize = FALSE),
          all(c("py-yaml12", "packaging") %in% reticulate::py_require()$packages),
          !"humanize" %in% reticulate::py_require()$packages,
          is.null(reticulate::py_require()$python_version),
          is.null(reticulate::py_require()$exclude_newer)
        )
        """)
    client.expect(r=r)
    client.send(r='reticulate::import("yaml12")$`__name__`')
    assert last_tool_text(client) == '[1] "yaml12"\n'
    return client.finish()[3:]


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_retires_python_resolver_descendant_after_leader_exit(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        real_uv = shutil.which("uv")
        assert real_uv is not None, "uv is required"
        started = FifoCheckpoint.create(temporary / "descendant-started")
        leader_release = FifoCheckpoint.create(temporary / "leader-release")
        lifetime = FifoCheckpoint.create(temporary / "descendant-lifetime")
        identity = temporary / "descendant-identity"
        wrapper = temporary / "uv"
        wrapper.write_text(
            # fmt: python
            code(r"""
                #!/usr/bin/env python3

                import os
                import sys


                def notify(path):
                    with open(path, "wb", buffering=0) as stream:
                        stream.write(b"1")


                def wait(path):
                    with open(path, "rb", buffering=0) as stream:
                        assert stream.read(1) == b"1"


                requirement = os.environ["MCP_CONSOLE_TEST_REQUIREMENT"]
                if requirement in sys.argv[1:]:
                    assert os.environ.get("R_LIBS") is None
                    child = os.fork()
                    if child == 0:
                        identity = os.environ["MCP_CONSOLE_TEST_DESCENDANT_IDENTITY"]
                        with open(identity, "x", encoding="utf-8") as stream:
                            stream.write(f"{os.getpid()} {os.getpgrp()}\n")
                        lifetime = os.environ["MCP_CONSOLE_TEST_DESCENDANT_LIFETIME"]
                        with open(lifetime, "rb", buffering=0) as stream:
                            notify(os.environ["MCP_CONSOLE_TEST_DESCENDANT_STARTED"])
                            stream.read(1)
                        os._exit(0)
                    wait(os.environ["MCP_CONSOLE_TEST_LEADER_RELEASE"])

                uv = os.environ["MCP_CONSOLE_TEST_REAL_UV"]
                os.execv(uv, [uv, *sys.argv[1:]])
                """),
            encoding="utf-8",
        )
        wrapper.chmod(0o755)

        environment = os.environ.copy()
        environment["UV_TOOL_DIR"] = str(temporary)
        environment.pop("RETICULATE_PYTHON", None)
        environment.pop("R_LIBS", None)
        environment["RETICULATE_UV"] = str(wrapper)
        environment["MCP_CONSOLE_TEST_REAL_UV"] = real_uv
        environment["MCP_CONSOLE_TEST_REQUIREMENT"] = "py-yaml12"
        environment["MCP_CONSOLE_TEST_DESCENDANT_IDENTITY"] = str(identity)
        environment["MCP_CONSOLE_TEST_DESCENDANT_STARTED"] = str(started.path)
        environment["MCP_CONSOLE_TEST_LEADER_RELEASE"] = str(leader_release.path)
        environment["MCP_CONSOLE_TEST_DESCENDANT_LIFETIME"] = str(lifetime.path)

        client = McpClient(binary, execution.serve("-c", "cache=host"), environment)
        resolver_group = None
        exit_events = Events()
        try:
            client.initialize_and_list_tools()
            client.expect("[prepared]", requirements={"r": ["DBI"]})
            preparation = client.start_send(
                requirements={"python": ["py-yaml12"]},
            )
            started.wait("Python resolver descendant")
            descendant, resolver_group = (
                host_process_id(int(pid), client.process.pid)
                for pid in identity.read_text(encoding="utf-8").split()
            )
            assert descendant != resolver_group
            assert resolver_group != os.getpgrp()
            exit_events.watch_process(descendant)

            leader_release.release()
            assert exit_events.wait(10) == {descendant}, (
                "resolver descendant did not exit"
            )

            client.receive(preparation)
            assert preparation["result"] == {
                "content": [{"type": "text", "text": "[prepared]"}],
                "isError": False,
            }, preparation
            assert not process_group_exists(resolver_group), (
                "resolver process group outlived its leader"
            )
            resolver_group = None
            transcript = client.finish()
            return transcript
        finally:
            leader_release.release()
            stop_process_group(resolver_group)
            stop_client(client)
            exit_events.close()
            started.close()
            leader_release.close()
            lifetime.close()


@executions(DIRECT, SANDBOXED)
def test_prepares_explicit_numpy_requirement(
    binary: Path, execution: Execution
) -> Transcript:
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    client = McpClient(binary, execution.serve(), environment)
    client.initialize_and_list_tools()
    client.expect("[prepared]", requirements={"python": ["numpy"]})
    # fmt: r
    r = code(r"""
        stopifnot(Sys.getenv("RETICULATE_PYTHON") == "managed")
        """)
    client.expect(r=r)
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_does_not_fail_resolution_when_matplotlib_cache_cannot_be_written(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        environment = matplotlib_test_environment(temporary / "host-cache")
        cache_directory = temporary / "user-matplotlib"
        environment["MPLCONFIGDIR"] = str(cache_directory)
        environment["MPL_IGNORE_SYSTEM_FONTS"] = "1"
        client = McpClient(
            binary,
            execution.serve("-c", "cache=host"),
            environment,
            current_directory=temporary,
        )
        client.initialize_and_list_tools()
        client.expect("[prepared]", requirements={"python": ["matplotlib"]})
        caches = list(cache_directory.glob("fontlist-v*.json"))
        assert len(caches) == 1, caches
        caches[0].unlink()
        caches[0].mkdir()

        client.expect("[prepared]", requirements={"python": ["py-yaml12"]})
        assert caches[0].is_dir()
        assert not [
            path for path in cache_directory.glob("fontlist-v*.json") if path.is_file()
        ]
        client.send(
            python="(__import__('matplotlib').__name__, __import__('yaml12').__name__)"
        )
        assert last_tool_text(client) == "('matplotlib', 'yaml12')\n"
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_restart_loses_state_and_retains_python_requirements(
    binary: Path,
    execution: Execution,
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    client.expect("[prepared]", requirements={"python": ["py-yaml12"]})
    client.expect(python="restart_marker = 42")

    client.send(control="restart")
    assert last_tool_text(client) == (
        "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
    )

    # fmt: python
    python = code("""
        import yaml12

        "restart_marker" in globals(), yaml12.__name__
        """)
    client.send(python=python)
    assert last_tool_text(client) == "(False, 'yaml12')\n"
    return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_restart_discards_pre_marker_python_activation(
    binary: Path, execution: Execution
) -> Transcript:
    return restart_discards_pre_marker_activation(binary, execution, {})


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_set_discards_pre_marker_python_activation(
    binary: Path, execution: Execution
) -> Transcript:
    return restart_discards_pre_marker_activation(binary, execution, {"action": "set"})


def restart_discards_pre_marker_activation(
    binary: Path, execution: Execution, action: dict
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        replacement_requirement = "mcp-console-restart-fixture"
        environment, uv_started, uv_release = checkpoint_uv_environment(
            temporary,
            replacement_requirement,
            reuse_resolved_python_for=("py-yaml12", replacement_requirement),
        )
        environment["TMPDIR"] = temporary_directory
        reuse_record = Path(environment["MCP_CONSOLE_TEST_UV_REUSE_RECORD"])

        client = McpClient(
            binary,
            execution.serve(
                "-c", "python.managed.resolution=implicit", "-c", "cache=host"
            ),
            environment,
        )
        passed = False
        worker_checkpoints: list[FifoCheckpoint] = []
        try:
            client.initialize_and_list_tools()
            # fmt: r
            r = code(r"""
                config <- reticulate::py_config()
                activation_ready <- tempfile("mcp-console-activation-ready-")
                activation_release <- tempfile("mcp-console-activation-release-")
                activation_sent <- tempfile("mcp-console-activation-sent-")
                cat(
                  activation_ready,
                  activation_release,
                  activation_sent,
                  config$python,
                  sep = "\n"
                )
                """)
            client.send(r=r)
            setup = client.transcript[-1]["result"]
            paths = setup["content"][0]["text"].splitlines()
            assert len(paths) == 4, setup
            resolved_python = paths.pop()
            assert Path(resolved_python).is_file(), resolved_python
            Path(environment["MCP_CONSOLE_TEST_UV_REUSE_PYTHON"]).write_text(
                resolved_python,
                encoding="utf-8",
            )
            setup["content"][0]["text"] = (
                "<activation ready>\n<activation release>\n<activation sent>"
            )
            activation_ready, activation_release, activation_sent = [
                FifoCheckpoint.create(Path(path)) for path in paths
            ]
            worker_checkpoints.extend(
                (activation_ready, activation_release, activation_sent)
            )

            # Pause the real managed worker after its new environment resolves,
            # immediately before its active binding publishes python_activated.
            # fmt: r
            r = code(r"""
                globals <- get(".globals", envir = asNamespace("reticulate"))
                original <- activeBindingFunction("python_requirements", globals)
                rm(list = "python_requirements", envir = globals)
                makeActiveBinding("python_requirements", function(value) {
                  if (missing(value)) {
                    return(original())
                  }
                  ready <- fifo(activation_ready, open = "wb", blocking = TRUE)
                  writeBin(charToRaw("1"), ready)
                  close(ready)
                  release <- fifo(activation_release, open = "rb", blocking = TRUE)
                  stopifnot(identical(readBin(release, "raw", n = 1L), charToRaw("1")))
                  close(release)
                  original(value)
                  sent <- fifo(activation_sent, open = "wb", blocking = TRUE)
                  writeBin(charToRaw("1"), sent)
                  close(sent)
                }, globals)
                reticulate::py_require("py-yaml12")
                """)
            evaluation = client.start_send(r=r, timeout_ms=0)
            activation_ready.wait("managed Python activation")
            client.receive(evaluation)
            evaluation_result = evaluation["result"]
            assert without_elapsed_result(evaluation_result) == {
                "content": [
                    {
                        "type": "text",
                        "text": "\n[running; poll with an empty send]",
                    }
                ],
                "isError": False,
            }, evaluation_result

            restart = client.start_send(
                control="restart",
                requirements={**action, "python": [replacement_requirement]},
            )
            uv_started.wait("restart Python resolution")
            activation_release.release()
            activation_sent.wait("published managed Python activation")
            uv_release.release()
            client.receive(restart)

            restart_result = restart["result"]
            assert restart_result.get("isError") is not True, restart_result
            assert restart_result["content"] == [
                {
                    "type": "text",
                    "text": (
                        "[active evaluation stopped by session restart request]\n"
                        "[worker stopped: in-memory state lost]\n"
                        "[starting new worker]\n"
                        "[idle]"
                    ),
                }
            ], restart_result

            # The replacement environment wins over the old generation's
            # activation, even though that event preceded ordered retirement.
            # fmt: r
            r = code(f"""
                packages <- reticulate::py_require()$packages
                c("{replacement_requirement}" %in% packages, "py-yaml12" %in% packages)
                """)
            client.send(r=r)
            assert last_tool_text(client) == "[1]  TRUE FALSE\n"
            assert reuse_record.read_text(encoding="utf-8").splitlines() == [
                "py-yaml12",
                replacement_requirement,
            ]
            transcript = client.finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_client(client)
            for checkpoint in worker_checkpoints:
                checkpoint.close()
            uv_started.close()
            uv_release.close()


@executions(DIRECT, SANDBOXED)
def test_prepares_python_requirements_after_worker_startup(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    # fmt: python
    python = code("""
        import importlib.util
        import os
        import sys

        sentinel = 42
        worker_pid = os.getpid()
        initial_prefix = sys.prefix
        importlib.util.find_spec("yaml12") is None
        """)
    client.send(python=python)
    assert last_tool_text(client) == "True\n"

    invalid = "not a valid requirement !!!"
    client.send(
        requirements={"python": [invalid]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == named_requirement_error(invalid)

    # fmt: python
    python = code("""
        sentinel, os.getpid() == worker_pid, importlib.util.find_spec("yaml12") is None
        """)
    client.send(python=python)
    assert last_tool_text(client) == "(42, True, True)\n"

    # fmt: python
    python = code("""
        import os
        import sys
        import yaml12

        (sentinel, os.getpid() == worker_pid, sys.prefix != initial_prefix, yaml12.__name__)
        """)
    client.send(
        python=python,
        requirements={"python": ["py-yaml12"]},
    )
    assert last_tool_text(client) == "(42, True, True, 'yaml12')\n"

    client.send(control="restart")
    assert last_tool_text(client) == (
        "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
    )
    client.send(r="is.null(reticulate::py_require()$python_version)")
    assert last_tool_text(client) == "[1] TRUE\n"
    # fmt: python
    python = code("""
        import yaml12

        "sentinel" in globals(), yaml12.__name__
        """)
    client.send(python=python)
    assert last_tool_text(client) == "(False, 'yaml12')\n"
    return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_failed_live_python_requirements_do_not_run_cell(
    binary: Path, execution: Execution
) -> Transcript:
    return failed_live_python_requirements_do_not_run_cell(binary, execution)


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_failed_no_r_live_python_requirements_do_not_run_cell(
    binary: Path, execution: Execution
) -> Transcript:
    return failed_live_python_requirements_do_not_run_cell(
        binary, execution, with_r=False
    )


def failed_live_python_requirements_do_not_run_cell(
    binary: Path, execution: Execution, *, with_r: bool = True
) -> Transcript:
    prior = "mcp_console_test_prior"
    candidate = "mcp_console_test_candidate"
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        # fmt: python
        prior_source = code("""
            state = {"answer": 42}


            def answer():
                return state["answer"]
            """)
        index = write_test_wheel(root, prior, prior_source)
        write_test_wheel(root, candidate, "answer = 7\n")
        environment, record = recording_uv_environment(root, fail_requirement=candidate)
        environment["UV_INDEX"] = index.as_uri()
        environment["UV_INDEX_STRATEGY"] = "first-index"
        for name in ("UV_FIND_LINKS", "UV_INDEX_URL", "UV_EXTRA_INDEX_URL"):
            environment.pop(name, None)
        if not with_r:
            uv = environment["RETICULATE_UV"]
            without_r(environment, root)
            commands = Path(environment["PATH"])
            (commands / "uv").symlink_to(uv)
            (commands / "python3").symlink_to(sys.executable)
        with McpClient(
            binary,
            execution.serve(
                "-c", "python.managed.resolution=implicit", "-c", "cache=host"
            ),
            environment,
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "[prepared]", requirements={"action": "set", "python": [prior]}
            )
            # fmt: python
            python = code("""
                import os
                import sys
                import mcp_console_test_prior

                live_module = mcp_console_test_prior
                live_object = live_module.state
                live_sentinel = live_module.answer()
                live_worker_pid = os.getpid()
                print(sys.executable)
                """)
            client.send(python=python)
            executable = last_tool_text(client).strip()
            assert Path(executable).is_absolute(), executable
            client.transcript[-1]["result"]["content"][0]["text"] = "<running Python>\n"
            accepted = client.send(requirements={"action": "get"})["structuredContent"][
                "requirements"
            ]
            assert accepted["python"] == [prior], accepted
            baseline = len(uv_tool_run_requirements(record))
            if with_r:
                # Ordinary tool preparation remains independent of the public
                # reticulate declaration function after R is initialized.
                # fmt: r
                r = code(r"""
                    reticulate_namespace <- asNamespace("reticulate")
                    unlockBinding("py_require", reticulate_namespace)
                    assign("py_require", function(...) stop("tool entered reticulate declaration"),
                           envir = reticulate_namespace)
                    lockBinding("py_require", reticulate_namespace)
                    """)
                client.expect(r=r)
            result = client.send(
                python="failed_live_python_cell = True",
                requirements={"python": [candidate]},
            )
            assert result["isError"] is True, result
            error = result["content"][0]["text"]
            request, diagnostic = error.removeprefix(
                "managed Python resolution failed:\nresolver input:\n"
            ).split("\nuv output:\n")
            requested = json.loads(request)
            assert requested["packages"] == [candidate, prior], requested
            assert requested["python"] == executable, requested
            assert diagnostic == "synthetic uv failure", diagnostic
            result["content"][0]["text"] = normalize_python_resolution_error(
                error, executable=executable
            )
            assert len(uv_tool_run_requirements(record)) == baseline + 1

            # Keep failure enforced: the negative import must not acquire the
            # candidate through automatic resolution while checking rejection.
            output = send_and_collect_runtime_python_resolution(
                client, python=f"import {candidate}"
            )
            assert client.transcript[-1]["result"]["isError"] is False
            for expected in ("ModuleNotFoundError", candidate, "synthetic uv failure"):
                assert expected in output, output
            client.transcript[-1]["result"]["content"][0]["text"] = (
                normalize_python_resolution_error(output, executable=executable)
            )
            runs = uv_tool_run_requirements(record)[baseline:]
            assert runs == [[candidate, prior], [candidate, prior]], runs
            assert (
                client.send(requirements={"action": "get"})["structuredContent"][
                    "requirements"
                ]
                == accepted
            )
            # fmt: python
            python = code("""
                import importlib.util
                import mcp_console_test_prior

                (
                    live_sentinel,
                    mcp_console_test_prior.answer(),
                    mcp_console_test_prior is live_module,
                    mcp_console_test_prior.state is live_object,
                    os.getpid() == live_worker_pid,
                    "failed_live_python_cell" not in globals(),
                    importlib.util.find_spec("mcp_console_test_candidate") is None,
                )
                """)
            client.send(python=python)
            assert last_tool_text(client) == "(42, 42, True, True, True, True, True)\n"
            assert len(uv_tool_run_requirements(record)) == baseline + 2

            (root / "uv-failure").unlink()
            # fmt: python
            python = code("""
                import mcp_console_test_candidate

                assert mcp_console_test_candidate.answer == 7
                raise RuntimeError("after accepted Python preparation")
                """)
            result = client.send(python=python, requirements={"python": [candidate]})
            assert result["isError"] is False, result
            assert "RuntimeError: after accepted Python preparation" in last_tool_text(
                client
            )
            retained = client.send(requirements={"action": "get"})["structuredContent"][
                "requirements"
            ]
            assert retained == dict(accepted, python=[candidate, prior]), retained
            resolved = len(uv_tool_run_requirements(record))
            assert resolved == baseline + 3
            # fmt: python
            python = code("""
                import mcp_console_test_prior
                import mcp_console_test_candidate

                (
                    live_sentinel,
                    mcp_console_test_prior.answer(),
                    mcp_console_test_candidate.answer,
                    mcp_console_test_prior is live_module,
                    mcp_console_test_prior.state is live_object,
                    os.getpid() == live_worker_pid,
                    "failed_live_python_cell" not in globals(),
                )
                """)
            client.send(python=python)
            assert last_tool_text(client) == "(42, 42, 7, True, True, True, True)\n"
            assert len(uv_tool_run_requirements(record)) == resolved
            return client.finish()


@requires(R_EVENT_LOOP)
@executions(DIRECT, SANDBOXED)
def test_prepares_after_idle_python_resolution(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(
        binary,
        execution.serve("-c", "python.managed.resolution=implicit"),
    )
    client.initialize_and_list_tools()
    client.send(requirements={"r": ["later"]})

    # fmt: r
    r = code(r"""
        callback_gate <- tempfile("mcp-console-callback-gate-")
        callback_checkpoint <- tempfile("mcp-console-callback-checkpoint-")
        run_callback <- function() {
          if (!file.exists(callback_gate)) {
            later::later(run_callback, delay = 0.01)
            return(invisible(NULL))
          }
          stopifnot(file.create(callback_checkpoint))
          reticulate::py_require("py-yaml12")
          reticulate::py_config()
          cat("idle Python ready\n")
        }
        later::later(run_callback, delay = 0.01)
        cat(callback_gate, callback_checkpoint, sep = "\n")
        """)
    client.send(r=r)
    release_worker_callback_gate(client, "idle Python callback")

    client.expect("[prepared]", requirements={"python": ["py-yaml12"]})
    client.send(r="dput(reticulate::py_require()$packages)")
    assert "idle Python ready\n" in last_tool_text(client)
    assert '"py-yaml12"' in last_tool_text(client)
    return client.finish()


@requires(R_EVENT_LOOP)
@executions(DIRECT, SANDBOXED)
def test_retains_idle_python_activation_during_continuous_collection(
    binary: Path,
    execution: Execution,
) -> Transcript:
    client = McpClient(
        binary,
        execution.serve("-c", "python.managed.resolution=implicit"),
    )
    client.initialize_and_list_tools()
    client.send(requirements={"r": ["later"]})
    client.expect(r="invisible(reticulate::py_config())")

    # fmt: r
    r = code(r"""
        callback_gate <- tempfile("mcp-console-callback-gate-")
        callback_checkpoint <- tempfile("mcp-console-callback-checkpoint-")
        callback_complete <- tempfile("mcp-console-callback-complete-")
        run_callback <- function() {
          if (!file.exists(callback_gate)) {
            later::later(run_callback, delay = 0.01)
            return(invisible(NULL))
          }
          stopifnot(file.create(callback_checkpoint))
          reticulate::py_require("py-yaml12")
          cat("idle Python activated\n")
          stopifnot(file.create(callback_complete))
        }
        later::later(run_callback, delay = 0.01)
        cat(callback_gate, callback_checkpoint, callback_complete, sep = "\n")
        """)
    client.send(r=r)
    (callback_complete,) = release_worker_callback_gate(
        client,
        "idle Python activation",
        ("complete",),
    )
    deadline = time.monotonic() + 30
    while not callback_complete.exists():
        assert client.process.poll() is None, (
            "mcp-console stopped before idle Python activation completed"
        )
        if time.monotonic() >= deadline:
            raise AssertionError("idle Python activation did not complete")
        time.sleep(0.01)
    wait_for_idle_output(
        client,
        "idle Python activated\n\n[idle]",
        "idle Python activation output",
    )

    client.send(control="restart")
    assert last_tool_text(client) == (
        "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
    )
    client.send(python="import yaml12; yaml12.__name__")
    assert last_tool_text(client) == "'yaml12'\n"
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_does_not_retain_stale_python_materialization(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    client.expect(r="invisible(reticulate::py_config())")

    # Resolve an unchanged candidate while explicit preparation has resolved
    # its real addition. Only the exact activated manifest may be retained.
    # fmt: r
    r = code(r"""
        resolve_unchanged <- function() {
          current <- reticulate::py_require()
          manifest <- list(
            packages = I(current$packages),
            python_version = I(if (is.null(current$python_version)) character() else current$python_version),
            exclude_newer = current$exclude_newer
          )
          request <- jsonlite::toJSON(list(
            requirements = manifest, retained_requirements = manifest
          ), auto_unbox = TRUE, null = "null")
          invisible(.Call("mcp_console_resolve_python", request))
        }
        """)
    client.expect(r=r)
    # fmt: python
    python = code("""
        import _mcp_console_environment as environment

        original_check = environment._check_compatible


        def check_after_materialization(candidate):
            environment._check_compatible = original_check
            r.resolve_unchanged()
            original_check(candidate)


        environment._check_compatible = check_after_materialization
        """)
    client.expect(python=python)

    client.expect("[prepared]", requirements={"python": ["py-yaml12"]})
    client.send(control="restart")
    assert last_tool_text(client) == (
        "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
    )
    client.send(python="import yaml12; yaml12.__name__")
    assert last_tool_text(client) == "'yaml12'\n"
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_failed_restart_requirements_preserve_worker(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    client.expect(python="restart_marker = 42")
    invalid = "not a valid requirement !!!"

    client.send(
        control="restart",
        requirements={"python": [invalid]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == named_requirement_error(invalid)

    client.send(python="restart_marker")
    assert last_tool_text(client) == "42\n"
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_layers_python_requirements_declared_by_r_packages(
    binary: Path,
    execution: Execution,
) -> Transcript:
    environment, rscript = r_test_environment()
    fixture = Path(__file__).parents[3] / "fixtures" / "py_require"
    with tempfile.TemporaryDirectory() as library:
        subprocess.run(
            [
                rscript.with_name("R"),
                "CMD",
                "INSTALL",
                f"--library={library}",
                fixture,
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        environment["R_LIBS"] = os.pathsep.join(
            filter(None, (library, environment.get("R_LIBS")))
        )
        client = McpClient(
            binary,
            execution.serve(),
            environment,
        )
        client.initialize_and_list_tools()
        # fmt: python
        python = code("""
            import importlib.util
            import sys

            runtime_marker = 42
            initial_prefix = sys.prefix
            importlib.util.find_spec("yaml12") is None
            """)
        client.send(python=python)
        assert last_tool_text(client) == "True\n"

        # fmt: r
        r = code(r"""
            initial_libpython <- reticulate::py_config()$libpython
            initial_worker <- Sys.getpid()
            """)
        client.expect(r=r)

        client.expect(r="library(mcpconsolepyrequire)")
        assert "py-yaml12" in inspect(client)["requirements"]["python"]

        # fmt: r
        r = code(r"""
            identical(reticulate::py_config()$libpython, initial_libpython) &&
              identical(Sys.getpid(), initial_worker)
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[1] TRUE\n"

        # fmt: python
        python = code("""
            import yaml12

            (runtime_marker, yaml12.__name__, sys.prefix != initial_prefix)
            """)
        client.send(python=python)
        output = last_tool_text(client)
        assert output == "(42, 'yaml12', True)\n", repr(output)

        client.send(control="restart")
        assert last_tool_text(client) == (
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        )

        # fmt: python
        python = code("""
            import yaml12

            ("runtime_marker" in globals(), yaml12.__name__)
            """)
        client.send(python=python)
        assert last_tool_text(client) == "(False, 'yaml12')\n"

        client.expect("[prepared]", requirements={"python": ["py-yaml12"]})
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_does_not_retain_package_requirements_before_python_initializes(
    binary: Path,
    execution: Execution,
) -> Transcript:
    environment, rscript = r_test_environment()
    environment["MCP_CONSOLE_LANGUAGES"] = "r"
    fixture = Path(__file__).parents[3] / "fixtures" / "py_require"
    with tempfile.TemporaryDirectory() as library:
        subprocess.run(
            [
                rscript.with_name("R"),
                "CMD",
                "INSTALL",
                f"--library={library}",
                fixture,
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        environment["R_LIBS"] = os.pathsep.join(
            filter(None, (library, environment.get("R_LIBS")))
        )
        client = McpClient(binary, execution.serve(), environment)
        client.initialize_and_list_tools()
        # fmt: r
        r = code(r"""
            library(mcpconsolepyrequire)
            request <- tail(reticulate::py_require()$history, 1L)[[1L]]
            stopifnot(
              identical(request$requested_from, "mcpconsolepyrequire"),
              isTRUE(request$env_is_package)
            )
            """)
        client.expect(r=r)

        # A lazy declaration is worker-owned until Python initializes or an
        # explicit preparation materializes it.
        # fmt: r
        r = code(r"""
            tools::pskill(Sys.getpid(), signal = 9L)
            """).removesuffix("\n")
        client.send(r=r)
        result = client.transcript[-1]["result"]
        assert result["isError"] is True
        actual = result["content"][0]["text"]
        assert actual == (
            "[worker sideband read failed: worker sideband closed]\n"
            "[worker terminated by signal 9]\n"
            "[worker stopped: in-memory state lost]\n"
            "[starting new worker]\n"
            "[idle]"
        ), repr(actual)

        # fmt: r
        r = code(r"""
            "py-yaml12" %in% reticulate::py_require()$packages
            """)
        client.send(r=r)
        output = last_tool_text(client)
        assert output == "[1] FALSE\n", repr(output)
        client.expect(
            r='library(mcpconsolepyrequire); yaml12 <- reticulate::import("yaml12")'
        )
        client.expect("[1] 42\n", r='yaml12$parse_yaml("answer: 42")$answer')
        assert "py-yaml12" in inspect(client)["requirements"]["python"]
        return client.finish()[3:]


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_retains_python_activation_before_later_cell_failure(
    binary: Path,
    execution: Execution,
) -> Transcript:
    client = McpClient(
        binary,
        execution.serve("-c", "python.managed.resolution=implicit"),
    )
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        invisible(reticulate::py_config())
        invisible(reticulate::py_require("py-yaml12"))
        stopifnot(reticulate::py_module_available("yaml12"))
        tools::pskill(Sys.getpid(), signal = 9L)
        """).removesuffix("\n")
    client.send(r=r)
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == (
        "[worker sideband read failed: worker sideband closed]\n"
        "[worker terminated by signal 9]\n"
        "[worker stopped: in-memory state lost]\n"
        "[starting new worker]\n"
        "[idle]"
    )

    # The successful activation is retained even though the cell later kills
    # the worker before its ordinary completion message.
    # fmt: r
    r = code(r"""
        worker_pid <- Sys.getpid()
        "py-yaml12" %in% reticulate::py_require()$packages
        """)
    client.send(r=r)
    output = last_tool_text(client)
    assert output == "[1] TRUE\n", repr(output)

    # fmt: python
    python = code("""
        import yaml12

        yaml12.__name__
        """)
    client.send(python=python)
    assert last_tool_text(client) == "'yaml12'\n"
    return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
