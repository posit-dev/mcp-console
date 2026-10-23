.onAttach <- function(libname, pkgname) {
  if (!interactive()) {
    return(invisible(NULL))
  }
  sys.source(
    Sys.getenv("MCP_CONSOLE_TEST_BOOTSTRAP_SCRIPT"),
    envir = globalenv()
  )
}
