# Two tool pages, neither named send, with one shared connection lifetime.
record <- commandArgs(TRUE)[[1L]]
input <- file("stdin", open = "r")
repeat {
  line <- readLines(input, n = 1L, warn = FALSE)
  if (!length(line)) {
    break
  }
  request <- jsonlite::fromJSON(line, simplifyVector = FALSE)
  if (is.null(request$id)) {
    next
  }
  result <- switch(
    request$method,
    initialize = list(protocolVersion = "2025-11-25"),
    `tools/list` = if (is.null(request$params$cursor)) {
      list(
        tools = list(list(
          name = "evaluate",
          description = "Evaluate values",
          inputSchema = list(
            type = "object",
            properties = list(
              values = list(
                type = list("array", "null"),
                items = list(type = "string")
              ),
              fields = list(type = "string"),
              metadata = list(type = "string"),
              arguments = list(type = "string"),
              client = list(type = list("string", "null")),
              extras = list(type = "object"),
              options = list(
                type = "object",
                properties = list(
                  labels = list(type = "array", items = list(type = "string")),
                  label = list(type = list("string", "null"))
                )
              )
            ),
            required = list("values")
          )
        )),
        nextCursor = "second-page"
      )
    } else {
      stopifnot(identical(request$params$cursor, "second-page"))
      list(
        tools = list(list(
          name = "inspect",
          inputSchema = list(
            type = "object",
            properties = structure(list(), names = character())
          )
        ))
      )
    },
    `tools/call` = list(
      content = list(list(
        type = "text",
        text = as.character(jsonlite::toJSON(
          request$params,
          auto_unbox = TRUE,
          null = "null"
        ))
      ))
    ),
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
writeLines("closed", paste0(record, ".closed"))
