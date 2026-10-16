config_scalar <- function() {
  S7::new_property(NULL | S7::class_character, validator = function(value) {
    if (
      !is.null(value) &&
        (length(value) != 1L || is.na(value) || !nzchar(value))
    ) {
      "must be NULL or one non-empty string"
    }
  })
}

config_environment <- function() {
  S7::new_property(NULL | S7::class_character, validator = function(value) {
    if (
      !is.null(value) &&
        length(value) &&
        (anyNA(value) ||
          is.null(names(value)) ||
          anyNA(names(value)) ||
          any(!nzchar(names(value))) ||
          anyDuplicated(names(value)))
    ) {
      "must be a named character vector with unique, non-empty names"
    }
  })
}

#' @name ConsoleConfig
#' @param global,project Whether to discover each configuration source.
#'   Both default to `TRUE` when `ConfigDiscovery()` is explicitly constructed.
#' @export
ConfigDiscovery <- S7::new_class(
  "ConfigDiscovery",
  parent = ConfigNode,
  properties = list(
    global = S7::new_property(
      S7::class_logical,
      default = TRUE,
      validator = function(value) {
        if (length(value) != 1L || is.na(value)) "must be TRUE or FALSE"
      }
    ),
    project = S7::new_property(
      S7::class_logical,
      default = TRUE,
      validator = function(value) {
        if (length(value) != 1L || is.na(value)) "must be TRUE or FALSE"
      }
    )
  )
)

#' Select R and configure its package preparation
#'
#' Use this object as `ConsoleConfig(r = RConfig(...))`. Console captures the
#' installation and package declaration once per connection. Worker restart
#' does not rediscover the installation or reread configuration files.
#'
#' @param executable `NULL` or a path to an R executable or ordinary launcher,
#'   such as `/opt/R/bin/R`. Directories and Rscript executables are unsupported.
#'   Console owns launch-relative resolution and supported `~` expansion.
#'   Use [current_r()] to select the R installation running this code.
#' @param packages `NULL` for bundled defaults, or a character vector replacing
#'   optional startup packages. `character()` requests no optional packages.
#' @param resolution `NULL` (defaulting to `"implicit"`), `"implicit"`, `"explicit"`,
#'   `"fixed"`, or `"disabled"`. Implicit permits runtime additions; explicit permits
#'   later deliberate requirement changes; fixed keeps the startup declaration; disabled uses
#'   preinstalled R packages and requires an empty or omitted package list.
#' @param vanilla `NULL`, `TRUE`, or `FALSE`; controls native R startup files.
#' @inherit ConsoleConfig return
#' @examples
#' RConfig(packages = "dplyr", resolution = "fixed")
#' RConfig(resolution = "disabled")
#' @export
RConfig <- S7::new_class(
  "RConfig",
  parent = ConfigNode,
  properties = list(
    executable = config_scalar(),
    packages = config_string_list(),
    resolution = config_choice(c(
      "implicit",
      "explicit",
      "fixed",
      "disabled"
    )),
    vanilla = config_flag()
  ),
  validator = function(self) {
    if (identical(self@resolution, "disabled") && length(self@packages)) {
      "packages must be empty when resolution = disabled"
    }
  }
)

#' Select the current R installation
#'
#' Use [RConfig()] with `executable = current_r()` to run Console with the R
#' installation running this code. `R.home("bin")` accounts for the current
#' architecture on Windows; the launcher is `R.exe` on Windows and `R` elsewhere. This does not
#' search `PATH` or copy the current R session's objects, libraries, or options.
#'
#' @return A path to the current installation's R launcher.
#' @examples
#' RConfig(executable = current_r(), vanilla = TRUE)
#' @export
current_r <- function() {
  file.path(R.home("bin"), if (.Platform$OS.type == "windows") "R.exe" else "R")
}

PythonConfig <- S7::new_class(
  "PythonConfig",
  parent = ConfigNode,
  abstract = TRUE
)

#' Select a Python environment
#'
#' Use one of these objects as `ConsoleConfig(python = ...)`. `ExistingPython()`
#' uses preinstalled packages in an interpreter or standard virtual environment.
#' `ManagedPython()` prepares a uv-managed environment. It does not fall back
#' to a system interpreter when managed preparation is unavailable.
#'
#' @param existing Path to a Python executable or standard virtual environment,
#'   such as `.venv`. Console retains the environment's interpreter spelling,
#'   captures selection at launch, and rejects unsupported Conda installations.
#' @inheritParams RConfig
#' @param version `NULL` or one Python version or supported constraint string,
#'   such as `"3.13"` or `">=3.12,<3.14"`.
#' @inherit ConsoleConfig return
#' @examples
#' ExistingPython(".venv")
#' ManagedPython(version = "3.13", packages = "pandas", resolution = "explicit")
#' @name PythonConfig
#' @export
ExistingPython <- S7::new_class(
  "ExistingPython",
  parent = PythonConfig,
  properties = list(
    existing = S7::new_property(
      S7::class_character,
      validator = function(value) {
        if (length(value) != 1L || is.na(value) || !nzchar(value)) {
          "must be one non-empty path"
        }
      }
    )
  )
)

#' @rdname PythonConfig
#' @param resolution `NULL` (defaulting to `"explicit"`), `"implicit"`, `"explicit"`,
#'   or `"fixed"`.
#'   To use preinstalled packages without preparation, choose `ExistingPython()`.
#' @export
ManagedPython <- S7::new_class(
  "ManagedPython",
  parent = PythonConfig,
  properties = list(
    version = config_scalar(),
    packages = config_string_list(),
    resolution = config_choice(c("implicit", "explicit", "fixed"))
  )
)

#' @rdname PythonConfig
#' @param first_available A nonempty list of [ExistingPython()] candidates,
#'   optionally one `"active_venv"` and a final [ManagedPython()] candidate.
#'   Only absent paths are skipped; present but unusable environments fail.
#' @export
FirstAvailablePython <- S7::new_class(
  "FirstAvailablePython",
  parent = PythonConfig,
  properties = list(
    first_available = S7::new_property(
      S7::class_list,
      validator = function(value) {
        if (
          !length(value) ||
            !all(vapply(
              value,
              function(candidate) {
                identical(candidate, "active_venv") ||
                  S7::S7_inherits(candidate, ExistingPython) ||
                  S7::S7_inherits(candidate, ManagedPython)
              },
              logical(1)
            ))
        ) {
          return(
            "must contain ExistingPython(), active_venv, or ManagedPython() candidates"
          )
        }
        if (sum(vapply(value, identical, logical(1), "active_venv")) > 1L) {
          return("must contain at most one active_venv candidate")
        }
        managed <- which(vapply(
          value,
          S7::S7_inherits,
          logical(1),
          ManagedPython
        ))
        if (length(managed) > 1L || any(managed != length(value))) {
          "must contain at most one ManagedPython() candidate, last"
        }
      }
    )
  )
)

#' Configure session startup source
#'
#' Pass this object as `ConsoleConfig(startup = StartupConfig(...))` to select
#' a user-owned SQL connection before the first SQL cell. The source runs once
#' per worker generation and must select a connection with
#' `.console$sql_connection()` in R or `_console.sql_connection()` in Python.
#' Native Console validates source and platform constraints at launch.
#'
#' @param language `"r"` or `"python"`, the interpreter running the source.
#' @param code One string containing startup source.
#' @inherit ConsoleConfig return
#' @export
StartupConfig <- S7::new_class(
  "StartupConfig",
  parent = ConfigNode,
  properties = list(
    language = config_choice(c("r", "python")),
    code = config_scalar()
  )
)

#' Configure dependency preparation permissions and environment
#'
#' Use this object as `ConsoleConfig(resolver = ResolverConfig(...))`. Worker
#' permissions and resolver permissions are independent. An omitted resolver
#' filesystem retains native cache grants; an explicit filesystem mapping
#' replaces them. Explicit resolver sandbox policies are unsupported on Windows,
#' where preparation uses host permissions. Environment settings still apply.
#'
#' @inheritParams ConsoleConfig
#' @param sandbox `NULL` or a [SandboxPolicy()] replacing resolver permissions.
#' @inherit ConsoleConfig return
#' @examples
#' ResolverConfig(environment = c(UV_INDEX_URL = "https://pypi.org/simple"))
#' @export
ResolverConfig <- S7::new_class(
  "ResolverConfig",
  parent = ConfigNode,
  properties = list(
    environment = config_environment(),
    sandbox = NULL | SandboxPolicy,
    inherit_environment = config_flag()
  )
)

config_description <- function() {
  S7::new_property(NULL | S7::class_character, validator = function(value) {
    if (!is.null(value) && (length(value) != 1L || is.na(value))) {
      "must be NULL or one string"
    }
  })
}

config_parameters <- function() {
  S7::new_property(NULL | S7::class_list, validator = function(value) {
    if (
      !is.null(value) &&
        length(value) &&
        (is.null(names(value)) ||
          anyNA(names(value)) ||
          any(!nzchar(names(value))) ||
          anyDuplicated(names(value)) ||
          !all(vapply(value, S7::S7_inherits, logical(1), ToolParameterConfig)))
    ) {
      "must be a named list of ToolParameterConfig() objects with unique, non-empty names"
    }
  })
}

#' Customize the exposed tool definition
#'
#' Use `ToolConfig()` as `ConsoleConfig(tool = ...)` to replace tool prose or
#' hide parameters for evaluations. Descriptions are used verbatim, including
#' empty strings. Omitted fields retain generated text or inherited settings.
#'
#' `ToolParameterConfig(enabled = FALSE)` removes a parameter from the exposed
#' signature and rejects calls supplying it. Omission keeps its runtime default;
#' hiding `timeout_ms` retains the 60,000 ms wait budget.
#'
#' Parameter names must exist in the schema selected by `ConsoleConfig(languages
#' = ...)`. Unknown or unavailable parameters and nested parameters under scalar
#' fields fail launch. Parameter types and runtime defaults cannot be replaced.
#'
#' @param description `NULL` or one string replacing the complete tool or
#'   parameter description.
#' @param instructions `NULL` or one string replacing MCP initialization
#'   instructions: server guidance received when the client connects, which some
#'   clients use for deferred tool search. The tool and parameter descriptions
#'   are returned later through `tools/list`. The ellmer adapter uses those
#'   descriptions and does not add initialization instructions to the chat prompt.
#' @param parameters `NULL` or a named list of [ToolParameterConfig()] objects.
#'   Nest another `parameters` list to configure fields inside an object parameter.
#' @param enabled `NULL`, `TRUE`, or `FALSE`; controls parameter visibility.
#' @param schema `NULL` or a JSON-ready list of read-only type and constraint
#'   metadata populated by [resolve_console_config()]. Native validation rejects
#'   changes to the parameter's types or constraints.
#' @inherit ConsoleConfig return
#' @examples
#' ConsoleConfig(tool = ToolConfig(
#'   description = "Run R code in a persistent session.",
#'   parameters = list(
#'     r = ToolParameterConfig(description = "Execute one complete R cell."),
#'     timeout_ms = ToolParameterConfig(enabled = FALSE)
#'   )
#' ))
#' @name ToolConfig
#' @export
ToolParameterConfig <- S7::new_class(
  "ToolParameterConfig",
  parent = ConfigNode,
  properties = list(
    description = config_description(),
    enabled = config_flag(),
    parameters = config_parameters(),
    schema = NULL | S7::class_list
  )
)

#' @rdname ToolConfig
#' @export
ToolConfig <- S7::new_class(
  "ToolConfig",
  parent = ConfigNode,
  properties = list(
    description = config_description(),
    instructions = config_description(),
    parameters = config_parameters()
  )
)

#' Configure a Console session
#'
#' `ConsoleConfig()` combines runtime selection, package preparation, worker
#' permissions, resolver settings, and tool presentation for [console_tool()].
#' Constructors have the same field names as Console's application configuration.
#'
#' An empty `ConsoleConfig()` uses built-in defaults without reading
#' global or project configuration files. Workers can read host files, write
#' private temporary storage, and cannot use the network. Dependency preparation
#' retains its independent built-in permissions and may download packages.
#'
#' @section Discovery and layering:
#' Prefer [discover_console_config()] to read files once into an inspectable
#' object, or [materialize_console_config()] to fill defaults without file reads.
#' [prune_console_config()] produces a compact view of nondefault settings.
#' [console_tool()] discovers files when `config` is omitted.
#'
#' The following launch-time discovery interface is retained for existing callers.
#' `ConfigDiscovery()` explicitly enables global and project file discovery.
#' Disable either source with `global = FALSE` or `project = FALSE`. Project
#' discovery reads `.agents/console/config.yaml` in `console_tool()`'s launch
#' directory; no ancestor directories are searched. Files can select runtimes,
#' child environments, and permissions, so discovery trusts the whole file.
#'
#' `NULL` fields are omitted: selected files supply their values before native
#' defaults apply. Runtime and environment mappings follow Console's recursive
#' layering rules; supplied package lists replace inherited lists, including
#' `character()` for no optional startup packages. Python selections replace
#' the whole `python` node to avoid mixing existing and managed environments.
#' Explicit worker and resolver [SandboxPolicy()] objects replace their entire
#' permission nodes; they do not inherit grants from discovered files.
#'
#' `sandbox = FALSE` disables enforcement for workers and dependency preparation,
#' clearing any discovered permission nodes. An explicit resolver policy cannot
#' be combined with this choice. Host execution has no native runner guarantee
#' of descendant cleanup.
#'
#' `as.list()` returns JSON-ready application settings. It excludes the launch
#' controls `discovery` and `sandbox = FALSE`; inspect those S7 properties with
#' `@`. Empty mappings, empty arrays, and omissions stay distinct.
#'
#' @param discovery `NULL` (no file discovery), or a [ConfigDiscovery()].
#' @param r `NULL` or an [RConfig()].
#' @param python `NULL`, an [ExistingPython()], a [ManagedPython()], or a
#'   [FirstAvailablePython()].
#' @param startup `NULL` or a [StartupConfig()] selecting a SQL connection.
#' @param resolver `NULL` or a [ResolverConfig()].
#' @param sandbox `NULL`, a [SandboxPolicy()], or `FALSE` for host execution.
#' @param languages `NULL` or a character vector selecting `"r"`, `"python"`,
#'   and/or `"sql"` tool inputs. This does not select the installed runtimes.
#' @param cache `NULL`, `"console"`, or `"host"` for dependency caches.
#' @param environment `NULL` or a named character vector of child environment
#'   assignments. Empty string values are allowed; `character()` is an empty
#'   mapping. Resolver assignments override worker assignments for preparation.
#' @param inherit_environment `NULL`, `TRUE`, or `FALSE`; controls inheritance
#'   of the trusted launch environment. Console retains its owned assignments.
#' @param tool `NULL` or a [ToolConfig()] customizing descriptions and parameter
#'   visibility.
#' @return An S7 configuration object. The constructor is also its S7 class.
#' @examples
#' ConsoleConfig()
#' ConsoleConfig(sandbox = FALSE)
#' ConsoleConfig(discovery = ConfigDiscovery(global = FALSE))
#' config <- ConsoleConfig(
#'   r = RConfig(packages = c("dplyr", "ggplot2"), resolution = "fixed"),
#'   python = ManagedPython(version = "3.13", packages = "pandas"),
#'   sandbox = SandboxPolicy(filesystem = SandboxFilesystem(read_write = "./output"))
#' )
#' as.list(config)
#' @name ConsoleConfig
#' @export
ConsoleConfig <- S7::new_class(
  "ConsoleConfig",
  parent = ConfigNode,
  properties = list(
    discovery = NULL | ConfigDiscovery,
    r = NULL | RConfig,
    python = NULL | PythonConfig,
    resolver = NULL | ResolverConfig,
    sandbox = S7::new_property(
      NULL | S7::class_logical | SandboxPolicy,
      validator = function(value) {
        if (is.logical(value) && !identical(value, FALSE)) {
          "must be NULL, FALSE, or a SandboxPolicy()"
        }
      }
    ),
    languages = config_string_list(),
    cache = config_choice(c("console", "host")),
    environment = config_environment(),
    inherit_environment = config_flag(),
    tool = NULL | ToolConfig,
    startup = NULL | StartupConfig
  ),
  validator = function(self) {
    if (
      identical(self@sandbox, FALSE) &&
        !is.null(self@resolver) &&
        !is.null(self@resolver@sandbox)
    ) {
      "resolver.sandbox cannot be supplied when sandbox = FALSE"
    }
  }
)

S7::method(as.list, ManagedPython) <- function(x, ...) {
  S7::validate(x)
  list(managed = config_mapping(S7::props(x)))
}

S7::method(print, ConsoleConfig) <- function(x, ...) {
  cat(
    "<ConsoleConfig>\n",
    jsonlite::toJSON(
      config_mapping(S7::props(x)),
      auto_unbox = TRUE,
      null = "null",
      pretty = TRUE,
      digits = NA
    ),
    "\n",
    sep = ""
  )
  invisible(x)
}

S7::method(as.list, ConsoleConfig) <- function(x, ...) {
  S7::validate(x)
  fields <- S7::props(x)
  fields$discovery <- NULL
  if (identical(x@sandbox, FALSE)) {
    fields$sandbox <- NULL
  }
  config_mapping(fields)
}

console_cli_arguments <- function(config, file, files = character()) {
  settings <- S7::props(config)
  discovery <- if (!is.null(settings$discovery)) S7::props(settings$discovery)
  resolver <- if (!is.null(settings$resolver)) S7::props(settings$resolver)
  flags <- if (
    is.null(discovery) || (!discovery$global && !discovery$project)
  ) {
    "--no-config"
  } else {
    c(
      if (!discovery$global) "--no-global-config",
      if (!discovery$project) "--no-project-config"
    )
  }
  disabled <- identical(settings$sandbox, FALSE)
  # Clear permission nodes before replacing them: ordinary map overlays merge.
  clears <- c(
    if (disabled || !is.null(settings$sandbox)) "sandbox",
    if (disabled || !is.null(resolver$sandbox)) "resolver.sandbox",
    if (!is.null(settings$python)) "python"
  )
  values <- as.list(config)
  overrides <- c(
    vapply(clears, function(key) paste0(key, "=null"), character(1)),
    unlist(Map(config_override, names(values), values), use.names = FALSE)
  )
  writeLines(
    as.character(jsonlite::toJSON(
      list(files = unname(as.list(files)), values = unname(as.list(overrides))),
      auto_unbox = TRUE
    )),
    file,
    useBytes = TRUE
  )
  c(
    flags,
    if (disabled) "--no-sandbox",
    "--config-overrides-file",
    file
  )
}
