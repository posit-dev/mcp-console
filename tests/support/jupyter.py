"""Published Jupyter kernels selected explicitly by the test host."""

import json
import os
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from support.client import McpClient
from support.execution import DIRECT, Execution
from support.records import Transcript


def normalize_jupyter_paths(transcript: Transcript) -> Transcript:
    """Normalize the selected interpreter's location after full output assertions."""
    python = os.environ["MCP_CONSOLE_TEST_JUPYTER_PYTHON"]
    locations = json.loads(
        subprocess.check_output(
            [
                python,
                "-c",
                "import json, sysconfig; print(json.dumps({name: sysconfig.get_path(name) for name in ('stdlib', 'purelib')}))",
            ],
            text=True,
        )
    )
    assert all(
        Path(path).is_absolute() and Path(path).is_dir() for path in locations.values()
    ), locations
    for entry in transcript:
        for block in entry.get("result", {}).get("content", []):
            if block["type"] == "text":
                for name in ("purelib", "stdlib"):
                    label = "packages" if name == "purelib" else "stdlib"
                    paths = {locations[name], str(Path(locations[name]).resolve())}
                    paths.update(
                        "~/" + str(Path(path).relative_to(Path.home()))
                        for path in tuple(paths)
                        if Path(path).is_relative_to(Path.home())
                    )
                    for path in sorted(paths, key=len, reverse=True):
                        block["text"] = block["text"].replace(
                            path + os.sep, f"<python-{label}>/"
                        )
    return transcript


@contextmanager
def jupyter_client(
    binary: Path,
    kernel: str = "ipykernel",
    *,
    transport: str | None = None,
    execution: Execution = DIRECT,
    startup_gate: Path | None = None,
    kernel_fixture: str | None = None,
) -> Iterator[tuple[McpClient, Path]]:
    assert kernel in {"ipykernel", "ark"}
    assert startup_gate is None or kernel == "ipykernel"
    assert kernel_fixture is None or (kernel == "ipykernel" and startup_gate is None)
    transport = transport or ("ipc" if kernel == "ipykernel" else "tcp")
    assert transport in {"ipc", "tcp"}
    python = Path(os.environ["MCP_CONSOLE_TEST_JUPYTER_PYTHON"])
    assert python.is_absolute() and python.is_file(), python
    with tempfile.TemporaryDirectory(prefix="console-jupyter-") as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        name = f"console-{kernel}"
        specification = root / "jupyter" / "kernels" / name
        specification.mkdir(parents=True)
        environment = dict(os.environ)
        environment.update(
            JUPYTER_PATH=str(root / "jupyter"),
            JUPYTER_CONFIG_DIR=str(root / "jupyter-config"),
            IPYTHONDIR=str(root / "ipython"),
            JUPYTER_RUNTIME_DIR=str(root / "jupyter-runtime"),
        )
        if kernel == "ipykernel":
            # Match the published installer's default: permit debugging stdlib
            # modules instead of triggering debugpy's frozen-module warning.
            argv = [
                str(python),
                "-Xfrozen_modules=off",
                "-m",
                "ipykernel_launcher",
                "-f",
                "{connection_file}",
            ]
            if startup_gate is not None:
                launcher = (
                    Path(__file__).resolve().parents[1] / "fixtures/jupyter_startup.py"
                )
                argv = [
                    str(python),
                    "-Xfrozen_modules=off",
                    str(launcher),
                    "-f",
                    "{connection_file}",
                ]
                environment["MCP_CONSOLE_TEST_JUPYTER_STARTUP_GATE"] = str(startup_gate)
            if kernel_fixture is not None:
                launcher = (
                    Path(__file__).resolve().parents[1] / "fixtures" / kernel_fixture
                )
                assert launcher.is_file(), launcher
                argv = [
                    str(python),
                    "-Xfrozen_modules=off",
                    str(launcher),
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
            environment["RUST_LOG"] = "error"
        (specification / "kernel.json").write_text(
            json.dumps({"argv": argv, "display_name": name, "language": language}),
            encoding="utf-8",
        )
        with McpClient(
            binary,
            execution.serve(
                "--no-config",
                "--jupyter-kernel",
                name,
                "--jupyter-python",
                str(python),
                "--jupyter-transport",
                transport,
                *(
                    (
                        "--writable-root",
                        str(root),
                        "-c",
                        "sandbox.network={proxy: {mode: limited}, sockets: {unix_sockets: dangerously_allow_all}, allow_local_binding: true}",
                    )
                    if execution.name == "sandbox"
                    else ()
                ),
            ),
            environment,
            current_directory=workspace,
        ) as client:
            client.transcript.append(
                {
                    "scenario": f"A published {kernel} kernel executes persistent cells.",
                    "fixture": (
                        "An isolated kernelspec selects the test host's installed release "
                        f"using {transport.upper()} transport."
                    ),
                }
            )
            client.initialize_and_list_tools()
            yield client, workspace
