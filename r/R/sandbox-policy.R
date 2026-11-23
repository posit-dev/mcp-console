# One internal parent gives configuration nodes a shared serializer.
# It has no state, defaults, or policy of its own.
ConfigNode <- S7::new_class(
  "ConfigNode",
  abstract = TRUE
)

config_strings <- function(value) {
  if (!is.null(value) && (anyNA(value) || any(!nzchar(value)))) {
    "must contain non-empty strings, without missing values"
  }
}

config_string_list <- function() {
  S7::new_property(NULL | S7::class_character, validator = config_strings)
}

config_path <- function() {
  S7::new_property(S7::class_character, validator = function(value) {
    if (length(value) != 1L || is.na(value) || !nzchar(value)) {
      "must be one non-empty path"
    }
  })
}

config_choice <- function(choices) {
  force(choices)
  S7::new_property(
    NULL | S7::class_character,
    validator = function(value) {
      if (
        !is.null(value) &&
          (length(value) != 1L || is.na(value) || !value %in% choices)
      ) {
        paste0("must be NULL or one of: ", paste(choices, collapse = ", "))
      }
    }
  )
}

config_flag <- function() {
  S7::new_property(
    NULL | S7::class_logical,
    validator = function(value) {
      if (!is.null(value) && (length(value) != 1L || is.na(value))) {
        "must be NULL, TRUE, or FALSE"
      }
    }
  )
}

SandboxFilesystem <- S7::new_class(
  "SandboxFilesystem",
  parent = ConfigNode,
  properties = list(
    read_only = config_string_list(),
    read_write = config_string_list(),
    deny = config_string_list()
  )
)

SandboxDomains <- S7::new_class(
  "SandboxDomains",
  parent = ConfigNode,
  properties = list(
    allow = config_string_list(),
    deny = config_string_list()
  )
)

SandboxProxy <- S7::new_class(
  "SandboxProxy",
  parent = ConfigNode,
  properties = list(
    mode = config_choice(c("full", "limited")),
    domains = config_property(NULL | SandboxDomains),
    socks5 = config_choice(c("disabled", "tcp", "tcp_udp")),
    allow_upstream_proxy = config_flag()
  ),
  validator = function(self) {
    if (identical(self@mode, "limited") && !is.null(self@socks5)) {
      "`socks5` must be NULL in limited mode"
    }
  }
)

SandboxSockets <- S7::new_class(
  "SandboxSockets",
  parent = ConfigNode,
  properties = list(unix_sockets = config_string_list())
)

SandboxNetwork <- S7::new_class(
  "SandboxNetwork",
  parent = ConfigNode,
  properties = list(
    proxy = config_property(NULL | SandboxProxy),
    sockets = config_property(NULL | SandboxSockets),
    allow_local_binding = config_property(
      NULL | S7::class_character | S7::class_logical,
      validator = function(value) {
        if (
          !is.null(value) &&
            (length(value) != 1L ||
              is.na(value) ||
              (is.character(value) && value != "auto"))
        ) {
          "must be NULL, auto, TRUE, or FALSE"
        }
      }
    )
  ),
  constructor = function(...) {
    fields <- config_node_fields(
      SandboxNetwork,
      config_constructor_fields(list(...))
    )
    S7::new_object(S7::S7_object(), fields)
  }
)

InheritCodexSandboxProfile <- S7::new_class(
  "InheritCodexSandboxProfile",
  parent = ConfigNode,
  properties = list(
    fallback = config_choice(c("read_only", "workspace"))
  )
)

ManagedTempdir <- S7::new_class(
  "ManagedTempdir",
  parent = ConfigNode,
  properties = list(parent = config_path())
)

sandbox_choice <- function(choices, mapping, description) {
  error <- paste0(
    "must be NULL, ",
    paste(choices, collapse = ", "),
    ", or ",
    description
  )
  config_property(
    NULL | S7::class_character | mapping,
    validator = function(value) {
      if (
        is.character(value) &&
          (length(value) != 1L || is.na(value) || !value %in% choices)
      ) {
        error
      }
    }
  )
}

SandboxPolicy <- S7::new_class(
  "SandboxPolicy",
  parent = ConfigNode,
  properties = list(
    filesystem = config_property(NULL | SandboxFilesystem),
    network = sandbox_choice(
      c("restricted", "enabled"),
      SandboxNetwork,
      "a network mapping"
    ),
    profile = sandbox_choice(
      c("read_only", "workspace", "inherit_codex"),
      InheritCodexSandboxProfile,
      "an inherit_codex mapping"
    ),
    tempdir = sandbox_choice(
      c("managed", "read_write", "read_only"),
      ManagedTempdir,
      "a managed mapping"
    )
  )
)

# Wire conversion preserves omissions, mappings, and arrays; native validation
# and defaults remain the CLI's responsibility.
config_mapping <- function(fields) {
  fields <- fields[!vapply(fields, is.null, logical(1))]
  arrays <- c(
    "read_only",
    "read_write",
    "deny",
    "allow",
    "unix_sockets",
    "packages",
    "languages"
  )
  for (name in names(fields)) {
    value <- fields[[name]]
    if (S7::S7_inherits(value, ConfigNode)) {
      fields[[name]] <- as.list(value)
    } else if (name == "environment") {
      fields[[name]] <- json_object(as.list(value))
    } else if (name == "cache" && is.list(value)) {
      fields[[name]] <- json_object(value)
    } else if (name == "parameters") {
      fields[[name]] <- json_object(lapply(value, as.list))
    } else if (name == "schema") {
      fields[[name]] <- json_object(value)
    } else if (name == "first_available") {
      fields[[name]] <- unname(lapply(value, function(candidate) {
        if (S7::S7_inherits(candidate, ConfigNode)) {
          as.list(candidate)
        } else {
          candidate
        }
      }))
    } else if (
      name %in%
        arrays &&
        !(name == "unix_sockets" &&
          identical(unname(value), "dangerously_allow_all"))
    ) {
      fields[[name]] <- unname(as.list(value))
    } else {
      fields[[name]] <- unname(value)
    }
  }
  json_object(fields)
}

S7::method(as.list, ConfigNode) <- function(x, ...) {
  S7::validate(x)
  config_mapping(S7::props(x))
}

S7::method(as.list, InheritCodexSandboxProfile) <- function(x, ...) {
  S7::validate(x)
  json_object(list(inherit_codex = config_mapping(S7::props(x))))
}

S7::method(as.list, ManagedTempdir) <- function(x, ...) {
  S7::validate(x)
  list(managed = config_mapping(S7::props(x)))
}

config_override <- function(key, value) {
  paste0(key, "=", jsonlite::toJSON(value, auto_unbox = TRUE))
}
