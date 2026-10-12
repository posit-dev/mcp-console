test_that("discovery exposes source paths, layers once, and returns an ordinary config", {
  with_sandbox_directory({
    binary <- sandbox_test_binary()
    project <- file.path(getwd(), "job")
    dir.create(file.path(project, ".agents/console"), recursive = TRUE)
    dir.create(Sys.getenv("MCP_CONSOLE_HOME"))
    global <- file.path(Sys.getenv("MCP_CONSOLE_HOME"), "config.yaml")
    writeLines("environment: {GLOBAL: global, LAYER: global}", global)
    writeLines(
      c("languages: [r]", "environment: {LAYER: project}"),
      file.path(project, ".agents/console/config.yaml")
    )

    config <- discover_console_config(path = binary, project = project)
    expect_identical(S7::S7_class(config), ConsoleConfig)
    expect_null(config@discovery)
    expect_identical(
      config@environment,
      c(GLOBAL = "global", LAYER = "project")
    )
    expect_identical(config@languages, "r")
    expect_true(nzchar(config@tool@description))

    writeLines("invalid: [", global)
    writeLines("invalid: [", file.path(project, ".agents/console/config.yaml"))
    expect_identical(
      as.list(materialize_console_config(
        config,
        path = binary,
        project = project
      )),
      as.list(config)
    )
    expect_error(
      discover_console_config(path = binary, project = project),
      "config.yaml"
    )

    writeLines(
      '{"environment":{"LAYER":"selected"}}',
      file.path(project, "global.json")
    )
    selected <- discover_console_config(
      global_file = "global.json",
      project_file = "absent.yaml",
      path = binary,
      project = project
    )
    expect_identical(selected@environment, c(LAYER = "selected"))
    defaults <- discover_console_config(
      global_file = NULL,
      project_file = NULL,
      path = binary,
      project = project
    )
    expect_identical(defaults@environment, setNames(character(), character()))
    expect_identical(
      defaults@tool@description,
      materialize_console_config(
        path = binary,
        project = project
      )@tool@description
    )
    dir.create(file.path(project, "directory.yaml"))
    expect_error(
      discover_console_config(
        global_file = NULL,
        project_file = "directory.yaml",
        path = binary,
        project = project
      ),
      "regular file"
    )
  })
})

test_that("materializing and pruning retain edits and explicit empty values", {
  with_sandbox_directory({
    binary <- sandbox_test_binary()
    config <- ConsoleConfig(
      languages = "r",
      inherit_environment = FALSE,
      r = RConfig(packages = character(), resolution = "disabled"),
      tool = ToolConfig(
        description = "",
        parameters = list(
          timeout_ms = ToolParameterConfig(enabled = FALSE)
        )
      )
    )
    full <- materialize_console_config(config, path = binary)
    compact <- prune_console_config(full, path = binary)
    expect_identical(S7::S7_class(compact), ConsoleConfig)
    expect_identical(compact@tool@description, "")
    expect_false(compact@tool@parameters$timeout_ms@enabled)
    expect_identical(compact@r@packages, character())
    expect_false(compact@inherit_environment)
    expect_null(compact@r@vanilla)
    expect_identical(
      as.list(materialize_console_config(compact, path = binary)),
      as.list(full)
    )
    expect_identical(
      as.list(prune_console_config(path = binary)),
      jsonlite::fromJSON("{}")
    )

    full@sandbox@filesystem <- SandboxFilesystem(read_write = "./output")
    full@sandbox@filesystem@read_write <- c(
      full@sandbox@filesystem@read_write,
      "./reports"
    )
    full@sandbox@network <- SandboxNetwork(
      proxy = SandboxProxy(
        domains = SandboxDomains(allow = "api.example.com")
      )
    )
    full@sandbox@network@proxy@domains@allow <- c(
      full@sandbox@network@proxy@domains@allow,
      "mail.example.com"
    )
    compact <- prune_console_config(full, path = binary)
    expect_identical(
      compact@sandbox@filesystem@read_write,
      c("./output", "./reports")
    )
    expect_identical(
      compact@sandbox@network@proxy@domains@allow,
      c("api.example.com", "mail.example.com")
    )
    expect_match(
      paste(capture.output(print(compact)), collapse = "\n"),
      "mail.example.com",
      fixed = TRUE
    )
  })
})

test_that("selected JSON and YAML files are required and bypass discovery", {
  with_sandbox_directory({
    binary <- sandbox_test_binary()
    dir.create(".agents/console", recursive = TRUE)
    writeLines("invalid: [", ".agents/console/config.yaml")
    writeLines(
      '{"languages":["r"],"tool":{"description":"Report tool"}}',
      "job.json"
    )
    config <- read_console_config("job.json", path = binary)
    expect_identical(config@languages, "r")
    expect_identical(config@tool@description, "Report tool")
    expect_null(config@discovery)
    compact <- prune_console_config(config, path = binary)
    writeLines(
      as.character(jsonlite::toJSON(as.list(compact), auto_unbox = TRUE)),
      "roundtrip.json"
    )
    expect_identical(
      as.list(read_console_config("roundtrip.json", path = binary)),
      as.list(config)
    )
    writeLines("tool: {description: Report tool}\nlanguages: [r]", "job.yaml")
    expect_identical(
      as.list(read_console_config("job.yaml", path = binary)),
      as.list(config)
    )
    expect_error(
      read_console_config("absent.json", path = binary),
      "absent.json"
    )
    writeLines('{"typo":true}', "invalid.json")
    expect_error(read_console_config("invalid.json", path = binary), "typo")
  })
})

test_that("the default tool discovers once while explicit configs bypass files", {
  with_sandbox_directory({
    binary <- sandbox_test_binary()
    dir.create(".agents/console", recursive = TRUE)
    writeLines(
      c(
        "languages: [r]",
        "r: {packages: [], resolution: disabled}",
        "tool: {description: Discovered report tool}"
      ),
      ".agents/console/config.yaml"
    )
    send <- console_tool(path = binary)
    expect_identical(send@description, "Discovered report tool")
    rm(send)
    invisible(gc())
    writeLines("invalid: [", ".agents/console/config.yaml")
    config <- ConsoleConfig(
      sandbox = FALSE,
      languages = "r",
      r = RConfig(packages = character(), resolution = "disabled"),
      tool = ToolConfig(description = "Explicit report tool")
    )
    send <- console_tool(path = binary, config = config)
    expect_identical(send@description, "Explicit report tool")
    expect_identical(send(r = "cat(6 * 7)")@text, "42")
    rm(send)
    invisible(gc())
  })
})
