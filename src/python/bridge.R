base::local(
  {
    managed <- Sys.getenv("MCP_CONSOLE_MANAGED_PYTHON", unset = NA_character_)
    # Python 3.9 and older are intentionally outside the bridge contract.
    minimum_python <- base::numeric_version("3.10")
    requirements_adapter <- NULL
    pending_python_setup <- NULL
    `%||%` <- function(x, y) if (is.null(x)) y else x

    manifest <- function(packages, python_version, exclude_newer) {
      list(
        packages = I(sort(unique(packages %||% character()))),
        python_version = I(sort(unique(python_version %||% character()))),
        exclude_newer = exclude_newer
      )
    }

    request_json <- function(
      requirements,
      retained_requirements,
      initialized
    ) {
      request <- list(
        requirements = requirements,
        retained_requirements = retained_requirements,
        explicit_declaration = TRUE,
        initialized = initialized
      )
      jsonlite::toJSON(
        request,
        auto_unbox = TRUE,
        null = "null",
        na = "null"
      )
    }

    version_request_json <- function(constraints) {
      jsonlite::toJSON(
        list(
          constraints = I(as.character(constraints %||% character())),
          explicit_declaration = TRUE
        ),
        auto_unbox = TRUE,
        null = "null",
        na = "null"
      )
    }

    activation_manifest <- function(requirements) {
      manifest(
        requirements$packages,
        requirements$python_version,
        requirements$exclude_newer
      )
    }

    conversion_config <- function(selection) {
      python <- selection$embedding$python
      metadata <- selection$metadata
      if (isTRUE(.Call("mcp_console_python_runtime_is_configured"))) {
        metadata <- jsonlite::fromJSON(.Call(
          "mcp_console_python_conversion_metadata"
        ))
      }
      root <- dirname(dirname(python))
      activate <- file.path(dirname(python), "activate_this.py")
      config <- structure(
        list(
          python = python,
          executable = python,
          libpython = selection$embedding$libpython,
          pythonhome = selection$embedding$python_home,
          prefix = selection$prefix,
          exec_prefix = selection$exec_prefix,
          base_prefix = selection$base_prefix,
          base_exec_prefix = selection$base_exec_prefix,
          base_executable = metadata$base_executable,
          pythonpath = metadata$pythonpath,
          version_string = metadata$version,
          version = as.package_version(metadata$version_number),
          architecture = metadata$architecture,
          anaconda = grepl(
            "anaconda|continuum",
            metadata$version,
            ignore.case = TRUE
          ),
          conda = as.logical(metadata$conda),
          virtualenv = if (
            file.exists(file.path(root, "pyvenv.cfg")) ||
              reticulate:::is_virtualenv(root)
          ) {
            root
          } else {
            ""
          },
          virtualenv_activate = if (file.exists(activate)) activate else "",
          python_versions = python,
          numpy = if (!is.null(metadata$numpy$path)) {
            list(
              path = reticulate:::canonical_path(metadata$numpy$path),
              version = numeric_version(reticulate:::clean_version(
                metadata$numpy$version
              ))
            )
          } else {
            NULL
          },
          available = FALSE
        ),
        class = "py_config"
      )
      if (!is.na(managed)) {
        config$ephemeral <- TRUE
      }
      config
    }

    install_managed_python <- function(...) {
      managed <- .Call("mcp_console_python_retained_manifest")
      namespace <- asNamespace("reticulate")
      current_requirements <- function() {
        reticulate:::py_reqs_get()
      }
      resolve <- function(
        packages = current_requirements()$packages,
        python_version = reticulate:::py_reqs_python_version(),
        exclude_newer = current_requirements()$exclude_newer,
        initialized = FALSE
      ) {
        # These manifests come from evaluated py_require() declarations,
        # including package hooks. Missing-import inference uses a separate
        # native request and does not receive declaration authorization.
        current <- current_requirements()
        requirements <- manifest(packages, python_version, exclude_newer)
        retained_requirements <- manifest(
          packages,
          current$python_version,
          exclude_newer
        )
        .Call(
          "mcp_console_resolve_python",
          request_json(
            requirements,
            retained_requirements,
            initialized
          )
        )
      }
      resolve_version <- function(constraints = NULL, uv = NULL) {
        # The host resolver owns its executable and environment; worker code
        # supplies only version constraints.
        .Call(
          "mcp_console_resolve_python_version",
          version_request_json(constraints)
        )
      }
      seed <- jsonlite::fromJSON(managed)
      packages <- unlist(seed$packages, use.names = FALSE)
      python_version <- unlist(seed$python_version, use.names = FALSE)
      if (!length(python_version)) {
        python_version <- NULL
      }
      globals <- get(".globals", envir = namespace)
      requirements <- reticulate:::py_reqs_get()
      changed <- !identical(
        manifest(
          requirements$packages,
          requirements$python_version,
          requirements$exclude_newer
        ),
        manifest(packages, python_version, seed$exclude_newer)
      )
      if (changed) {
        requirements$packages <- packages
        requirements$python_version <- python_version
        requirements$exclude_newer <- seed$exclude_newer
        requirements$history <- c(
          requirements$history,
          list(list(
            requested_from = "mcp-console",
            env_is_package = FALSE,
            packages = packages,
            python_version = python_version,
            exclude_newer = seed$exclude_newer,
            exclude_newer_supplied = !is.null(seed$exclude_newer),
            action = "set"
          ))
        )
        globals$python_requirements <- requirements
      }
      stopifnot(
        !bindingIsActive("python_requirements", globals),
        !bindingIsLocked("python_requirements", globals)
      )
      .Call("mcp_console_python_requirements_set", requirements, NULL)
      rm(requirements)
      rm(list = "python_requirements", envir = globals)
      makeActiveBinding(
        "python_requirements",
        function(value) {
          if (missing(value)) {
            return(.Call("mcp_console_python_requirements_get"))
          }
          activation <- if (.Call("mcp_console_python_activation_pending")) {
            activation_manifest(value)
          } else {
            NULL
          }
          .Call("mcp_console_python_requirements_set", value, activation)
          invisible(value)
        },
        globals
      )

      replace_binding <- function(name, value) {
        was_locked <- bindingIsLocked(name, namespace)
        if (was_locked) {
          unlockBinding(name, namespace)
        }
        on.exit(
          if (was_locked) lockBinding(name, namespace),
          add = TRUE
        )
        assign(name, value, envir = namespace)
        invisible()
      }
      # Keep reticulate's declaration checks and conversion metadata. The
      # native owner supplies the inspected identity and activates it.
      live_python_version <- function() {
        as.character(reticulate::py_version(patch = TRUE))
      }
      check_version <- function(request) {
        current_version <- reticulate::py_version(patch = TRUE)
        if (
          !.Call(
            "mcp_console_python_version_matches",
            as.character(current_version),
            request$python_version
          )
        ) {
          stop(paste0(
            "Python version requirements cannot be changed after Python has ",
            "been initialized.\n",
            "* Python version request: '",
            paste(request$python_version, collapse = ","),
            "'",
            if (request$env_is_package) {
              paste0(" (from package:", request$requested_from, ")")
            },
            "\n* Python version initialized: '",
            current_version,
            "'"
          ))
        }
        invisible()
      }
      check_packages <- function(added, current) {
        added_names <- reticulate:::py_requirement_name(added)
        current_names <- reticulate:::py_requirement_name(current)
        conflicts <- added_names %in% current_names
        if (any(conflicts)) {
          new <- paste0("`", sort(added[conflicts]), "`", collapse = ", ")
          old <- current[current_names %in% added_names[conflicts]]
          old <- paste0("`", sort(old), "`", collapse = ", ")
          stop(paste(
            "After Python has initialized, only `action = 'add'` with new packages is supported.",
            "You tried to add",
            new,
            "but requirements contain",
            old,
            "already."
          ))
        }
        invisible()
      }
      activation_config <- function(selection) {
        inspected <- jsonlite::fromJSON(selection)
        selection <- inspected$selection
        metadata <- inspected$environment
        python <- selection$embedding$python
        config <- globals$py_config
        config$python <- python
        config$executable <- python
        config$libpython <- selection$embedding$libpython
        config$pythonhome <- selection$embedding$python_home
        config$prefix <- selection$prefix
        config$exec_prefix <- selection$exec_prefix
        config$base_exec_prefix <- selection$base_exec_prefix
        config$base_executable <- metadata$base_executable
        config$pythonpath <- metadata$pythonpath
        config$virtualenv <- selection$prefix
        config$virtualenv_activate <- file.path(
          dirname(python),
          "activate_this.py"
        )
        config$python_versions <- python
        config$numpy <- if (!is.null(metadata$numpy)) {
          list(
            path = metadata$numpy$path,
            version = numeric_version(reticulate:::clean_version(
              metadata$numpy$version
            ))
          )
        } else {
          NULL
        }
        config$ephemeral <- TRUE
        config
      }
      available_config <- function(config) {
        config$available <- TRUE
        config
      }
      current_config <- function() globals$py_config
      raise_python_setup_error <- function() check_python_setup(FALSE)
      raise_python_interrupt <- function() {
        # Python already consumed the pending signal; preserve its R condition.
        stop(structure(list(), class = c("interrupt", "condition")))
      }
      record_activation <- function(requirements) {
        .Call(
          "mcp_console_python_activation_record",
          activation_manifest(requirements)
        )
      }
      replace_binding("uv_get_or_create_env", resolve)
      replace_binding("resolve_python_version", resolve_version)
      transition <- function(current, request, initialized) {
        if (!is.null(.Call("mcp_console_running_python"))) {
          # A declaration against a live interpreter needs its R compatibility
          # metadata; ordinary Python execution does not need this attachment.
          reticulate::py_config()
          initialized <- TRUE
        }
        result <- .Call(
          "mcp_console_python_transition",
          current,
          request,
          initialized,
          requirements_adapter
        )
        if (inherits(result, "interrupt")) {
          stop(result)
        }
        result
      }
      manifest_json <- function() {
        jsonlite::toJSON(
          activation_manifest(current_requirements()),
          auto_unbox = TRUE,
          null = "null",
          na = "null"
        )
      }
      project_packages <- function(selection, additions) {
        distribution <- unlist(jsonlite::fromJSON(additions), use.names = FALSE)
        # Construct only the R presentation of an already validated addition.
        # Resolution and interpreter mutation belong to the common owner.
        caller <- topenv(environment())
        request <- list(
          requested_from = environmentName(caller),
          env_is_package = isNamespace(caller),
          packages = distribution,
          python_version = NULL,
          exclude_newer = NULL,
          exclude_newer_supplied = FALSE,
          action = "add"
        )
        current <- current_requirements()
        added <- setdiff(distribution, current$packages)
        initialized <- !is.null(.Call("mcp_console_running_python"))
        current$packages <- if (initialized) {
          c(added, current$packages)
        } else {
          c(current$packages, added)
        }
        current$history <- c(current$history, list(request))
        list(
          manifest = current,
          config = if (initialized) activation_config(selection) else NULL
        )
      }
      commit_import <- function(projection, environment = NULL) {
        if (!is.null(projection$config)) {
          if (!is.null(environment)) {
            active <- jsonlite::fromJSON(environment)
            projection$config$pythonpath <- active$pythonpath
          }
          record_activation(projection$manifest)
          if (reticulate:::is_python_initialized()) {
            globals$py_config <- available_config(projection$config)
          }
        }
        globals$python_requirements <- projection$manifest
        invisible()
      }
      requirements_adapter <<- environment()
      .Call("mcp_console_python_requirements_attach", requirements_adapter)
      replace_binding("py_reqs_transition", transition)
      initialize_requirements <- function() {
        invisible(.Call(
          "mcp_console_python_initialized",
          activation_manifest(current_requirements())
        ))
      }
      setHook(
        "reticulate.onPyInit",
        initialize_requirements,
        action = "append"
      )
      invisible()
    }

    if (!is.na(managed)) {
      Sys.unsetenv("MCP_CONSOLE_MANAGED_PYTHON")
      setHook(
        packageEvent("reticulate", "onLoad"),
        install_managed_python,
        action = "append"
      )
      if ("reticulate" %in% loadedNamespaces()) {
        install_managed_python()
      }
    }

    check_python_setup <- function(completed) {
      # NULL is shutdown cancellation, with no retained Python exception.
      if (is.null(completed)) {
        return(FALSE)
      }
      if (!completed) {
        # Native setup retains the exception without printing it. Preserve
        # reticulate's R condition classes, last error, and interrupt handling.
        reticulate::py_eval(
          "(lambda: None).__builtins__['__import__']('_mcp_console').raise_setup_error()",
          convert = TRUE
        )
      }
      TRUE
    }

    attached_python_config <- function() {
      reticulate::py_config()
    }

    check_python_version <- function(python_config, strict = TRUE) {
      if (python_config$version < minimum_python) {
        if (!strict) {
          return(invisible(FALSE))
        }
        stop(
          paste0(
            "MCP Console requires Python 3.10 or later; selected ",
            "interpreter reports Python ",
            as.character(python_config$version)
          ),
          call. = FALSE
        )
      }
      TRUE
    }

    initialize_python_runtime <- function(strict = FALSE) {
      if (.Call("mcp_console_python_setup_in_progress")) {
        # A site hook explicitly requested conversion. Its outer setup still
        # owns site processing, defaults, and publication of runtime completion.
        return(invisible(TRUE))
      }
      pending <- pending_python_setup
      pending_python_setup <<- NULL
      python_config <- attached_python_config()
      if (!check_python_version(python_config, strict)) {
        return(invisible(FALSE))
      }
      if (!is.null(pending)) {
        # Fresh selection already ran setup before conversion attachment.
        # R can now rethrow its retained exception without replaying hooks.
        if (!check_python_setup(pending$completed)) {
          return(invisible(FALSE))
        }
      }
      if (!isTRUE(.Call("mcp_console_python_runtime_is_configured"))) {
        completed <- check_python_setup(.Call(
          "mcp_console_setup_python_runtime",
          python_config$libpython,
          !is.na(managed)
        ))
        if (!completed) {
          return(invisible(FALSE))
        }
      }
      # Publish metadata after shared setup, including adoption of an
      # interpreter initialized by native R startup before the adapter existed.
      live <- conversion_config(jsonlite::fromJSON(.Call(
        "mcp_console_running_python"
      )))
      globals <- get(".globals", envir = asNamespace("reticulate"))
      globals$py_config[c("pythonpath", "numpy", "ephemeral")] <- live[c(
        "pythonpath",
        "numpy",
        "ephemeral"
      )]
      invisible(TRUE)
    }

    install_python_hooks <- function(...) {
      on_python_init <- function() {
        initialize_python_runtime(strict = FALSE)
      }
      base::setHook(
        "reticulate.onPyInit",
        on_python_init,
        action = "append"
      )
      if (reticulate:::is_python_initialized()) {
        # The initializer has registered the already-running identity before
        # installing these hooks. Its original onPyInit event has already run.
        if (!is.na(managed)) {
          requirements_adapter$initialize_requirements()
        }
        on_python_init()
      }
      invisible()
    }
    evaluate_impl <- function() {
      if (source %in% c("select", "select_optional")) {
        return(selected_python(optional = identical(source, "select_optional")))
      }
      if (identical(source, "attach")) {
        attached_python_config()
        return(invisible())
      }
      stopifnot(identical(source, "setup"))
      initialize_python_runtime(strict = TRUE)
      invisible()
    }

    interrupted <- FALSE

    evaluate <- function(id) {
      interrupted <<- FALSE
      # Observe the condition without handling it; R_tryEval remains the boundary.
      withCallingHandlers(
        evaluate_impl(),
        interrupt = function(condition) interrupted <<- TRUE,
        error = function(condition) interrupted <<- FALSE
      )
    }

    tools <- base::attach(
      NULL,
      pos = 2L,
      name = "tools:mcp-console",
      warn.conflicts = FALSE
    )
    # Resolve the actual exports lazily, preserving reticulate's arguments and
    # caller provenance without loading its namespace just to install names.
    base::delayedAssign("py_import", reticulate::import, assign.env = tools)
    base::delayedAssign("py_eval", reticulate::py_eval, assign.env = tools)
    base::delayedAssign(
      "py_run_string",
      reticulate::py_run_string,
      assign.env = tools
    )
    base::delayedAssign(
      "py_require",
      reticulate::py_require,
      assign.env = tools
    )

    environment()
  },
  envir = base::new.env(parent = base::baseenv())
)
