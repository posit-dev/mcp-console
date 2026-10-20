# Build on Console

Use Console as the execution environment behind a data assistant or a tool for a specific analysis.
You can choose the languages and arguments the model can use, describe the data available to it, and prepare a session with data, connections, and helper functions.
Your application keeps the same Console connection open as the model works, so later calls can use what is already in memory.

## Choose languages and arguments

Create a [configuration file](CONFIGURATION.md#create-a-configuration-file) for your project, then select the interface you want to expose.
For example, a SQL-only interface can omit package requests and interactive input:

```yaml
languages: [sql]
tool:
  parameters:
    requirements:
      enabled: false
    stdin:
      enabled: false
```

The tool accepts SQL, polling, and the remaining control arguments.
R and Python source fields, `requirements`, and `stdin` are absent from the advertised schema and rejected if supplied, including as `null`.
Use `[r]`, `[python]`, or a combination of languages for other applications.

Language selection controls the interface; SQL can still use an R or Python connection underneath.
Use [sandbox permissions](SANDBOX_CONFIGURATION.md) and database permissions to control what the workload can access.
[Tool configuration](TOOL_CONFIGURATION.md) lists the available settings, including controls for nested arguments.

## Prepare the session

Select [interpreters, startup packages, and package preparation policies](RUNTIME_CONFIGURATION.md) in configuration.
Preparing a package makes it available; loading data and defining variables require code in the session.
Choose the setup approach that fits your application.

### Select a database connection

For the SQL interface above, add a `startup` mapping to select a connection before the first SQL call.
For example, with an existing SQLite database at `data/sales.sqlite`:

```yaml
startup:
  language: python
  code: |
    import sqlite3
    connection = sqlite3.connect("file:data/sales.sqlite?mode=ro", uri=True)
    _console.sql_connection(connection)
```

This opens that database in read-only mode.
The path is relative to Console's launch directory, and Python must be available even when only SQL is exposed to the model.

`startup` is specifically for creating and selecting a SQL connection.
It runs again after a worker restart, and the code must finish with a usable, user-created connection selected.
See [SQL connection startup](RUNTIME_CONFIGURATION.md#session-startup-source) for R and Python connection requirements.

### Load R data and helpers

Console uses [R's native startup files](RUNTIME_CONFIGURATION.md#native-r-startup) by default.
In a project's `.Rprofile`, you can prepare objects before the agent's first R call.
For example:

```r
sales <- data.frame(
  region = c("North", "South"),
  revenue = c(12000, 9000)
)
lockBinding("sales", globalenv())
```

Replace the small example data frame with code that reads your project's data, and define any helper functions in the same profile.
`lockBinding()` prevents ordinary reassignment to `sales`; R code can unlock the binding, so this is not a security boundary.

Start Console in that project directory with R exposed.
The profile runs again when R restarts; `r.vanilla: true` disables native startup files.

### Initialize from your application

An application can also send setup code before giving the model access to the session.
With the [Python client](PYTHON.md#call-console-directly), for example:

```python
from mcp_console import MCPConsole

with MCPConsole(args=["serve", "-c", "languages=[python]"]) as console:
    result = console.send(python="sales = {'North': 12000, 'South': 9000}")
    print(result)
    # Use this connection for the model's subsequent calls.
```

Complete setup and handle any errors before handing the connection to the model.
If the response says work is still running, [poll without resubmitting the code](SEND_OPERATIONS.md#cells-and-polling).
Keep the connection open for the conversation and repeat your setup after a restart or reconnect.
Setup calls use the same enabled arguments and sandbox permissions as later calls.

## Describe the data and workflow

Give the model information about the workspace it will receive: available tables or objects, column meanings, helper functions, and conventions for reporting results.

| Setting                              | Use                                                                  |
| ------------------------------------ | -------------------------------------------------------------------- |
| `tool.instructions`                  | Introduce the workspace in the server's initialization instructions. |
| `tool.description`                   | Explain when and how to use the `send` tool.                         |
| `tool.parameters.<name>.description` | Describe a particular argument, such as `sql` or `r`.                |

Each string replaces the corresponding generated text completely.
To add context while keeping Console's existing guidance, first export the resolved configuration:

```sh
mcp-console config resolve --full > application.yaml
```

Edit the appropriate description in `application.yaml` to include your context.
For a sales database, you might add:

> The `orders` table contains one row per order, with `order_date`, `region`, and `revenue` columns.
> Revenue is recorded in USD.
> Use these columns to answer sales questions and state the date range used in each result.

Launch Console with the edited file:

```sh
mcp-console serve --config-file application.yaml
```

Choose the languages and enabled arguments before exporting so the generated text and parameter entries match the interface.
The client decides when to show initialization instructions and tool descriptions to the model.
See [Tool configuration](TOOL_CONFIGURATION.md#inspect-and-edit-resolved-configuration) for export details and the effect of omitted, `null`, and empty descriptions.

## Name and connect your tool

For an existing MCP client, give the server registration a name that describes its purpose.
For a client that launches Console in your project, run this from that directory to use the edited configuration:

```sh
mcp-console configure-client claude --directory . --name sales-data -- --config-file application.yaml
```

This names the server entry `sales-data`.
The MCP tool remains `send`; configuration does not have a `tool.name` setting.
See [Client registration](CLI.md#configure-a-client) for client and launch options.

An application can expose its own function name while forwarding calls to Console.
For example, install `mcp-console[chatlas]` and, with an existing chatlas `chat`, create a `query_sales` tool from the SQL server's actual schema:

```python
from chatlas import Tool
from mcp_console import MCPConsole

with MCPConsole(args=["serve", "--config-file", "application.yaml"]) as console:
    definition = console.send_tool
    query_sales = Tool(
        func=console.send,
        name="query_sales",
        description=definition.description or "",
        parameters=definition.input_schema,
        strict=False,
    )
    chat.set_tools([query_sales])
    chat.chat("Which region has the highest total revenue?")
```

`set_tools()` replaces the chat's tool list.
Using `console.send_tool.input_schema` preserves configured languages and hidden arguments; inferring a schema from the Python method's signature would lose those settings.

[Client integration](CLIENTS.md) links to MCP, Python, and R interfaces.
The [Python adapters](PYTHON.md#choose-a-model-integration) and [R package](../r/README.md) cover model integration and connection lifetime.
Your application chooses the model and manages the conversation.

## Use the sandbox independently

You can also use Console's sandbox to run a program from a terminal, script, or build job:

```sh
mcp-console sandbox -- python analysis.py
```

[Run commands in a sandbox](SANDBOX_COMMAND.md) covers installation, file and network permissions, reusable policies, and cleanup.
It provides a separate entry point for applications that need command isolation without a persistent Console session.
