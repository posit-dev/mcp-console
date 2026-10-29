"""Stopped named sessions prepare requirements before their next worker starts."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.server.test_no_r import no_r_environment
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.python import write_test_wheel
from support.records import Transcript
from support.requirements import command, requires
from support.suites import run_this_suite


@requires(command("uv"))
@executions(DIRECT, SANDBOXED)
def test_stopped_named_session_prepares_its_next_environment(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        base = "mcp_console_test_session_base"
        combined = "mcp_console_test_session_combined"
        standalone = "mcp_console_test_session_standalone"
        index = write_test_wheel(root, base, "answer = 41\n")
        write_test_wheel(root, base, "answer = 44\n", version="2.0.0")
        write_test_wheel(root, combined, "answer = 42\n", requires=(f"{base}==1.0.0",))
        write_test_wheel(
            root, standalone, "answer = 43\n", requires=(f"{base}==1.0.0",)
        )
        environment = no_r_environment(root)
        environment.update(UV_INDEX=index.as_uri(), UV_INDEX_STRATEGY="first-index")
        with McpClient(
            binary,
            execution.serve("-c", f"python.managed.packages=[{base}==1.0.0]"),
            environment,
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(python="retained = 'default state'")
            client.expect(
                "41\n", session="alpha", python=f"import {base}; {base}.answer"
            )
            client.expect(
                "[worker stopped: in-memory state lost]\n[stopped]",
                session="alpha",
                control="restart",
            )
            client.expect(
                "[starting new worker]\n42\n",
                session="alpha",
                requirements={"python": [combined]},
                python=f"import {combined}; {combined}.answer",
            )
            client.expect(
                "[worker stopped: in-memory state lost]\n[stopped]",
                session="alpha",
                control="restart",
            )
            client.expect(
                "[prepared]",
                session="alpha",
                requirements={"python": [standalone]},
            )
            client.expect("\n[stopped]", session="alpha")
            assert client.send(timeout_ms=0)["structuredContent"] == {
                "sessions": [{"session": "default", "status": "idle"}]
            }
            client.expect(
                "[starting new worker]\n43\n",
                session="alpha",
                python=f"import {standalone}; {standalone}.answer",
            )
            client.expect(
                "[worker stopped: in-memory state lost]\n[stopped]",
                session="alpha",
                control="restart",
            )
            client.expect(
                "[starting new worker]\n44\n",
                session="alpha",
                requirements={"action": "set", "python": [f"{base}==2.0.0"]},
                # fmt: python
                python=code(f"""
                    import importlib.util
                    import {base}

                    assert importlib.util.find_spec({combined!r}) is None
                    assert importlib.util.find_spec({standalone!r}) is None
                    print({base}.answer)
                    """),
            )
            client.expect(
                "[worker stopped: in-memory state lost]\n[stopped]",
                session="alpha",
                control="restart",
            )
            client.expect(
                "[prepared]",
                session="alpha",
                requirements={"action": "reset"},
            )
            client.expect("\n[stopped]", session="alpha")
            client.expect(
                "[starting new worker]\n41\n",
                session="alpha",
                python=f"import {base}; {base}.answer",
            )
            client.expect("'default state'\n", python="retained")
            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
