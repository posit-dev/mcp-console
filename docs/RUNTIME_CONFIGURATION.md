# Languages and startup

Choose an R installation or Python environment, control native R startup, and select a SQL connection before analysis begins.
[Sessions and languages](BUILTIN_RUNTIME.md) introduces the shared session.
[Package settings](PACKAGE_CONFIGURATION.md) covers startup packages and preparation policies; [Tool settings](TOOL_CONFIGURATION.md#model-visible-languages) controls which languages the model can use.

- [Choose a Python environment](#python-environment-selection).
- [Choose an R installation](#r-executable-selection).
- [Control R startup files](#native-r-startup).
- [Select a SQL connection at startup](#session-startup-source).
- [Limit concurrent sessions](#session-limits).

Put these settings in a [configuration file](CONFIGURATION.md).
Relative interpreter paths use Console's launch directory, including when written in a global file; a leading `~` follows the shared [path rules](CONFIGURATION.md#paths).
Settings are captured when the server starts.
Open a new server connection to change them; restarting the runtime does not reload YAML.

## Limit concurrent sessions {#session-limits}

Set `sessions.max` to limit live sessions on each MCP connection, including `default`:

```yaml
sessions:
  max: 4
```

The value must be a positive integer or `null`.
Omission and `null` mean unlimited; `1` allows only the default session.
The default session reserves a slot for the connection's lifetime, including when its startup fails.
Starting named sessions count toward the limit, and a restart that starts a replacement worker retains the slot.
A stopped named session releases its slot after worker cleanup, and can be started again when capacity is available.
Preparing requirements in a stopped named session occupies a slot until preparation cleanup finishes.
Calls that would exceed the limit fail without running their code; calls to existing live sessions remain available.
See [Named sessions](API.md#named-sessions) for creation, polling, and stopping workers.

## Choose a Python environment {#python-environment-selection}

<span id="python"></span>

Use an existing environment when you want to manage its packages yourself, or managed Python when you want Console to prepare the environment and packages.

| Setting                  | Accepted values                  | Selection behavior                                                                                                                                                                              |
| ------------------------ | -------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `python`                 | Omitted or `null`                | Honor a launch-time `RETICULATE_PYTHON` selection; otherwise use managed Python. See [Environment variables](ENVIRONMENT.md#server-paths-and-executable-discovery) for the compatibility rules. |
| `python`                 | Nonempty path string             | Use an existing Python executable or standard virtual environment. Equivalent to `python: {existing: PATH}`.                                                                                    |
| `python`                 | Managed settings mapping or `{}` | Select managed Python explicitly, overriding launch-time `RETICULATE_PYTHON`. An empty mapping uses managed defaults.                                                                           |
| `python.existing`        | Nonempty path string             | Use the installed packages in that executable's environment or virtual environment.                                                                                                             |
| `python.managed`         | Mapping                          | Nested form of the [managed settings](#managed-python).                                                                                                                                         |
| `python.first_available` | Nonempty list of candidates      | Select the first available candidate in order; see [fallback choices](#first-available-python).                                                                                                 |

A Python mapping contains either managed fields directly or exactly one of `existing`, `managed`, or `first_available`.
Combining direct managed fields with a selection key in the same mapping is an error.

### Use an existing environment

```yaml
python: .venv
```

Existing Python requires CPython 3.10 or newer with a usable shared embedding library.
Console itself needs no third-party Python packages; the standard library is sufficient for standard-library code.
Install the packages your analysis needs in the selected environment, such as pandas, Matplotlib, or `duckdb` for the [Python-owned SQL connection](RUNTIME_REFERENCE.md#sql-and-duckdb).
Console uses the environment as supplied and does not resolve or install its Python packages.

A virtual-environment directory must contain `pyvenv.cfg` and its ordinary `bin/python` or Windows `Scripts/python.exe`.
You can also select the Python executable directly; executable paths preserve virtual-environment identity and symlinks.
On Windows, select a real interpreter or virtual environment, not a Windows App Execution Alias.
Recognized Conda installations are unsupported.

Existing selections accept no package, version, or resolution settings.
An explicit selection overrides launch-time `RETICULATE_PYTHON`.
A broken environment fails rather than falling back to another interpreter.
The session's filesystem and network permissions still apply.

### Managed Python

```yaml
python:
  version: ">=3.12,<3.14"
```

| Setting          | Accepted values                                                                      | Meaning and default                                                                                                      |
| ---------------- | ------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------ |
| `python.version` | One quoted Python version or constraint string, such as `"3.13"` or `">=3.12,<3.14"` | Restrict the managed CPython version. Omission adds no version constraint; versions older than 3.10 are always excluded. |

The same fields are accepted under `python.managed` and in a managed fallback candidate.
For `packages` and `resolution` fields in these mappings, see [Package settings](PACKAGE_CONFIGURATION.md#startup-package-declarations).

Managed selection uses uv-managed CPython, rather than selecting a fallback interpreter on `PATH`.
It excludes versions older than CPython 3.10 before ranking available versions or applying constraints.
Without R, managed preparation requires `uv` on the execution host.

Before merging each configuration layer, Console expands direct managed fields to `python.managed`.
Flat and nested settings in separate layers therefore combine under the ordinary [merge rules](CONFIGURATION.md#merge-rules).
`config resolve` output and validation diagnostics use the nested paths.
Changing one selection form does not remove incompatible fields inherited from an earlier layer; clear the earlier branch before replacing it when necessary.

### Try environments in order {#first-available-python}

```yaml
python:
  first_available:
    - existing: .venv
    - active_venv
    - managed:
        version: "3.13"
```

| Candidate        | Selection behavior                                                                                                                                                           |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `existing: PATH` | Use that executable or virtual environment when its path exists. An absent path advances to the next candidate; a present but unusable environment fails.                    |
| `active_venv`    | Use the incoming `VIRTUAL_ENV`. An unset or empty value advances to the next candidate; a supplied but unusable environment fails. The launcher must preserve this variable. |
| `managed: {...}` | Use managed Python with the settings above. This candidate must be last.                                                                                                     |

The list must be flat and nonempty, with at most one `active_venv` and at most one managed candidate.
Only absence advances to the next candidate; a broken selection does not.
Exhausting the list is an error.
Managed candidate fields have paths such as `python.first_available[].managed.packages`, where `[]` denotes a list item.

## Choose an R installation {#r-executable-selection}

<span id="r"></span>

By default, Console discovers an installed R from installation hints and `PATH`.
Select a particular installation with an executable path:

```yaml
r: /opt/R/bin/R
```

The scalar form expands to `r: {executable: PATH}` before configuration layers are merged.
Use a mapping to combine executable selection with startup settings:

```yaml
r:
  executable: /opt/R/bin/R
  vanilla: true
```

| Setting        | Accepted values                     | Meaning and default                                                                                                                |
| -------------- | ----------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `r`            | Executable path, mapping, or `null` | Select an installation and its package/startup settings. Omission or `null` uses normal R discovery and defaults.                  |
| `r.executable` | Nonempty path string                | Installed R executable or ordinary launcher. Omission uses installation hints and `PATH` discovery.                                |
| `r.vanilla`    | `true` or `false`                   | Request R's native `--vanilla` startup. Default: `false`, retaining normal environment files, profiles, and workspace restoration. |

For `r.packages` and `r.resolution`, see [Package settings](PACKAGE_CONFIGURATION.md).

Select an executable or launcher, not an R installation directory, Rscript, or a command with arguments.
An explicit executable overrides installation hints and `PATH` discovery.
The [environment reference](ENVIRONMENT.md#server-paths-and-executable-discovery) covers discovery variables such as `R_HOME`.

Console retains the selected installation across runtime restarts.
If its captured executable or resources change incompatibly, start a new server connection.
Executables and the code they load are trusted inputs; selecting them does not make them immutable sandbox assets.

<span id="startup-package-declarations"></span> <span id="default-startup-packages"></span> <span id="dependency-resolution-policies"></span> <span id="resolution-policies"></span>

Startup declarations, default packages, and resolution policies are documented in [Package settings](PACKAGE_CONFIGURATION.md).

## Control R startup files {#native-r-startup}

R uses normal native startup by default: environment files, profiles, workspace restoration, `.First()`, and default-package attachment are R's responsibility.
Startup has the session's ordinary permissions.
Console passes `--quiet`, `--interactive`, and `--no-save`, and does not save a workspace at shutdown.

To request R's native `--vanilla` behavior:

```yaml
r:
  vanilla: true
```

This affects the session's R runtime.
Runtime discovery and dependency preparation remain profile-free, so worker `.Rprofile` and `.Renviron` files do not configure the separate resolver.
Use [resolver environment settings](ENVIRONMENT.md#where-to-set-variables) for preparation credentials or mirrors.
See [R startup and libraries](ENVIRONMENT.md#r-startup-and-libraries) for the recognized native variables.

Console attaches its bridges and managed graphics after native startup, so startup plots use R's native device.
A profile diagnostic that R survives does not make the session unusable.
If R fails initialization or is interrupted, fix the startup file externally and explicitly restart; ordinary calls do not retry initialization.
Edits to `.Rprofile` take effect on runtime restart because R reads the file again, even though YAML configuration remains captured for the server connection.

## Select a SQL connection at startup {#session-startup-source}

<span id="session-startup"></span>

Without a `startup` setting, SQL uses the session's managed DuckDB connection.
Use startup source to create and select a different connection before the first SQL cell:

```yaml
startup:
  language: python
  code: |
    import sqlite3
    connection = sqlite3.connect(":memory:")
    _console.sql_connection(connection)
```

| Setting            | Accepted values                    | Meaning                                                                                              |
| ------------------ | ---------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `startup`          | Mapping or `null`                  | Optional SQL connection setup. Omitted or `null`: no user startup source.                            |
| `startup.language` | `r` or `python`                    | Interpreter that runs the source. Required when `startup` is a mapping.                              |
| `startup.code`     | Nonblank source string without NUL | Create and select a user-owned connection on that interpreter. Required when `startup` is a mapping. |

For R, use `language: r`, create a normal DBI connection, and select it with `.console$sql_connection(connection)`.
Python uses a DB-API connection and `_console.sql_connection(connection)`.
The program must finish with a usable, user-created connection selected on that same interpreter.
Retrieving or resetting the managed default does not satisfy startup.

Source runs after runtime helpers are available, once per worker, including after a restart.
It is captured when the server launches; the encoded startup configuration, including escaping, is limited to 32 KiB.
It is not automatically echoed as a submitted cell, but emitted output and errors are visible.
Startup code has ordinary session permissions and can have side effects.

Failure or interruption withholds SQL until explicit restart.
Console does not fall back to managed DuckDB or automatically replay failed startup source.
Restart can repeat earlier side effects.
After successful startup, resetting the connection restores managed DuckDB without rerunning the source.
See [SQL connections](RUNTIME_REFERENCE.md#select-a-native-connection) for connection ownership and reset behavior.
