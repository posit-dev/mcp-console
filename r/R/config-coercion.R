# Configuration objects retain typed nodes for validation and completion, while
# their public constructor and replacement properties accept YAML-shaped lists.
config_property <- function(class, validator = NULL, default = NULL) {
  S7::new_property(
    class,
    validator = validator,
    default = default,
    setter = function(self, name, value) {
      S7::prop(self, name) <- config_coerce_value(
        S7::S7_class(self)@name,
        name,
        value,
        name
      )
      self
    }
  )
}

config_mapping_fields <- function(fields, path = "configuration") {
  if (!is.list(fields) || (length(fields) && is.null(names(fields)))) {
    stop(sprintf("`%s` must be a named mapping.", path), call. = FALSE)
  }
  if (
    length(fields) &&
      (anyNA(names(fields)) ||
        any(!nzchar(names(fields))) ||
        anyDuplicated(names(fields)))
  ) {
    stop(
      sprintf("`%s` must have unique, non-empty names.", path),
      call. = FALSE
    )
  }
  fields
}

config_constructor_fields <- function(arguments) {
  if (!length(arguments)) {
    return(list())
  }
  names <- names(arguments)
  unnamed <- if (is.null(names)) {
    rep(TRUE, length(arguments))
  } else {
    !nzchar(names)
  }
  if (any(unnamed)) {
    if (length(arguments) != 1L || !unnamed[[1L]]) {
      stop("Supply a single mapping or named fields, not both.", call. = FALSE)
    }
    arguments <- arguments[[1L]]
  }
  config_mapping_fields(arguments)
}

config_node_fields <- function(class, fields, path = "") {
  fields <- config_mapping_fields(
    fields,
    if (nzchar(path)) path else "configuration"
  )
  unknown <- setdiff(names(fields), names(class@properties))
  if (length(unknown)) {
    name <- paste(c(if (nzchar(path)) path, unknown[[1L]]), collapse = ".")
    stop(sprintf("Unknown configuration field `%s`.", name), call. = FALSE)
  }
  for (name in names(fields)) {
    field <- paste(c(if (nzchar(path)) path, name), collapse = ".")
    fields[name] <- list(config_coerce_value(
      class@name,
      name,
      fields[[name]],
      field
    ))
  }
  fields
}

config_node <- function(class, fields, path) {
  if (is.null(fields) || S7::S7_inherits(fields, class)) {
    return(fields)
  }
  do.call(class, config_node_fields(class, fields, path))
}

config_character_array <- function(value, path) {
  if (is.atomic(value) && !length(value)) {
    return(character())
  }
  if (is.list(value)) {
    if (!is.null(names(value))) {
      stop(
        sprintf("`%s` must be an array of strings, not a mapping.", path),
        call. = FALSE
      )
    }
    valid <- vapply(
      value,
      function(x) is.character(x) && length(x) == 1L,
      logical(1)
    )
    if (!all(valid)) {
      stop(sprintf("`%s` must contain strings.", path), call. = FALSE)
    }
    value <- as.character(unlist(value, use.names = FALSE))
  }
  value
}

config_environment_value <- function(value, path) {
  if (!is.list(value)) {
    return(value)
  }
  value <- config_mapping_fields(value, path)
  valid <- vapply(
    value,
    function(x) is.character(x) && length(x) == 1L,
    logical(1)
  )
  if (!all(valid)) {
    stop(sprintf("`%s` values must be strings.", path), call. = FALSE)
  }
  structure(
    as.character(unlist(value, use.names = FALSE)),
    names = names(value)
  )
}

config_python <- function(value, path = "python") {
  if (is.null(value) || S7::S7_inherits(value, PythonConfig)) {
    return(value)
  }
  if (is.character(value)) {
    return(ExistingPython(value))
  }
  value <- config_mapping_fields(value, path)
  selectors <- intersect(
    names(value),
    c("existing", "managed", "first_available")
  )
  if (!length(selectors)) {
    return(config_node(ManagedPython, value, path))
  }
  if (length(selectors) != 1L || length(value) != 1L) {
    stop(
      sprintf(
        "`%s` must select existing, managed, or first_available without mixing fields.",
        path
      ),
      call. = FALSE
    )
  }
  switch(
    selectors[[1L]],
    existing = config_node(ExistingPython, value, path),
    managed = {
      if (is.null(value$managed)) {
        stop(sprintf("`%s.managed` must be a mapping.", path), call. = FALSE)
      }
      config_node(ManagedPython, value$managed, paste0(path, ".managed"))
    },
    first_available = {
      candidates <- value$first_available
      if (is.character(candidates)) {
        candidates <- as.list(candidates)
      }
      if (
        !is.list(candidates) ||
          !length(candidates) ||
          !is.null(names(candidates))
      ) {
        stop(
          sprintf("`%s.first_available` must be a non-empty array.", path),
          call. = FALSE
        )
      }
      candidates <- lapply(seq_along(candidates), function(i) {
        candidate <- candidates[[i]]
        if (identical(candidate, "active_venv")) {
          candidate
        } else {
          config_python(candidate, sprintf("%s.first_available[%s]", path, i))
        }
      })
      FirstAvailablePython(candidates)
    }
  )
}

config_coerce_value <- function(class, name, value, path) {
  if (name == "allow_local_binding" && is.null(value)) {
    return("auto")
  }
  if (is.null(value)) {
    return(NULL)
  }
  if (
    name %in%
      c(
        "packages",
        "languages",
        "read_only",
        "read_write",
        "deny",
        "allow",
        "unix_sockets"
      )
  ) {
    return(config_character_array(value, path))
  }
  if (name == "environment") {
    return(config_environment_value(value, path))
  }
  if (
    name == "parameters" && class %in% c("ToolConfig", "ToolParameterConfig")
  ) {
    value <- config_mapping_fields(value, path)
    keys <- structure(as.character(names(value)), names = names(value))
    return(lapply(
      keys,
      function(parameter) {
        config_node(
          ToolParameterConfig,
          value[[parameter]],
          paste0(path, ".", parameter)
        )
      }
    ))
  }
  child <- switch(
    class,
    ConsoleConfig = switch(
      name,
      r = RConfig,
      resolver = ResolverConfig,
      sandbox = SandboxPolicy,
      tool = ToolConfig,
      startup = StartupConfig,
      sessions = SessionsConfig
    ),
    ResolverConfig = switch(name, sandbox = SandboxPolicy),
    SandboxPolicy = switch(
      name,
      filesystem = SandboxFilesystem,
      network = SandboxNetwork
    ),
    SandboxNetwork = switch(
      name,
      proxy = SandboxProxy,
      sockets = SandboxSockets
    )
  )
  if (class == "ConsoleConfig" && name == "python") {
    return(config_python(value, path))
  }
  if (class == "ConsoleConfig" && name == "r" && is.character(value)) {
    return(RConfig(executable = value))
  }
  if (name == "sandbox" && identical(value, FALSE)) {
    return(FALSE)
  }
  if (class == "SandboxPolicy" && name == "network" && is.character(value)) {
    return(value)
  }
  if (
    class == "SandboxPolicy" &&
      name == "profile" &&
      !S7::S7_inherits(value, InheritCodexSandboxProfile) &&
      is.list(value)
  ) {
    value <- config_mapping_fields(value, path)
    if (
      !identical(names(value), "inherit_codex") || is.null(value$inherit_codex)
    ) {
      stop(
        sprintf("`%s` must contain an inherit_codex mapping.", path),
        call. = FALSE
      )
    }
    return(config_node(
      InheritCodexSandboxProfile,
      value$inherit_codex,
      paste0(path, ".inherit_codex")
    ))
  }
  if (!is.null(child)) {
    return(config_node(child, value, path))
  }
  value
}
