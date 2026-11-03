test_that("loading fills configuration defaults without saving generated tool prose", {
  with_sandbox_directory({
    binary <- sandbox_test_binary()
    config <- load_console_config(
      global_file = NULL,
      project_file = NULL,
      overrides = list(sandbox = FALSE),
      path = binary
    )
    expect_identical(config@cache, "host")
    expect_identical(config@r@vanilla, FALSE)
    expect_identical(config@r@resolution, "implicit")
    expect_null(config@sessions@max)
    expect_null(config@tool)
    expect_identical(config@sandbox, FALSE)
  })
})

test_that("loading preserves explicit defaults and authored tool settings", {
  with_sandbox_directory({
    binary <- sandbox_test_binary()
    fields <- list(
      sessions = list(max = 2L),
      r = list(packages = character(), vanilla = FALSE),
      inherit_environment = TRUE,
      tool = list(
        description = "",
        parameters = list(timeout_ms = list(enabled = FALSE))
      )
    )
    config <- load_console_config(
      global_file = NULL,
      project_file = NULL,
      overrides = fields,
      path = binary
    )
    expect_equal(config@sessions@max, 2)
    expect_identical(config@r@packages, character())
    expect_identical(config@r@vanilla, FALSE)
    expect_true(config@inherit_environment)
    expect_identical(as.list(config)$tool, fields$tool)
  })
})

test_that("loading resolves cache paths without creating directories", {
  with_sandbox_directory({
    binary <- sandbox_test_binary()
    project <- normalizePath(getwd(), winslash = "/")
    cache <- normalizePath(file.path(project, "cache"), mustWork = FALSE)
    for (directory in unique(c("cache", file.path(project, "cache"), cache))) {
      config <- load_console_config(
        global_file = NULL,
        project_file = NULL,
        overrides = list(cache = list(directory = directory)),
        path = binary,
        project = project
      )
      expect_identical(config@cache, list(directory = cache))
      expect_false(dir.exists(cache))
    }
  })
})

test_that("loading retains the configured client inheritance policy", {
  with_sandbox_directory({
    binary <- sandbox_test_binary()
    for (profile in list(
      "inherit_codex",
      list(inherit_codex = list()),
      list(inherit_codex = list(fallback = "read_only")),
      list(inherit_codex = list(fallback = "workspace"))
    )) {
      requested <- ConsoleConfig(sandbox = list(profile = profile))
      config <- load_console_config(
        global_file = NULL,
        project_file = NULL,
        overrides = requested,
        path = binary
      )
      expect_identical(config@sandbox@profile, requested@sandbox@profile)
      expect_null(config@cache)
    }
  })
})

test_that("launch regenerates tool guidance after edits while retaining authored text", {
  with_sandbox_directory({
    binary <- sandbox_test_binary()
    metadata <- function(config) {
      tools <- console_tools(config = config, path = binary)
      result <- list(
        description = tools$send@description,
        arguments = tools$send@arguments
      )
      rm(tools)
      invisible(gc())
      result
    }
    fields <- list(
      r = list(resolution = "disabled"),
      languages = c("r", "python")
    )
    config <- load_console_config(
      global_file = NULL,
      project_file = NULL,
      overrides = fields,
      path = binary
    )
    initial <- metadata(config)
    config@sandbox@network <- "enabled"
    config@languages <- "r"
    config@tool <- list(parameters = list(timeout_ms = list(enabled = FALSE)))
    edited <- metadata(config)
    fresh <- metadata(ConsoleConfig(
      r = fields$r,
      languages = "r",
      sandbox = list(network = "enabled"),
      tool = list(parameters = list(timeout_ms = list(enabled = FALSE)))
    ))
    expect_identical(edited, fresh)
    expect_false(identical(edited$description, initial$description))
    expect_false("python" %in% names(edited$arguments@properties))
    expect_false("timeout_ms" %in% names(edited$arguments@properties))

    for (description in c("", initial$description)) {
      writeLines(
        as.character(jsonlite::toJSON(
          list(tool = list(description = description)),
          auto_unbox = TRUE
        )),
        "authored.json"
      )
      config <- load_console_config(
        global_file = NULL,
        project_file = "authored.json",
        overrides = fields,
        path = binary
      )
      config@sandbox@network <- "enabled"
      config@languages <- "r"
      config@tool@parameters <- list(timeout_ms = list(enabled = FALSE))
      expect_identical(metadata(config)$description, description)
    }
  })
})
