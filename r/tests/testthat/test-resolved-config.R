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

test_that("materialization and pruning retain relative and absolute cache directories", {
  binary <- Sys.getenv("MCP_CONSOLE_TEST_BINARY")
  skip_if(!nzchar(binary), "Set MCP_CONSOLE_TEST_BINARY to the checkout binary")
  project <- tempfile("console-config-")
  dir.create(project)
  project <- normalizePath(project, winslash = "/")
  on.exit(unlink(project, recursive = TRUE), add = TRUE)
  # Resolution returns native absolute paths without creating the cache.
  cache <- normalizePath(file.path(project, "cache"), mustWork = FALSE)
  for (directory in unique(c("cache", file.path(project, "cache"), cache))) {
    config <- materialize_console_config(
      ConsoleConfig(cache = list(directory = directory)),
      path = binary,
      project = project
    )
    expect_identical(config@cache, list(directory = cache))
    pruned <- prune_console_config(config, path = binary, project = project)
    expect_identical(as.list(pruned), list(cache = list(directory = cache)))
    expect_false(dir.exists(cache))
  }
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

test_that("materialization and pruning preserve optional read-only paths", {
  with_sandbox_directory({
    binary <- sandbox_test_binary()
    project <- normalizePath(getwd(), winslash = "/")
    paths <- file.path(project, c("existing", "missing"))
    dir.create(paths[[1]])
    for (optional_paths in list(paths, character())) {
      requested <- ConsoleConfig(
        sandbox = SandboxPolicy(
          filesystem = SandboxFilesystem(
            read_only = character(),
            read_write = character(),
            deny = character(),
            read_only_if_exists = optional_paths
          )
        )
      )
      config <- materialize_console_config(
        requested,
        path = binary,
        project = project
      )
      expect_identical(
        config@sandbox@filesystem@read_only_if_exists,
        optional_paths
      )
      pruned <- prune_console_config(config, path = binary, project = project)
      expect_identical(as.list(pruned), as.list(requested))
    }
    expect_false(file.exists(paths[[2]]))
  })
})
