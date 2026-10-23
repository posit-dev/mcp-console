.onLoad <- function(libname, pkgname) {
  cat(
    paste0(
      "namespace:",
      paste(commandArgs(trailingOnly = TRUE), collapse = " "),
      "\n"
    ),
    file = Sys.getenv("MCP_CONSOLE_TEST_RETICULATE_RECORD"),
    append = TRUE
  )
}

uv_binary <- function() {
  cat(
    "uv_binary\n",
    file = Sys.getenv("MCP_CONSOLE_TEST_RETICULATE_RECORD"),
    append = TRUE
  )
  stop("fixture ambient reticulate bootstrap failed", call. = FALSE)
}
