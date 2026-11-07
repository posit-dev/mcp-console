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

## Read and display results

`console.send(...)` and its shorthand `console(...)` return a [ConsoleResult](https://posit-dev.github.io/mcp-console/python/reference/consoleresult/).
The asynchronous client returns the same type after `await`.
`ConsoleResult` extends the MCP SDK's `CallToolResult`, inheriting its fields and serialization methods.
It retains the complete MCP response, including images and structured content, and adds text/image views and notebook display:

| Property                    | Contents                                                                                       |
| --------------------------- | ---------------------------------------------------------------------------------------------- |
| `result.content`            | MCP content blocks in their original text/image order.                                         |
| `result.text`               | Text blocks concatenated in their original order, without image placeholders.                  |
| `result.images`             | The image blocks from `content`, with their original MIME types and base64-encoded image data. |
| `result.structured_content` | Structured data returned by the tool, when present.                                            |
| `result.is_error`           | The MCP tool-error flag.                                                                       |
| `result.meta`               | Metadata attached to the MCP result, when present.                                             |

The result can be passed directly to code accepting `CallToolResult`.
Use the SDK's `result.model_dump()` or `result.model_dump_json()` methods to serialize it.

`print(result)` produces readable text with MIME placeholders such as `[image/png output]` where images occurred.
For requirements inspection, `print(result)` retains the formatted JSON representation; use `result.structured_content` to read the complete declaration directly.
Use `content` when interleaving matters: separate text and image views do not retain each image's position among the text blocks.

In an IPython notebook, display the result to render text and images in order:

```python
from IPython.display import display
from mcp_console import MCPConsole

with MCPConsole() as console:
    result = console(r="plot(1:3)")
    display(result)
```

Leaving `result` as the last expression in a notebook cell also displays it.
Assigning a result does not display it.
IPython is needed only for notebook display; ordinary client calls and `print(result)` do not require it.

MCP tool errors raise [ConsoleError](https://posit-dev.github.io/mcp-console/python/reference/consoleerror/), a `RuntimeError` subclass whose `result` attribute retains the full `ConsoleResult`.
For example, if a worker exits after producing a plot, the exception retains the image and the failure text.
Language errors reported as ordinary Console output remain in the returned result.
The lower-level `console.call_tool(name, arguments)` continues to return a plain `CallToolResult`, including its error flag.

This changes the return type of `send()` from a string to `ConsoleResult`.
Existing `print(console.send(...))` calls still work.
Use `.text` for string operations, such as `result.text.endswith("[running; poll with an empty send]")`, and for collecting text chunks while polling.
Replace `json.loads(console.send(requirements={"action": "get"}))` with `console.send(requirements={"action": "get"}).structured_content`.
If you used the earlier wrapper's `result.raw`, use `result` itself instead.
Construct results with the SDK's keyword fields, such as `ConsoleResult(content=[])`, instead of passing a `CallToolResult` positionally.

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
Chatlas returns a `ContentToolResult` for successful calls, with text-only output in `.value` and server result metadata in `.extra`; requirements inspection returns a string.
The other callable adapters return strings for text-only results.
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
