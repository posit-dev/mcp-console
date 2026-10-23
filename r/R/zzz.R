#' @import S7
#' @importFrom reticulate uv_run_tool
#' @rawNamespace if (getRversion() < "4.3.0") importFrom("S7", "@")
NULL

.onLoad <- function(...) {
  S7::S7_on_load()
}

.onUnload <- function(...) {
  # S7 removes its load hooks, but currently leaves S3 registrations such as
  # as.list() in place. Reloading replaces this method, as for other S3 methods.
  S7::S7_on_unload()
}

S7::S7_on_build()
