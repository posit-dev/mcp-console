#!/usr/bin/env -S uv run --script

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient, stop_client
from support.execution import SANDBOXED
from support.normalization import code
from support.records import Transcript
from support.requirements import SANDBOX, requires
from support.suites import run_this_suite


@requires(SANDBOX)
def test_serve_selects_its_policy_without_ambient_overrides(binary: Path) -> Transcript:
    client = McpClient(
        binary,
        SANDBOXED.serve(),
        {**os.environ, "MCP_CONSOLE_SANDBOX_CONFIG": "malformed ambient policy"},
    )
    try:
        client.initialize_and_list_tools()
        client.send(
            # fmt: python
            python=code(r"""
                import os
                from pathlib import Path

                assert "MCP_CONSOLE_SANDBOX_CONFIG" not in os.environ
                temporary = Path(os.environ["TMPDIR"])
                _ = (temporary / "configured-by-serve").write_text("allowed", encoding="utf-8")
                print("serve supplied its own policy")
                """)
        )
        assert last_tool_text(client) == "serve supplied its own policy\n", (
            last_tool_text(client)
        )
        return client.transcript
    finally:
        stop_client(client)


if __name__ == "__main__":
    run_this_suite(__file__)
