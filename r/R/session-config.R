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
#' `NULL` fields are omitted. Empty mappings and character vectors are retained.
#' `as.list()` returns JSON-ready application settings; the launch-only choice
#' `sandbox = FALSE` is not included in that document.
#'
#' For worker or resolver `network` settings, `TRUE` enables direct networking
#' and `FALSE` restricts it. A character vector or unnamed list supplies allowed
#' proxy patterns. Named mappings use `proxy = list(domains = list(allow = ...,
#' deny = ...))` for explicit destination rules.
#'
#' @param ... Named configuration fields, or a single unnamed mapping. The two
#'   forms cannot be combined. Supported fields are `r`, `python`, `resolver`,
#'   `sandbox`, `languages`, `cache`, `environment`, `inherit_environment`, `tool`,
#'   `startup`, and `sessions`.
#' @return An S7 configuration object.
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
    sessions = config_property(NULL | SessionsConfig)
  ),
  constructor = function(...) {
    fields <- config_node_fields(
      ConsoleConfig,
      config_constructor_fields(list(...))
    )
    S7::new_object(S7::S7_object(), fields)
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
  config_mapping(fields)
}

console_cli_arguments <- function(
  config,
  directory,
  files = character(),
  required_files = character()
) {
  settings <- S7::props(config)
  resolver <- if (!is.null(settings$resolver)) S7::props(settings$resolver)
  disabled <- identical(settings$sandbox, FALSE)
  optional_files <- setdiff(files, required_files)
  # A separate ordinary configuration layer clears complete inherited nodes
  # before applying replacements. Launching a supplied config has no file layers.
  if (length(files)) {
    resets <- list()
    if (isTRUE(attr(config, "r_executable_reset"))) {
      resets$r <- list(executable = NULL)
    }
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
      as.list(config),
      auto_unbox = TRUE,
      null = "null"
    )),
    file,
    useBytes = TRUE
  )
  c(
    if (
      (is.null(settings$r) || is.null(settings$r@executable)) &&
        !isTRUE(attr(config, "r_executable_reset"))
    ) {
      c("--default-config", config_override("r.executable", current_r()))
    },
    if (disabled) "--no-sandbox",
    unlist(lapply(c(files, file), function(path) c("--config-file", path))),
    unlist(lapply(optional_files, function(path) {
      c("--optional-config-file", path)
    }))
  )
}
