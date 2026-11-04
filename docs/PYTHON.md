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
Closing it ends all its sessions; reconnecting starts with a fresh default session.

Use [AsyncMCPConsole](https://posit-dev.github.io/mcp-console/python/reference/asyncmcpconsole/) with `async with` and `await` for asynchronous code.
Enter and close an async connection in the same task.
The [API reference](https://posit-dev.github.io/mcp-console/python/reference/) documents launch commands, working directories, environment options, and exact signatures.

## Choose a model integration

Install the extra for your framework.
The API reference includes examples with setup and cleanup.

| Framework                    | Extra           | Existing Console connection                                                                          | Framework-managed connection                                                                                                            |
| ---------------------------- | --------------- | ---------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| chatlas                      | `chatlas`       | [tools](https://posit-dev.github.io/mcp-console/python/reference/chatlas.tools/)                     | [register](https://posit-dev.github.io/mcp-console/python/reference/chatlas.register/)                                                  |
| `openai` — Responses API     | `openai`        | [responses_tools](https://posit-dev.github.io/mcp-console/python/reference/openai.responses_tools/)  | Application-owned loop.                                                                                                                 |
| `openai-agents` — Agents SDK | `openai-agents` | [agents_tools](https://posit-dev.github.io/mcp-console/python/reference/openai.agents_tools/)        | [agents_server](https://posit-dev.github.io/mcp-console/python/reference/openai.agents_server/)                                         |
| `openai-codex` — Codex SDK   | `codex`         | —                                                                                                    | [server](https://posit-dev.github.io/mcp-console/python/reference/codex.server/) supplies configuration; it does not open a connection. |
| Anthropic                    | `anthropic`     | [callable_tools](https://posit-dev.github.io/mcp-console/python/reference/anthropic.callable_tools/) | [tools](https://posit-dev.github.io/mcp-console/python/reference/anthropic.tools/)                                                      |

Your application still owns its model client, chat or agent, and any function-calling loop.
Registering Console tools does not choose a model.

The callable adapters preserve images in successful results using each framework's native content types, with Console's original image bytes, MIME types, and text/image order.
Text-only results remain strings.
MCP tool errors raise: Anthropic preserves image error content in its `ToolError`, while the other callable adapters raise text exceptions.
For OpenAI Agents, use `agents_server()` to retain image content in MCP error results.

Use the plural adapters so the model receives every discovered tool and its actual schema, including tools added or renamed by future servers.
Inferring a schema from `console.send` annotations loses configured fields.
Recreate adapters after reconnecting.
For chatlas, use `chat.set_tools(mcp_console.chatlas.tools(console))` on a new chat.
For a Responses loop, route function calls by name:

```python
tools = mcp_console.openai.responses_tools(console)
definitions = [tool.definition for tool in tools]
by_name = {tool.definition["name"]: tool for tool in tools}
# Pass definitions to responses.create(tools=definitions).
# For each returned function call: by_name[call.name](call).
```

The clients expose all definitions through `console.tools` and preserve complete MCP results through `console.call_tool(name, arguments)`.
Discovery follows pagination and does not require a tool named `send`.
Use `console.get_tool(name)` to inspect a particular definition, or `send()` to call the server's `send` tool directly.
Tool definitions are captured when the connection opens.

## Work with the session

The shared guides explain [sessions and languages](BUILTIN_RUNTIME.md), [packages and environments](REQUIREMENTS.md), [sandbox permissions](SANDBOX.md), and [recordings and reports](RECORDING.md).
These describe the same workbench your agent uses from Python.

<span id="results-and-lifetime"></span> <span id="interrupt-a-wait-or-stop-execution"></span>

[Drive send directly](SEND_OPERATIONS.md) walks through collecting unfinished results, answering input, interrupting work, and restarting.
The [MCPConsole reference](https://posit-dev.github.io/mcp-console/python/reference/mcpconsole/) documents result handling and connection cleanup, including `KeyboardInterrupt`.

<span id="inspect-or-replace-requirements"></span>

For exact dependency fields and replacement rules, see [Requirements](https://posit-dev.github.io/mcp-console/python/reference/requirements/) and the [send reference](API.md#requirements).
To prepare data and customize what the model sees, follow [Build a data assistant](BUILD_ON_CONSOLE.md).
