# Python clients and adapters

Use `mcp_console` to connect a Python application to Console or add it to a model framework.
This is the client package, not the Python environment in which the agent's analysis runs.
It requires Python 3.11 or newer.

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

Use `AsyncMCPConsole` with `async with` and `await` for asynchronous code.
Enter and close an async connection in the same task.
The [API reference](https://t-kalinowski.github.io/mcp-console/python/reference/index.html) documents launch commands, working directories, environment options, and exact signatures.

The clients prefer Console in the current Python environment, then PATH, and launch `serve`.
Use `command` and `args` to select another executable.

## Choose a model integration

Install the extra for your framework.
Each reference page provides a complete example, including setup and cleanup.

| Framework                    | Extra           | Integration                                                                                                                                                                                                                             |
| ---------------------------- | --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| chatlas                      | `chatlas`       | [tool](https://t-kalinowski.github.io/mcp-console/python/reference/chatlas.tool.html) for an open connection, or [register](https://t-kalinowski.github.io/mcp-console/python/reference/chatlas.register.html) for native registration. |
| `openai` — Responses API     | `openai`        | [responses_tool](https://t-kalinowski.github.io/mcp-console/python/reference/openai.responses_tool.html); the application owns the tool loop.                                                                                           |
| `openai-agents` — Agents SDK | `openai-agents` | [agents_tool](https://t-kalinowski.github.io/mcp-console/python/reference/openai.agents_tool.html) or [agents_server](https://t-kalinowski.github.io/mcp-console/python/reference/openai.agents_server.html).                           |
| `openai-codex` — Codex SDK   | `codex`         | [server](https://t-kalinowski.github.io/mcp-console/python/reference/codex.server.html) supplies configuration; it does not open a connection.                                                                                          |
| Anthropic                    | `anthropic`     | [tool](https://t-kalinowski.github.io/mcp-console/python/reference/anthropic.tool.html) or [tools](https://t-kalinowski.github.io/mcp-console/python/reference/anthropic.tools.html).                                                   |

Your application still owns its model client, chat or agent, and any function-calling loop.
Creating a Console tool does not choose a model.

Use the supplied adapters so the model receives the connected server's actual schema.
Inferring a schema from `console.send` annotations loses configured fields.
Recreate adapters after reconnecting.
For chatlas, use `set_tools()` on a new chat to retain the adapter's explicit schema.

## Results and lifetime

Ordinary `send()` calls return text, represent images with MIME placeholders, and raise `RuntimeError` for MCP tool errors.
Text-tool adapters have the same image limitation; Responses and native MCP integrations preserve images.
`console(...)` is an alias for `send(...)`.

A call can stop waiting while its code continues.
After a running notice, poll without code:

```python
result = console.send(timeout_ms=300000)
```

Do not resubmit the original cell.
Omitting `timeout_ms`, or passing `None`, retains the server's default 60,000 ms wait.
If [tool configuration](TOOL_CONFIGURATION.md) hides that argument, use `console.send()` instead.

Use [Calls, input, and control](SEND_OPERATIONS.md) for sequencing, input, interruption, and restart, and [Recordings](RECORDING.md) to retrieve retained output.

### Interrupt a wait or stop execution

A `KeyboardInterrupt` during a synchronous call cancels the response wait, not an already accepted cell.
Catch it inside the connection's context to collect more output or explicitly interrupt execution:

```python
console.send(control="interrupt")
```

Leaving the context closes the session.
Connect and close only when no other call is in progress.
A control-only interrupt may be sent from another thread while a call waits.

If cancellation cleanup cannot finish, the connection rejects further calls until it has been closed and reconnected.
Reconnecting loses the old session.
See [Python client cancellation](SEND_OPERATIONS.md#python-client-cancellation) for cleanup deadlines and failure behavior.

## Inspect or replace requirements

Both clients support `get`, `add`, `set`, and `reset` through the `requirements` argument.
Inspection normally returns JSON text but can return a startup notice before the runtime is ready; check the response before parsing it.

Replacing a live declaration normally requires restart.
A replacement empties omitted fields rather than preserving them.
See [Requirements API](REQUIREMENTS_API.md) for complete examples and constraints.
