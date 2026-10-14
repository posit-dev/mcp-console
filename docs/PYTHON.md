# Python clients and adapters

The `mcp_console` package provides synchronous/asynchronous clients and model-framework adapters around the same persistent MCP session.
The package requires Python 3.11 or newer; framework dependencies are optional.

## Call Console directly

Install the `client` extra.
From a source checkout:

```sh
uv tool run --python 3.13 --from ".[client]" python your_client.py
```

```python
from mcp_console import MCPConsole

with MCPConsole() as console:
    print(console.send(python="values = [2, 4, 6]; values"))
    print(console.send(python="sum(values)"))
```

Use `AsyncMCPConsole` with `async with` and `await` for asynchronous code.
Enter and close an async connection in the same task.
Reconnect starts a new session; recreate adapters after reconnecting.

Both clients use the official MCP Python SDK for transport and protocol handling.
`MCPConsole` provides blocking calls through an AnyIO background event loop.
An application can send an interrupt from another thread while a call awaits output.
Connect and close only when no other call is in progress.

Helpers prefer Console in the current Python environment, then PATH, and launch `serve`.
Override `command`/`args` for another executable.
Stdio environment and working-directory options are supplied through the corresponding helper's parameter mapping; exact signatures are in the [API reference](https://t-kalinowski.github.io/mcp-console/python/reference/index.html).

## Choose a model integration

| Framework                    | Extra           | Adapter for an open client                                                                                      | Framework-owned connection                                                                                                |
| ---------------------------- | --------------- | --------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| chatlas                      | `chatlas`       | [chatlas.tool](https://t-kalinowski.github.io/mcp-console/python/reference/chatlas.tool.html)                   | [chatlas.register](https://t-kalinowski.github.io/mcp-console/python/reference/chatlas.register.html)                     |
| `openai` — Responses API     | `openai`        | [openai.responses_tool](https://t-kalinowski.github.io/mcp-console/python/reference/openai.responses_tool.html) | Application owns the tool loop.                                                                                           |
| `openai-agents` — Agents SDK | `openai-agents` | [openai.agents_tool](https://t-kalinowski.github.io/mcp-console/python/reference/openai.agents_tool.html)       | [openai.agents_server](https://t-kalinowski.github.io/mcp-console/python/reference/openai.agents_server.html)             |
| `openai-codex` — Codex SDK   | `codex`         | —                                                                                                               | [codex.server](https://t-kalinowski.github.io/mcp-console/python/reference/codex.server.html) returns configuration only. |
| Anthropic                    | `anthropic`     | [anthropic.tool](https://t-kalinowski.github.io/mcp-console/python/reference/anthropic.tool.html)               | [anthropic.tools](https://t-kalinowski.github.io/mcp-console/python/reference/anthropic.tools.html)                       |

Each reference page contains a complete example with framework setup and cleanup.
The application owns model clients, chats, agents, and any function-calling loop.
Creating a Console tool does not create or choose a model.

Adapters read `console.send_tool` from the connected server.
Do not register `console.send` through annotation inference: it cannot describe that server's configured fields.
For chatlas, use `set_tools()` on a new chat to retain the adapter's explicit schema; native registration has its own cleanup method.
Use default native tool names unless the installed adapter documents otherwise.

## Results and lifetime

`console(...)` aliases `send(...)`.
Ordinary Python calls return text, represent images with MIME placeholders, and raise `RuntimeError` for MCP tool errors.
The text-tool adapters have the same image limitation.
Responses and native MCP integrations preserve images.

Keep one connection per conversation.
`timeout_ms` limits observation, not execution; after a running notice, poll with `console.send(timeout_ms=300000)` rather than rerunning code.
Omitting `timeout_ms`, or passing `None`, leaves the server's 60,000 ms default in effect.
If [tool configuration](CONFIGURATION.md#tool-descriptions-and-parameters) hides that parameter, poll with `console.send()` and omit it from other calls.
Use [Send operations](SEND_OPERATIONS.md) for input, control, and sequencing, and [Recordings](RECORDING.md) for retained output.

If `KeyboardInterrupt` interrupts a synchronous response wait, the client cancels that request and re-raises the exception.
It waits for the SDK's request cleanup before re-raising so subsequent calls cannot overtake cancellation.
Concurrent sends are rejected while cancellation cleanup is in progress; ordinary concurrent control calls remain available when a cell is running.
Cleanup has a five-second limit; another `KeyboardInterrupt` also ends the wait.
If cleanup does not finish, the client aborts that request task and rejects further calls until the connection has been closed and reconnected.
Closing waits at most five seconds for the aborted request to stop before closing the SDK transport and portal.
If that request still cannot stop, closing raises an error and retains the invalid connection until it stops.
An admitted cell keeps running.
Catch the exception inside the connection's context to poll for its output or call `console.send(control="interrupt")` to interrupt execution explicitly.
Leaving the context closes the session.

## Inspect or replace requirements

Both clients accept the shared `get`, `add`, `set`, and `reset` actions through the `requirements` argument.
Inspection returns JSON text after startup is ready; during startup it can return `[worker starting]` instead.
Check the response before parsing it as JSON—an earlier call returning does not prove startup completed.

To replace a declaration, copy the complete `requirements` object, edit it, add `action: "set"`, and send it with `control="restart"`.
Omitted fields are emptied, not preserved.
[Requirements](REQUIREMENTS.md) owns the examples, policies, syntax, and activation semantics.
