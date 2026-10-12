#' Materialize Console configuration
#'
#' Compatibility alias for [materialize_console_config()]. Returns an ordinary
#' S7 [ConsoleConfig()] with complete defaults and tool presentation.
#' Use [discover_console_config()] to explicitly read optional configuration files,
#' [read_console_config()] for a required file, and [prune_console_config()] for
#' a compact view of nondefault settings.
#'
#' @inheritParams config-workflow
#' @return A [ConsoleConfig()] with file discovery disabled.
#' @export
resolve_console_config <- function(
  config = ConsoleConfig(),
  ...,
  path = NULL,
  version = NULL,
  project = getwd()
) {
  materialize_console_config(
    config,
    ...,
    path = path,
    version = version,
    project = project
  )
}

#' Discover, materialize, prune, and read Console configuration
#'
#' These operations return ordinary [ConsoleConfig()] objects. Discovery is an
#' explicit read of optional files; materialization fills in defaults and tool
#' presentation; pruning removes settings equal to the selected binary's defaults.
#' None starts interpreters, installs packages, or creates a session.
#'
#' `discover_console_config()` reads `global_file`, then `project_file`, then
#' applies `config`. Missing discovery files are skipped; malformed or unreadable
#' files fail. Set either file to `NULL` to skip it. Relative paths use `project`,
#' including paths inside the files. It returns a fully materialized config with
#' file discovery disabled, so later edits to those files do not affect launch.
#'
#' `materialize_console_config()` fills in the settings of a manually constructed
#' or pruned config. With no argument it materializes built-in defaults without
#' reading configuration files. `prune_console_config()` removes default-valued
#' fields recursively, retaining changed arrays, explicit empty values, and
#' overrides needed for conditional defaults. Both return the same S7 class.
#' Pruned omissions follow the defaults of the binary used for the next launch.
#'
#' `read_console_config()` reads one required JSON or YAML file and materializes
#' it without global or project discovery. Use `jsonlite::toJSON(as.list(config),
#' auto_unbox = TRUE)` to serialize application settings. The launch-only choice
#' `sandbox = FALSE` is not stored in that document; supply it again in `config`
#' when reading for host execution.
#'
#' [print()] shows every supplied property; `as.list()` provides a JSON-ready
#' view for serialization or comparison with [all.equal()]. Materialize to inspect
#' generated descriptions and signature metadata, or prune to review overrides.
#' Materialized descriptions are literal overrides: editing visibility or permissions
#' does not regenerate saved text. Set the affected description to `NULL` first.
#'
#' File discovery and environment inheritance are separate. For a scheduled job,
#' supply a config explicitly and set `inherit_environment = FALSE` to exclude
#' inherited worker/resolver variables. Interpreter selection, automatic package
#' lists, and runtime-owned cache paths remain launch policies; these operations
#' do not create a dependency lock or snapshot the process environment.
#'
#' See `vignette("autonomous-report", package = "mcp.console")` for a
#' scheduled-report workflow using these operations.
#'
#' @param config A [ConsoleConfig()]. In discovery and file reads, its settings
#'   override the selected files. Explicit permissions replace inherited permission
#'   nodes. Leave its legacy `discovery` property `NULL` for these operations.
#' @param global_file Optional global configuration path. Defaults to
#'   `config.yaml` in `MCP_CONSOLE_HOME`, or `~/.agents/console/config.yaml`.
#' @param project_file Optional project configuration path. Defaults to
#'   `.agents/console/config.yaml` in `project`.
#' @param file A required JSON or YAML configuration path.
#' @inheritParams console_tool
#' @return A [ConsoleConfig()] with file discovery disabled. Nested settings use
#'   the same typed constructors as manually written configurations.
#' @examples
#' \dontrun{
#' # Read once, inspect, then launch exactly this configuration.
#' config <- discover_console_config(global_file = NULL)
#' print(prune_console_config(config))
#' send <- console_tool(config = config)
#'
#' # Built-in defaults, without configuration-file discovery.
#' config <- materialize_console_config(ConsoleConfig(languages = "r"))
#' config@sandbox@filesystem <- SandboxFilesystem(read_write = "./reports")
#' config@tool@parameters$timeout_ms@enabled <- FALSE
#' config@tool@description <- "Run one R report cell."
#' print(prune_console_config(config))
#'
#' config <- read_console_config("job.json")
#' }
#' @name config-workflow
#' @export
materialize_console_config <- function(
  config = ConsoleConfig(),
  ...,
  path = NULL,
  version = NULL,
  project = getwd()
) {
  stopifnot(...length() == 0L)
  console_config_transform(config, "--full", path, version, project)
}

#' @rdname config-workflow
#' @export
prune_console_config <- function(
  config = ConsoleConfig(),
  ...,
  path = NULL,
  version = NULL,
  project = getwd()
) {
  stopifnot(...length() == 0L)
  console_config_transform(config, "--prune", path, version, project)
}

#' @rdname config-workflow
#' @export
discover_console_config <- function(
  ...,
  global_file = console_global_config_file(),
  project_file = ".agents/console/config.yaml",
  config = ConsoleConfig(),
  path = NULL,
  version = NULL,
  project = getwd()
) {
  stopifnot(
    ...length() == 0L,
    S7::S7_inherits(config, ConsoleConfig),
    is.null(config@discovery)
  )
  for (file in list(global_file, project_file)) {
    stopifnot(
      is.null(file) ||
        (is.character(file) &&
          length(file) == 1L &&
          !is.na(file) &&
          nzchar(file))
    )
  }
  console_config_transform(
    config,
    "--full",
    path,
    version,
    project,
    files = c(global_file, project_file)
  )
}

#' @rdname config-workflow
#' @export
read_console_config <- function(
  file,
  ...,
  config = ConsoleConfig(),
  path = NULL,
  version = NULL,
  project = getwd()
) {
  stopifnot(
    ...length() == 0L,
    is.character(file),
    length(file) == 1L,
    !is.na(file),
    nzchar(file),
    S7::S7_inherits(config, ConsoleConfig),
    is.null(config@discovery)
  )
  console_config_transform(
    config,
    "--full",
    path,
    version,
    project,
    file = file
  )
}

console_global_config_file <- function() {
  directory <- Sys.getenv("MCP_CONSOLE_HOME", unset = NA_character_)
  if (is.na(directory)) {
    directory <- Sys.getenv("HOME")
    if (!nzchar(directory) && .Platform$OS.type == "windows") {
      directory <- Sys.getenv("USERPROFILE")
    }
    if (!nzchar(directory)) {
      return(NULL)
    }
    directory <- file.path(directory, ".agents", "console")
  }
  if (!grepl("^(/|[A-Za-z]:[/\\\\]|\\\\\\\\)", directory)) {
    stop("Console home must be an absolute path", call. = FALSE)
  }
  file.path(directory, "config.yaml")
}

console_config_transform <- function(
  config,
  mode,
  path,
  version,
  project,
  files = character(),
  file = NULL
) {
  stopifnot(
    S7::S7_inherits(config, ConsoleConfig),
    is.character(project),
    length(project) == 1L,
    !is.na(project),
    dir.exists(project)
  )
  S7::validate(config)
  binary <- resolve_mcp_console(path, version)
  project <- normalizePath(project, mustWork = TRUE)
  transport <- tempfile("mcp-console-overrides-", fileext = ".json")
  on.exit(unlink(transport), add = TRUE)
  arguments <- console_cli_arguments(config, transport, files)
  if (!is.null(file)) {
    arguments <- c("--config-file", file, arguments[arguments != "--no-config"])
  }
  result <- processx::run(
    binary,
    c("config", "resolve", mode, "--format", "json", arguments),
    wd = project,
    error_on_status = FALSE
  )
  if (result$status != 0L) {
    stop(result$stderr, call. = FALSE)
  }
  settings <- jsonlite::fromJSON(result$stdout, simplifyVector = FALSE)
  if (identical(config@sandbox, FALSE)) {
    settings$sandbox <- FALSE
  }
  config_from_json(settings)
}

# Native capture validates the document before this conversion. Constructors
# share the same properties and serialization as hand-built configuration.
config_node_from_json <- function(class, fields, children = list()) {
  if (is.null(fields)) {
    return(NULL)
  }
  for (name in intersect(names(fields), names(children))) {
    fields[name] <- list(children[[name]](fields[[name]]))
  }
  for (name in intersect(
    names(fields),
    c(
      "read_only",
      "read_write",
      "deny",
      "allow",
      "unix_sockets",
      "packages",
      "languages"
    )
  )) {
    if (!is.null(fields[[name]])) {
      fields[[name]] <- as.character(unlist(fields[[name]], use.names = FALSE))
    }
  }
  if (!is.null(fields$environment)) {
    fields$environment <- structure(
      as.character(unlist(fields$environment, use.names = FALSE)),
      names = names(fields$environment)
    )
  }
  do.call(class, fields)
}

config_python_from_json <- function(value) {
  if (is.null(value)) {
    return(NULL)
  }
  if (!is.null(value$existing)) {
    return(ExistingPython(value$existing))
  }
  if (!is.null(value$managed)) {
    return(config_node_from_json(ManagedPython, value$managed))
  }
  FirstAvailablePython(lapply(value$first_available, function(candidate) {
    if (identical(candidate, "active_venv")) {
      candidate
    } else {
      config_python_from_json(candidate)
    }
  }))
}

config_from_json <- function(settings) {
  parameter <- function(fields) {
    config_node_from_json(
      ToolParameterConfig,
      fields,
      list(
        parameters = function(fields) lapply(fields, parameter)
      )
    )
  }
  policy <- function(fields) {
    if (identical(fields, FALSE)) {
      return(FALSE)
    }
    config_node_from_json(
      SandboxPolicy,
      fields,
      list(
        filesystem = function(fields) {
          config_node_from_json(SandboxFilesystem, fields)
        },
        network = function(fields) {
          if (is.character(fields)) {
            return(fields)
          }
          config_node_from_json(
            SandboxNetwork,
            fields,
            list(
              proxy = function(fields) {
                config_node_from_json(
                  SandboxProxy,
                  fields,
                  list(
                    domains = function(fields) {
                      config_node_from_json(SandboxDomains, fields)
                    }
                  )
                )
              },
              sockets = function(fields) {
                config_node_from_json(SandboxSockets, fields)
              }
            )
          )
        }
      )
    )
  }
  config_node_from_json(
    ConsoleConfig,
    settings,
    list(
      r = function(fields) config_node_from_json(RConfig, fields),
      python = config_python_from_json,
      startup = function(fields) config_node_from_json(StartupConfig, fields),
      sandbox = policy,
      resolver = function(fields) {
        config_node_from_json(ResolverConfig, fields, list(sandbox = policy))
      },
      tool = function(fields) {
        config_node_from_json(
          ToolConfig,
          fields,
          list(
            parameters = function(fields) lapply(fields, parameter)
          )
        )
      }
    )
  )
}
