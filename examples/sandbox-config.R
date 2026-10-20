args <- commandArgs(trailingOnly = TRUE)
console <- if (length(args)) args[[1L]] else "mcp-console"
config <- list(
  sandbox = list(profile = "read_only", network = "restricted"),
  environment = list(MESSAGE = 'value: café 雪, "quotes", $() and spaces')
)
result <- processx::run(
  console,
  c(
    "sandbox",
    "--config-env",
    "CONSOLE_CONFIG",
    "--",
    "/bin/sh",
    "-c",
    'printf "%s\\n" "$MESSAGE" "$1"',
    "sh",
    "literal argument: * ; $(echo no)"
  ),
  env = c(
    "current",
    CONSOLE_CONFIG = jsonlite::toJSON(config, auto_unbox = TRUE)
  )
)
cat(result$stdout)
