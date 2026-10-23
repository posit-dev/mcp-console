# Use Console from Python

Use `mcp_console` to call Console from Python or add it to a model framework.
This is the client package, not the Python environment in which the agent's analysis runs.
It requires Python 3.11 or newer.

The [documentation](README.md) covers the shared workbench: sessions, packages, sandbox permissions, and recordings.

## Call Console directly

Install the client extra in your application's environment:

```sh
pip install 'mcp-console[client]'
```

```python
from mcp_console import MCPConsole

with MCPConsole() as console:
    print(console.send(python="values = [2, 4, 6]; values"))
    print(console.send(python="sum(values)"))
```

Keep the connection open to retain state.
Closing it ends the session; reconnecting starts a new one.

Use [AsyncMCPConsole](https://t-kalinowski.github.io/mcp-console/python/reference/asyncmcpconsole/) with `async with` and `await` for asynchronous code.
Enter and close an async connection in the same task.
The [API reference](https://t-kalinowski.github.io/mcp-console/python/reference/) documents launch commands, working directories, environment options, and exact signatures.

## Choose a model integration

Install the extra for your framework.
Each reference page provides a complete example, including setup and cleanup.

| Framework                    | Extra           | Existing Console connection                                                                          | Framework-managed connection                                                                                                               |
| ---------------------------- | --------------- | ---------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| chatlas                      | `chatlas`       | [tool](https://t-kalinowski.github.io/mcp-console/python/reference/chatlas.tool/)                    | [register](https://t-kalinowski.github.io/mcp-console/python/reference/chatlas.register/)                                                  |
| `openai` — Responses API     | `openai`        | [responses_tool](https://t-kalinowski.github.io/mcp-console/python/reference/openai.responses_tool/) | Application-owned loop.                                                                                                                    |
| `openai-agents` — Agents SDK | `openai-agents` | [agents_tool](https://t-kalinowski.github.io/mcp-console/python/reference/openai.agents_tool/)       | [agents_server](https://t-kalinowski.github.io/mcp-console/python/reference/openai.agents_server/)                                         |
| `openai-codex` — Codex SDK   | `codex`         | —                                                                                                    | [server](https://t-kalinowski.github.io/mcp-console/python/reference/codex.server/) supplies configuration; it does not open a connection. |
| Anthropic                    | `anthropic`     | [tool](https://t-kalinowski.github.io/mcp-console/python/reference/anthropic.tool/)                  | [tools](https://t-kalinowski.github.io/mcp-console/python/reference/anthropic.tools/)                                                      |

Your application still owns its model client, chat or agent, and any function-calling loop.
Creating a Console tool does not choose a model.

Use the supplied adapters so the model receives the connected server's actual schema.
Inferring a schema from `console.send` annotations loses configured fields.
Recreate adapters after reconnecting.
For chatlas, use `set_tools()` on a new chat to retain the adapter's explicit schema.

## Work with the session

The shared guides explain [sessions and languages](BUILTIN_RUNTIME.md), [packages and environments](REQUIREMENTS.md), [sandbox permissions](SANDBOX.md), and [recordings and reports](RECORDING.md).
These describe the same workbench your agent uses from Python.

<span id="results-and-lifetime"></span> <span id="interrupt-a-wait-or-stop-execution"></span>

[Drive send directly](SEND_OPERATIONS.md) walks through collecting unfinished results, answering input, interrupting work, and restarting.
The [MCPConsole reference](https://t-kalinowski.github.io/mcp-console/python/reference/mcpconsole/) documents result handling and connection cleanup, including `KeyboardInterrupt`.

<span id="inspect-or-replace-requirements"></span>

For exact dependency fields and replacement rules, see [Requirements](https://t-kalinowski.github.io/mcp-console/python/reference/requirements/) and the [send reference](API.md#requirements).
To prepare data and customize what the model sees, follow [Build a data assistant](BUILD_ON_CONSOLE.md).
