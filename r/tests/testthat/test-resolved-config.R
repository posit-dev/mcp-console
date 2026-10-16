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
  expect_identical(config@r@resolution, "implicit")
  expect_true(nzchar(config@tool@description))
  expect_true(nzchar(config@tool@instructions))
  expect_identical(config@tool@parameters$timeout_ms@enabled, TRUE)
  expect_true(
    "integer" %in% unlist(config@tool@parameters$timeout_ms@schema$type)
  )
})

test_that("materialization and pruning retain client sandbox inheritance", {
  binary <- Sys.getenv("MCP_CONSOLE_TEST_BINARY")
  skip_if(!nzchar(binary), "Set MCP_CONSOLE_TEST_BINARY to the checkout binary")
  project <- tempfile("console-config-")
  dir.create(project)
  on.exit(unlink(project, recursive = TRUE), add = TRUE)

  for (profile in list(
    "inherit_codex",
    InheritCodexSandboxProfile(),
    InheritCodexSandboxProfile(fallback = "read_only"),
    InheritCodexSandboxProfile(fallback = "workspace")
  )) {
    requested <- SandboxPolicy(profile = profile)
    config <- materialize_console_config(
      ConsoleConfig(sandbox = requested),
      path = binary,
      project = project
    )
    expect_identical(config@sandbox@profile, profile)
    expect_null(config@cache)
    pruned <- prune_console_config(config, path = binary, project = project)
    expect_identical(as.list(pruned)$sandbox, as.list(requested))
  }
})
