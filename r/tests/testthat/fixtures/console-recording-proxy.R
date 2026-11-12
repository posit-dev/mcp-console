# Record the public R client's requests while using the real Console server.
# This fixture relays one request at a time and expects no server notifications.
args <- commandArgs(TRUE)
record <- args[[1L]]
binary <- Sys.getenv("MCP_CONSOLE_TEST_BINARY")
stopifnot(nzchar(binary), file.exists(binary))
server <- processx::process$new(
  binary,
  args[-1L],
  stdin = "|",
  stdout = "|",
  stderr = "",
  cleanup = TRUE,
  cleanup_tree = FALSE
)
input <- file("stdin", open = "r")
repeat {
  line <- readLines(input, n = 1L, warn = FALSE)
  if (!length(line)) {
    break
  }
  write(line, record, append = TRUE)
  request <- jsonlite::fromJSON(line, simplifyVector = FALSE)
  stopifnot(length(server$write_input(paste0(line, "\n"))) == 0L)
  if (is.null(request$id)) {
    next
  }
  repeat {
    server$poll_io(-1L)
    response <- server$read_output_lines()
    if (length(response)) {
      stopifnot(length(response) == 1L)
      cat(response, "\n", sep = "")
      flush(stdout())
      break
    }
    stopifnot(server$is_alive())
  }
}
close(input)
close(server$get_input_connection())
server$wait(15000)
stopifnot(!server$is_alive(), server$get_exit_status() == 0L)
