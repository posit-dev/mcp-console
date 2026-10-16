sandbox_test_binary <- function() {
  binary <- Sys.getenv("MCP_CONSOLE_TEST_BINARY")
  skip_if(!nzchar(binary), "Set MCP_CONSOLE_TEST_BINARY to the checkout binary")
  normalizePath(binary, mustWork = TRUE)
}

sandbox_test_python <- function() {
  command <- if (.Platform$OS.type == "windows") "python" else "python3"
  path <- Sys.which(command)
  skip_if(!nzchar(path), "Existing Python test requires a Python interpreter")
  unname(path)
}

sandbox_test_r <- function() {
  file.path(R.home("bin"), if (.Platform$OS.type == "windows") "R.exe" else "R")
}

require_sandbox_execution <- local({
  results <- new.env(parent = emptyenv())
  function(binary) {
    if (.Platform$OS.type != "windows") {
      return(invisible(NULL))
    }
    result <- results[[binary]]
    if (is.null(result)) {
      directory <- tempfile("mcp-console-mxc-probe-")
      stopifnot(dir.create(directory))
      on.exit(unlink(directory, recursive = TRUE), add = TRUE)
      result <- processx::run(
        binary,
        c(
          "sandbox",
          "--no-config",
          "--",
          normalizePath(
            file.path(Sys.getenv("SystemRoot"), "System32", "cmd.exe"),
            mustWork = TRUE
          ),
          "/d",
          "/c",
          "echo MXC probe executed"
        ),
        wd = directory,
        env = c("current", MCP_CONSOLE_HOME = file.path(directory, "home")),
        error_on_status = FALSE,
        timeout = 30000
      )
      if (result$status == 0L) {
        stopifnot(
          trimws(result$stdout) == "MXC probe executed",
          !nzchar(result$stderr)
        )
      } else {
        # Only unavailable MXC permits skipping; policy, installation, and
        # workload errors must still fail acceptance.
        if (
          result$status != 1L ||
            nzchar(result$stdout) ||
            !grepl(
              "MXC is unavailable on this executor;",
              result$stderr,
              fixed = TRUE
            )
        ) {
          stop(result$stderr)
        }
      }
      results[[binary]] <- result
    }
    if (result$status != 0L) {
      if (Sys.getenv("MCP_CONSOLE_TEST_REQUIRE_MXC") == "1") {
        stop(result$stderr)
      }
      skip(trimws(result$stderr))
    }
    invisible(NULL)
  }
})

with_sandbox_directory <- function(code) {
  directory <- tempfile("mcp-console-sandbox-test-")
  stopifnot(dir.create(directory))
  old <- setwd(directory)
  old_home <- Sys.getenv("MCP_CONSOLE_HOME", unset = NA_character_)
  Sys.setenv(MCP_CONSOLE_HOME = file.path(directory, "console"))
  on.exit(
    {
      if (is.na(old_home)) {
        Sys.unsetenv("MCP_CONSOLE_HOME")
      } else {
        Sys.setenv(MCP_CONSOLE_HOME = old_home)
      }
      setwd(old)
      unlink(directory, recursive = TRUE)
    },
    add = TRUE
  )
  force(code)
}

with_console_config_probe <- function(code) {
  skip_on_os("windows")
  directory <- tempfile("mcp-console-mcp-probe-")
  stopifnot(dir.create(directory))
  on.exit(unlink(directory, recursive = TRUE), add = TRUE)
  path <- file.path(directory, "console probe")
  record <- file.path(directory, "argv")
  fixture <- normalizePath(test_path("fixtures", "console-config-probe.R"))
  writeLines(
    c(
      "#!/bin/sh",
      paste(
        "exec",
        shQuote(file.path(R.home("bin"), "Rscript")),
        "--vanilla",
        shQuote(fixture),
        shQuote(record),
        '"$@"'
      )
    ),
    path
  )
  Sys.chmod(path, "0755")
  code(path, record)
}
