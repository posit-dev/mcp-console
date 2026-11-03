test_that("loading expands home paths with the native home convention", {
  with_sandbox_directory({
    binary <- sandbox_test_binary()
    home <- file.path(getwd(), "home")
    dir.create(home)
    old_home <- Sys.getenv("HOME", unset = NA_character_)
    on.exit(
      {
        if (is.na(old_home)) {
          Sys.unsetenv("HOME")
        } else {
          Sys.setenv(HOME = old_home)
        }
      },
      add = TRUE
    )
    Sys.setenv(HOME = home)
    writeLines("environment: {GLOBAL: global}", file.path(home, "global.yaml"))
    writeLines(
      "sandbox: {filesystem: {deny: [~/private]}}",
      file.path(home, "project.yaml")
    )
    config <- load_console_config(
      global_file = "~/global.yaml",
      project_file = "~/project.yaml",
      path = binary
    )
    expect_identical(config@environment, c(GLOBAL = "global"))
    expect_equal(
      gsub("\\\\", "/", config@sandbox@filesystem@deny),
      gsub("\\\\", "/", file.path(home, "private"))
    )
    Sys.setenv(MCP_CONSOLE_HOME = "~/console")
    dir.create(file.path(home, "console"))
    writeLines("languages: [python]", file.path(home, "console/config.yaml"))
    expect_identical(load_console_config(path = binary)@languages, "python")
  })
})

test_that("loading one selected JSON or YAML file bypasses unselected files", {
  with_sandbox_directory({
    binary <- sandbox_test_binary()
    dir.create(".agents/console", recursive = TRUE)
    writeLines("invalid: [", ".agents/console/config.yaml")
    writeLines(
      '{"languages":["r"],"tool":{"description":"Report tool"}}',
      "job.json"
    )
    config <- load_console_config(
      global_file = NULL,
      project_file = "job.json",
      path = binary
    )
    expect_identical(config@languages, "r")
    expect_identical(config@tool@description, "Report tool")
    writeLines(
      as.character(jsonlite::toJSON(as.list(config), auto_unbox = TRUE)),
      "roundtrip.json"
    )
    expect_identical(
      as.list(load_console_config(
        global_file = NULL,
        project_file = "roundtrip.json",
        path = binary
      )),
      as.list(config)
    )
    writeLines("tool: {description: Report tool}\nlanguages: [r]", "job.yaml")
    expect_identical(
      as.list(load_console_config(
        global_file = NULL,
        project_file = "job.yaml",
        path = binary
      )),
      as.list(config)
    )
    writeLines('{"typo":true}', "invalid.json")
    expect_error(
      load_console_config(
        global_file = NULL,
        project_file = "invalid.json",
        path = binary
      ),
      "typo"
    )
    dir.create("directory.yaml")
    expect_error(
      load_console_config(
        global_file = NULL,
        project_file = "directory.yaml",
        path = binary
      ),
      "regular file"
    )
  })
})

test_that("default tool loading reads files while explicit configs bypass them", {
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
    send <- console_tools(path = binary)$send
    expect_identical(send@description, "Discovered report tool")
    rm(send)
    invisible(gc())
    writeLines("invalid: [", ".agents/console/config.yaml")
    config <- ConsoleConfig(
      sandbox = FALSE,
      languages = "r",
      r = list(packages = character(), resolution = "disabled"),
      tool = list(description = "Explicit report tool")
    )
    send <- console_tools(path = binary, config = config)$send
    expect_identical(send@description, "Explicit report tool")
    expect_true(inspect_requirements(send)$prepared)
    expect_identical(send(r = "cat(6 * 7)")@text, "42")
    rm(send)
    invisible(gc())
  })
})
