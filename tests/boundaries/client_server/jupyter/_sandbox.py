#!/usr/bin/env -S uv run --script

import base64
import errno
import json
import os
import socket
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import assert_result_content, last_tool_text
from support.client import McpClient
from support.execution import SANDBOXED
from support.jupyter import normalize_jupyter_paths
from support.normalization import code, normalize_recording
from support.records import Transcript


def default_sandbox_session(
    binary: Path,
    kernel: str,
    *,
    network_fixture: str,
    network_output: str,
    startup_output: str = "",
    shutdown_output: str = "",
    python_arguments: tuple[str, ...] = ("-Xfrozen_modules=off",),
    reticulate_ipykernel: bool = False,
) -> Transcript:
    assert not reticulate_ipykernel or kernel == "ark"
    python = Path(os.environ["MCP_CONSOLE_TEST_JUPYTER_PYTHON"])
    assert python.is_absolute() and python.is_file(), python
    probe = Path(__file__).resolve().parents[3] / "fixtures" / network_fixture
    assert probe.is_file(), probe
    with (
        tempfile.TemporaryDirectory(prefix="console-private-ipc-") as directory,
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp,
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as unix,
    ):
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        sentinel = root / "host-sentinel"
        sentinel.write_text("original\n", encoding="utf-8")
        tcp.bind(("127.0.0.1", 0))
        tcp.listen()
        unix_path = root / "host.sock"
        unix.bind(str(unix_path))
        unix.listen()
        for listener in (tcp, unix):
            # An outside-sandbox connection proves the endpoint is listening;
            # denial inside the kernel must not come from an absent host listener.
            with socket.socket(listener.family, socket.SOCK_STREAM) as baseline:
                baseline.settimeout(1)
                baseline.connect(listener.getsockname())
                accepted, _ = listener.accept()
                accepted.close()
            listener.setblocking(False)

        name = f"console-{kernel}"
        specification = root / "jupyter" / "kernels" / name
        specification.mkdir(parents=True)
        if kernel == "ipykernel":
            argv = [
                str(python),
                *python_arguments,
                "-m",
                "ipykernel_launcher",
                "-f",
                "{connection_file}",
            ]
            language = "python"
        else:
            ark = Path(os.environ["MCP_CONSOLE_TEST_ARK"])
            assert ark.is_absolute() and ark.is_file(), ark
            argv = [
                str(ark),
                "--connection_file",
                "{connection_file}",
                "--session-mode",
                "notebook",
                "--",
                "--vanilla",
            ]
            language = "R"
        (specification / "kernel.json").write_text(
            json.dumps({"argv": argv, "display_name": name, "language": language}),
            encoding="utf-8",
        )
        environment = dict(os.environ)
        for variable in (
            "IPYTHONDIR",
            "JUPYTER_RUNTIME_DIR",
            "JUPYTER_CONFIG_DIR",
            "MPLCONFIGDIR",
        ):
            environment.pop(variable, None)
        environment.update(
            JUPYTER_PATH=str(root / "jupyter"),
            RUST_LOG="error",
            MCP_CONSOLE_TEST_JUPYTER_PYTHON=str(python),
            MCP_CONSOLE_TEST_JUPYTER_NETWORK_PROBE=str(probe),
            MCP_CONSOLE_TEST_JUPYTER_HOST_FILE=str(sentinel),
            MCP_CONSOLE_TEST_JUPYTER_FILE_ERRNO=str(
                errno.EPERM if sys.platform == "darwin" else errno.EROFS
            ),
            MCP_CONSOLE_TEST_JUPYTER_ENDPOINTS=json.dumps(
                {"tcp_port": tcp.getsockname()[1], "unix_path": str(unix_path)}
            ),
        )
        if reticulate_ipykernel:
            library = Path(os.environ["MCP_CONSOLE_TEST_RETICULATE_LIBRARY"])
            assert library.is_absolute() and library.is_dir(), library
            environment.update(
                R_LIBS_USER=str(library),
                RETICULATE_PYTHON=str(python),
                _RETICULATE_POSITRON_DISABLE_VARIABLE_INSPECTORS_="1",
            )
        with McpClient(
            binary,
            SANDBOXED.serve(
                "--no-config",
                "--jupyter-kernel",
                name,
                "--jupyter-python",
                str(python),
                *(
                    ("--jupyter-reticulate", "--jupyter-reticulate-ipykernel")
                    if reticulate_ipykernel
                    else ()
                ),
            ),
            environment,
            current_directory=workspace,
        ) as client:
            client.transcript.append(
                {
                    "scenario": f"The published {kernel + '+ipykernel' if reticulate_ipykernel else kernel} kernel works in the default read-only sandbox.",
                    "fixture": "No transport or sandbox permissions are configured. Host TCP and Unix listeners accept baseline connections outside the sandbox; a host file contains a protected sentinel.",
                }
            )
            client.initialize_and_list_tools()
            for generation in range(2):
                if generation:
                    result = client.send(control="restart")
                    assert not result.get("isError"), result
                    output = last_tool_text(client)
                    assert output == (
                        shutdown_output
                        + "[worker stopped: in-memory state lost]\n[starting new worker]\n"
                        + startup_output
                        + "[idle]"
                    ), output
                    client.expect(
                        "False\n" if kernel == "ipykernel" else "[1] FALSE\n",
                        code='"answer" in globals()'
                        if kernel == "ipykernel"
                        else 'exists("answer", inherits = FALSE)',
                    )
                client.expect(
                    (startup_output if generation == 0 else "")
                    + ("42\n" if kernel == "ipykernel" else "[1] 42\n"),
                    code="answer = 42\nprint(answer)",
                )
                if reticulate_ipykernel:
                    client.expect("[done]", r="r_pid <- Sys.getpid()")
                    client.expect(
                        "same process: True\n42\n",
                        # fmt: python
                        python=code("""
                            import os
                            from pathlib import Path

                            print("same process:", os.getpid() == r.r_pid)
                            from_python = int(r.answer)
                            from_python
                            """),
                    )
                    client.expect(
                        "[1] 42\n",
                        r="stopifnot(reticulate::py$from_python == answer); print(reticulate::py$from_python)",
                    )
                    encoded = (
                        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
                        "/x8AAwMCAO+aSuoAAAAASUVORK5CYII="
                    )
                    client.send(
                        # fmt: python
                        python=code(f"""
                            import base64
                            from IPython.display import Image, display

                            display(Image(data=base64.b64decode("{encoded}")))
                            """),
                    )
                    assert_result_content(
                        client,
                        [base64.b64decode(encoded)],
                        image_reference="the submitted one-pixel PNG",
                    )
                if kernel == "ipykernel":
                    client.send(
                        # fmt: python
                        code=code("""
                            import os
                            import tempfile
                            from pathlib import Path

                            private = Path(tempfile.gettempdir()) / "kernel-permission-probe"
                            private.write_text("private")
                            assert private.read_text() == "private"
                            private.unlink()
                            print("private temp: writable")
                            host = Path(os.environ["MCP_CONSOLE_TEST_JUPYTER_HOST_FILE"])
                            assert host.read_text() == "original\\n"
                            try:
                                host.write_text("changed")
                            except OSError as error:
                                assert error.errno == int(os.environ["MCP_CONSOLE_TEST_JUPYTER_FILE_ERRNO"]), error
                                print("host file:", error)
                            else:
                                raise AssertionError("host file unexpectedly writable")
                            """),
                    )
                else:
                    client.send(
                        # fmt: r
                        code=code("""
                            private <- tempfile("kernel-permission-probe-")
                            writeLines("private", private)
                            stopifnot(identical(readLines(private), "private"))
                            unlink(private)
                            cat("private temp: writable\\n")
                            host <- Sys.getenv("MCP_CONSOLE_TEST_JUPYTER_HOST_FILE")
                            stopifnot(identical(readLines(host), "original"))
                            failure <- tryCatch(
                              withCallingHandlers(
                                writeLines("changed", host),
                                warning = function(w) {
                                  cat("host file warning: ", conditionMessage(w), "\\n", sep = "")
                                  invokeRestart("muffleWarning")
                                }
                              ),
                              error = identity
                            )
                            stopifnot(inherits(failure, "error"))
                            cat("host file error: ", conditionMessage(failure), "\\n", sep = "")
                            """),
                    )
                output = last_tool_text(client)
                assert output.startswith("private temp: writable\n"), output
                assert str(sentinel) in output, output
                assert sentinel.read_text(encoding="utf-8") == "original\n"
                if kernel == "ipykernel":
                    client.send(
                        # fmt: python
                        code=code("""
                            exec(Path(os.environ["MCP_CONSOLE_TEST_JUPYTER_NETWORK_PROBE"]).read_text())
                            """),
                    )
                else:
                    client.send(
                        # fmt: r
                        code=code("""
                            status <- system2(
                              Sys.getenv("MCP_CONSOLE_TEST_JUPYTER_PYTHON"),
                              shQuote(Sys.getenv("MCP_CONSOLE_TEST_JUPYTER_NETWORK_PROBE"))
                            )
                            stopifnot(status == 0L)
                            """),
                    )
                output = last_tool_text(client)
                assert output == network_output, output
                if reticulate_ipykernel:
                    client.expect(
                        network_output,
                        # fmt: python
                        python=code("""
                            exec(Path(os.environ["MCP_CONSOLE_TEST_JUPYTER_NETWORK_PROBE"]).read_text())
                            """),
                    )
                for listener in (tcp, unix):
                    try:
                        accepted, _ = listener.accept()
                    except BlockingIOError:
                        pass
                    else:
                        accepted.close()
                        raise AssertionError(
                            "host listener accepted a kernel connection"
                        )
            return normalize_jupyter_paths(normalize_recording(client.finish(), root))
