test_that("the public R API contains the two makers and tool adapter", {
  expect_setequal(
    getNamespaceExports("mcp.console"),
    c("ConsoleConfig", "load_console_config", "console_tools")
  )
  expect_identical(ConsoleConfig@name, "ConsoleConfig")
})

test_that("configuration validates runtime, cache, and session choices", {
  cache <- file.path(tempdir(), "console cache")
  expect_identical(
    as.list(ConsoleConfig(cache = list(directory = cache))),
    list(cache = list(directory = cache))
  )
  expect_identical(
    as.list(ConsoleConfig(cache = "managed")),
    list(cache = "managed")
  )
  expect_error(ConsoleConfig(cache = cache), "cache")
  expect_error(ConsoleConfig(cache = list(directory = "")), "cache")
  expect_error(ConsoleConfig(cache = list(path = cache)), "cache")
  expect_identical(
    as.list(ConsoleConfig(sessions = list(max = 3L))),
    list(sessions = list(max = 3L))
  )
  for (value in list(
    0,
    -1,
    1.5,
    Inf,
    NA_real_,
    numeric(),
    c(1, 2),
    TRUE,
    "2"
  )) {
    expect_error(ConsoleConfig(sessions = list(max = value)), "max")
  }
  for (value in c("implicit", "explicit", "fixed", "disabled")) {
    expect_identical(
      ConsoleConfig(r = list(resolution = value))@r@resolution,
      value
    )
  }
  expect_error(ConsoleConfig(r = list(resolution = "automatic")), "resolution")
  expect_error(
    ConsoleConfig(r = list(resolution = "disabled", packages = "dplyr")),
    "packages"
  )
  expect_error(
    ConsoleConfig(python = list(resolution = "disabled")),
    "resolution"
  )
  expect_error(
    ConsoleConfig(python = list(version = c("3.12", "3.13"))),
    "version"
  )
  expect_error(ConsoleConfig(
    python = list(first_available = list("active_venv", "active_venv"))
  ))
  expect_error(ConsoleConfig(
    python = list(
      first_available = list(
        list(managed = list()),
        list(existing = ".venv")
      )
    )
  ))
})

test_that("printing and serialization retain explicit values", {
  description <- paste(rep("Complete description.", 50), collapse = "\n")
  config <- ConsoleConfig(
    sandbox = FALSE,
    tool = list(description = description, instructions = "")
  )
  printed <- capture.output(print(config))
  expect_identical(printed[[1]], "<ConsoleConfig>")
  document <- jsonlite::fromJSON(paste(printed[-1], collapse = "\n"))
  expect_identical(document$tool$description, description)
  expect_identical(document$tool$instructions, "")
  expect_identical(document$sandbox, FALSE)
  expect_false("sandbox" %in% names(as.list(config)))
  expect_identical(
    as.list(ConsoleConfig()),
    structure(list(), names = character())
  )
})

test_that("tool settings preserve nested parameters and validate strings", {
  fields <- list(
    tool = list(
      description = "Evaluation tool.\n\nUse the parameter descriptions.",
      instructions = "",
      parameters = list(
        r = list(description = "Run one R cell."),
        timeout_ms = list(enabled = FALSE),
        requirements = list(
          parameters = list(r = list(description = "", enabled = TRUE))
        )
      )
    )
  )
  expect_identical(as.list(ConsoleConfig(fields)), fields)
  expect_error(
    ConsoleConfig(tool = list(description = NA_character_)),
    "description"
  )
  expect_error(
    ConsoleConfig(tool = list(description = c("one", "two"))),
    "description"
  )
  expect_error(
    ConsoleConfig(tool = list(parameters = list(r = list(enabled = NA)))),
    "enabled"
  )
  expect_error(
    ConsoleConfig(tool = list(parameters = list(list()))),
    "parameters"
  )
  expect_error(
    ConsoleConfig(tool = list(parameters = list(r = list(), r = list()))),
    "parameters"
  )
})

test_that("configuration rejects unsupported choices before launching", {
  expect_error(ConsoleConfig(sandbox = TRUE), "sandbox")
  expect_error(
    ConsoleConfig(sandbox = FALSE, resolver = list(sandbox = list())),
    "sandbox"
  )
  expect_error(
    ConsoleConfig(resolver = list(environment = "unnamed")),
    "environment"
  )
  expect_error(
    console_tools(config = list(), path = "missing"),
    "ConsoleConfig"
  )
  expect_error(
    console_tools(sandbox = FALSE, path = "missing"),
    "must be empty"
  )
  expect_error(
    console_tools(no_sandbox = TRUE, path = "missing"),
    "must be empty"
  )
  expect_error(console_tools(project = "missing", path = "missing"), "project")
})

test_that("every explicit config bypasses file discovery", {
  with_console_config_probe(function(path, record) {
    choices <- list(NULL, ConsoleConfig(), ConsoleConfig(sandbox = FALSE))
    for (i in seq_along(choices)) {
      tools <- console_tools(path = path, config = choices[[i]])
      argv <- readLines(record, warn = FALSE)
      file <- match("--config-file", argv)
      expect_false(is.na(file))
      expect_identical(
        argv[-c(file, file + 1L)],
        c(
          "serve",
          "--default-config",
          paste0(
            "r.executable=",
            jsonlite::toJSON(sandbox_test_r(), auto_unbox = TRUE)
          ),
          if (i == 3L) "--no-sandbox"
        )
      )
      expect_false(file.exists(argv[[file + 1L]]))
      expect_identical(
        jsonlite::fromJSON(
          paste0(record, ".config.json"),
          simplifyVector = FALSE
        ),
        as.list(ConsoleConfig())
      )
      rm(tools)
      invisible(gc())
    }
  })
})

test_that("loaded R resets launch without a calling-R default", {
  with_console_config_probe(function(path, record) {
    with_sandbox_directory({
      writeLines("r: {executable: null}", "reset.yaml")
      config <- load_console_config(
        global_file = NULL,
        project_file = "reset.yaml",
        path = sandbox_test_binary()
      )
      tools <- console_tools(path = path, config = config)
      expect_false("--default-config" %in% readLines(record, warn = FALSE))
      document <- jsonlite::fromJSON(
        paste0(record, ".config.json"),
        simplifyVector = FALSE
      )
      expect_true("executable" %in% names(document$r))
      expect_null(document$r$executable)
      rm(tools)
      invisible(gc())
    })
  })
})

test_that("constructed settings use a canonical configuration document", {
  with_console_config_probe(function(path, record) {
    config <- ConsoleConfig(
      r = list(packages = character()),
      python = list(packages = character()),
      sandbox = list(),
      resolver = list(sandbox = list())
    )
    tools <- console_tools(path = path, config = config)
    expect_identical(
      jsonlite::fromJSON(
        paste0(record, ".config.json"),
        simplifyVector = FALSE
      ),
      as.list(config)
    )
    rm(tools)
    invisible(gc())
  })
})

test_that("project selection changes only the child's launch directory", {
  with_console_config_probe(function(path, record) {
    with_sandbox_directory({
      project <- file.path(getwd(), "project with spaces")
      dir.create(project)
      old <- getwd()
      tools <- console_tools(
        path = path,
        project = project,
        config = ConsoleConfig()
      )
      expect_identical(
        normalizePath(readLines(paste0(record, ".cwd"))),
        normalizePath(project)
      )
      expect_identical(getwd(), old)
      rm(tools)
      invisible(gc())
    })
  })
})
