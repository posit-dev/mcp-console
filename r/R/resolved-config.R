#' Inspect and edit resolved Console configuration
#'
#' `resolve_console_config()` asks the selected Console executable to capture
#' configuration and generate its complete tool presentation. It does not start
#' interpreters, install packages, or create a session. The returned named list
#' prints in full and supports ordinary `$` edits.
#'
#' Pass the result to [console_tool()] to use its captured executable, project,
#' permissions, and settings without discovering configuration files again.
#' Do not also supply `path`, `version`, or `project` to that call.
#'
#' Tool descriptions and parameter descriptions are literal strings. Setting a
#' parameter's `enabled` to `FALSE` hides it without rewriting the materialized
#' text. The parameter's `schema` contains read-only types and constraints;
#' nested fields live in `parameters`. Use `enabled = FALSE` to hide a field;
#' deleting a setting with `NULL` restores that setting's native default.
#'
#' Runtime-dependent choices remain policies: interpreter discovery, inherited
#' process environment, automatic package lists, and runtime-created cache and
#' temporary paths are resolved at launch. This object is not a dependency lock.
#'
#' @param config A [ConsoleConfig()] supplying settings and optional file discovery.
#' @inheritParams console_tool
#' @return A `resolved_console_config` named list. [as.list()] returns its
#'   JSON-ready settings without the retained launch attributes. [print()] shows
#'   all settings and tool metadata without truncating descriptions.
#' @examples
#' \dontrun{
#' config <- resolve_console_config(ConsoleConfig(languages = "r"))
#' print(config)
#' config$tool$description <- "Run one R cell in a persistent workbench."
#' config$tool$parameters$timeout_ms$enabled <- FALSE
#' send <- console_tool(config = config)
#' }
#' @export
resolve_console_config <- function(
  config = ConsoleConfig(),
  ...,
  path = NULL,
  version = NULL,
  project = getwd()
) {
  stopifnot(
    ...length() == 0L,
    S7::S7_inherits(config, ConsoleConfig),
    is.character(project),
    length(project) == 1L,
    !is.na(project),
    dir.exists(project)
  )
  S7::validate(config)
  binary <- resolve_mcp_console(path, version)
  project <- normalizePath(project, mustWork = TRUE)
  result <- processx::run(
    binary,
    c(
      "config",
      "resolve",
      "--full",
      "--format",
      "json",
      console_cli_arguments(config)
    ),
    wd = project,
    error_on_status = FALSE
  )
  if (result$status != 0L) {
    stop(result$stderr, call. = FALSE)
  }
  settings <- jsonlite::fromJSON(result$stdout, simplifyVector = FALSE)
  structure(
    settings,
    class = c("resolved_console_config", "list"),
    launch = list(
      path = binary,
      project = project,
      no_sandbox = identical(config@sandbox, FALSE)
    )
  )
}

#' @rdname resolve_console_config
#' @param x A resolved configuration.
#' @export
as.list.resolved_console_config <- function(x, ...) {
  attributes(x) <- list(names = names(x))
  x
}

#' @rdname resolve_console_config
#' @export
print.resolved_console_config <- function(x, ...) {
  cat(
    jsonlite::toJSON(
      as.list(x),
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
