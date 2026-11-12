test_that("console_tools identifies the client in every request", {
  sandbox_test_binary()
  with_console_config_probe(
    function(path, record) {
      tools <- console_tools(
        path = path,
        config = ConsoleConfig(
          sandbox = FALSE,
          languages = "r",
          r = list(resolution = "disabled")
        )
      )
      result <- tools$send(r = "42L", timeout_ms = 60000L)
      expect_match(result@text, "42")
      messages <- lapply(
        readLines(record),
        jsonlite::fromJSON,
        simplifyVector = FALSE
      )
      requests <- Filter(function(message) !is.null(message$id), messages)
      expect_identical(
        vapply(requests, function(request) request$method, character(1)),
        c("initialize", "tools/list", "tools/call")
      )
      identity <- requests[[1L]]$params$clientInfo
      expect_identical(identity$name, "mcp.console")
      expect_identical(
        identity$version,
        as.character(utils::packageVersion("mcp.console"))
      )
      for (request in requests) {
        expect_identical(
          request$params$`_meta`$`io.modelcontextprotocol/clientInfo`,
          identity,
          info = request$method
        )
      }
      rm(tools)
      invisible(gc())
    },
    fixture = "console-recording-proxy.R"
  )
})
