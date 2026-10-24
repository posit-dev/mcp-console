#!/usr/bin/env -S uv run --script

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient
from support.execution import SANDBOXED
from support.jupyter import normalize_jupyter_paths
from support.normalization import code, normalize_recording
from support.records import Transcript
from support.requirements import JUPYTER, MACOS_SANDBOX, requires
from support.suites import run_this_suite


@requires(JUPYTER, MACOS_SANDBOX)
def test_unrestricted_filesystem_preserves_private_ipc(binary: Path) -> Transcript:
    python = Path(os.environ["MCP_CONSOLE_TEST_JUPYTER_PYTHON"])
    iostream = subprocess.check_output(
        [
            str(python),
            "-c",
            "import ipykernel.iostream; print(ipykernel.iostream.__file__)",
        ],
        text=True,
    ).strip()
    assert Path(iostream).is_absolute() and Path(iostream).is_file(), iostream
    with tempfile.TemporaryDirectory(prefix="console-inherited-ipc-") as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        host = root / "host-file"
        specification = root / "jupyter" / "kernels" / "console-python"
        specification.mkdir(parents=True)
        (specification / "kernel.json").write_text(
            json.dumps(
                {
                    "argv": [
                        str(python),
                        "-Xfrozen_modules=off",
                        "-m",
                        "ipykernel_launcher",
                        "-f",
                        "{connection_file}",
                    ],
                    "display_name": "console-python",
                    "language": "python",
                }
            )
        )
        environment = dict(
            os.environ,
            JUPYTER_PATH=str(root / "jupyter"),
            MCP_CONSOLE_TEST_HOST_FILE=str(host),
        )
        with McpClient(
            binary,
            SANDBOXED.serve(
                "--no-config",
                "--jupyter-kernel",
                "console-python",
                "--jupyter-python",
                str(python),
                "-c",
                "sandbox.profile=inherit_codex",
            ),
            environment,
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            client.request(
                "tools/call",
                name="send",
                arguments={
                    "timeout_ms": 60000,
                    # fmt: python
                    "code": code("""
                        import os
                        from pathlib import Path

                        host = Path(os.environ["MCP_CONSOLE_TEST_HOST_FILE"])
                        _ = host.write_text("inherited write access")
                        print(host.read_text())
                        """),
                },
                _meta={
                    "codex/sandbox-state": {
                        "permissionProfile": {
                            "type": "managed",
                            "file_system": {"type": "unrestricted"},
                            "network": "restricted",
                        }
                    }
                },
            )
            assert last_tool_text(client) == (
                f"{iostream}:73: UserWarning: Couldn't bind IOPub Pipe to 127.0.0.1: "
                "Operation not permitted (addr='tcp://127.0.0.1:*')\n"
                "subprocess output will be unavailable.\n"
                "  self._setup_pipe_in()\n"
                "inherited write access\n"
            ), last_tool_text(client)
            assert host.read_text() == "inherited write access"
            transcript = normalize_recording(client.finish(), root)
    return normalize_jupyter_paths(transcript)


if __name__ == "__main__":
    run_this_suite(__file__)
