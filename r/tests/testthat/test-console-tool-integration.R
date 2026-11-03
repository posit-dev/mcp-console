real_mcp_console <- function() {
  binary <- Sys.getenv("MCP_CONSOLE_TEST_BINARY")
  testthat::skip_if(
    !nzchar(binary),
    "MCP_CONSOLE_TEST_BINARY does not identify the checkout binary"
  )
  normalizePath(binary, mustWork = TRUE)
}

mcp_console_fixture <- function(binary, environment, unset) {
  if (.Platform$OS.type == "windows") {
    # Launch the native executable directly so MCP retains its stdio pipes.
    return(list(path = binary, environment = environment, unset = unset))
  }
  launcher <- tempfile("mcp-console-launcher-")
  lines <- c(
    "#!/bin/sh",
    paste("unset", paste(unset, collapse = " ")),
    sprintf("export %s=%s", names(environment), shQuote(environment)),
    sprintf("exec %s \"$@\"", shQuote(binary))
  )
  writeLines(lines, launcher)
  Sys.chmod(launcher, "0755")
  list(path = launcher, environment = NULL, unset = character())
}

with_mcp_console_environment <- function(fixture, code) {
  if (!length(fixture$environment) && !length(fixture$unset)) {
    return(force(code))
  }
  old <- Sys.getenv(
    c(names(fixture$environment), fixture$unset),
    unset = NA_character_,
    names = TRUE
  )
  on.exit(
    {
      missing <- is.na(old)
      Sys.unsetenv(names(old)[missing])
      if (any(!missing)) {
        do.call(Sys.setenv, as.list(old[!missing]))
      }
    },
    add = TRUE
  )
  Sys.unsetenv(fixture$unset)
  if (length(fixture$environment)) {
    do.call(Sys.setenv, as.list(fixture$environment))
  }
  force(code)
}

bare_mcp_console <- function() {
  binary <- real_mcp_console()
  library <- tempfile("mcp-console-bare-library-")
  dir.create(library)
  windows <- .Platform$OS.type == "windows"
  if (windows) {
    python <- Sys.which("python")
    skip_if(
      !nzchar(python),
      "Windows fixture needs Python for the stdlib relay"
    )
    python <- trimws(
      processx::run(
        python,
        c("-I", "-c", "import sys; print(sys._base_executable)")
      )$stdout
    )
    stopifnot(file.exists(python))
  }
  mcp_console_fixture(
    binary,
    c(
      PATH = if (windows) {
        paste(
          dirname(binary),
          R.home("bin"),
          dirname(python),
          file.path(Sys.getenv("SystemRoot"), "System32"),
          sep = .Platform$path.sep
        )
      } else {
        "/usr/bin:/bin:/usr/sbin:/sbin"
      },
      R_HOME = R.home(),
      R_LIBS = library,
      R_LIBS_USER = library,
      R_LIBS_SITE = library
    ),
    unset = c("R_TESTS", "RETICULATE_UV")
  )
}

managed_python_mcp_console <- function() {
  binary <- real_mcp_console()
  directory <- tempfile("mcp-console-python-")
  dir.create(directory)
  uv <- Sys.which("uv")
  stopifnot(nzchar(uv))
  windows <- .Platform$OS.type == "windows"
  exposed <- if (windows) {
    file.copy(uv, file.path(directory, "uv.exe"))
  } else {
    file.symlink(uv, file.path(directory, "uv"))
  }
  stopifnot(exposed)
  path <- if (windows) {
    paste(
      directory,
      file.path(Sys.getenv("SystemRoot"), "System32"),
      sep = .Platform$path.sep
    )
  } else {
    directory
  }
  fixture <- mcp_console_fixture(
    binary,
    c(PATH = path),
    unset = c(
      "R_TESTS",
      "R_HOME",
      "RHOME",
      "RETICULATE_UV",
      "RETICULATE_PYTHON"
    )
  )
  reset <- file.path(directory, "python-only.yaml")
  writeLines("r: null", reset)
  fixture$config <- load_console_config(
    global_file = NULL,
    project_file = reset,
    overrides = list(sandbox = FALSE),
    path = binary
  )
  fixture
}

with_path <- function(path, code) {
  old <- Sys.getenv("PATH")
  on.exit(Sys.setenv(PATH = old), add = TRUE)
  Sys.setenv(PATH = path)
  force(code)
}

with_mcp_console_languages <- function(languages, code) {
  old <- Sys.getenv("MCP_CONSOLE_LANGUAGES", unset = NA_character_)
  on.exit(
    {
      if (is.na(old)) {
        Sys.unsetenv("MCP_CONSOLE_LANGUAGES")
      } else {
        Sys.setenv(MCP_CONSOLE_LANGUAGES = old)
      }
    },
    add = TRUE
  )
  Sys.setenv(MCP_CONSOLE_LANGUAGES = paste(languages, collapse = ","))
  force(code)
}

with_temp_working_directory <- function(code) {
  directory <- tempfile("mcp-console-test-")
  dir.create(directory)
  old <- setwd(directory)
  old_environment <- Sys.getenv(
    c("MCP_CONSOLE_HOME", "R_USER_CACHE_DIR", "IR_CACHE_DIR"),
    unset = NA_character_
  )
  on.exit(
    {
      setwd(old)
      missing <- is.na(old_environment)
      Sys.unsetenv(names(old_environment)[missing])
      if (any(!missing)) {
        do.call(Sys.setenv, as.list(old_environment[!missing]))
      }
      unlink(directory, recursive = TRUE)
    },
    add = TRUE
  )
  # Pak requires an explicit cache during R CMD check. A fresh ir cache keeps
  # cached resolutions from hiding failures in the preparation path.
  Sys.setenv(
    MCP_CONSOLE_HOME = file.path(getwd(), "console"),
    R_USER_CACHE_DIR = file.path(getwd(), "r-cache"),
    IR_CACHE_DIR = file.path(getwd(), "ir-cache")
  )
  force(code)
}

test_that("console_tools expose configured descriptions and omit hidden parameters", {
  with_temp_working_directory({
    send <- console_tools(
      path = real_mcp_console(),
      config = ConsoleConfig(
        r = list(resolution = "disabled"),
        languages = "r",
        sandbox = FALSE,
        tool = list(
          description = "Evaluation tool.",
          instructions = "Evaluation server.",
          parameters = list(
            r = list(description = "Run one R cell."),
            timeout_ms = list(enabled = FALSE),
            requirements = list(
              parameters = list(
                r = list(description = "R packages.")
              )
            )
          )
        )
      )
    )$send
    expect_identical(send@description, "Evaluation tool.")
    arguments <- send@arguments@properties
    expect_setequal(
      names(arguments),
      c("r", "note", "session", "control", "stdin", "requirements")
    )
    expect_identical(arguments$r@json$description, "Run one R cell.")
    expect_identical(
      arguments$requirements@json$properties$r$description,
      "R packages."
    )
    expect_identical(send(r = "6 * 7")@text, "[1] 42\n")
    expect_identical(
      send(r = "answer <- 42L", session = "analysis")@text,
      "[done]"
    )
    expect_identical(send(r = "answer", session = "analysis")@text, "[1] 42\n")
    expect_identical(send(r = 'exists("answer")')@text, "[1] FALSE\n")
    rm(send)
    invisible(gc())
  })
})

test_that("console_tools work when registered with an ellmer chat", {
  # fmt: r
  source <- r"(
    x <- 41L
    cat("adapter head\n", strrep("x", 10000), "\nadapter tail\n", sep = "")
    x + 1L
  )"
  responses <- list(
    mock_completion(
      list(
        role = "assistant",
        content = NULL,
        tool_calls = list(list(
          id = "call_1",
          type = "function",
          `function` = list(
            name = "send",
            arguments = jsonlite::toJSON(list(r = source), auto_unbox = TRUE)
          )
        ))
      ),
      "tool_calls"
    ),
    mock_completion(list(role = "assistant", content = "done"), "stop")
  )
  requests <- list()
  httr2::local_mocked_responses(function(req) {
    requests[[length(requests) + 1L]] <<- httr2::req_get_body(req)
    httr2::response_json(body = responses[[length(requests)]])
  })

  with_temp_working_directory({
    with_mcp_console_languages("r", {
      fixture <- bare_mcp_console()
      if (.Platform$OS.type == "windows") {
        directory <- dirname(fixture$path)
      } else {
        directory <- tempfile("mcp-console-path-")
        dir.create(directory)
        file.copy(fixture$path, file.path(directory, "mcp-console"))
        Sys.chmod(file.path(directory, "mcp-console"), "0755")
      }

      with_path(directory, {
        chat <- ellmer::chat_openai_compatible(
          base_url = "https://example.test/v1",
          model = "mock",
          credentials = function() "unused",
          echo = "none"
        )
        tryCatch(
          {
            chat$register_tools(with_mcp_console_environment(
              fixture,
              # Tool registration and output retention are independent of
              # native enforcement, covered by the sandbox acceptance cases.
              console_tools(config = ConsoleConfig(sandbox = FALSE))
            ))
            text <- NULL
            chat$on_tool_result(function(result) text <<- result@value@text)

            expect_identical(
              as.character(chat$chat("Use the console.")),
              "done"
            )
            expect_named(chat$get_tools(), "send")
            expect_length(requests, 2L)
            expect_identical(
              requests[[1L]]$tools[[1L]][["function"]]$name,
              "send"
            )
            expect_match(
              jsonlite::toJSON(requests[[2L]], auto_unbox = TRUE),
              "[1] 42",
              fixed = TRUE
            )
            expect_type(text, "character")
            expect_lte(nchar(text, type = "bytes"), 8192L)
            expect_match(text, "adapter head\n", fixed = TRUE)
            expect_match(text, "adapter tail\n", fixed = TRUE)
            expect_match(text, "[output omitted: ", fixed = TRUE)
            sessions <- list.dirs("console/sessions", recursive = FALSE)
            expect_length(sessions, 1L)
            raw_log <- file.path(
              getwd(),
              sessions,
              "capture",
              "sessions",
              "001",
              "cell-001.txt"
            )
            expect_true(file.exists(raw_log))
            notice <- regmatches(
              text,
              regexec(" UTF-8 bytes; raw log: ([^\\n]+)\\]", text, perl = TRUE)
            )[[1L]]
            expect_length(notice, 2L)
            expect_identical(
              normalizePath(notice[[2L]], mustWork = TRUE),
              normalizePath(raw_log, mustWork = TRUE)
            )
            expect_identical(
              readChar(raw_log, file.info(raw_log)$size, useBytes = TRUE),
              paste0(
                "adapter head\n",
                strrep("x", 10000),
                "\nadapter tail\n[1] 42\n"
              )
            )
          },
          finally = {
            rm(chat)
            gc()
          }
        )
      })
    })
  })
})


test_that("requirements actions preserve scalar fields and empty lists", {
  with_temp_working_directory({
    # Exercise declaration serialization without rebuilding unrelated R packages.
    fixture <- managed_python_mcp_console()
    send <- with_mcp_console_environment(
      fixture,
      console_tools(
        path = fixture$path,
        config = fixture$config
      )$send
    )
    startup <- inspect_requirements(send)
    # Inspect the committed default environment after worker readiness.
    expect_true(startup$prepared)
    prepared <- send(
      requirements = list(
        action = "set",
        r = character(),
        python = character(),
        duckdb = character(),
        python_version = ">=3.11",
        exclude_newer = "2026-01-01"
      )
    )
    expect_identical(prepared@text, "[prepared]")
    selected <- inspect_requirements(send)
    expect_length(selected$requirements$python, 0L)
    expect_identical(selected$requirements$python_version, ">=3.11")
    expect_identical(selected$requirements$exclude_newer, "2026-01-01")
    declaration <- selected$requirements
    declaration$action <- "set"
    expect_identical(send(requirements = declaration)@text, "[prepared]")
    expect_identical(
      inspect_requirements(send),
      selected
    )
  })
})

test_that("ellmer chat distinguishes requirements placeholders from explicit null", {
  with_temp_working_directory({
    fixture <- managed_python_mcp_console()
    tools <- with_mcp_console_environment(
      fixture,
      console_tools(
        path = fixture$path,
        config = fixture$config
      )
    )
    startup <- inspect_requirements(tools$send)
    expect_true(startup$prepared)
    declaration <- list(
      action = "set",
      r = character(),
      python = character(),
      duckdb = character(),
      python_version = ">=3.11",
      exclude_newer = "2026-01-01"
    )
    cleared <- declaration
    cleared["exclude_newer"] <- list(NULL)
    placeholders <- list(
      r = NULL,
      python = NULL,
      duckdb = NULL,
      python_version = NULL,
      exclude_newer = NULL
    )
    get <- c(list(action = "get"), placeholders)
    add <- c(list(action = "add"), placeholders)
    add$python_version <- list(">=3.11")
    default_add <- c(list(action = NULL), placeholders)
    default_add$python_version <- list(">=3.11")
    reset <- c(list(action = "reset"), placeholders)
    get_packages <- get
    get_packages["python"] <- list(character())
    reset_packages <- reset
    reset_packages["python"] <- list(character())
    get_constraint <- get
    get_constraint$exclude_newer <- "2026-01-01"
    reset_constraint <- reset
    reset_constraint$exclude_newer <- "2026-01-01"
    calls <- lapply(
      list(
        set = declaration,
        get_set = get,
        add = add,
        get_add = get,
        default_add = default_add,
        get_default_add = get,
        clear = cleared,
        get_cleared = get,
        reset = reset,
        get_reset = get,
        get_packages = get_packages,
        reset_packages = reset_packages,
        get_constraint = get_constraint,
        reset_constraint = reset_constraint
      ),
      function(requirements) {
        # Strict tool schemas include every optional top-level field as null.
        fields <- names(tools$send@arguments@properties)
        arguments <- stats::setNames(rep(list(NULL), length(fields)), fields)
        arguments["requirements"] <- list(requirements)
        list(name = "send", arguments = arguments)
      }
    )
    results <- mock_console_chat(tools, calls)
    expect_identical(
      lapply(results, function(result) result@error),
      rep(list(NULL), length(calls))
    )
    names(results) <- names(calls)
    expect_identical(
      unname(lapply(
        results[c("set", "add", "default_add", "clear", "reset")],
        function(result) result@value@text
      )),
      rep(list("[prepared]"), 5L)
    )
    inspected <- lapply(
      results[c(
        "get_set",
        "get_add",
        "get_default_add",
        "get_cleared",
        "get_reset"
      )],
      function(result) jsonlite::fromJSON(result@value@text)
    )
    expect_identical(
      inspected$get_set$requirements$exclude_newer,
      "2026-01-01"
    )
    expect_identical(
      inspected$get_add$requirements,
      inspected$get_set$requirements
    )
    expect_identical(
      inspected$get_default_add$requirements,
      inspected$get_set$requirements
    )
    expect_identical(
      inspected$get_cleared$requirements["exclude_newer"],
      list(exclude_newer = NULL)
    )
    expect_identical(inspected$get_reset$requirements, startup$requirements)
    expect_identical(
      unname(lapply(
        results[c(
          "get_packages",
          "reset_packages",
          "get_constraint",
          "reset_constraint"
        )],
        function(result) result@value@text
      )),
      rep(
        list(
          "requirements.action=get/reset takes no package lists or constraint fields"
        ),
        4L
      )
    )
    rm(tools, results)
    invisible(gc())
  })
})

test_that("resolved configurations can be inspected, edited, and launched", {
  binary <- real_mcp_console()
  with_temp_working_directory({
    dir.create(".agents/console", recursive = TRUE)
    writeLines(
      '{"languages":["r"],"tool":{"instructions":"Discover this workbench."}}',
      ".agents/console/config.yaml"
    )
    config <- load_console_config(
      global_file = NULL,
      overrides = list(
        sandbox = FALSE,
        r = list(packages = character(), resolution = "disabled")
      ),
      path = binary
    )
    expect_identical(S7::S7_class(config), ConsoleConfig)
    expect_identical(config@languages, "r")
    # Generated metadata is not copied into the editable configuration.
    expect_null(config@tool@description)
    expect_null(config@tool@parameters)
    printed <- paste(capture.output(print(config)), collapse = "\n")
    expect_match(printed, "Discover this workbench.", fixed = TRUE)
    config@tool@description <- "Run an evaluation cell."
    config@tool@parameters <- list(
      timeout_ms = list(enabled = FALSE),
      r = list(description = "")
    )
    reconstructed <- do.call(ConsoleConfig, S7::props(config))
    expect_identical(as.list(reconstructed), as.list(config))
    writeLines("unknown: must-not-rediscover", ".agents/console/config.yaml")
    dir.create("changed-console-home")
    writeLines("invalid: [", "changed-console-home/config.yaml")
    Sys.setenv(MCP_CONSOLE_HOME = file.path(getwd(), "changed-console-home"))
    dir.create("another-directory")
    project <- getwd()
    setwd("another-directory")
    tool <- console_tools(
      path = binary,
      config = reconstructed,
      project = project
    )$send
    setwd(project)
    expect_identical(tool@description, "Run an evaluation cell.")
    expect_false("timeout_ms" %in% names(tool@arguments@properties))
    expect_identical(tool@arguments@properties$r@json$description, "")
    expect_match(tool(r = "cat(6 * 7)")@text, "42", fixed = TRUE)
    tool(control = "restart")
    expect_identical(tool(r = "cat(basename(getwd()))")@text, basename(project))
    rm(tool)
    invisible(gc())
  })
})

test_that("loaded R resets survive edits and launch without the calling-R preference", {
  binary <- real_mcp_console()
  with_temp_working_directory({
    fixture <- mcp_console_fixture(
      binary,
      c(R_HOME = file.path(getwd(), "missing-r-reset-home")),
      unset = c("R_TESTS", "RHOME")
    )
    launch <- function(config) {
      with_mcp_console_environment(
        fixture,
        console_tools(config = config, path = fixture$path)$send
      )
    }
    overrides <- list(
      sandbox = FALSE,
      languages = "r",
      r = list(packages = character(), resolution = "disabled")
    )
    config <- load_console_config(
      global_file = NULL,
      project_file = NULL,
      overrides = overrides,
      path = binary
    )
    expect_identical(
      normalizePath(config@r@executable, mustWork = TRUE),
      normalizePath(sandbox_test_r(), mustWork = TRUE)
    )
    send <- launch(config)
    expect_identical(send(r = "cat(42)")@text, "42")
    rm(send)
    invisible(gc())

    for (reset in c("r: null", "r: {executable: null}")) {
      writeLines(reset, "reset.yaml")
      config <- load_console_config(
        global_file = NULL,
        project_file = "reset.yaml",
        overrides = overrides,
        path = binary
      )
      expect_null(config@r@executable)
      config@tool <- list(description = "Edited after loading")
      writeLines("invalid: [", "reset.yaml")
      send <- launch(config)
      expect_identical(send@description, "Edited after loading")
      expect_match(
        send(r = "cat(42)")@text,
        "missing-r-reset-home",
        fixed = TRUE
      )
      rm(send)
      invisible(gc())

      # The captured reset also clears selections inherited by a later load.
      writeLines("r: {executable: /inherited/R}", "reset.yaml")
      config <- load_console_config(
        global_file = NULL,
        project_file = "reset.yaml",
        overrides = config,
        path = binary
      )
      expect_null(config@r@executable)
      config@r@executable <- sandbox_test_r()
      send <- launch(config)
      expect_identical(send(r = "cat(42)")@text, "42")
      rm(send)
      invisible(gc())
    }
  })
})

test_that("materialization preserves typed policies and startup source", {
  binary <- real_mcp_console()
  with_mcp_console_environment(
    list(environment = NULL, unset = "VIRTUAL_ENV"),
    with_temp_working_directory({
      python <- sandbox_test_python()
      source <- ConsoleConfig(
        sandbox = FALSE,
        python = list(
          first_available = list(
            list(existing = "absent-python"),
            "active_venv",
            list(existing = python),
            list(managed = list(packages = character(), resolution = "fixed"))
          )
        ),
        startup = list(
          language = "python",
          code = paste(
            "import sqlite3",
            "_console.sql_connection(sqlite3.connect(':memory:'))",
            sep = "\n"
          )
        ),
        r = list(packages = character(), resolution = "disabled"),
        languages = c("python", "sql"),
        environment = c(EMPTY = ""),
        resolver = list(environment = c(LAYER = "resolver"))
      )
      config <- load_console_config(
        global_file = NULL,
        project_file = NULL,
        overrides = source,
        path = binary
      )
      expect_identical(as.list(config@python), as.list(source@python))
      expect_identical(as.list(config@startup), as.list(source@startup))
      expect_identical(config@environment, c(EMPTY = ""))
      expect_identical(
        config@resolver@environment,
        c(EMPTY = "", LAYER = "resolver")
      )
      expect_identical(config@sandbox, FALSE)
      expect_identical(config@python@first_available[[4]]@packages, character())
      config <- load_console_config(
        global_file = NULL,
        project_file = NULL,
        overrides = config,
        path = binary
      )
      tool <- console_tools(config = config, path = binary)$send
      expect_match(tool(sql = "SELECT 42 AS answer")@text, "42", fixed = TRUE)
      rm(tool)
      invisible(gc())

      config@tool <- list(
        parameters = list(
          timeout_ms = list(schema = list(type = list("string")))
        )
      )
      expect_error(
        load_console_config(
          global_file = NULL,
          project_file = NULL,
          overrides = config,
          path = binary
        ),
        "read-only"
      )
    })
  )
})

test_that("sandbox materialization round trips through typed configuration", {
  binary <- real_mcp_console()
  with_temp_working_directory({
    policies <- list(
      list(profile = "workspace", tempdir = "read_write"),
      list(tempdir = "read_only"),
      list(),
      list(
        filesystem = list(
          read_write = character(),
          deny = "./secrets"
        ),
        network = "enabled"
      ),
      list(
        network = list(
          allow = "example.com",
          deny = character(),
          proxy = list(mode = "limited")
        )
      )
    )
    for (policy in policies) {
      config <- load_console_config(
        global_file = NULL,
        project_file = NULL,
        overrides = list(sandbox = policy),
        path = binary
      )
      again <- load_console_config(
        global_file = NULL,
        project_file = NULL,
        overrides = config,
        path = binary
      )
      expect_identical(as.list(again), as.list(config))
      expect_identical(S7::S7_class(config), ConsoleConfig)
    }
  })
})

test_that("large constructed and materialized settings use bounded launch arguments", {
  binary <- real_mcp_console()
  with_temp_working_directory({
    description <- paste(rep("Evaluation cell.", 80000), collapse = "\n")
    startup <- list(
      language = "python",
      code = paste0(
        "#",
        strrep("x", 31000),
        "\n",
        "import sqlite3\n",
        "_console.sql_connection(sqlite3.connect(':memory:'))"
      )
    )
    size <- nchar(
      jsonlite::toJSON(startup, auto_unbox = TRUE),
      type = "bytes"
    )
    expect_gt(size, 30000)
    expect_lt(size, 32 * 1024)
    source <- ConsoleConfig(
      sandbox = FALSE,
      r = list(packages = character(), resolution = "disabled"),
      python = list(existing = sandbox_test_python()),
      languages = c("python", "sql"),
      startup = startup,
      tool = list(description = description),
      environment = c(LARGE_CONFIG = "explicit")
    )
    dir.create(".agents/console", recursive = TRUE)
    writeLines(
      "environment: {LARGE_CONFIG: discovered}",
      ".agents/console/config.yaml"
    )
    resolved <- load_console_config(
      global_file = NULL,
      overrides = source,
      path = binary
    )
    expect_identical(resolved@tool@description, description)
    expect_identical(resolved@startup@code, startup$code)
    for (config in list(source, resolved)) {
      tool <- console_tools(config = config, path = binary)$send
      expect_identical(tool@description, description)
      expect_match(tool(sql = "SELECT 42 AS answer")@text, "42", fixed = TRUE)
      expect_identical(
        tool(python = "import os; print(os.environ['LARGE_CONFIG'])")@text,
        "explicit\n"
      )
      rm(tool)
      invisible(gc())
    }
  })
})
