#!/usr/bin/env -S uv run --script

import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import (
    assert_exact_interleaving,
    assert_result_content,
    last_result_text,
    wait_for_evaluation_output,
    wait_for_idle_output,
    wait_for_worker_ready,
)
from support.checkpoints import FifoCheckpoint, wait_for_worker_file
from support.client import McpClient, stop_client
from support.evidence import compact_text
from support.execution import DIRECT, RUNTIME, SANDBOXED, Execution, executions
from support.normalization import code
from support.r import r_test_environment, reference_plots
from support.records import Transcript
from support.requirements import POSIX, R, requires
from support.resolvers import matplotlib_test_environment
from support.suites import run_this_suite
from boundaries.client_server.server.test_no_r import no_r_environment
from boundaries.client_server.python.test_setup import deferred_selection_client


@executions(DIRECT, SANDBOXED)
def test_evaluates_cells_in_persistent_reticulate_state(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        from_r <- 40L
        python_source_visible <- function() {
          calls <- vapply(sys.calls(), deparse1, character(1))
          marker <- paste0("unique_python_", "source_marker")
          any(grepl(marker, calls, fixed = TRUE))
        }
        reticulate::py_run_string(
          r"---(
        test_sys = __import__("sys")
        test_types = __import__("types")
        __import__ = None
        exec = None
        setattr = None
        _io = "user io"
        _main = "user main"
        _sys = "user sys"
        sorted = "user sorted"
        test_sys.modules["matplotlib.pyplot"] = test_types.SimpleNamespace(
            get_fignums=lambda: [],
            close=lambda *_args, **_kwargs: None,
        )
        )---"
        )
        """)
    client.send(r=r)
    # fmt: python
    python = code("""
        answer = r.from_r + 1
        print("from Python")
        (
            answer + 1,
            (__import__, exec, setattr) == (None, None, None),
            (_io, _main, _sys, sorted) == ("user io", "user main", "user sys", "user sorted"),
            "_mcp_console" not in globals()
            and test_sys.modules["_mcp_console"].__name__ == "_mcp_console",
        )
        """)
    client.send(python=python)
    output = last_result_text(client)
    assert output == "from Python\n(42, True, True, True)\n", repr(output)
    # fmt: python
    python = code("""
        1
        2
        """)
    client.send(python=python)
    assert last_result_text(client) == "2\n"
    client.send(python="answer")
    assert last_result_text(client) == "41\n"
    # fmt: r
    r = code(r"""
        stopifnot(!"package:reticulate" %in% search())
        py <- "user shadow"
        stopifnot(identical(py, "user shadow"))
        rm(py)
        py$answer
        """)
    client.send(r=r)
    assert last_result_text(client) == "[1] 41\n"
    # fmt: python
    python = code("""
        unique_python_source_marker = r.python_source_visible()
        unique_python_source_marker
        """)
    client.send(python=python)
    output = last_result_text(client)
    assert output == "False\n", repr(output)
    # fmt: r
    r = code(r"""
        .mcp_console_private <- "user value"
        .mcp_console_python_source <- "user source"
        .mcp_console_python_filename <- "user filename"
        is.null <- function(...) FALSE
        """)
    client.send(r=r)
    client.send(python="answer + 1")
    assert last_result_text(client) == "42\n"
    # fmt: python
    python = code("""
        compile = "user compile"
        eval = "user eval"
        exec = "user exec"
        isinstance = "user isinstance"
        BaseException = "user BaseException"
        """)
    client.send(python=python)
    assert last_result_text(client) == "[done]"
    client.send(python="answer + 1")
    assert last_result_text(client) == "42\n"
    # fmt: python
    python = code("""
        import builtins as test_builtins

        test_original_import = test_builtins.__import__
        test_builtins.__import__ = None
        """)
    client.send(python=python)
    assert last_result_text(client) == "[done]"
    # fmt: python
    python = code("""
        test_builtins.__import__ = test_original_import
        answer + 1
        """)
    client.send(python=python)
    assert last_result_text(client) == "42\n"
    client.send(python="silent = True")
    assert last_result_text(client) == "[done]"
    # fmt: r
    r = code(r"""
        rm(list = ls())
        py$assigned_from_r <- 43L
        py$answer
        """)
    client.send(r=r)
    assert last_result_text(client) == "[1] 41\n"
    # fmt: python
    python = code("""
        assigned_from_r
        """)
    client.send(python=python)
    assert last_result_text(client) == "43\n"
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_returns_r_plots_from_python_bridge(
    binary: Path, execution: Execution
) -> Transcript:
    environment, rscript = r_test_environment()
    client = McpClient(binary, execution.serve(), environment)
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        bridge_plot <- function() {
          plot(1:3)
          invisible(NULL)
        }
        """)
    client.send(r=r)
    assert last_result_text(client) == "[done]"

    expected_plot = reference_plots(
        rscript,
        environment,
        r + "bridge_plot()\n",
        width=800 / 96,
        height=600 / 96,
        dpi=96,
        pages=1,
    )
    # fmt: python
    python = code("""
        print("before plot")
        r.bridge_plot()
        print("after plot")
        """)
    client.send(python=python)
    assert_result_content(
        client,
        ["before plot\nafter plot\n", expected_plot[0]],
    )
    return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(R)
def test_returns_matplotlib_plots(binary: Path, execution: Execution) -> Transcript:
    return returns_matplotlib_plots(binary, execution, with_r=True)


@executions(DIRECT, SANDBOXED)
@requires(R)
def test_returns_matplotlib_plots_from_r_bridge(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        environment = matplotlib_test_environment(temporary / "host-cache")
        environment["TMPDIR"] = directory
        with McpClient(
            binary, execution.serve("-c", "cache=host"), environment
        ) as client:
            client.initialize_and_list_tools()
            wait_for_worker_ready(client, "Matplotlib declaration readiness")
            client.expect("[prepared]", requirements={"python": ["matplotlib"]})
            client.expect(
                "[done]",
                # fmt: r
                r=code("""
                    plt <- reticulate::import("matplotlib.pyplot")
                    invisible(plt$show())
                    """),
            )
            client.send(
                # fmt: r
                r=code("""
                    figure <- plt$figure()
                    invisible(plt$plot(1:3, c(3, 1, 2)))
                    invisible(figure$savefig(file.path(Sys.getenv("TMPDIR"), "r-show.png")))
                    cat("before show\\n")
                    invisible(plt$show())
                    stopifnot(length(plt$get_fignums()) == 0L)
                    invisible(plt$show())
                    cat("after show\\n")
                    """),
            )
            assert_result_content(
                client,
                [
                    "before show\n",
                    wait_for_worker_file(temporary, "r-show.png", client).read_bytes(),
                    "after show\n",
                ],
                image_reference="live R bridge matplotlib savefig {page}",
            )
            client.expect(
                "[]\n",
                python="import matplotlib.pyplot as plt; plt.get_fignums()",
            )
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_returns_no_r_matplotlib_plots(
    binary: Path, execution: Execution
) -> Transcript:
    return returns_matplotlib_plots(binary, execution, with_r=False)


@contextmanager
def no_r_matplotlib_client(binary: Path, execution: Execution):
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        environment = no_r_environment(temporary)
        environment["TMPDIR"] = temporary_directory
        environment["MPL_IGNORE_SYSTEM_FONTS"] = "1"
        with McpClient(binary, execution.serve(), environment) as client:
            client.initialize_and_list_tools()
            wait_for_worker_ready(client, "Matplotlib declaration readiness")
            client.expect("[prepared]", requirements={"python": ["matplotlib"]})
            yield client, temporary


@executions(DIRECT, SANDBOXED)
def test_preserves_matplotlib_pause_figures(
    binary: Path, execution: Execution
) -> Transcript:
    with no_r_matplotlib_client(binary, execution) as (client, temporary):
        client.send(
            # fmt: python
            python=code("""
                import os
                from pathlib import Path
                import matplotlib.pyplot as plt

                other_figure = plt.figure(num=20)
                other_figure.savefig(Path(os.environ["TMPDIR"]) / "pause-other.png")
                figure, axes = plt.subplots(num=10)
                (line,) = axes.plot([0, 1], [0, 1])
                original_event_loop = figure.canvas.start_event_loop


                def checked_event_loop(interval):
                    assert plt.get_fignums() == [10, 20]
                    assert plt.gcf() is figure
                    original_event_loop(interval)


                figure.canvas.start_event_loop = checked_event_loop
                for index in range(3):
                    line.set_ydata([index, 1])
                    figure.savefig(Path(os.environ["TMPDIR"]) / f"pause-{index}.png")
                    plt.pause(0.001)
                    assert plt.get_fignums() == [10, 20]
                    assert plt.gcf() is figure
                plt.close("all")
                "pause loop finished"
                """),
        )
        assert_result_content(
            client,
            [
                wait_for_worker_file(temporary, filename, client).read_bytes()
                for index in range(3)
                for filename in (f"pause-{index}.png", "pause-other.png")
            ]
            + ["'pause loop finished'\n"],
            image_reference="live pause matplotlib savefig {page}",
        )
        client.expect("[]\n", python="plt.get_fignums()")
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_interrupts_explicit_matplotlib_show(
    binary: Path, execution: Execution
) -> Transcript:
    with no_r_matplotlib_client(binary, execution) as (client, _temporary):
        # Complete cold imports and figure setup before the render checkpoint.
        client.expect(
            # fmt: python
            python=code("""
                import matplotlib.pyplot as plt

                figure = plt.figure()
                plt.close(figure)


                def blocked_savefig(*args, **kwargs):
                    input("render> ")
                """),
        )
        wait_for_evaluation_output(
            client,
            '[input requested: "render> "]\n[waiting for stdin]',
            "explicit show render checkpoint",
            completion_timeout_seconds=client.response_timeout,
            # fmt: python
            python=code("""
                figure = plt.figure()
                figure.savefig = blocked_savefig
                continued_after_show = False
                plt.show()
                continued_after_show = True
                """),
            timeout_ms=0,
        )
        output = wait_for_evaluation_output(
            client,
            lambda text: (
                text.startswith("Traceback (most recent call last):\n")
                and text.endswith("KeyboardInterrupt\n")
            ),
            "explicit show interruption",
            control="interrupt",
        )
        assert output.count("KeyboardInterrupt") == 1, output
        client.expect("(False, [])\n", python="continued_after_show, plt.get_fignums()")
        return client.finish()


def returns_matplotlib_plots(
    binary: Path, execution: Execution, *, with_r: bool
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory, ExitStack() as clients:
        temporary = Path(temporary_directory)
        workspace = temporary / "workspace"
        workspace.mkdir()
        host_matplotlib = temporary / "host-matplotlib"
        host_matplotlib.mkdir()
        host_matplotlibrc = host_matplotlib / "matplotlibrc"
        host_matplotlibrc.write_text("lines.linewidth: 7.25\n", encoding="utf-8")
        environment = (
            matplotlib_test_environment(temporary / "host-cache")
            if with_r
            else no_r_environment(temporary)
        )
        environment["XDG_CACHE_HOME"] = str(temporary / "host-cache")
        environment["TMPDIR"] = temporary_directory
        environment["MPLCONFIGDIR"] = str(host_matplotlib)
        environment["MCP_CONSOLE_TEST_MATPLOTLIBRC"] = str(host_matplotlibrc)
        environment.pop("MATPLOTLIBRC", None)
        environment["MPL_IGNORE_SYSTEM_FONTS"] = "1"
        client = clients.enter_context(
            McpClient(
                binary,
                execution.serve(
                    "-c", "python.managed.resolution=implicit", "-c", "cache=host"
                ),
                environment,
                current_directory=workspace,
            )
        )
        client.initialize_and_list_tools()
        if with_r:
            # fmt: r
            r = code(r"""
                reticulate::py_require("matplotlib")
                invisible(reticulate::py_config())
                """)
            client.expect(r=r)
        else:
            wait_for_worker_ready(client, "Matplotlib declaration readiness")
            client.expect("[prepared]", requirements={"python": ["matplotlib"]})
        # fmt: python
        python = code("""
            import os
            from pathlib import Path

            import matplotlib
            import matplotlib.pyplot as plt

            assert Path(matplotlib.matplotlib_fname()).samefile(
                os.environ["MCP_CONSOLE_TEST_MATPLOTLIBRC"]
            )
            assert matplotlib.rcParams["lines.linewidth"] == 7.25

            later_figure, later_axes = plt.subplots(num=20)
            later_axes.plot([1, 2, 3], [1, 2, 1])
            later_reference = Path(os.environ["TMPDIR"]) / "matplotlib-later-reference.png"
            later_figure.savefig(later_reference, format="png")

            figure, axes = plt.subplots(num=10)
            axes.plot([1, 2, 3], [3, 1, 2])

            reference = Path(os.environ["TMPDIR"]) / "matplotlib-reference.png"
            figure.savefig(reference, format="png")
            """)
        client.send(python=python)
        reference = wait_for_worker_file(
            Path(temporary_directory),
            "matplotlib-reference.png",
            client,
        )
        later_reference = wait_for_worker_file(
            Path(temporary_directory),
            "matplotlib-later-reference.png",
            client,
        )
        assert_result_content(
            client,
            [reference.read_bytes(), later_reference.read_bytes()],
            image_reference="live matplotlib savefig {page}",
        )

        # fmt: python
        python = code("""
            shown_figure, shown_axes = plt.subplots()
            shown_axes.plot([1, 2, 3], [1, 3, 2])
            shown_reference = Path(os.environ["TMPDIR"]) / "matplotlib-shown-reference.png"
            shown_figure.savefig(shown_reference, format="png")
            print("before show")
            plt.show()
            plt.close(shown_figure)
            print("after show")
            shown_figure
            """)
        client.send(python=python)
        shown_reference = wait_for_worker_file(
            Path(temporary_directory),
            "matplotlib-shown-reference.png",
            client,
        )
        result = client.transcript[-1]["result"]
        assert result["content"][0] == {"type": "text", "text": "before show\n"}
        output = result["content"][-1]["text"]
        assert output.startswith("after show\n<Figure size "), output
        assert output.endswith(" with 1 Axes>\n"), output
        result["content"][-1]["text"] = """after show
<matplotlib figure displayhook representation>
"""
        assert_result_content(
            client,
            [
                "before show\n",
                shown_reference.read_bytes(),
                result["content"][-1]["text"],
            ],
            image_reference="live shown matplotlib savefig {page}",
        )

        client.send(
            # fmt: python
            python=code("""
                for index in range(3):
                    loop_figure, loop_axes = plt.subplots(num=10)
                    loop_axes.plot([0, 1, 2], [index, 2, 1])
                    loop_figure.savefig(
                        Path(os.environ["TMPDIR"]) / f"matplotlib-loop-{index}.png",
                        format="png",
                    )
                    plt.show(block=False)
                    assert plt.get_fignums() == []
                    plt.close(loop_figure)
                    plt.show()

                # An unshown figure using the same number still captures at cell end.
                remaining_figure, remaining_axes = plt.subplots(num=10)
                remaining_axes.plot([0, 1], [3, 1])
                remaining_figure.savefig(
                    Path(os.environ["TMPDIR"]) / "matplotlib-remaining.png",
                    format="png",
                )
                """),
        )
        assert_result_content(
            client,
            [
                wait_for_worker_file(
                    temporary, f"matplotlib-loop-{index}.png", client
                ).read_bytes()
                for index in range(3)
            ]
            + [
                wait_for_worker_file(
                    temporary, "matplotlib-remaining.png", client
                ).read_bytes()
            ],
            image_reference="live repeated-show matplotlib savefig {page}",
        )
        client.expect("[]\n", python="plt.get_fignums()")

        # fmt: python
        python = code("""
            closed_figure, closed_axes = plt.subplots()
            closed_axes.plot([1, 2, 3], [2, 1, 3])
            closed_reference = Path(os.environ["TMPDIR"]) / "matplotlib-closed-reference.png"
            closed_figure.savefig(closed_reference, format="png")
            plt.close(closed_figure)
            plt.get_fignums()
            """)
        client.send(python=python)
        closed_reference = wait_for_worker_file(
            Path(temporary_directory),
            "matplotlib-closed-reference.png",
            client,
        )
        assert closed_reference.is_file()
        assert last_result_text(client) == "[]\n"

        # fmt: python
        python = code("""
            axes.plot([1, 3], [2, 0])
            plt.get_fignums()
            """)
        client.send(python=python)
        assert last_result_text(client) == "[]\n"

        # fmt: python
        python = code("""
            error_figure, error_axes = plt.subplots()
            error_axes.plot([1, 2], [2, 1])
            error_reference = Path(os.environ["TMPDIR"]) / "matplotlib-error-reference.png"
            error_figure.savefig(error_reference, format="png")
            raise ValueError("cell failed")
            """)
        client.send(python=python)
        result = client.transcript[-1]["result"]
        assert result["isError"] is False, result
        output = result["content"][0]["text"]
        assert output.startswith("Traceback (most recent call last):\n"), output
        assert output.endswith("ValueError: cell failed\n"), output
        error_reference = wait_for_worker_file(
            Path(temporary_directory),
            "matplotlib-error-reference.png",
            client,
        )
        assert_result_content(
            client,
            [result["content"][0]["text"], error_reference.read_bytes()],
            image_reference="live error-cell matplotlib savefig {page}",
        )

        client.send(python="plt.get_fignums()")
        assert last_result_text(client) == "[]\n"

        # fmt: python
        python = code("""
            def fail_plot_capture(*args, **kwargs):
                raise RuntimeError("plot render failed")


            failed_figure = plt.figure()
            failed_figure.savefig = fail_plot_capture
            figure, axes = plt.subplots()
            axes.plot([1, 3], [2, 0])
            second_reference = Path(os.environ["TMPDIR"]) / "matplotlib-second-reference.png"
            figure.savefig(second_reference, format="png")
            """)
        client.send(python=python)
        result = client.transcript[-1]["result"]
        assert result["isError"] is False, result
        output = result["content"][0]["text"]
        assert output.startswith("Traceback (most recent call last):\n"), output
        assert 'File "<string>"' not in output, output
        assert output.endswith("RuntimeError: plot render failed\n"), output
        second_reference = wait_for_worker_file(
            Path(temporary_directory),
            "matplotlib-second-reference.png",
            client,
        )
        assert_result_content(
            client,
            [result["content"][0]["text"], second_reference.read_bytes()],
            image_reference="live second matplotlib savefig {page}",
        )

        client.send(python="plt.get_fignums()")
        assert last_result_text(client) == "[]\n"

        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_inherits_explicit_matplotlib_config(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory, ExitStack() as clients:
        temporary = Path(temporary_directory)
        explicit = temporary / "explicit"
        explicit.mkdir()
        explicit_rc = explicit / "matplotlibrc"
        explicit_rc.write_text("lines.linewidth: 8.25\n", encoding="utf-8")
        inherited = temporary / "inherited"
        inherited.mkdir()
        (inherited / "matplotlibrc").write_text(
            "lines.linewidth: 18.25\n",
            encoding="utf-8",
        )
        environment = matplotlib_test_environment(temporary / "host-cache")
        environment["TMPDIR"] = temporary_directory
        environment["MPLCONFIGDIR"] = str(inherited)
        environment["MATPLOTLIBRC"] = str(explicit_rc)
        environment["MPL_IGNORE_SYSTEM_FONTS"] = "1"
        environment["MCP_CONSOLE_TEST_MATPLOTLIBRC"] = str(explicit_rc)
        client = clients.enter_context(
            McpClient(binary, execution.serve("-c", "cache=host"), environment)
        )
        client.initialize_and_list_tools()
        wait_for_worker_ready(client, "explicit Matplotlib declaration readiness")
        client.expect("[prepared]", requirements={"python": ["matplotlib"]})
        # fmt: python
        python = code("""
            import os
            from pathlib import Path

            import matplotlib

            config = Path(matplotlib.matplotlib_fname())
            private_probe = Path(os.environ["MPLCONFIGDIR"]) / "config-write-probe"
            private_probe.write_text("ok", encoding="utf-8")

            (
                config.samefile(os.environ["MCP_CONSOLE_TEST_MATPLOTLIBRC"]),
                matplotlib.rcParams["lines.linewidth"],
                private_probe.read_text(encoding="utf-8") == "ok",
            )
            """)
        client.send(python=python)
        output = last_result_text(client)
        assert output == "(True, 8.25, True)\n", repr(output)
        transcript = client.finish()
        assert explicit_rc.read_text(encoding="utf-8") == "lines.linewidth: 8.25\n"
        assert not list(explicit.glob("fontlist-v*.json"))
        caches = list(inherited.glob("fontlist-v*.json"))
        assert len(caches) == 1, caches
        assert not list(
            (temporary / "host-cache" / "mcp-console" / "matplotlib").glob(
                "fontlist-v*.json"
            )
        )
        return transcript


@executions(DIRECT, SANDBOXED)
def test_inherits_default_matplotlib_config(
    binary: Path, execution: Execution
) -> Transcript:
    return inherits_matplotlib_config(binary, execution, xdg=False)


@executions(DIRECT, SANDBOXED)
def test_inherits_xdg_matplotlib_config(
    binary: Path, execution: Execution
) -> Transcript:
    return inherits_matplotlib_config(binary, execution, xdg=True)


def inherits_matplotlib_config(
    binary: Path, execution: Execution, *, xdg: bool
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory, ExitStack() as clients:
        temporary = Path(temporary_directory)
        home = temporary / "home"
        config_root = temporary / "xdg-config" if xdg else home / ".config"
        cache_root = temporary / "xdg-cache" if xdg else home / ".cache"
        matplotlib = (
            config_root / "matplotlib"
            if sys.platform == "linux"
            else home / ".matplotlib"
        )
        font_cache = (
            cache_root / "matplotlib" if sys.platform == "linux" else matplotlib
        )
        matplotlib.mkdir(parents=True)
        matplotlibrc = matplotlib / "matplotlibrc"
        matplotlibrc.write_text("lines.linewidth: 9.25\n", encoding="utf-8")
        r_environment, rscript = r_test_environment()
        # fmt: r
        source = code(r"""
            writeLines(.libPaths())
            """)
        r_libraries = subprocess.run(
            [rscript, "--vanilla", "-e", source],
            check=True,
            capture_output=True,
            text=True,
            env=r_environment,
        ).stdout.splitlines()
        uv = shutil.which("uv")
        assert uv is not None, "real uv is required for managed-Python tests"
        uv_cache = subprocess.run(
            [uv, "cache", "dir"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        uv_python = subprocess.run(
            [uv, "python", "dir"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        environment = matplotlib_test_environment(temporary / "host-cache")
        home.mkdir(exist_ok=True)
        environment["HOME"] = str(home)
        if os.name == "nt":
            environment["USERPROFILE"] = str(home)
        environment.pop("XDG_CONFIG_HOME", None)
        environment.pop("XDG_CACHE_HOME", None)
        if xdg:
            environment["XDG_CONFIG_HOME"] = str(config_root)
            environment["XDG_CACHE_HOME"] = str(cache_root)
        environment["TMPDIR"] = temporary_directory
        environment["R_LIBS_USER"] = os.pathsep.join(r_libraries)
        environment["RETICULATE_UV"] = uv
        environment["UV_CACHE_DIR"] = uv_cache
        environment["UV_PYTHON_INSTALL_DIR"] = uv_python
        environment["MPL_IGNORE_SYSTEM_FONTS"] = "1"
        environment["MCP_CONSOLE_TEST_MATPLOTLIBRC"] = str(matplotlibrc)
        environment.pop("MATPLOTLIBRC", None)
        environment.pop("MPLCONFIGDIR", None)
        client = clients.enter_context(
            McpClient(binary, execution.serve("-c", "cache=host"), environment)
        )
        client.initialize_and_list_tools()
        wait_for_worker_ready(client, "inherited Matplotlib declaration readiness")
        client.expect("[prepared]", requirements={"python": ["matplotlib"]})
        # fmt: python
        python = code("""
            import os
            from pathlib import Path

            import matplotlib

            (
                Path(matplotlib.matplotlib_fname()).samefile(
                    os.environ["MCP_CONSOLE_TEST_MATPLOTLIBRC"]
                ),
                matplotlib.rcParams["lines.linewidth"],
            )
            """)
        client.send(python=python)
        output = last_result_text(client)
        assert output == "(True, 9.25)\n", repr(output)
        transcript = client.finish()
        assert matplotlibrc.read_text(encoding="utf-8") == "lines.linewidth: 9.25\n"
        caches = list(font_cache.glob("fontlist-v*.json"))
        assert len(caches) == 1, caches
        assert not list(
            (temporary / "host-cache" / "mcp-console" / "matplotlib").glob(
                "fontlist-v*.json"
            )
        )
        return transcript


@executions(RUNTIME)
def test_runs_async_python_explicitly(binary: Path, execution: Execution) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    # fmt: python
    python = code("""
        import asyncio


        async def answer():
            await asyncio.sleep(0)
            return 42
        """)
    client.send(python=python)
    assert last_result_text(client) == "[done]"
    client.send(python="asyncio.run(answer())")
    assert last_result_text(client) == "42\n"
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_runs_python_thread_while_idle(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        release = Path(temporary_directory) / "python-thread-release"
        environment["MCP_CONSOLE_TEST_THREAD_RELEASE"] = str(release)
        with McpClient(binary, execution.serve(), environment) as client:
            try:
                client.initialize_and_list_tools()
                # fmt: python
                python = code("""
                    import os
                    import threading
                    import time
                    from pathlib import Path

                    temporary = Path(os.environ["TMPDIR"])
                    started = temporary / "python-thread-started"
                    release = Path(os.environ["MCP_CONSOLE_TEST_THREAD_RELEASE"])
                    finished = temporary / "python-thread-finished"
                    background_value = "waiting"


                    def run_in_background():
                        global background_value
                        started.touch()
                        while not release.exists():
                            time.sleep(0.01)
                        background_value = "finished while idle"
                        print("hello from background thread", flush=True)
                        finished.touch()


                    background_thread = threading.Thread(target=run_in_background)
                    background_thread.start()
                    background_thread.is_alive()
                    """)
                client.send(python=python)
                assert last_result_text(client) == "True\n"

                wait_for_worker_file(
                    Path(temporary_directory),
                    "python-thread-started",
                    client,
                )
                client.send(timeout_ms=0)
                assert last_result_text(client) == "\n[idle]"

                release.touch()
                wait_for_worker_file(
                    Path(temporary_directory),
                    "python-thread-finished",
                    client,
                )
                wait_for_idle_output(
                    client,
                    f"hello from background thread{os.linesep}\n[idle]",
                    "idle Python thread output",
                    timeout_ms=0,
                )
                # The receipt above checks the native bytes; idle scheduling owns
                # this case, so newline presentation can share a snapshot.
                client.transcript[-1]["result"]["content"][0]["text"] = (
                    last_result_text(client).replace("\r\n", "\n")
                )

                client.send(
                    python=(
                        "background_thread.join(); "
                        "(background_thread.is_alive(), background_value)"
                    )
                )
                assert last_result_text(client) == "(False, 'finished while idle')\n"
                return client.finish()
            finally:
                release.touch(exist_ok=True)


@executions(RUNTIME)
def test_recovers_from_python_errors(binary: Path, execution: Execution) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    # fmt: python
    python = code("""
        answer = 41


        def fail():
            raise ValueError("boom")


        fail()
        """)
    client.send(python=python)
    output = last_result_text(client)
    assert client.transcript[-1]["result"]["isError"] is False, client.transcript[-1]
    assert output.startswith("Traceback (most recent call last):\n")
    assert "<mcp-console:python:" in output
    assert "in fail\n" in output
    assert 'File "<string>"' not in output
    assert output.endswith("ValueError: boom\n")
    # fmt: python
    python = code("""
        compile_partial = 9
        await missing()
        """)
    client.send(python=python)
    output = last_result_text(client)
    assert not output.startswith("Traceback (most recent call last):\n")
    assert "<mcp-console:python:" in output
    assert 'File "<string>"' not in output
    assert output.endswith("SyntaxError: 'await' outside function\n")
    client.send(python='"compile_partial" in globals()')
    assert last_result_text(client) == "False\n"

    client.send(python="nul_state = 42\0")
    output = last_result_text(client)
    assert client.transcript[-1]["result"]["isError"] is False
    assert output == "SyntaxError: source code string cannot contain null bytes\n"
    client.send(python="1 / 0")
    output = last_result_text(client)
    assert 'File "<mcp-console:python:e5>", line 1, in <module>' in output
    assert 'File "<string>"' not in output
    assert output.endswith("ZeroDivisionError: division by zero\n")
    client.send(python="exec(\"raise RuntimeError('from exec')\")")
    output = last_result_text(client)
    assert 'File "<mcp-console:python:e6>", line 1, in <module>' in output
    assert 'File "<string>", line 1, in <module>' in output
    assert output.endswith("RuntimeError: from exec\n")
    # fmt: python
    python = code("""
        try:
            raise ValueError("cause")
        except ValueError as error:
            raise RuntimeError("chained") from error
        """)
    client.send(python=python)
    output = last_result_text(client)
    assert 'File "<string>"' not in output
    assert "ValueError: cause\n" in output
    assert output.endswith("RuntimeError: chained\n")
    client.send(python="answer")
    assert last_result_text(client) == "41\n"
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_releases_python_threads_before_running_init_hooks(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(binary, execution.serve(), environment)
        release: Path | None = None
        try:
            client.initialize_and_list_tools()
            # fmt: r
            r = code(r"""
                invisible(loadNamespace("reticulate"))
                hook_failed <- FALSE
                setHook(
                  "reticulate.onPyInit",
                  function() {
                    if (!hook_failed) {
                      hook_failed <<- TRUE
                      stop("synthetic Python initialization hook failure")
                    }
                  },
                  action = "prepend"
                )
                message <- tryCatch(
                  {
                    invisible(reticulate::py_config())
                    "Python initialization unexpectedly succeeded"
                  },
                  error = conditionMessage
                )
                cat(message, "\n", sep = "")
                """)
            client.send(r=r)
            output = last_result_text(client)
            assert output == "synthetic Python initialization hook failure\n", repr(
                output
            )

            # fmt: python
            python = code("""
                import os
                import threading
                import time
                from pathlib import Path

                directory = Path(os.environ["TMPDIR"])
                started = directory / "python-init-hook-thread-started"
                release = directory / "release-python-init-hook-thread"
                completed = directory / "python-init-hook-thread-completed"


                def complete_after_release():
                    started.touch()
                    while not release.exists():
                        time.sleep(0.01)
                    completed.touch()


                initialization_thread = threading.Thread(
                    target=complete_after_release,
                    daemon=True,
                )
                initialization_thread.start()
                while not started.exists():
                    time.sleep(0.01)
                """)
            client.send(python=python)
            assert last_result_text(client) == "[done]"

            started = wait_for_worker_file(
                Path(temporary_directory),
                "python-init-hook-thread-started",
                client,
            )
            release = started.parent / "release-python-init-hook-thread"
            release.touch()
            wait_for_worker_file(
                Path(temporary_directory),
                "python-init-hook-thread-completed",
                client,
            )

            client.send(
                python='hook_input = input("hook> "); hook_input',
                stdin="after hook\n",
            )
            assert last_result_text(client) == (
                "[input requested: \"hook> \"]\n'after hook'\n"
            )
            return client.finish()
        finally:
            if release is not None and release.parent.exists():
                release.touch(exist_ok=True)
            stop_client(client)


@executions(DIRECT, SANDBOXED)
def test_routes_python_input(binary: Path, execution: Execution) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()

    # fmt: python
    python = code("""
        name = input("name> ")
        name
        """)
    client.send(python=python)
    assert (
        last_result_text(client) == '[input requested: "name> "]\n[waiting for stdin]'
    )
    wait_for_evaluation_output(
        client,
        "'Ada'\n",
        "Python stdin routing",
        stdin="Ada\n",
        timeout_ms=0,
    )

    # fmt: python
    python = code("""
        color = input("color> ")
        color
        """)
    client.send(python=python, stdin="blue\n")
    assert last_result_text(client) == ("[input requested: \"color> \"]\n'blue'\n")

    # fmt: python
    python = code("""
        import sys

        direct = sys.stdin.readline()
        direct
        """)
    client.send(python=python, stdin="fd 0\n")
    assert last_result_text(client) == "'fd 0\\n'\n"
    return client.finish()


@executions(RUNTIME)
def test_reads_unicode_nul_and_long_python_input(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        # fmt: python
        python = code(r"""
            saved_object = object()
            original_object = saved_object
            first = input("unicode> ")
            second = input("long> ")
            third = input("queued> ")
            assert first == "Zażółć 🐍\0fin"
            assert second == "🐍" * 4097 + "\0tail"
            assert third == "queued"
            print(len(first), len(second), third)
            """)
        # Observe managed input after startup before timing input completion.
        client.send(python=python)
        assert last_result_text(client) == (
            '[input requested: "unicode> "]\n[waiting for stdin]'
        )
        expected = (
            '[input requested: "long> "]\n'
            '[input requested: "queued> "]\n'
            "12 4102 queued\n"
        )
        wait_for_evaluation_output(
            client,
            expected,
            "Unicode, NUL, long and queued Python input",
            stdin="Zażółć 🐍\0fin\n" + "🐍" * 4097 + "\0tail\nqueued\n",
        )
        # fmt: python
        python = code(r"""
            try:
                input("partial> ")
            except KeyboardInterrupt:
                print("input interrupted")
            """)
        client.send(python=python, stdin="🐍" * 1025 + "\0prefix")
        assert last_result_text(client) == (
            '[input requested: "partial> "]\n[waiting for stdin]'
        )
        wait_for_evaluation_output(
            client,
            "input interrupted\n",
            "partial Unicode input interruption",
            control="interrupt",
        )
        # fmt: python
        python = code(r"""
            replayed = input("replay> ")
            assert replayed == "🐍" * 1025 + "\0prefix!"
            print("complete input retained")
            """)
        wait_for_evaluation_output(
            client,
            '[input requested: "replay> "]\ncomplete input retained\n',
            "partial Unicode input replay",
            python=python,
            stdin="!\n",
        )
        client.send(python="saved_object is original_object")
        assert last_result_text(client) == "True\n"
        for entry in client.transcript:
            arguments = entry.get("send", {})
            if "stdin" in arguments:
                arguments["stdin"] = compact_text(arguments["stdin"], "🐍")
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_python_input_eof_retires_worker(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        # Keep the cell alive after EOF to observe the input-completion notice.
        # fmt: r
        r = code(r"""
            eof_gate <- tempfile("python-input-eof-")
            cat(eof_gate)
            """)
        client.send(r=r)
        gate = FifoCheckpoint.create(Path(last_result_text(client)))
        client.transcript[-1]["result"]["content"][0]["text"] = (
            "<Python EOF checkpoint>"
        )
        try:
            # Exercise actual fd-0 EOF without closing the client's MCP transport.
            # fmt: python
            python = code(r"""
                import os

                gate_path = r.eof_gate
                eof_marker = object()
                input("ready for EOF> ")
                reader, writer = os.pipe()
                os.close(writer)
                os.dup2(reader, 0)
                os.close(reader)
                try:
                    input("EOF> ")
                except EOFError:
                    print("Python input reached EOF")
                with open(gate_path, "rb", buffering=0) as gate:
                    assert gate.read(1) == b"1"
                """)
            client.send(python=python)
            assert last_result_text(client) == (
                '[input requested: "ready for EOF> "]\n[waiting for stdin]'
            )
            wait_for_evaluation_output(
                client,
                '[input requested: "EOF> "]\n'
                "Python input reached EOF\n\n[running; poll with an empty send]",
                "Python EOF completes managed input before retirement",
                stdin="\n",
                timeout_ms=0,
            )
            gate.release()
            wait_for_evaluation_output(
                client,
                "[worker sideband read failed: worker sideband closed]\n"
                "[worker exited with status 0]\n"
                "[worker stopped: in-memory state lost]\n"
                "[starting new worker]\n"
                "[idle]",
                "Python input EOF retirement",
                expected_error=True,
                # Confirmed retirement and replacement startup use the client
                # lifecycle budget, separate from the input-arrival deadline.
                completion_timeout_seconds=client.response_timeout,
            )
            client.send(python='"eof_marker" in globals()')
            assert last_result_text(client) == "False\n"
            return client.finish()
        finally:
            gate.close()


@executions(RUNTIME)
def test_python_debugger_input(binary: Path, execution: Execution) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()

    # fmt: python
    python = code("""
        import pdb

        debug_value = 41
        pdb.set_trace()
        debug_value += 1
        """)
    client.send(python=python, stdin="p debug_value\n")
    output = last_result_text(client)
    assert output.count('[input requested: "(Pdb) "]') == 2, output
    assert output.endswith('41\n[input requested: "(Pdb) "]\n[waiting for stdin]'), (
        output
    )

    wait_for_evaluation_output(
        client,
        "[done]",
        "Python debugger input",
        stdin="continue\n",
        timeout_ms=3_000,
    )
    client.send(python="debug_value")
    assert last_result_text(client) == "42\n"
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_restarts_after_python_bridge_failure(
    binary: Path, execution: Execution
) -> Transcript:
    with deferred_selection_client(binary, execution.serve()) as client:
        # fmt: r
        r = code(r"""
            python_worker_marker <- TRUE
            Sys.setenv(RETICULATE_PYTHON = "/mcp-console-missing-python")
            invisible(suppressMessages(base::trace(
              "py_discover_config",
              tracer = quote(base::signalCondition(base::structure(
                base::list(message = "synthetic interrupt", call = NULL),
                class = c("interrupt", "condition")
              ))),
              print = FALSE,
              where = asNamespace("reticulate")
            )))
            """)
        client.send(r=r)
        client.send(python="6 * 7")
        result = client.transcript[-1]["result"]
        assert result["isError"] is True
        bridge_failure = "Python bridge failed during R evaluation\n"
        python_failure = (
            "Error in py_discover_config(required_module, use_environment) : \n"
            "  Python specified in RETICULATE_PYTHON "
            "(/mcp-console-missing-python) does not exist\n"
        )
        worker_failure = (
            "[worker sideband read failed: worker sideband closed]\n"
            "[worker exited with status 1]\n"
            "[worker stopped: in-memory state lost]\n"
            "[starting new worker]\n"
            "[idle]"
        )
        output = result["content"][0]["text"]
        assert output.endswith(worker_failure), output
        assert_exact_interleaving(
            output.removesuffix(worker_failure),
            bridge_failure,
            python_failure,
        )
        result["content"][0]["text"] = bridge_failure + python_failure + worker_failure
        client.send(r='exists("python_worker_marker", inherits = FALSE)')
        assert last_result_text(client) == "[1] FALSE\n"
        client.send(python="6 * 7")
        assert last_result_text(client) == "42\n"
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
