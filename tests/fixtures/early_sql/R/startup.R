.onLoad <- function(libname, pkgname) {
  if (!interactive()) {
    return(invisible())
  }
  if (nzchar(Sys.getenv("MCP_CONSOLE_TEST_SQL_STARTED"))) {
    assign("startup_sql_pid", Sys.getpid(), envir = globalenv())
    observed <- FALSE
    observe_connection <- function(connection) {
      stopifnot(DBI::dbIsValid(connection))
      DBI::dbExecute(
        connection,
        "CREATE TABLE IF NOT EXISTS startup_catalog AS SELECT 42 AS answer"
      )
      # DuckDB may use a temporary connection while constructing the driver.
      # Gate the first actual DBI connection and mark every returned catalog.
      if (!observed) {
        observed <<- TRUE
        behavior <- Sys.getenv("MCP_CONSOLE_TEST_SQL_BEHAVIOR")
        if (identical(behavior, "interrupt")) {
          readline("SQL warmup> ")
          return(invisible())
        }
        if (identical(behavior, "error")) {
          stop("optional SQL warmup failed")
        }
        if (!identical(behavior, "observe")) {
          return(invisible())
        }
        signal <- fifo(
          Sys.getenv("MCP_CONSOLE_TEST_SQL_STARTED"),
          open = "wb",
          blocking = TRUE
        )
        writeBin(charToRaw("1"), signal)
        close(signal)
        gate <- fifo(
          Sys.getenv("MCP_CONSOLE_TEST_SQL_RELEASE"),
          open = "rb",
          blocking = TRUE
        )
        stopifnot(identical(readBin(gate, "raw", 1L), charToRaw("1")))
        close(gate)
      }
    }
    suppressMessages(trace(
      "dbConnect",
      where = asNamespace("DBI"),
      print = FALSE,
      exit = bquote(.(observe_connection)(returnValue()))
    ))
    behavior <- Sys.getenv("MCP_CONSOLE_TEST_SQL_BEHAVIOR")
    if (startsWith(behavior, "setup-")) {
      state <- globalenv()
      state$startup_setup_connections <- list()
      state$startup_allow_sql_setup <- FALSE
      observe_setup <- function(conn, statement) {
        if (!identical(statement, "SET enable_progress_bar = false")) {
          return(invisible())
        }
        state$startup_setup_connections <- c(
          state$startup_setup_connections,
          list(conn)
        )
        if (state$startup_allow_sql_setup) {
          return(invisible())
        }
        if (startsWith(behavior, "setup-interrupt")) {
          state$startup_allow_sql_setup <- TRUE
          readline("SQL warmup> ")
        } else {
          stop("optional SQL warmup setup failed")
        }
      }
      suppressMessages(trace(
        "dbExecute",
        where = asNamespace("DBI"),
        print = FALSE,
        tracer = bquote(.(observe_setup)(conn, statement))
      ))
      if (endsWith(behavior, "disconnect-error")) {
        observe_disconnect <- function(conn) {
          if (
            length(state$startup_setup_connections) > 0L &&
              identical(conn, state$startup_setup_connections[[1L]])
          ) {
            stop("optional SQL disconnect failed")
          }
        }
        suppressMessages(trace(
          "dbDisconnect",
          where = asNamespace("DBI"),
          print = FALSE,
          exit = bquote(.(observe_disconnect)(conn))
        ))
      }
    }
    if (startsWith(behavior, "probe-interrupt-")) {
      target <- substring(behavior, nchar("probe-interrupt-") + 1L)
      probed <- FALSE
      observe_probe <- function(package) {
        if (identical(package, target) && !probed) {
          probed <<- TRUE
          readline("SQL warmup> ")
        }
      }
      suppressMessages(trace(
        "system.file",
        where = baseenv(),
        print = FALSE,
        tracer = bquote(.(observe_probe)(package))
      ))
    }
  }
}
