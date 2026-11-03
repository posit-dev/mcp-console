# Minimal MCP transport fixture. Records launch flags and public configuration.
args <- commandArgs(TRUE)
writeLines(args[-1L], args[[1L]])
file <- match("--config-file", args)
if (!is.na(file)) {
  invisible(file.copy(
    args[[file + 1L]],
    paste0(args[[1L]], ".config.json"),
    overwrite = TRUE
  ))
}
writeLines(getwd(), paste0(args[[1L]], ".cwd"))
input <- file("stdin", open = "r")
repeat {
  line <- readLines(input, n = 1L, warn = FALSE)
  if (!length(line)) {
    break
  }
  request <- jsonlite::fromJSON(line)
  if (is.null(request$id)) {
    next
  }
  result <- switch(
    request$method,
    initialize = list(protocolVersion = "2025-11-25"),
    `tools/list` = list(
      tools = list(list(
        name = "send",
        description = "Test configuration transport",
        inputSchema = list(
          type = "object",
          properties = list(r = list(type = "string"))
        )
      ))
    ),
    `tools/call` = list(content = list(list(type = "text", text = "fixture"))),
    stop("Unexpected fixture request")
  )
  cat(
    as.character(jsonlite::toJSON(
      list(jsonrpc = "2.0", id = request$id, result = result),
      auto_unbox = TRUE
    )),
    "\n",
    sep = ""
  )
  flush(stdout())
}
close(input)
