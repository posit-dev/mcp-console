# Reference

Look up configuration keys, command-line options, language behavior, and the MCP, R, and Python APIs.
For a walkthrough or a task, start with the [guides](README.md); for application integration, start with [Build on Console](BUILD.md).

<span id="find-a-task"></span>

## Configuration

[Configuration files](CONFIGURATION.md) explains discovery, precedence, merging, paths, and resolved settings.
The index below links each setting to its reference section.
Dotted paths show nesting; managed Python also accepts package and policy settings under `python.managed` and in managed fallback candidates.

<span id="top-level-settings"></span> <span id="settings-by-task"></span>

| Setting                                                                                                                                      | Reference                                                                                                                         |
| -------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `cache`, `cache.directory`                                                                                                                   | <span id="caches"></span> [Package cache locations](PACKAGE_CONFIGURATION.md#cache-locations)                                     |
| `environment`, `inherit_environment`                                                                                                         | <span id="environment"></span> [Shared environment values and inheritance](ENVIRONMENT.md#where-to-set-variables)                 |
| `languages`                                                                                                                                  | <span id="languages"></span> [Model-visible languages](TOOL_CONFIGURATION.md#model-visible-languages)                             |
| `python`, `python.existing`                                                                                                                  | <span id="python"></span> [Python environment selection](RUNTIME_CONFIGURATION.md#python-environment-selection)                   |
| `python.managed`, `python.version`                                                                                                           | <span id="managed-python"></span> [Managed Python selection](RUNTIME_CONFIGURATION.md#managed-python)                             |
| `python.first_available`                                                                                                                     | <span id="first-available-python"></span> [Ordered Python choices](RUNTIME_CONFIGURATION.md#first-available-python)               |
| `python.packages`, `r.packages`                                                                                                              | [Startup package declarations and defaults](PACKAGE_CONFIGURATION.md#startup-package-declarations)                                |
| `python.resolution`, `r.resolution`                                                                                                          | <span id="resolution-policies"></span> [Package preparation policies](PACKAGE_CONFIGURATION.md#dependency-resolution-policies)    |
| `r`, `r.executable`                                                                                                                          | <span id="r"></span> [R installation selection](RUNTIME_CONFIGURATION.md#r-executable-selection)                                  |
| `r.vanilla`                                                                                                                                  | [Native R startup files](RUNTIME_CONFIGURATION.md#native-r-startup)                                                               |
| `resolver`, `resolver.environment`, `resolver.inherit_environment`                                                                           | <span id="package-preparation"></span> [Package sources and preparation environment](PACKAGE_CONFIGURATION.md#configuration)      |
| `resolver.sandbox`                                                                                                                           | [Package preparation permissions](PACKAGE_CONFIGURATION.md#preparation-permissions)                                               |
| `sandbox`                                                                                                                                    | <span id="sandbox"></span> [Session and command permissions](SANDBOX_CONFIGURATION.md#which-permissions-to-configure)             |
| `sandbox.profile`                                                                                                                            | <span id="workspace-profile"></span> [Base profiles](SANDBOX_CONFIGURATION.md#profiles-and-temporary-storage)                     |
| `sandbox.profile.inherit_codex.fallback`                                                                                                     | [Fallback when client permissions are unavailable](SANDBOX_CONFIGURATION.md#choose-a-fallback)                                    |
| `sandbox.tempdir`                                                                                                                            | <span id="temporary-storage"></span> [Temporary storage](SANDBOX_CONFIGURATION.md#temporary-storage)                              |
| `sandbox.filesystem.read_only`, `sandbox.filesystem.read_write`, `sandbox.filesystem.deny`                                                   | <span id="filesystem"></span> [File access](SANDBOX_CONFIGURATION.md#filesystem)                                                  |
| `sandbox.network`                                                                                                                            | <span id="network-access"></span> [Network choices](SANDBOX_CONFIGURATION.md#network-choices)                                     |
| `sandbox.network.proxy.mode`, `sandbox.network.proxy.socks5`, `sandbox.network.proxy.allow_upstream_proxy`                                   | <span id="proxy-settings"></span> [Proxy settings](SANDBOX_CONFIGURATION.md#proxy-modes-and-destinations)                         |
| `sandbox.network.proxy.domains.allow`, `sandbox.network.proxy.domains.deny`                                                                  | <span id="destination-rules"></span> [Destination rules](SANDBOX_CONFIGURATION.md#destination-rules)                              |
| `sandbox.network.sockets.unix_sockets`, `sandbox.network.allow_local_binding`                                                                | <span id="local-sockets-and-binding"></span> [Local sockets and binding](SANDBOX_CONFIGURATION.md#unix-sockets-and-local-binding) |
| `startup`, `startup.language`, `startup.code`                                                                                                | <span id="session-startup"></span> [SQL connection startup](RUNTIME_CONFIGURATION.md#session-startup-source)                      |
| `tool`, `tool.description`, `tool.instructions`                                                                                              | <span id="tool-presentation"></span> [Tool descriptions and instructions](TOOL_CONFIGURATION.md#tool-descriptions-and-parameters) |
| `tool.parameters.<name>.enabled`, `tool.parameters.<name>.description`, `tool.parameters.<name>.parameters`, `tool.parameters.<name>.schema` | <span id="parameter-settings"></span> [Tool parameter settings](TOOL_CONFIGURATION.md#parameter-settings)                         |

The sandbox fields also apply under `resolver.sandbox`, with the preparation defaults and profile restrictions documented in [Package settings](PACKAGE_CONFIGURATION.md#preparation-permissions).

<span id="null-empty-values-and-merging"></span>

See [Merge rules](CONFIGURATION.md#merge-rules) for omitted, `null`, and empty values, and [Environment variables](ENVIRONMENT.md) for recognized variable names.

## Behavior

<span id="language-behavior"></span>

[Language behavior](RUNTIME_REFERENCE.md) specifies R and Python helpers, SQL connections and results, plotting, and output handling.
For an introduction, read [Sessions and languages](BUILTIN_RUNTIME.md).

[Recording reference](RECORDING_REFERENCE.md) specifies storage locations, files, retention, and failure behavior.
For reading and reusing a session, see [Recordings and reports](RECORDING.md).

## Interfaces

### Command line

- [Command-line reference](CLI.md) documents command behavior, options, and installation checks.
- [CLI help](cli-help.qmd) contains the public commands' complete help text.

<span id="apis-and-package-manuals"></span>

### MCP

- [MCP send reference](API.md) documents tool arguments, results, output limits, call ordering, and requirements actions.
- [Drive send directly](SEND_OPERATIONS.md) is a guide to polling, interactive input, interruption, and restart.

### Packages

- The [Python API](https://posit-dev.github.io/mcp-console/python/reference/) documents synchronous and asynchronous clients and model integrations.
  [Use Console from Python](PYTHON.md) introduces the clients and framework adapters.
- The [R API](https://posit-dev.github.io/mcp-console/r/reference/) documents tool registration and session configuration.
  [Use Console from R](R.md) introduces the ellmer integration and links to configuration and analysis examples.

Package overviews: [Python](https://posit-dev.github.io/mcp-console/python/) and [R](../r/README.md).

## Glossary

The [glossary](GLOSSARY.md) defines terms used throughout Console's documentation.
