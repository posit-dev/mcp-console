"""MCP Console configuration for the Codex SDK (openai-codex)."""

from collections.abc import Mapping, Sequence
from typing import Any

from ._common import Command, stdio_command


def server(
    *,
    command: Command | None = None,
    args: Sequence[Command] | None = None,
    server_parameters: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Configure Console as a stdio MCP server for the Codex SDK (openai-codex).

    Put this entry inside the thread's `mcp_servers` configuration. The SDK starts
    Console and routes tool calls to it; this function only builds configuration.
    Reuse the thread for follow-up requests so its Console session retains state.

    Args:
        command: Console executable. Defaults to the installed `mcp-console`.
        args: Server arguments. Defaults to `["serve"]`.
        server_parameters: Additional stdio configuration, such as `cwd` or `env`.

    Returns:
        One server entry for `config["mcp_servers"]`.

    Examples:
        Install with `pip install 'mcp-console[codex]'` and authenticate Codex
        before running this script. It starts a thread with Console
        available as a tool and prints the final response:

        ```python
        import os
        import mcp_console
        from openai_codex import Codex

        config = {
            "mcp_servers": {
                "mcp-console": mcp_console.codex.server(
                    server_parameters={"cwd": os.getcwd()},
                ),
            },
        }
        with Codex() as client:
            thread = client.thread_start(config=config)
            result = thread.run(
                "Use Python in Console to calculate the mean and sample "
                "standard deviation of [12, 15, 18, 20, 25]."
            )
            print(result.final_response)
        ```
    """
    command, args = stdio_command(command, args)
    return dict(server_parameters or {}) | {"command": command, "args": args}
