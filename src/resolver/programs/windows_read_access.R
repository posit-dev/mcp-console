# MXC denies R's impersonation-based ACL query even for readable files. Verify
# failed read/search checks with real opens/listing; MXC enforces every operation.
# Leave existence, write and file execute checks unchanged; never source profiles.
base::local({
  original <- base::file.access
  if (!base::isTRUE(base::attr(original, "mcp_console_read_probe"))) {
    readable <- function(path) {
      base::tryCatch(
        {
          if (base::dir.exists(path)) {
            base::length(base::suppressWarnings(base::list.files(
              path,
              all.files = TRUE,
              no.. = FALSE
            ))) >
              0L
          } else {
            connection <- base::suppressWarnings(base::file(path, open = "rb"))
            base::close(connection)
            TRUE
          }
        },
        error = function(error) FALSE
      )
    }
    access <- function(names, mode = 0L) {
      result <- original(names, mode)
      requested <- base::as.integer(mode)
      if (base::identical(requested, 4L) || base::identical(requested, 5L)) {
        for (index in base::seq_along(result)) {
          if (
            result[[index]] != 0L &&
              !base::is.na(names[[index]]) &&
              (requested == 4L || base::dir.exists(names[[index]])) &&
              readable(names[[index]])
          ) {
            result[[index]] <- 0L
          }
        }
      }
      result
    }
    base::attr(access, "mcp_console_read_probe") <- TRUE
    base::unlockBinding("file.access", base::baseenv())
    base::assign("file.access", access, envir = base::baseenv())
    base::lockBinding("file.access", base::baseenv())
  }
})
