config_scalar <- function() {
  S7::new_property(NULL | S7::class_character, validator = function(value) {
    if (
      !is.null(value) &&
        (length(value) != 1L || is.na(value) || !nzchar(value))
    ) {
      "must be NULL or one non-empty string"
    }
  })
}

config_environment <- function() {
  config_property(NULL | S7::class_character, validator = function(value) {
    if (
      !is.null(value) &&
        length(value) &&
        (anyNA(value) ||
          is.null(names(value)) ||
          anyNA(names(value)) ||
          any(!nzchar(names(value))) ||
          anyDuplicated(names(value)))
    ) {
      "must be a named character vector with unique, non-empty names"
    }
  })
}

RConfig <- S7::new_class(
  "RConfig",
  parent = ConfigNode,
  properties = list(
    executable = config_scalar(),
    packages = config_string_list(),
    resolution = config_choice(c(
      "implicit",
      "explicit",
      "fixed",
      "disabled"
    )),
    vanilla = config_flag()
  ),
  validator = function(self) {
    if (identical(self@resolution, "disabled") && length(self@packages)) {
      "packages must be empty when resolution = disabled"
    }
  }
)

# Internal fallback evaluated only at load/launch, below explicit settings.
current_r <- function() {
  file.path(R.home("bin"), if (.Platform$OS.type == "windows") "R.exe" else "R")
}

PythonConfig <- S7::new_class(
  "PythonConfig",
  parent = ConfigNode,
  abstract = TRUE
)

ExistingPython <- S7::new_class(
  "ExistingPython",
  parent = PythonConfig,
  properties = list(
    existing = S7::new_property(
      S7::class_character,
      validator = function(value) {
        if (length(value) != 1L || is.na(value) || !nzchar(value)) {
          "must be one non-empty path"
        }
      }
    )
  )
)

ManagedPython <- S7::new_class(
  "ManagedPython",
  parent = PythonConfig,
  properties = list(
    version = config_scalar(),
    packages = config_string_list(),
    resolution = config_choice(c("implicit", "explicit", "fixed"))
  )
)

FirstAvailablePython <- S7::new_class(
  "FirstAvailablePython",
  parent = PythonConfig,
  properties = list(
    first_available = S7::new_property(
      S7::class_list,
      validator = function(value) {
        if (
          !length(value) ||
            !all(vapply(
              value,
              function(candidate) {
                identical(candidate, "active_venv") ||
                  S7::S7_inherits(candidate, ExistingPython) ||
                  S7::S7_inherits(candidate, ManagedPython)
              },
              logical(1)
            ))
        ) {
          return(
            "must contain existing, active_venv, or managed candidates"
          )
        }
        if (sum(vapply(value, identical, logical(1), "active_venv")) > 1L) {
          return("must contain at most one active_venv candidate")
        }
        managed <- which(vapply(
          value,
          S7::S7_inherits,
          logical(1),
          ManagedPython
        ))
        if (length(managed) > 1L || any(managed != length(value))) {
          "must contain at most one managed candidate, last"
        }
      }
    )
  )
)

StartupConfig <- S7::new_class(
  "StartupConfig",
  parent = ConfigNode,
  properties = list(
    language = config_choice(c("r", "python")),
    code = config_scalar()
  )
)

ResolverConfig <- S7::new_class(
  "ResolverConfig",
  parent = ConfigNode,
  properties = list(
    environment = config_environment(),
    sandbox = config_property(NULL | SandboxPolicy),
    inherit_environment = config_flag()
  )
)

config_description <- function() {
  S7::new_property(NULL | S7::class_character, validator = function(value) {
    if (!is.null(value) && (length(value) != 1L || is.na(value))) {
      "must be NULL or one string"
    }
  })
}

config_parameters <- function() {
  config_property(NULL | S7::class_list, validator = function(value) {
    if (
      !is.null(value) &&
        length(value) &&
        (is.null(names(value)) ||
          anyNA(names(value)) ||
          any(!nzchar(names(value))) ||
          anyDuplicated(names(value)) ||
          !all(vapply(value, S7::S7_inherits, logical(1), ToolParameterConfig)))
    ) {
      "must be a parameter mapping with unique, non-empty names"
    }
  })
}

ToolParameterConfig <- S7::new_class(
  "ToolParameterConfig",
  parent = ConfigNode,
  properties = list(
    description = config_description(),
    enabled = config_flag(),
    parameters = config_parameters(),
    schema = NULL | S7::class_list
  )
)

ToolConfig <- S7::new_class(
  "ToolConfig",
  parent = ConfigNode,
  properties = list(
    description = config_description(),
    instructions = config_description(),
    parameters = config_parameters()
  )
)

SessionsConfig <- S7::new_class(
  "SessionsConfig",
  parent = ConfigNode,
  properties = list(
    max = S7::new_property(
      NULL | S7::class_integer | S7::class_double,
      validator = function(value) {
        if (
          !is.null(value) &&
            (length(value) != 1L ||
              !is.finite(value) ||
              value <= 0 ||
              value != floor(value))
        ) {
          "must be NULL or one positive whole number"
        }
      }
    )
  )
)

retention_days <- function() {
  S7::new_property(
    NULL | S7::class_integer | S7::class_double,
    validator = function(value) {
      if (
        !is.null(value) &&
          (length(value) != 1L ||
            !is.finite(value) ||
            value <= 0 ||
            value >= 2^32 ||
            value != floor(value))
      ) {
        "must be NULL or one positive whole number below 2^32"
      }
    },
    setter = function(self, name, value) {
      S7::prop(self, name) <- value
      disabled <- attr(self, "disabled_fields")
      attr(self, "disabled_fields") <- if (is.null(value)) {
        union(disabled, name)
      } else {
        setdiff(disabled, name)
      }
      self
    }
  )
}

RetentionConfig <- S7::new_class(
  "RetentionConfig",
  parent = ConfigNode,
  properties = list(
    cache_days = retention_days(),
    recordings_days = retention_days()
  ),
  constructor = function(cache_days = NULL, recordings_days = NULL) {
    fields <- list()
    if (!missing(cache_days)) {
      fields["cache_days"] <- list(cache_days)
    }
    if (!missing(recordings_days)) {
      fields["recordings_days"] <- list(recordings_days)
    }
    config <- S7::new_object(S7::S7_object(), fields)
    # Here NULL disables cleanup, while an absent field keeps earlier layers.
    attr(config, "disabled_fields") <- names(fields)[vapply(
      fields,
      is.null,
      logical(1)
    )]
    config
  }
)

S7::method(as.list, RetentionConfig) <- function(x, ...) {
  S7::validate(x)
  fields <- S7::props(x)
  for (name in attr(x, "disabled_fields")) {
    fields[[name]] <- jsonlite::unbox(NA_character_)
  }
  config_mapping(fields)
}

#' Configure a Console session
#'
#' Construct configuration from named fields or one named list using the same
#' field names and nesting as `config.yaml`. Construction validates the supplied
#' values without reading files, finding executables, or starting a process.
#' Nested lists are converted to typed configuration objects; edit properties
#' with `@`, including replacing a nested object with another named list.
#'
#' Use [load_console_config()] to read configuration files once. Both constructors
#' return the same class, and passing either result to [console_tools()] disables
#' further file discovery. Unspecified R selection uses the calling R installation
#' at load or launch, below explicit configuration.
#'
#' `NULL` fields are omitted, except within `retention`, where an explicit `NULL`
#' disables the corresponding cleanup. Empty mappings and character vectors
#' are retained.
#' `as.list()` returns JSON-ready application settings; the launch-only choice
#' `sandbox = FALSE` is not included in that document.
#' A loaded R executable reset is retained as an explicit JSON `null`, so saving
#' and reloading the settings preserves native R discovery.
#'
#' For worker or resolver `network` settings, `TRUE` enables direct networking
#' and `FALSE` restricts it. The scalar strings `"enabled"` and `"restricted"`
#' select the same modes; other strings are allowed proxy patterns. Character
#' vectors and unnamed lists supply allowed patterns, including an empty vector
#' or list for an explicit empty allow list. Named mappings use
#' `proxy = list(domains = list(allow = ..., deny = ...))` for explicit rules.
#' YAML accepts the same boolean, string, and sequence forms. A YAML
#' `network: null` is omitted when files merge, preserving an earlier network
#' field. R `network = NULL` is omitted from the constructed settings.
#'
#' @param ... A single unnamed list of configuration fields, instead of named
#'   arguments. All other arguments must be supplied by their full names.
#' @param r An R executable path, or a list with `executable`, `packages`,
#'   `resolution`, and `vanilla`. Resolution is `"implicit"` (prepare packages
#'   as needed), `"explicit"` (only on request), `"fixed"` (startup packages
#'   only), or `"disabled"` (use installed packages). `vanilla = TRUE` skips
#'   native R startup files.
#' @param python An existing environment path, `list(existing = path)`, or
#'   `list(managed = list(version = "3.13", packages = "pandas",
#'   resolution = "fixed"))`. Managed Python supports `"implicit"`, `"explicit"`,
#'   and `"fixed"`. `list(first_available = list(...))` tries existing paths,
#'   `"active_venv"`, and an optional managed choice, last, in order.
#' @param resolver A list with `environment`, `inherit_environment`, and
#'   `sandbox`, controlling package preparation separately from executed code.
#' @param sandbox A permission list with `profile`, `filesystem`, `network`,
#'   and `tempdir`, or `FALSE` for host execution. Profiles are `"read_only"`
#'   (the default), `"workspace"` (project writes with metadata protection),
#'   and `"inherit_codex"` (client permissions). Filesystem entries are
#'   `read_only`, `read_write`, and `deny` path vectors. Temporary storage is
#'   `"managed"` (the default), `"read_write"`, or `"read_only"`.
#' @param languages A character vector selecting `"r"`, `"python"`, and/or
#'   `"sql"`. By default all three are visible to the model.
#' @param cache `"managed"`, `"host"`, or `list(directory = "/path/to/cache")`
#'   for package caches. Defaults to managed caches with sandboxing and host
#'   caches with `sandbox = FALSE`.
#' @param environment Named character vector or list of string environment
#'   values shared by workers and package preparation.
#' @param inherit_environment Whether child processes inherit the launch
#'   environment, `TRUE` by default.
#' @param tool A list with `description`, `instructions`, and `parameters`.
#'   Each named parameter accepts `enabled`, `description`, and nested
#'   `parameters`; for example, `list(parameters = list(timeout_ms =
#'   list(enabled = FALSE)))` hides the timeout argument.
#' @param startup A list with `language` (`"r"` or `"python"`) and `code` that
#'   creates and selects a SQL connection before the first cell in each session.
#' @param sessions A list with `max`, a positive whole number limiting concurrent
#'   sessions. By default there is no limit.
#' @param retention A list with `cache_days` and `recordings_days`, each a positive
#'   whole number of days or `NULL` to disable that cleanup. Both default to 14
#'   days. Omitted fields keep earlier configuration values.
#' @return An S7 configuration object.
#' @seealso [console_tools()] to launch with this configuration;
#'   [load_console_config()] to read configuration files;
#'   `vignette("configuration", package = "mcp.console")` for settings and examples.
#' @examples
#' ConsoleConfig()
#' ConsoleConfig(r = list(packages = c("dplyr", "ggplot2")))
#' ConsoleConfig(sandbox = list(network = c("api.example.com", "*.example.org")))
#' ConsoleConfig(list(
#'   python = ".venv",
#'   sandbox = list(filesystem = list(read_write = "./output"))
#' ))
#' @export
ConsoleConfig <- S7::new_class(
  "ConsoleConfig",
  parent = ConfigNode,
  properties = list(
    r = config_property(NULL | RConfig),
    python = config_property(NULL | PythonConfig),
    resolver = config_property(NULL | ResolverConfig),
    sandbox = config_property(
      NULL | S7::class_logical | SandboxPolicy,
      validator = function(value) {
        if (is.logical(value) && !identical(value, FALSE)) {
          "must be NULL, FALSE, or a sandbox mapping"
        }
      }
    ),
    languages = config_string_list(),
    cache = S7::new_property(
      NULL | S7::class_character | S7::class_list,
      validator = function(value) {
        if (is.null(value)) {
          return(NULL)
        }
        if (
          is.character(value) &&
            length(value) == 1L &&
            !is.na(value) &&
            value %in% c("managed", "host", "console")
        ) {
          return(NULL)
        }
        if (is.list(value) && identical(names(value), "directory")) {
          path <- value$directory
          if (
            is.character(path) &&
              length(path) == 1L &&
              !is.na(path) &&
              nzchar(path)
          ) {
            return(NULL)
          }
        }
        "must be NULL, managed, host, or list(directory = path)"
      }
    ),
    environment = config_environment(),
    inherit_environment = config_flag(),
    tool = config_property(NULL | ToolConfig),
    startup = config_property(NULL | StartupConfig),
    sessions = config_property(NULL | SessionsConfig),
    retention = config_property(NULL | RetentionConfig)
  ),
  constructor = function(
    ...,
    r = NULL,
    python = NULL,
    resolver = NULL,
    sandbox = NULL,
    languages = NULL,
    cache = NULL,
    environment = NULL,
    inherit_environment = NULL,
    tool = NULL,
    startup = NULL,
    sessions = NULL,
    retention = NULL
  ) {
    fields <- list(
      r = r,
      python = python,
      resolver = resolver,
      sandbox = sandbox,
      languages = languages,
      cache = cache,
      environment = environment,
      inherit_environment = inherit_environment,
      tool = tool,
      startup = startup,
      sessions = sessions,
      retention = retention
    )
    if (...length()) {
      if (any(names(fields) %in% names(match.call()))) {
        stop(
          "Supply a single mapping or named fields, not both.",
          call. = FALSE
        )
      }
      fields <- config_constructor_fields(list(...))
    }
    # as.list() uses an unboxed missing string for an explicit JSON null.
    # Ordinary NULL fields remain omissions in pure construction.
    r_reset <- is.list(fields[["r"]]) &&
      identical(fields[["r"]][["executable"]], jsonlite::unbox(NA_character_))
    if (r_reset) {
      fields[["r"]][["executable"]] <- NULL
    }
    fields <- config_node_fields(ConsoleConfig, fields)
    config <- S7::new_object(S7::S7_object(), fields)
    if (r_reset) {
      attr(config, "r_executable_reset") <- TRUE
    }
    config
  },
  validator = function(self) {
    if (
      identical(self@sandbox, FALSE) &&
        !is.null(self@resolver) &&
        !is.null(self@resolver@sandbox)
    ) {
      "resolver.sandbox cannot be supplied when sandbox = FALSE"
    }
  }
)

S7::method(as.list, ManagedPython) <- function(x, ...) {
  S7::validate(x)
  list(managed = config_mapping(S7::props(x)))
}

S7::method(print, ConsoleConfig) <- function(x, ...) {
  cat(
    "<ConsoleConfig>\n",
    jsonlite::toJSON(
      config_mapping(S7::props(x)),
      auto_unbox = TRUE,
      null = "null",
      pretty = TRUE,
      digits = NA
    ),
    "\n",
    sep = ""
  )
  invisible(x)
}

S7::method(as.list, ConsoleConfig) <- function(x, ...) {
  S7::validate(x)
  fields <- S7::props(x)
  if (identical(x@sandbox, FALSE)) {
    fields$sandbox <- NULL
  }
  fields <- config_mapping(fields)
  if (
    isTRUE(attr(x, "r_executable_reset")) &&
      is.null(fields[["r"]][["executable"]])
  ) {
    # This remains JSON null with jsonlite's default null = "list" option.
    fields[["r"]][["executable"]] <- jsonlite::unbox(NA_character_)
  }
  fields
}

console_cli_arguments <- function(
  config,
  directory,
  files = character(),
  required_files = character()
) {
  settings <- S7::props(config)
  document <- as.list(config)
  resolver <- if (!is.null(settings$resolver)) S7::props(settings$resolver)
  disabled <- identical(settings$sandbox, FALSE)
  optional_files <- setdiff(files, required_files)
  # A separate ordinary configuration layer clears complete inherited nodes
  # before applying replacements. Launching a supplied config has no file layers.
  if (length(files)) {
    resets <- list()
    if (disabled || !is.null(settings$sandbox)) {
      resets["sandbox"] <- list(NULL)
    }
    if (disabled || !is.null(resolver$sandbox)) {
      resets$resolver <- list(sandbox = NULL)
    }
    if (!is.null(settings$python)) {
      resets["python"] <- list(NULL)
    }
    if (length(resets)) {
      file <- file.path(directory, "replace.json")
      writeLines(
        as.character(jsonlite::toJSON(
          resets,
          auto_unbox = TRUE,
          null = "null"
        )),
        file,
        useBytes = TRUE
      )
      files <- c(files, file)
    }
  }
  file <- file.path(directory, "config.json")
  writeLines(
    as.character(jsonlite::toJSON(
      document,
      auto_unbox = TRUE,
      null = "null"
    )),
    file,
    useBytes = TRUE
  )
  # An explicit serialized reset must not inspect the calling R installation.
  c(
    if (is.null(document[["r"]][["executable"]])) {
      c("--default-config", config_override("r.executable", current_r()))
    },
    if (disabled) "--no-sandbox",
    unlist(lapply(c(files, file), function(path) c("--config-file", path))),
    unlist(lapply(optional_files, function(path) {
      c("--optional-config-file", path)
    }))
  )
}
