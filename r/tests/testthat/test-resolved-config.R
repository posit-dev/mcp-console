test_that("resolving configuration retains the complete editable defaults", {
  binary <- Sys.getenv("MCP_CONSOLE_TEST_BINARY")
  skip_if(!nzchar(binary), "Set MCP_CONSOLE_TEST_BINARY to the checkout binary")
  project <- tempfile("console-config-")
  dir.create(project)
  on.exit(unlink(project, recursive = TRUE), add = TRUE)

  config <- resolve_console_config(
    ConsoleConfig(
      discovery = ConfigDiscovery(global = FALSE, project = FALSE),
      sandbox = FALSE
    ),
    path = binary,
    project = project
  )
  expect_identical(config@cache, "host")
  expect_identical(config@r@vanilla, FALSE)
  expect_identical(config@r@resolution, "automatic")
  expect_true(nzchar(config@tool@description))
  expect_true(nzchar(config@tool@instructions))
  expect_identical(config@tool@parameters$timeout_ms@enabled, TRUE)
  expect_true(
    "integer" %in% unlist(config@tool@parameters$timeout_ms@schema$type)
  )
})
