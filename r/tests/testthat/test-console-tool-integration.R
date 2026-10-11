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
    unset = NA_character_
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
  mcp_console_fixture(
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

mock_completion <- function(message, finish_reason) {
  list(
    id = "chatcmpl-test",
    object = "chat.completion",
    created = 0L,
    model = "mock",
    choices = list(list(
      index = 0L,
      message = message,
      finish_reason = finish_reason
    )),
    usage = list(
      prompt_tokens = 0L,
      completion_tokens = 0L,
      total_tokens = 0L
    )
  )
}

test_that("console_tool exposes configured descriptions and omits hidden parameters", {
  with_temp_working_directory({
    send <- console_tool(
      path = real_mcp_console(),
      config = ConsoleConfig(
        r = RConfig(resolution = "disabled"),
        languages = "r",
        sandbox = FALSE,
        tool = ToolConfig(
          description = "Evaluation tool.",
          instructions = "Evaluation server.",
          parameters = list(
            r = ToolParameterConfig(description = "Run one R cell."),
            timeout_ms = ToolParameterConfig(enabled = FALSE),
            requirements = ToolParameterConfig(
              parameters = list(
                r = ToolParameterConfig(description = "R packages.")
              )
            )
          )
        )
      )
    )
    expect_identical(send@description, "Evaluation tool.")
    arguments <- send@arguments@properties
    expect_setequal(
      names(arguments),
      c("r", "control", "stdin", "requirements")
    )
    expect_identical(arguments$r@json$description, "Run one R cell.")
    expect_identical(
      arguments$requirements@json$properties$r$description,
      "R packages."
    )
    expect_identical(send(r = "6 * 7")@text, "[1] 42\n")
    rm(send)
    invisible(gc())
  })
})

test_that("console_tool works when registered with an ellmer chat", {
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
            chat$register_tool(with_mcp_console_environment(
              fixture,
              console_tool()
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
              "outputs",
              "call-000001.log"
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


inspect_requirements <- function(send) {
  deadline <- Sys.time() + 600
  text <- send(requirements = list(action = "get"), timeout_ms = 0)@text
  while (endsWith(text, "[worker starting]")) {
    remaining <- as.numeric(difftime(deadline, Sys.time(), units = "secs"))
    stopifnot(remaining > 0)
    # This is the public readiness poll. Each call waits for startup rather
    # than parsing a pending notice as a JSON requirements declaration.
    text <- send(
      requirements = list(action = "get"),
      timeout_ms = as.integer(1000 * min(60, remaining))
    )@text
  }
  jsonlite::fromJSON(text)
}


test_that("requirements actions preserve scalar fields and empty lists", {
  with_temp_working_directory({
    # Exercise declaration serialization without rebuilding unrelated R packages.
    fixture <- managed_python_mcp_console()
    send <- with_mcp_console_environment(
      fixture,
      console_tool(path = fixture$path, config = ConsoleConfig(sandbox = FALSE))
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

test_that("resolved configurations can be inspected, edited, and launched", {
  binary <- real_mcp_console()
  with_temp_working_directory({
    dir.create(".agents/console", recursive = TRUE)
    writeLines(
      '{"languages":["r"],"tool":{"instructions":"Discover this workbench."}}',
      ".agents/console/config.yaml"
    )
    config <- resolve_console_config(
      ConsoleConfig(
        discovery = ConfigDiscovery(global = FALSE),
        sandbox = FALSE,
        r = RConfig(packages = character(), resolution = "disabled")
      ),
      path = binary
    )
    expect_s3_class(config, "resolved_console_config")
    expect_identical(config$languages, list("r"))
    expect_true(config$tool$parameters$timeout_ms$enabled)
    expect_true(
      "integer" %in% unlist(config$tool$parameters$timeout_ms$schema$type)
    )
    printed <- paste(capture.output(print(config)), collapse = "\n")
    expect_match(printed, "Discover this workbench.", fixed = TRUE)
    expect_match(printed, "timeout_ms", fixed = TRUE)
    config$tool$description <- "Run an evaluation cell."
    config$tool$parameters$timeout_ms$enabled <- FALSE
    config$tool$parameters$r$description <- ""
    writeLines("unknown: must-not-rediscover", ".agents/console/config.yaml")
    dir.create("another-directory")
    project <- getwd()
    setwd("another-directory")
    tool <- console_tool(config = config)
    setwd(project)
    expect_identical(tool@description, "Run an evaluation cell.")
    expect_false("timeout_ms" %in% names(tool@arguments@properties))
    expect_identical(tool@arguments@properties$r@json$description, "")
    expect_match(tool(r = "cat(6 * 7)")@text, "42", fixed = TRUE)
    expect_identical(tool(r = "cat(basename(getwd()))")@text, basename(project))
    rm(tool)
    invisible(gc())
  })
})
