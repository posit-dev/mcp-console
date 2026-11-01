"""Public Python clients retain bundle discovery with requirement inspection."""

import asyncio
import inspect
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "python"))

from mcp_console import AsyncMCPConsole, MCPConsole
from boundaries.client_server.integrations.test_control import ControlClient
from boundaries.client_server.python.test_without_r import (
    environment as no_r_environment,
)
from support.bundles import normalize_bundle_locations, split_bundle_notice
from support.execution import DIRECT
from support.records import Transcript
from support.requirements import POSIX, requires
from support.suites import run_this_suite


@requires(POSIX)
def test_requirements_preserve_bundle_discovery(binary: Path) -> Transcript:
    async def exercise(console, endpoint: Path) -> dict:
        with ControlClient(endpoint) as control:
            result = control.request(
                op="evaluate",
                language="python",
                code="print('recorded through control')",
                busy="wait",
                wait_ms=30000,
            )
            assert result["result"]["execution_error"] is None, result

        outputs = []
        for _ in range(2):
            output = console.send(requirements={"action": "get"})
            outputs.append(await output if inspect.isawaitable(output) else output)
        notice, requirements = split_bundle_notice(outputs[0])
        assert notice, outputs[0]
        assert requirements == outputs[1], outputs
        declaration = json.loads(requirements)
        assert "requirements" in declaration, declaration
        assert requirements == json.dumps(declaration, indent=2), requirements
        bundle = Path(json.loads(notice.splitlines()[0].split(": ", 1)[1]))
        assert "recorded through control" in (bundle / "session.qmd").read_text()
        return {
            "client": type(console).__name__,
            "bundle_notice": normalize_bundle_locations(notice),
            "requirements": declaration,
            "next_inspection": "identical requirement JSON without another notice",
        }

    async def run() -> Transcript:
        transcript = []
        with tempfile.TemporaryDirectory(
            dir="/tmp", prefix="console-sdk-"
        ) as directory:
            for client_type in (AsyncMCPConsole, MCPConsole):
                workspace = Path(directory) / client_type.__name__
                workspace.mkdir(mode=0o700)
                (workspace / ".agents/console").mkdir(parents=True)
                endpoint = workspace / "control.sock"
                console = client_type(
                    command=binary,
                    args=DIRECT.serve(
                        "--control-socket",
                        str(endpoint),
                        "--no-config",
                        "-c",
                        "languages=[python]",
                        "-c",
                        "python=" + json.dumps(sys.executable),
                    ),
                    server_parameters={
                        "cwd": str(workspace),
                        "env": no_r_environment(workspace)
                        | {"MCP_CONSOLE_HOME": str(workspace)},
                    },
                )
                if client_type is AsyncMCPConsole:
                    async with console:
                        transcript.append(await exercise(console, endpoint))
                else:
                    with console:
                        transcript.append(await exercise(console, endpoint))
        return transcript

    return asyncio.run(run())


if __name__ == "__main__":
    run_this_suite(__file__)
