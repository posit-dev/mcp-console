test_that("ConsoleConfig preserves schema shapes and separates launch choices", {
  config <- ConsoleConfig(
    discovery = ConfigDiscovery(global = FALSE),
    r = RConfig(
      executable = "/opt/R/bin/R",
      packages = character(),
      resolution = "fixed",
      vanilla = TRUE
    ),
    python = ManagedPython(
      version = "3.13",
      packages = "pandas",
      resolution = "explicit"
    ),
    resolver = ResolverConfig(
      environment = c(INDEX = "", LABEL = "resolver"),
      sandbox = SandboxPolicy(network = "enabled")
    ),
    sandbox = SandboxPolicy(
      filesystem = SandboxFilesystem(read_write = "./output")
    ),
    languages = "r",
    cache = "host",
    environment = c(LABEL = "worker"),
    inherit_environment = FALSE
  )
  expected <- list(
    r = list(
      executable = "/opt/R/bin/R",
      packages = list(),
      resolution = "fixed",
      vanilla = TRUE
    ),
    python = list(
      managed = list(
        version = "3.13",
        packages = list("pandas"),
        resolution = "explicit"
      )
    ),
    resolver = list(
      environment = list(INDEX = "", LABEL = "resolver"),
      sandbox = list(network = "enabled")
    ),
    sandbox = list(filesystem = list(read_write = list("./output"))),
    languages = list("r"),
    cache = "host",
    environment = list(LABEL = "worker"),
    inherit_environment = FALSE
  )
  expect_identical(as.list(config), expected)
  expect_identical(
    jsonlite::fromJSON(
      jsonlite::toJSON(as.list(config), auto_unbox = TRUE),
      simplifyVector = FALSE
    ),
    expected
  )
  expect_identical(
    as.list(ConsoleConfig()),
    structure(list(), names = character())
  )
  expect_identical(
    as.list(ConsoleConfig(python = ExistingPython(".venv"))),
    list(python = list(existing = ".venv"))
  )
  expect_identical(
    as.list(ConsoleConfig(environment = character())),
    list(environment = structure(list(), names = character()))
  )
  expect_identical(
    as.list(ConsoleConfig(sandbox = FALSE)),
    structure(list(), names = character())
  )
})

test_that("ConsoleConfig preserves a custom cache directory", {
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
})

test_that("ConsoleConfig validates and serializes session limits", {
  expect_identical(
    as.list(ConsoleConfig(sessions = SessionsConfig(max = 3L))),
    list(sessions = list(max = 3L))
  )
  expect_identical(SessionsConfig(max = 3)@max, 3)
  expect_null(SessionsConfig()@max)
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
    expect_error(SessionsConfig(max = value), "max")
  }
})

test_that("resolution policies accept implicit and reject automatic", {
  expect_identical(RConfig(resolution = "implicit")@resolution, "implicit")
  expect_identical(
    ManagedPython(resolution = "implicit")@resolution,
    "implicit"
  )
  expect_error(RConfig(resolution = "automatic"), "resolution")
  expect_error(ManagedPython(resolution = "automatic"), "resolution")
})

test_that("resolution policies accept fixed and reject the old name", {
  expect_identical(RConfig(resolution = "fixed")@resolution, "fixed")
  expect_identical(ManagedPython(resolution = "fixed")@resolution, "fixed")
  expect_error(RConfig(resolution = "startup_only"), "resolution")
  expect_error(ManagedPython(resolution = "startup_only"), "resolution")
})

test_that("configuration classes have matching uppercase CamelCase names", {
  expect_false(any(
    c("Filesystem", "Network", "Proxy", "Domains", "Sockets") %in%
      getNamespaceExports("mcp.console")
  ))
  for (name in c(
    "ConsoleConfig",
    "ToolConfig",
    "ToolParameterConfig",
    "ConfigDiscovery",
    "RConfig",
    "ManagedPython",
    "ExistingPython",
    "FirstAvailablePython",
    "StartupConfig",
    "SessionsConfig",
    "ResolverConfig",
    "SandboxPolicy",
    "SandboxFilesystem",
    "SandboxNetwork",
    "SandboxProxy",
    "SandboxDomains",
    "SandboxSockets"
  )) {
    class <- getExportedValue("mcp.console", name)
    expect_identical(class@name, name)
  }
})

test_that("printing ConsoleConfig retains complete descriptions and launch controls", {
  description <- paste(rep("Complete description.", 50), collapse = "\n")
  config <- ConsoleConfig(
    sandbox = FALSE,
    discovery = ConfigDiscovery(project = FALSE),
    tool = ToolConfig(description = description)
  )
  printed <- capture.output(print(config))
  expect_identical(printed[[1]], "<ConsoleConfig>")
  document <- jsonlite::fromJSON(paste(printed[-1], collapse = "\n"))
  expect_identical(document$tool$description, description)
  expect_identical(document$sandbox, FALSE)
  expect_identical(document$discovery, list(global = TRUE, project = FALSE))
})

test_that("new properties preserve existing positional constructor arguments", {
  nested <- list(r = ToolParameterConfig(description = "R cell."))
  parameter <- ToolParameterConfig("Requirements.", FALSE, nested)
  expect_identical(parameter@parameters, nested)
  expect_null(parameter@schema)
  resolver <- ResolverConfig(environment = c(INDEX = "local"))
  config <- ConsoleConfig(NULL, NULL, NULL, resolver, FALSE)
  expect_identical(config@resolver, resolver)
  expect_identical(config@sandbox, FALSE)
  expect_null(config@startup)
})

test_that("fallback chains reject duplicate selectors and misplaced managed candidates", {
  expect_error(
    FirstAvailablePython(list("active_venv", "active_venv")),
    "at most one active_venv"
  )
  expect_error(
    FirstAvailablePython(list(ManagedPython(), ManagedPython())),
    "ManagedPython.*last"
  )
  expect_error(
    FirstAvailablePython(list(ManagedPython(), ExistingPython(".venv"))),
    "ManagedPython.*last"
  )
  expect_no_error(S7::validate(FirstAvailablePython(list(
    "active_venv",
    ExistingPython(".venv"),
    ManagedPython()
  ))))
})

test_that("tool configuration preserves descriptions and nested parameter mappings", {
  tool <- ToolConfig(
    description = "Evaluation tool.\n\nUse the parameter descriptions.",
    instructions = "",
    parameters = list(
      r = ToolParameterConfig(description = "Run one R cell."),
      timeout_ms = ToolParameterConfig(enabled = FALSE),
      requirements = ToolParameterConfig(
        parameters = list(
          r = ToolParameterConfig(description = "", enabled = TRUE)
        )
      )
    )
  )
  expected <- list(
    tool = list(
      description = "Evaluation tool.\n\nUse the parameter descriptions.",
      instructions = "",
      parameters = list(
        r = list(description = "Run one R cell."),
        timeout_ms = list(enabled = FALSE),
        requirements = list(
          parameters = list(
            r = list(description = "", enabled = TRUE)
          )
        )
      )
    )
  )
  config <- ConsoleConfig(tool = tool)
  expect_identical(as.list(config), expected)
  expect_identical(
    jsonlite::fromJSON(
      jsonlite::toJSON(as.list(config), auto_unbox = TRUE),
      simplifyVector = FALSE
    ),
    expected
  )
  empty <- structure(list(), names = character())
  expect_identical(as.list(ToolConfig()), empty)
  expect_identical(
    as.list(ToolConfig(parameters = list())),
    list(parameters = empty)
  )
  expect_identical(as.list(ToolParameterConfig()), empty)
  expect_error(ToolConfig(description = NA_character_), "description")
  expect_error(
    ToolParameterConfig(description = c("one", "two")),
    "description"
  )
  expect_error(ToolParameterConfig(enabled = NA), "enabled")
  expect_error(
    ToolConfig(parameters = list(ToolParameterConfig())),
    "parameters"
  )
  expect_error(ToolConfig(parameters = list(r = list())), "parameters")
  expect_error(
    ToolParameterConfig(
      parameters = list(r = ToolParameterConfig(), r = ToolParameterConfig())
    ),
    "parameters"
  )
})

test_that("configuration rejects unsupported choices before launching", {
  expect_error(ConsoleConfig(sandbox = TRUE), "sandbox")
  expect_error(
    ConsoleConfig(
      sandbox = FALSE,
      resolver = ResolverConfig(sandbox = SandboxPolicy())
    ),
    "sandbox"
  )
  expect_error(ConfigDiscovery(global = NA), "global")
  expect_error(RConfig(resolution = "sometimes"), "resolution")
  expect_error(RConfig(resolution = "disabled", packages = "dplyr"), "packages")
  expect_error(ManagedPython(resolution = "disabled"), "resolution")
  expect_error(ManagedPython(version = c("3.12", "3.13")), "version")
  expect_error(ExistingPython(".venv", packages = "pandas"), "unused argument")
  expect_error(ResolverConfig(environment = c("unnamed")), "environment")
  expect_error(console_tool(config = list(), path = "missing"), "ConsoleConfig")
  expect_error(console_tool(sandbox = FALSE, path = "missing"), "must be empty")
  expect_error(
    console_tool(no_sandbox = TRUE, path = "missing"),
    "must be empty"
  )
  expect_error(console_tool(project = "missing", path = "missing"), "project")
})

test_that("explicit configs preserve legacy source choices", {
  with_console_config_probe(function(path, record) {
    choices <- list(
      NULL,
      ConsoleConfig(),
      ConsoleConfig(discovery = ConfigDiscovery()),
      ConsoleConfig(discovery = ConfigDiscovery(global = FALSE)),
      ConsoleConfig(discovery = ConfigDiscovery(project = FALSE)),
      ConsoleConfig(
        discovery = ConfigDiscovery(global = FALSE, project = FALSE)
      ),
      ConsoleConfig(sandbox = FALSE)
    )
    expected <- list(
      c("serve", "--no-config"),
      c("serve", "--no-config"),
      "serve",
      c("serve", "--no-global-config"),
      c("serve", "--no-project-config"),
      c("serve", "--no-config"),
      c(
        "serve",
        "--no-config",
        "--no-sandbox"
      )
    )
    for (i in seq_along(choices)) {
      tool <- console_tool(path = path, config = choices[[i]])
      argv <- readLines(record, warn = FALSE)
      file <- match("--config-overrides-file", argv)
      expect_false(is.na(file))
      expect_identical(argv[-c(file, file + 1L)], expected[[i]])
      expect_false(file.exists(argv[[file + 1L]]))
      expect_identical(
        readLines(paste0(record, ".overrides")),
        if (i == 7L) c("sandbox=null", "resolver.sandbox=null") else character()
      )
      rm(tool)
      invisible(gc())
    }
  })
})

test_that("runtime options layer while explicit permission nodes replace", {
  with_console_config_probe(function(path, record) {
    config <- ConsoleConfig(
      discovery = ConfigDiscovery(),
      r = RConfig(packages = character()),
      python = ManagedPython(packages = character()),
      sandbox = SandboxPolicy(),
      resolver = ResolverConfig(sandbox = SandboxPolicy())
    )
    tool <- console_tool(path = path, config = config)
    expect_identical(
      readLines(paste0(record, ".overrides")),
      c(
        "sandbox=null",
        "resolver.sandbox=null",
        "python=null",
        "r={\"packages\":[]}",
        "python={\"managed\":{\"packages\":[]}}",
        "resolver={\"sandbox\":{}}",
        "sandbox={}"
      )
    )
    rm(tool)
    invisible(gc())
  })
})

test_that("project selection changes only the child's launch directory", {
  with_console_config_probe(function(path, record) {
    with_sandbox_directory({
      project <- file.path(getwd(), "project with spaces")
      dir.create(project)
      old <- getwd()
      tool <- console_tool(
        path = path,
        project = project,
        config = ConsoleConfig()
      )
      expect_identical(
        normalizePath(readLines(paste0(record, ".cwd"))),
        normalizePath(project)
      )
      expect_identical(getwd(), old)
      rm(tool)
      invisible(gc())
    })
  })
})
