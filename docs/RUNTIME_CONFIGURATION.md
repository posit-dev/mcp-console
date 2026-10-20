# Interpreters and startup

Choose an R installation or Python environment, set the packages available at startup, and select a SQL connection before analysis begins.
Console works without these settings; [Packages and environments](REQUIREMENTS.md) explains its defaults and how package preparation fits into a session.

- [Choose a Python environment](#python-environment-selection).
- [Choose an R installation](#r-executable-selection).
- [Choose startup packages](#startup-package-declarations).
- [Choose when packages may be prepared](#dependency-resolution-policies).
- [Control R startup files](#native-r-startup).
- [Select a SQL connection at startup](#session-startup-source).

Put these settings in a [configuration file](CONFIGURATION.md).
Relative interpreter paths use Console's launch directory, including when written in a global file; a leading `~` follows the shared [path rules](CONFIGURATION.md#paths).
Settings are captured when the server starts.
Open a new server connection to change them; restarting the runtime does not reload YAML.

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
  packages: [numpy, pandas]
  resolution: explicit
```

| Setting             | Accepted values                                                                      | Meaning and default                                                                                                                                                   |
| ------------------- | ------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `python.version`    | One quoted Python version or constraint string, such as `"3.13"` or `">=3.12,<3.14"` | Restrict the managed CPython version. Omission adds no version constraint; versions older than 3.10 are always excluded.                                              |
| `python.packages`   | List of named PEP 508 registry requirements                                          | Packages to prepare at startup. Omission keeps the [default Python packages](REQUIREMENTS.md#defaults-and-startup-configuration); `[]` requests no optional packages. |
| `python.resolution` | `implicit`, `explicit`, or `fixed`                                                   | When Console may prepare packages. Default: `explicit`. See [Resolution policies](#dependency-resolution-policies).                                                   |

The same fields are accepted under `python.managed` and in a managed fallback candidate.
See [Startup packages](#startup-package-declarations) for package syntax and replacement rules.

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
        packages: [numpy, pandas, matplotlib]
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
Use a mapping to combine executable selection with package and startup settings:

```yaml
r:
  executable: /opt/R/bin/R
  packages: [dplyr, ggplot2]
  resolution: explicit
  vanilla: true
```

| Setting        | Accepted values                                | Meaning and default                                                                                                                                              |
| -------------- | ---------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `r`            | Executable path, mapping, or `null`            | Select an installation and its package/startup settings. Omission or `null` uses normal R discovery and defaults.                                                |
| `r.executable` | Nonempty path string                           | Installed R executable or ordinary launcher. Omission uses installation hints and `PATH` discovery.                                                              |
| `r.packages`   | List of single-line `ir` package references    | Packages to prepare at startup. Omission keeps the [default R packages](REQUIREMENTS.md#defaults-and-startup-configuration); `[]` requests no optional packages. |
| `r.resolution` | `implicit`, `explicit`, `fixed`, or `disabled` | When Console may prepare packages. Default: `implicit`. See [Resolution policies](#dependency-resolution-policies).                                              |
| `r.vanilla`    | `true` or `false`                              | Request R's native `--vanilla` startup. Default: `false`, retaining normal environment files, profiles, and workspace restoration.                               |

Select an executable or launcher, not an R installation directory, Rscript, or a command with arguments.
An explicit executable overrides installation hints and `PATH` discovery.
The [environment reference](ENVIRONMENT.md#server-paths-and-executable-discovery) covers discovery variables such as `R_HOME`.

Console retains the selected installation across runtime restarts.
If its captured executable or resources change incompatibly, start a new server connection.
Executables and the code they load are trusted inputs; selecting them does not make them immutable sandbox assets.

## Choose startup packages {#startup-package-declarations}

```yaml
r:
  packages: [dplyr, dbplyr, ggplot2]
python:
  packages: [numpy, pandas, matplotlib]
```

An omitted package list keeps that runtime's [default startup declaration](REQUIREMENTS.md#defaults-and-startup-configuration).
A supplied list replaces it; an empty list (`[]`) requests no optional packages.
Empty lists do not remove Console's required runtime infrastructure, uninstall packages, or delete cached installations.
Preparation makes packages available without attaching R packages or importing Python modules.

Built-in optional defaults are prepared on a best-effort basis; Console reports and skips failed defaults.
Packages listed explicitly in configuration are required, even if they also occur in the built-in defaults.
Required runtime infrastructure remains strict.

R package strings use single-line `ir` references, including supported remote references; local sources, NUL, and line breaks are rejected.
Python strings use named PEP 508 registry requirements, including versions, extras, and markers; paths, editable requirements, URLs, archives, and `name @ URL` references are rejected.
See [accepted requirement input](REQUIREMENTS_API.md#accepted-requirement-input) for package-request fields.

Later `requirements` calls with `action: reset` restore the effective startup declaration captured at server launch, rather than rereading a subsequently edited configuration file.
[Requirements API](REQUIREMENTS_API.md) covers additions, replacement, and changes that require a runtime restart.

## Choose when packages may be prepared {#dependency-resolution-policies}

<span id="resolution-policies"></span>

Set `r.resolution` or `python.resolution` to control Console's package preparation.
The Python setting also applies under `python.managed` and in managed fallback candidates.
R defaults to `implicit`; managed Python defaults to `explicit`.

| Policy              | Startup preparation                                                     | During the session                                                                                                                                                         |
| ------------------- | ----------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `fixed`             | Prepare the startup declaration.                                        | Keep that declaration fixed for the connection; reject package additions and version changes through Console.                                                              |
| `explicit`          | Prepare the startup declaration.                                        | Also accept package requests through `requirements` and managed Python declarations through `reticulate::py_require()`. Ordinary loads/imports do not trigger preparation. |
| `implicit`          | Prepare the startup declaration.                                        | Also accept explicit requests and prepare missing packages when execution reaches a supported load/import. The cell is not rerun.                                          |
| `disabled` (R only) | No managed R package preparation, including Console's R infrastructure. | No managed preparation. If `r.packages` is supplied, it must be empty.                                                                                                     |

For example, allow explicit R package requests while keeping the Python startup declaration fixed:

```yaml
r:
  packages: [dplyr]
  resolution: explicit
python:
  packages: [numpy, pandas]
  resolution: fixed
```

To enable preparation on missing Python imports, set `python.resolution: implicit`.
For preinstalled-only Python, select an [existing environment](#use-an-existing-environment).
DuckDB extension preparation follows the resolution policy of its managed provider.

Changing a resolution policy requires a new server connection.
These policies govern Console's preparation; they do not restrict arbitrary installation commands in submitted code or grant network access.
[Packages and environments](REQUIREMENTS.md#resolution-policy-admission) explains the policies with examples, and [Requirements API](REQUIREMENTS_API.md) specifies live-request behavior.
[Package sources and caches](RESOLVER.md) covers download permissions and package sources.

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
