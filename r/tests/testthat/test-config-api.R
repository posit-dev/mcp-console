test_that("ConsoleConfig accepts YAML-shaped mappings without reading files", {
  with_sandbox_directory({
    dir.create(".agents/console", recursive = TRUE)
    writeLines("invalid: [", ".agents/console/config.yaml")
    fields <- list(
      r = list(packages = character(), resolution = "disabled"),
      python = list(version = "3.13", packages = "pandas"),
      sandbox = list(filesystem = list(read_write = "./reports")),
      resolver = list(environment = list(UV_INDEX_URL = "https://example.com")),
      environment = list(EMPTY = ""),
      sessions = list(max = 2),
      tool = list(parameters = list(timeout_ms = list(enabled = FALSE)))
    )
    config <- ConsoleConfig(fields)
    expect_identical(S7::S7_class(config), ConsoleConfig)
    expect_identical(as.list(do.call(ConsoleConfig, fields)), as.list(config))
    expect_null(config@r@executable)
    expect_identical(config@r@packages, character())
    expect_identical(config@python@packages, "pandas")
    expect_identical(config@sandbox@filesystem@read_write, "./reports")
    expect_identical(config@environment, c(EMPTY = ""))
    expect_false(config@tool@parameters$timeout_ms@enabled)
    expect_false("discovery" %in% names(S7::props(config)))
    expect_false(dir.exists("reports"))
    config@r <- list(packages = "dplyr")
    config@sandbox@filesystem <- list(read_write = "./output")
    config@tool@parameters <- list(timeout_ms = list(description = "Wait."))
    expect_identical(config@r@packages, "dplyr")
    expect_identical(config@sandbox@filesystem@read_write, "./output")
    expect_identical(config@tool@parameters$timeout_ms@description, "Wait.")
  })
})

test_that("ConsoleConfig rejects ambiguous or misspelled mappings", {
  expect_error(ConsoleConfig(list(languages = "r"), cache = "host"), "single")
  expect_error(ConsoleConfig("r"), "mapping")
  expect_error(
    ConsoleConfig(list(languages = "r", languages = "python")),
    "unique"
  )
  expect_error(ConsoleConfig(discovery = list(global = TRUE)), "discovery")
  expect_error(ConsoleConfig(r = list(packges = "dplyr")), "r.packges")
  expect_error(
    ConsoleConfig(sandbox = list(filesystem = list(readwrite = "."))),
    "sandbox.filesystem.readwrite"
  )
  expect_error(ConsoleConfig(environment = list(COUNT = 1)), "environment")
  expect_error(ConsoleConfig(r = list(packages = list(1))), "r.packages")
  expect_error(
    ConsoleConfig(
      sandbox = list(filesystem = list(read_write = list(output = ".")))
    ),
    "sandbox.filesystem.read_write.*array"
  )
  expect_error(
    ConsoleConfig(
      sandbox = list(network = list(allow = list(api = "example.com")))
    ),
    "sandbox.network.allow.*array"
  )
  expect_error(
    ConsoleConfig(r = list(packages = structure(list(), names = character()))),
    "r.packages.*array"
  )
  expect_error(
    ConsoleConfig(
      python = list(first_available = list(selected = list(existing = ".venv")))
    ),
    "python.first_available.*array"
  )
  expect_identical(
    ConsoleConfig(r = list(packages = c(package = "dplyr")))@r@packages,
    c(package = "dplyr")
  )
})

test_that("invalid default Console home fails before executable or file lookup", {
  skip_on_os("windows")
  with_sandbox_directory({
    for (directory in c("relative", "C:/console", "\\\\server\\share")) {
      Sys.setenv(MCP_CONSOLE_HOME = directory)
      expect_error(
        load_console_config(project_file = NULL, path = "missing"),
        "MCP_CONSOLE_HOME must be an absolute path"
      )
    }
    expect_error(
      load_console_config(
        global_file = NULL,
        project_file = NULL,
        path = "missing"
      ),
      "path.*existing file"
    )
  })
})

test_that("ConsoleConfig accepts a yaml12 file read without constructor-specific syntax", {
  skip_if_not_installed("yaml12")
  file <- tempfile(fileext = ".yaml")
  on.exit(unlink(file), add = TRUE)
  writeLines(
    c(
      "r:",
      "  packages: []",
      "  resolution: disabled",
      "python:",
      "  managed: {version: '3.13', packages: [pandas]}",
      "sandbox:",
      "  filesystem: {read_write: [./reports]}",
      "  network: {allow: [api.example.com]}",
      "environment: {MODE: on, EMPTY: ''}",
      "inherit_environment: false"
    ),
    file
  )
  for (simplify in c(TRUE, FALSE)) {
    config <- ConsoleConfig(yaml12::read_yaml(file, simplify = simplify))
    expect_identical(config@r@packages, character())
    expect_identical(config@python@packages, "pandas")
    expect_identical(config@sandbox@network@allow, "api.example.com")
    expect_identical(config@environment, c(MODE = "on", EMPTY = ""))
    expect_false(config@inherit_environment)
  }
  writeLines("python: {first_available: [active_venv]}", file)
  for (simplify in c(TRUE, FALSE)) {
    config <- ConsoleConfig(yaml12::read_yaml(file, simplify = simplify))
    expect_identical(
      as.list(config)$python,
      list(first_available = list("active_venv"))
    )
  }
})

test_that("ConsoleConfig preserves omitted fields and empty JSON shapes", {
  config <- ConsoleConfig(
    r = list(packages = character()),
    python = list(managed = list(packages = "pandas")),
    environment = list(),
    tool = list(description = "", parameters = list()),
    startup = NULL
  )
  value <- as.list(config)
  expect_identical(value$r$packages, list())
  expect_identical(value$python$managed$packages, list("pandas"))
  expect_identical(value$environment, structure(list(), names = character()))
  expect_identical(
    value$tool$parameters,
    structure(list(), names = character())
  )
  expect_identical(value$tool$description, "")
  expect_false("startup" %in% names(value))
  expect_identical(
    jsonlite::fromJSON(
      jsonlite::toJSON(value, auto_unbox = TRUE),
      simplifyVector = FALSE
    ),
    value
  )
})

test_that("runtime shorthands and fallback selections use the YAML schema", {
  expect_identical(
    as.list(ConsoleConfig(r = "/opt/R/bin/R", python = ".venv")),
    list(
      r = list(executable = "/opt/R/bin/R"),
      python = list(existing = ".venv")
    )
  )
  config <- ConsoleConfig(
    python = list(
      first_available = list(
        list(existing = ".venv"),
        "active_venv",
        list(managed = list(version = "3.13"))
      )
    )
  )
  expect_identical(
    as.list(config)$python$first_available,
    list(
      list(existing = ".venv"),
      "active_venv",
      list(managed = list(version = "3.13"))
    )
  )
  expect_error(ConsoleConfig(
    python = list(existing = ".venv", packages = "pandas")
  ))
})

test_that("load_console_config reads sources once and honors explicit overrides", {
  with_sandbox_directory({
    binary <- sandbox_test_binary()
    dir.create(Sys.getenv("MCP_CONSOLE_HOME"))
    dir.create(".agents/console", recursive = TRUE)
    global <- file.path(Sys.getenv("MCP_CONSOLE_HOME"), "config.yaml")
    project <- ".agents/console/config.yaml"
    writeLines(
      c(
        "environment: {GLOBAL: global, LAYER: global}",
        "sandbox: {network: enabled}",
        "python: {existing: .venv}"
      ),
      global
    )
    writeLines("environment: {LAYER: project}", project)
    config <- load_console_config(
      overrides = ConsoleConfig(
        environment = list(LAYER = "explicit"),
        sandbox = list(filesystem = list(read_write = "./reports")),
        python = list(managed = list(version = "3.13"))
      ),
      path = binary
    )
    expect_identical(S7::S7_class(config), ConsoleConfig)
    expect_identical(
      config@environment,
      c(GLOBAL = "global", LAYER = "explicit")
    )
    expect_false("existing" %in% names(as.list(config)$python))
    expect_false(identical(config@sandbox@network, "enabled"))
    expect_identical(config@r@executable, sandbox_test_r())
    writeLines("invalid: [", global)
    writeLines("invalid: [", project)
    tools <- console_tools(config = config, path = binary)
    expect_true(length(tools) > 0L)
    rm(tools)
    invisible(gc())
  })
})

test_that("loader skips omitted defaults but requires explicitly selected paths", {
  with_sandbox_directory({
    binary <- sandbox_test_binary()
    expect_silent(load_console_config(path = binary))
    expect_silent(load_console_config(overrides = NULL, path = binary))
    expect_error(
      load_console_config(project_file = "missing.yaml", path = binary),
      "missing.yaml"
    )
    expect_error(
      load_console_config(global_file = "missing.yaml", path = binary),
      "missing.yaml"
    )
    writeLines("r: {executable: /selected/R}", "job.yaml")
    config <- load_console_config(
      global_file = NULL,
      project_file = "job.yaml",
      path = binary
    )
    expect_identical(config@r@executable, "/selected/R")
  })
})
