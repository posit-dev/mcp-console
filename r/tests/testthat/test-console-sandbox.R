test_that("console_tools transmit the same application policy object", {
  with_console_config_probe(function(path, record) {
    policy <- list(
      filesystem = list(read_write = list("./space here")),
      network = "restricted"
    )
    tools <- console_tools(
      path = path,
      config = ConsoleConfig(sandbox = policy)
    )
    overrides <- readLines(paste0(record, ".overrides"))
    expect_identical(
      overrides[[1]],
      "sandbox=null"
    )
    expect_identical(
      jsonlite::fromJSON(
        substring(overrides[[2]], nchar("sandbox=") + 1L),
        simplifyVector = FALSE
      ),
      policy
    )
    rm(tools)
    invisible(gc())
  })
})

test_that("no discovery ignores malformed files and retains restrictive defaults", {
  with_sandbox_directory({
    binary <- sandbox_test_binary()
    require_sandbox_execution(binary)
    dir.create(Sys.getenv("MCP_CONSOLE_HOME"))
    dir.create(".agents/console", recursive = TRUE)
    writeLines(
      "invalid: [",
      file.path(Sys.getenv("MCP_CONSOLE_HOME"), "config.yaml")
    )
    writeLines("invalid: [", ".agents/console/config.yaml")
    send <- console_tools(
      path = binary,
      config = ConsoleConfig(
        r = list(resolution = "disabled"),
        languages = "r"
      )
    )$send
    result <- send(
      r = 'suppressWarnings(tryCatch(writeLines("bad", "blocked"), error = function(e) cat("denied\\n")))',
      timeout_ms = 60000L
    )
    expect_identical(result@text, "denied\n")
    expect_false(file.exists("blocked"))
    rm(send)
    invisible(gc())
  })
})

test_that("project discovery layers runtime settings and can disable enforcement", {
  with_sandbox_directory({
    binary <- sandbox_test_binary()
    dir.create(Sys.getenv("MCP_CONSOLE_HOME"))
    writeLines(
      "invalid: [",
      file.path(Sys.getenv("MCP_CONSOLE_HOME"), "config.yaml")
    )
    project <- file.path(getwd(), "project with spaces")
    dir.create(file.path(project, ".agents/console"), recursive = TRUE)
    writeLines(
      c(
        "languages: [r]",
        "r: {resolution: disabled}",
        "environment: {MCP_CONSOLE_TEST_SETTING: retained}",
        "sandbox: {filesystem: {deny: [.]}}",
        "resolver: {sandbox: {}}"
      ),
      file.path(project, ".agents/console/config.yaml")
    )
    send <- console_tools(
      path = binary,
      project = project,
      config = load_console_config(
        global_file = NULL,
        overrides = list(sandbox = FALSE),
        project = project,
        path = binary
      )
    )$send
    result <- send(
      r = 'writeLines(Sys.getenv("MCP_CONSOLE_TEST_SETTING"), "result"); cat(getwd(), "\\n", sep = "")',
      timeout_ms = 60000L
    )
    expect_identical(normalizePath(trimws(result@text)), normalizePath(project))
    expect_identical(readLines(file.path(project, "result")), "retained")
    expect_length(
      jsonlite::fromJSON(
        send(requirements = list(action = "get"), timeout_ms = 60000L)@text
      )$requirements$r,
      0L
    )
    rm(send)
    invisible(gc())
  })
})

test_that("explicit runtime selections reach native configuration", {
  with_sandbox_directory({
    binary <- sandbox_test_binary()
    require_sandbox_execution(binary)
    python <- sandbox_test_python()
    config <- ConsoleConfig(
      r = list(
        executable = sandbox_test_r(),
        resolution = "disabled"
      ),
      python = python,
      languages = c("r", "python"),
      environment = c(MCP_CONSOLE_TEST_SETTING = "worker"),
      resolver = list(
        environment = c(MCP_CONSOLE_TEST_SETTING = "resolver")
      )
    )
    send <- console_tools(path = binary, config = config)$send
    result <- send(
      r = 'cat(R.home(), "\\n", Sys.getenv("MCP_CONSOLE_TEST_SETTING"), "\\n", sep = "")',
      timeout_ms = 60000L
    )
    expect_identical(result@text, paste0(R.home(), "\nworker\n"))
    result <- send(
      python = "import os, sys; print(sys.executable); print(os.environ['MCP_CONSOLE_TEST_SETTING'])",
      timeout_ms = 60000L
    )
    lines <- strsplit(result@text, "\n", fixed = TRUE)[[1L]]
    expect_identical(normalizePath(lines[[1L]]), normalizePath(python))
    expect_identical(lines[[2L]], "worker")
    rm(send)
    invisible(gc())
  })
})

test_that("managed Python prepares an explicit empty startup declaration once", {
  with_sandbox_directory({
    binary <- sandbox_test_binary()
    skip_if(!nzchar(Sys.which("uv")), "Managed Python requires uv")
    send <- console_tools(
      path = binary,
      config = ConsoleConfig(
        r = list(resolution = "disabled"),
        languages = "python",
        python = list(
          version = "3.13",
          packages = character(),
          resolution = "fixed"
        ),
        sandbox = FALSE
      )
    )$send
    declaration <- jsonlite::fromJSON(
      send(requirements = list(action = "get"), timeout_ms = 60000L)@text
    )
    expect_true(declaration$prepared)
    expect_length(declaration$requirements$python, 0L)
    expect_identical(declaration$requirements$python_version, "3.13")
    result <- send(
      python = "import sys; print(sys.version_info[:2])",
      timeout_ms = 60000L
    )
    expect_identical(result@text, "(3, 13)\n")
    result <- send(
      requirements = list(action = "add", python = "requests"),
      timeout_ms = 60000L
    )
    expect_match(result@text, "python.managed.resolution=fixed", fixed = TRUE)
    rm(send)
    invisible(gc())
  })
})

test_that("console_tools replace ambient grants and retain other settings", {
  with_sandbox_directory({
    binary <- sandbox_test_binary()
    require_sandbox_execution(binary)
    dir.create(Sys.getenv("MCP_CONSOLE_HOME"))
    dir.create("allowed")
    writeLines(
      c(
        "languages: [r]",
        "r:",
        "  resolution: disabled",
        "environment:",
        "  MCP_CONSOLE_TEST_SETTING: retained",
        "sandbox:",
        "  filesystem:",
        "    read_write: [.]",
        "  network: enabled"
      ),
      file.path(Sys.getenv("MCP_CONSOLE_HOME"), "config.yaml")
    )
    send <- console_tools(
      path = binary,
      config = load_console_config(
        overrides = list(
          sandbox = list(filesystem = list(read_write = "./allowed"))
        ),
        path = binary
      )
    )$send
    expect_false("python" %in% names(formals(send)))
    # fmt: r
    source <- r"(
      writeLines("allowed", "allowed/result")
      suppressWarnings(tryCatch(
        writeLines("ambient grant survived", "blocked"),
        error = function(e) cat("denied\n")
      ))
      cat(Sys.getenv("MCP_CONSOLE_TEST_SETTING"), "\n", sep = "")
    )"
    result <- send(r = source, timeout_ms = 60000L)
    expect_identical(result@text, "denied\nretained\n")
    expect_identical(readLines("allowed/result"), "allowed")
    expect_false(file.exists("blocked"))
    declaration <- send(
      requirements = list(action = "get"),
      timeout_ms = 60000L
    )
    expect_length(jsonlite::fromJSON(declaration@text)$requirements$r, 0L)
    rm(send)
    invisible(gc())
  })
})
