# ir supplies one literal library in R_LIBS. R may already have expanded or
# dropped it from .libPaths(). Preserve unsafe spellings for the host to reject
# before a restart, even when the path is a symlink to a safe directory.
path <- base::Sys.getenv("R_LIBS")
forbidden <- base::paste0(base::commandArgs(trailingOnly = TRUE), collapse = "")
if (!base::any(base::charToRaw(path) %in% base::charToRaw(forbidden))) {
  path <- base::normalizePath(path, winslash = "/", mustWork = TRUE)
}
base::writeLines(path, sep = "", useBytes = TRUE)
