#' Use MCP Console as ellmer tools
#'
#' `console_tools()` starts `mcp-console serve` and returns a named list of
#' [ellmer::ToolDef] objects for all tools advertised by the server. Discovery
#' does not require a tool named `send`. The tools share one server process and
#' its persistent R, Python, and DuckDB state.
#'
#' Explicit `NULL` values are preserved where the server schema permits null.
#' For `send` requirements, `NULL` package and constraint fields mean omission
#' unless `action = "set"`. Replacement declarations retain nullable values
#' so explicit clears reach the server.
#'
#' When the last tool is garbage collected, it closes the server's input to request
#' shutdown and waits up to 15 seconds before forcibly stopping the server.
#' With sandboxing enabled, the sandbox manager owns cleanup of worker
#' descendants; the R wrapper's fallback targets only the server process.
#' With `ConsoleConfig(sandbox = FALSE)`, worker descendants may survive
#' server shutdown.
#'
#' See `vignette("autonomous-report", package = "mcp.console")` for an explicit
#' configuration, permission edits, inspection, and an ellmer launch.
#'
#' @section Result shape:
#'
#' Successful calls return an [ellmer::ContentToolResult] whose value holds
#' the ordered text and image content.
#' Its `extra` carries the server's result `_meta` and, under
#' `io.modelcontextprotocol/serverInfo`, the identity the server reported in
#' the handshake, which compatible chat UIs can use to attribute results.
#' Errored results keep their existing unwrapped return shape.
#'
#' @section Executable resolution:
#'
#' * If `path` is supplied, `console_tools()` uses that executable directly.
#' * Otherwise, if `version` is supplied, it resolves that published version
#'   with [reticulate::uv_run_tool()] without consulting `PATH`.
#' * Otherwise, it uses `mcp-console` from `PATH` when available and falls back
#'   to the latest published version through [reticulate::uv_run_tool()].
#'
#' `path` and `version` are mutually exclusive.
#'
#' @param ... Must be empty. Reserved for future use.
#' @param config A [ConsoleConfig()]. By default, [load_console_config()]
#'   reads optional global/project files once and combines their settings.
#'   Pass `ConsoleConfig()` to skip files and use built-in defaults: host reads,
#'   private temporary writes, and no worker networking. Resolver defaults are
#'   independent. A supplied config never reads additional configuration files.
#'   The calling R installation is the default unless `r.executable` selects
#'   another installation. Explicit `NULL` is equivalent to
#'   `ConsoleConfig()`. Use the same `path`, `version`, and `project` when
#'   inspecting and launching a config.
#' @param project Existing directory to use as the child process's working
#'   directory, defaulting to `getwd()`. Relative config and runtime paths use
#'   this directory; the caller's working directory is unchanged.
#' @param path `NULL`, or a path to an `mcp-console` executable.
#' @param version `NULL`, or one published `mcp-console` version, such as
#'   `"0.0.2"`.
#' @return `console_tools()` returns a named list of [ellmer::ToolDef] objects
#'   to pass to an ellmer chat's `$register_tools()` method. Select a tool by
#'   name from the list for direct calls.
#' @examples
#' \dontrun{
#' chat <- ellmer::chat_openai()
#' chat$register_tools(console_tools())
#' chat$chat(
#'   "Tell me something interesting about mtcars. Use the console as a workbench."
#' )
#'
#' console_tools(path = Sys.which("mcp-console"))
#' console_tools(version = "0.0.2")
#' }
#' @export
console_tools <- function(
  config = load_console_config(
    project = project,
    path = path,
    version = version
  ),
  ...,
  project = getwd(),
  path = NULL,
  version = NULL
) {
  if (...length() != 0L) {
    stop("`...` must be empty.", call. = FALSE)
  }
  if (is.null(config)) {
    config <- ConsoleConfig()
  }
  if (!S7::S7_inherits(config, ConsoleConfig)) {
    stop("`config` must be NULL or a ConsoleConfig() object.", call. = FALSE)
  }
  S7::validate(config)
  if (
    !is.character(project) ||
      length(project) != 1L ||
      is.na(project) ||
      !nzchar(project) ||
      !dir.exists(project)
  ) {
    stop("`project` must identify an existing directory.", call. = FALSE)
  }
  project <- normalizePath(project, mustWork = TRUE)
  directory <- tempfile("mcp-console-config-")
  stopifnot(dir.create(directory))
  on.exit(unlink(directory, recursive = TRUE), add = TRUE)
  client <- new_mcp_client(
    resolve_mcp_console(path, version),
    console_cli_arguments(config, directory),
    project
  )
  ready <- FALSE
  on.exit(if (!ready) close_mcp_client(client), add = TRUE)

  initialized <- mcp_request(
    client,
    "initialize",
    list(
      protocolVersion = "2025-11-25",
      capabilities = json_object(),
      clientInfo = list(
        name = "mcp.console",
        version = as.character(utils::packageVersion("mcp.console"))
      )
    )
  )
  # Legacy tool results carry no identity; the handshake is its only source.
  client$server_info <- initialized$serverInfo
  mcp_write(client, list(jsonrpc = "2.0", method = "notifications/initialized"))

  metadata <- list()
  params <- NULL
  repeat {
    page <- mcp_request(client, "tools/list", params)
    metadata <- c(metadata, page$tools)
    if (is.null(page$nextCursor)) {
      break
    }
    params <- list(cursor = page$nextCursor)
  }
  tools <- lapply(metadata, mcp_ellmer_tool, client = client)
  names(tools) <- vapply(metadata, \(tool) tool$name, character(1))
  ready <- TRUE
  tools
}

mcp_ellmer_tool <- function(metadata, client) {
  force(client)
  properties <- metadata$inputSchema$properties
  fields <- names(properties)
  required <- fields %in%
    unlist(metadata$inputSchema$required, use.names = FALSE)
  omitted <- new.env(parent = emptyenv())

  invoke <- function(...) {
    arguments <- list(...)
    arguments <- arguments[!vapply(arguments, identical, logical(1), omitted)]
    requirements <- arguments$requirements
    if (
      identical(metadata$name, "send") &&
        is.list(requirements) &&
        !identical(requirements$action, "set")
    ) {
      # Strict-mode NULL placeholders mean omission for requirements operations.
      # Only set can clear nullable constraints; other values reach validation.
      arguments$requirements <- requirements[
        !vapply(requirements, is.null, logical(1))
      ]
    }
    result <- mcp_request(
      client,
      "tools/call",
      list(
        name = metadata$name,
        arguments = mcp_argument_value(arguments, metadata$inputSchema)
      )
    )
    mcp_contents(result, client$server_info)
  }
  arguments <- rep(list(omitted), length(fields))
  arguments[required] <- rep(alist(argument = ), sum(required))
  names(arguments) <- fields
  call <- function() NULL
  formals(call) <- arguments
  # Embed the dispatcher so advertised argument names cannot shadow its state.
  values <- lapply(fields, as.name)
  names(values) <- fields
  body(call) <- as.call(c(list(invoke), values))

  ellmer::tool(
    call,
    name = metadata$name,
    # The adapter owns schema conversion, including explicit nullable arguments.
    convert = FALSE,
    # shinychat reads the definition title for multi-call group headers.
    annotations = if (is.null(metadata$title)) {
      list()
    } else {
      ellmer::tool_annotations(title = metadata$title)
    },
    description = if (is.null(metadata$description)) {
      ""
    } else {
      metadata$description
    },
    arguments = Map(
      \(schema, required) {
        ConsoleToolArgument(
          json = schema,
          required = required
        )
      },
      properties,
      required
    )
  )
}

ConsoleToolArgument <- S7::new_class(
  "ConsoleToolArgument",
  parent = ellmer::TypeJsonSchema
)

# ellmer's serializer and OpenAI provider family are internal. Extend only our
# schema type so strict-mode adjustments leave other providers' schemas intact.
ellmer_as_json <- S7::new_external_generic(
  "ellmer",
  "as_json",
  c("provider", "x")
)
S7::method(
  ellmer_as_json,
  list(ellmer::Provider, ConsoleToolArgument)
) <- function(provider, x, ...) {
  if (
    inherits(provider, "ellmer::ProviderOpenAICompatible") &&
      provider@strict
  ) {
    return(ellmer_argument_schema(x@json, x@required))
  }
  x@json
}

mcp_argument_value <- function(value, schema) {
  if (is.null(value)) {
    return(NULL)
  }
  types <- unlist(schema$type, use.names = FALSE)
  if ("array" %in% types) {
    return(unname(lapply(as.list(value), mcp_argument_value, schema$items)))
  }
  if ("object" %in% types) {
    properties <- lapply(names(value), \(name) schema$properties[[name]])
    required <- names(value) %in% unlist(schema$required, use.names = FALSE)
    nullable <- vapply(
      properties,
      function(property) {
        is.null(property) ||
          "null" %in% unlist(property$type, use.names = FALSE)
      },
      logical(1)
    )
    keep <- required | nullable | !vapply(value, is.null, logical(1))
    value <- Map(mcp_argument_value, value[keep], properties[keep])
    return(json_object(value))
  }
  value
}

ellmer_argument_schema <- function(schema, required) {
  types <- unlist(schema$type, use.names = FALSE)
  if (!required) {
    types <- setdiff(types, "null")
  }
  schema$type <- if (length(types) == 1L) types[[1L]] else as.list(types)

  if (!is.null(schema$enum)) {
    schema$enum <- unique(c(as.list(schema$enum), list(NULL)))
  }

  if (!"object" %in% types) {
    return(schema)
  }

  required <- unlist(schema$required, use.names = FALSE)
  optional <- !names(schema$properties) %in% required
  schema$properties[optional] <- lapply(
    schema$properties[optional],
    function(property) {
      property$type <- unique(c(as.list(property$type), "null"))
      if (!is.null(property$enum)) {
        property$enum <- unique(c(as.list(property$enum), list(NULL)))
      }
      property
    }
  )
  schema$required <- as.list(names(schema$properties))
  schema
}

resolve_mcp_console <- function(path, version) {
  if (!is.null(path) && !is.null(version)) {
    stop("Only one of `path` and `version` may be supplied.", call. = FALSE)
  }
  if (!is.null(path)) {
    if (
      !is.character(path) || length(path) != 1L || is.na(path) || !nzchar(path)
    ) {
      stop("`path` must be NULL or one non-empty string.", call. = FALSE)
    }
    if (!file.exists(path) || isTRUE(file.info(path)$isdir)) {
      stop("`path` must identify an existing file.", call. = FALSE)
    }
    path <- normalizePath(path, mustWork = TRUE)
    if (file.access(path, mode = 1L) != 0L) {
      stop("`path` must identify an executable file.", call. = FALSE)
    }
    return(path)
  }

  if (
    !is.null(version) &&
      (!is.character(version) ||
        length(version) != 1L ||
        is.na(version) ||
        !nzchar(version))
  ) {
    stop("`version` must be NULL or one non-empty string.", call. = FALSE)
  }

  if (!is.null(version)) {
    return(resolve_mcp_console_uv(paste0("mcp-console==", version)))
  }

  binary <- unname(Sys.which("mcp-console"))
  if (nzchar(binary)) {
    return(normalizePath(binary, mustWork = TRUE))
  }

  resolve_mcp_console_uv("mcp-console")
}

resolve_mcp_console_uv <- function(from) {
  code <- paste(
    "import os, pathlib, sys",
    "name = 'mcp-console.exe' if os.name == 'nt' else 'mcp-console'",
    "print(pathlib.Path(sys.executable).with_name(name).resolve())",
    sep = "; "
  )
  errors <- tempfile("mcp-console-uv-", fileext = ".log")
  on.exit(unlink(errors), add = TRUE)
  output <- suppressWarnings(uv_run_tool(
    "python",
    c("-c", shQuote(code)),
    from = from,
    stdout = TRUE,
    stderr = errors
  ))

  status <- attr(output, "status")
  if (!is.null(status) && status != 0L) {
    stop_with_errors(
      "Could not resolve mcp-console with reticulate::uv_run_tool().",
      errors
    )
  }
  output <- output[nzchar(output)]
  if (length(output) == 0L || !file.exists(utils::tail(output, 1L))) {
    stop(
      "reticulate::uv_run_tool() did not return an mcp-console path.",
      call. = FALSE
    )
  }
  normalizePath(utils::tail(output, 1L), mustWork = TRUE)
}

new_mcp_client <- function(binary, arguments, project) {
  client <- new.env(parent = emptyenv())
  client$errors <- tempfile("mcp-console-", fileext = ".log")
  client$process <- processx::process$new(
    binary,
    c("serve", arguments),
    wd = project,
    stdin = "|",
    stdout = "|",
    stderr = client$errors,
    cleanup = FALSE,
    cleanup_tree = FALSE,
    encoding = "UTF-8"
  )
  client$id <- 1L
  client$output <- ""
  client$abandoned <- integer()
  reg.finalizer(client, close_mcp_client, onexit = TRUE)
  client
}

close_mcp_client <- function(client) {
  process <- client$process
  on.exit(suspendInterrupts({
    if (!is.null(process)) {
      try(
        if (process$is_alive()) {
          ps::ps_send_signal(process$as_ps_handle(), 9L)
        },
        silent = TRUE
      )
      try(process$wait(1000), silent = TRUE)
    }
    unlink(client$errors)
  }))
  client$process <- NULL
  if (!is.null(process)) {
    try(close(process$get_input_connection()), silent = TRUE)
    try(process$wait(15000), silent = TRUE)
  }
  invisible()
}

mcp_request <- function(client, method, params = NULL) {
  id <- client$id
  client$id <- id + 1L
  message <- list(jsonrpc = "2.0", id = id, method = method)
  if (!is.null(params)) {
    message$params <- params
  }
  complete <- FALSE
  on.exit(if (!complete) close_mcp_client(client), add = TRUE)
  mcp_write(client, message)

  response <- tryCatch(
    mcp_read(client, id),
    interrupt = function(condition) {
      client$abandoned <- c(client$abandoned, id)
      cancelled <- tryCatch(
        {
          mcp_write(
            client,
            list(
              jsonrpc = "2.0",
              method = "notifications/cancelled",
              params = list(
                requestId = id,
                reason = "User interrupted the request."
              )
            )
          )
          TRUE
        },
        error = function(...) FALSE
      )
      if (cancelled) {
        complete <<- TRUE
      }
      stop(condition)
    }
  )
  complete <- TRUE
  if (!is.null(response$error)) {
    stop(
      sprintf(
        "mcp-console JSON-RPC error %s: %s",
        response$error$code,
        response$error$message
      ),
      call. = FALSE
    )
  }
  response$result
}

mcp_write <- function(client, message) {
  process <- client$process
  if (is.null(process) || !process$is_alive()) {
    stop_with_errors("mcp-console is not running.", client$errors)
  }
  payload <- paste0(
    as.character(jsonlite::toJSON(
      message,
      auto_unbox = TRUE,
      null = "null",
      na = "null",
      digits = NA
    )),
    "\n"
  )

  remaining <- process$write_input(payload)
  while (length(remaining) > 0L) {
    mcp_pump(client, 100)
    if (!process$is_alive()) {
      stop_with_errors(
        "mcp-console stopped while receiving a request.",
        client$errors
      )
    }
    remaining <- process$write_input(remaining)
  }
  invisible()
}

mcp_read <- function(client, id) {
  repeat {
    newline <- regexpr("\n", client$output, fixed = TRUE)[[1L]]
    if (newline > 0L) {
      line <- substr(client$output, 1L, newline - 1L)
      client$output <- substr(client$output, newline + 1L, nchar(client$output))
      response <- jsonlite::fromJSON(
        sub("\r$", "", line),
        simplifyVector = FALSE
      )
      if (identical(response$id, id)) {
        return(response)
      }
      stale <- match(response$id, client$abandoned, nomatch = 0L)
      if (stale > 0L) {
        client$abandoned <- client$abandoned[-stale]
        next
      }
      stop("mcp-console sent an unexpected JSON-RPC message.", call. = FALSE)
    }

    # processx checks pending R interrupts while waiting indefinitely.
    mcp_pump(client, -1L)
    if (is.null(client$process) || !client$process$is_alive()) {
      mcp_pump(client, 0)
      if (regexpr("\n", client$output, fixed = TRUE)[[1L]] < 0L) {
        stop_with_errors("mcp-console stopped before replying.", client$errors)
      }
    }
  }
}

mcp_pump <- function(client, timeout) {
  process <- client$process
  if (is.null(process)) {
    return(invisible())
  }
  process$poll_io(timeout)
  output <- process$read_output()
  if (length(output) > 0L && nzchar(output)) {
    client$output <- paste0(client$output, output)
  }
  invisible()
}

mcp_contents <- function(result, server_info) {
  if (!is.null(result$structuredContent$requirements)) {
    return(ellmer::ContentText(as.character(jsonlite::toJSON(
      result$structuredContent,
      auto_unbox = TRUE,
      null = "null",
      pretty = TRUE
    ))))
  }
  content <- result$content
  content <- lapply(content, function(x) {
    if (identical(x$type, "text")) {
      ellmer::ContentText(x$text)
    } else if (identical(x$type, "image")) {
      ellmer::ContentImageInline(type = x$mimeType, data = x$data)
    } else {
      stop("mcp-console returned an unsupported content type.", call. = FALSE)
    }
  })

  # ellmer cannot attach images to an errored tool result. MCP Console includes
  # the failure text in `content`, so pass the complete ordered content through
  # in its unwrapped shape.
  if (isTRUE(result$isError)) {
    if (length(content) == 0L) {
      content <- list(ellmer::ContentText("mcp-console tool call failed."))
    }
    return(if (length(content) == 1L) content[[1L]] else content)
  }

  # Successful results return a ContentToolResult so `extra` can carry the
  # result `_meta` and the server's identity. A modern server stamps the
  # identity on each result; this legacy client supplies the one from the
  # handshake under the same key. Presentation belongs to chat UIs.
  extra <- as.list(result[["_meta"]])
  extra[["io.modelcontextprotocol/serverInfo"]] <- server_info
  ellmer::ContentToolResult(
    value = if (length(content) == 1L) content[[1L]] else content,
    extra = extra
  )
}

json_object <- function(x = list()) {
  if (length(x) == 0L) structure(x, names = character()) else x
}

stop_with_errors <- function(message, path) {
  errors <- if (file.exists(path)) {
    utils::tail(readLines(path, warn = FALSE), 20L)
  } else {
    character()
  }
  if (length(errors) > 0L) {
    message <- paste(c(message, "", errors), collapse = "\n")
  }
  stop(message, call. = FALSE)
}
