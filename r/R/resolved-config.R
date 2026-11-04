#' Load Console configuration files
#'
#' Read global and project settings once, apply explicit overrides, and return
#' an ordinary [ConsoleConfig()] that can be inspected, edited, and passed to
#' [console_tools()]. Supplying that object never reads configuration files again.
#'
#' The default locations are optional. An explicitly supplied non-NULL path must
#' exist. Malformed, unreadable, and non-file sources are errors in either case.
#' Set either path to `NULL` to skip it. Relative filenames and configured paths
#' use `project`; `~` follows Console's launch-home convention, not R's `R_USER`.
#'
#' The loader fills configuration defaults without starting interpreters or
#' preparing runtime packages. Generated tool descriptions and schema metadata
#' are excluded from the editable configuration. Authored tool settings, including
#' empty descriptions and text equal to a generated default, remain explicit.
#' Tool presentation is generated from the final configuration at launch.
#'
#' Explicit worker/resolver permissions and Python selections replace their
#' corresponding inherited nodes. Other mappings merge recursively.
#' The calling R installation is a low-priority default; file settings and
#' overrides can select another executable. A file's `r: null` or
#' `r.executable: null` clears that preference and retains native R discovery
#' when the loaded object is edited, exported with `as.list()`, reloaded, or
#' launched.
#'
#' File loading does not capture inherited environment variables or install a
#' dependency lock. `inherit_environment` controls worker/resolver inheritance,
#' independently of configuration-file discovery.
#'
#' @param global_file Global YAML or JSON path. Defaults to `config.yaml` under
#'   `MCP_CONSOLE_HOME`, or `~/.agents/console/config.yaml` when unset.
#' @param project_file Project YAML or JSON path. Defaults to
#'   `file.path(project, ".agents", "console", "config.yaml")`. An explicitly
#'   supplied relative path is relative to `project`.
#' @param overrides `NULL`, a [ConsoleConfig()], or a named mapping applied after
#'   the files.
#' @inheritParams console_tools
#' @return A [ConsoleConfig()] with the loaded settings and configuration defaults.
#' @examples
#' \dontrun{
#' config <- load_console_config()
#' config@sandbox@filesystem <- list(read_write = "./reports")
#' tools <- console_tools(config = config)
#'
#' # Read one required file without the global settings.
#' config <- load_console_config(global_file = NULL, project_file = "report.yaml")
#' }
#' @export
load_console_config <- function(
  project = getwd(),
  global_file = file.path(
    Sys.getenv("MCP_CONSOLE_HOME", unset = "~/.agents/console"),
    "config.yaml"
  ),
  project_file = file.path(project, ".agents", "console", "config.yaml"),
  overrides = NULL,
  path = NULL,
  version = NULL
) {
  required_files <- c(
    if (!missing(global_file)) global_file,
    if (!missing(project_file)) project_file
  )
  if (missing(global_file)) {
    global_file <- console_global_config_file()
  }
  if (
    !is.character(project) ||
      length(project) != 1L ||
      is.na(project) ||
      !dir.exists(project)
  ) {
    stop("`project` must identify an existing directory.", call. = FALSE)
  }
  # Normalize before evaluating project_file's default so a relative project is
  # not interpreted twice when Console reads files from its working directory.
  project <- normalizePath(project, mustWork = TRUE)
  for (name in c("global_file", "project_file")) {
    file <- get(name)
    if (
      !is.null(file) &&
        (!is.character(file) ||
          length(file) != 1L ||
          is.na(file) ||
          !nzchar(file))
    ) {
      stop(
        sprintf("`%s` must be NULL or one non-empty path.", name),
        call. = FALSE
      )
    }
  }
  if (is.null(overrides)) {
    overrides <- ConsoleConfig()
  } else if (!S7::S7_inherits(overrides, ConsoleConfig)) {
    overrides <- ConsoleConfig(overrides)
  }
  S7::validate(overrides)
  binary <- resolve_mcp_console(path, version)
  directory <- tempfile("mcp-console-config-")
  stopifnot(dir.create(directory))
  on.exit(unlink(directory, recursive = TRUE), add = TRUE)
  arguments <- console_cli_arguments(
    overrides,
    directory,
    files = c(global_file, project_file),
    required_files = required_files
  )
  result <- processx::run(
    binary,
    c(
      "config",
      "resolve",
      "--full",
      "--editable",
      "--format",
      "json",
      arguments
    ),
    wd = project,
    error_on_status = FALSE
  )
  if (result$status != 0L) {
    stop(result$stderr, call. = FALSE)
  }
  settings <- jsonlite::fromJSON(result$stdout, simplifyVector = FALSE)
  if (identical(overrides@sandbox, FALSE)) {
    settings$sandbox <- FALSE
  }
  config <- ConsoleConfig(settings)
  # Native capture already applied the calling-R preference. Its absence in
  # the materialized result therefore records an explicit reset, unlike NULL
  # in pure construction. The serializer carries this provenance as an explicit
  # r.executable null unless the executable has since been selected.
  if (is.null(config@r@executable)) {
    attr(config, "r_executable_reset") <- TRUE
  }
  config
}

console_global_config_file <- function() {
  directory <- Sys.getenv("MCP_CONSOLE_HOME", unset = NA_character_)
  if (is.na(directory)) {
    directory <- Sys.getenv("HOME")
    if (!nzchar(directory) && .Platform$OS.type == "windows") {
      directory <- Sys.getenv("USERPROFILE")
    }
    if (!nzchar(directory)) {
      if (.Platform$OS.type != "windows") {
        return(NULL)
      }
      # Native ~ expansion can still use Windows user-profile discovery.
      directory <- "~"
    }
    directory <- file.path(directory, ".agents", "console")
  }
  # Leave leading-~ expansion to native capture, independently of R_USER.
  home_relative <- grepl(
    if (.Platform$OS.type == "windows") "^~([/\\\\]|$)" else "^~(/|$)",
    directory
  )
  absolute <- if (.Platform$OS.type == "windows") {
    grepl("^([A-Za-z]:[/\\\\]|[/\\\\]{2}[^/\\\\]+[/\\\\][^/\\\\]+)", directory)
  } else {
    startsWith(directory, "/")
  }
  if (!home_relative && !absolute) {
    stop("MCP_CONSOLE_HOME must be an absolute path", call. = FALSE)
  }
  file.path(directory, "config.yaml")
}
