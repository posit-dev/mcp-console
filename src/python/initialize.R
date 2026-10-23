base::local(
  {
    namespace <- NULL
    globals <- NULL
    selected <- NULL
    inspected <- NULL
    selection_callback <- NULL
    initializing <- FALSE
    incomplete_attachment <- FALSE

    same_python_selection <- function(requested, running) {
      requested <- reticulate:::normalize_python_path(requested)$path
      # Virtualenv executables can link to the same base binary. Compare their
      # containing directories too, while allowing aliases within one bin directory.
      identical(
        normalizePath(dirname(requested), mustWork = FALSE),
        normalizePath(dirname(running), mustWork = FALSE)
      ) &&
        identical(
          normalizePath(requested, mustWork = FALSE),
          normalizePath(running, mustWork = FALSE)
        )
    }

    replace_binding <- function(name, value) {
      was_locked <- bindingIsLocked(name, namespace)
      if (was_locked) {
        unlockBinding(name, namespace)
      }
      assign(name, value, envir = namespace)
      if (was_locked) lockBinding(name, namespace)
    }

    select_python <- function(
      required_module = NULL,
      use_environment = NULL,
      run_before_initialized = FALSE,
      optional = FALSE
    ) {
      if (is.null(namespace)) {
        asNamespace("reticulate")
      }
      running <- .Call("mcp_console_running_python")
      if (!is.null(running)) {
        identity <- jsonlite::fromJSON(running)
        requests <- globals$required_python_version
        # An unchanged hint already selected the captured identity. Resolving
        # it again after Python changes cwd or PATH would select a new path.
        if (!.Call("mcp_console_python_environment_selection_unchanged")) {
          requests <- c(Sys.getenv("RETICULATE_PYTHON"), requests)
        }
        for (requested in requests) {
          if (
            nzchar(requested) &&
              (requested == "managed" ||
                !same_python_selection(requested, identity$embedding$python))
          ) {
            stop(
              "Python is already initialized with another selection; restart required",
              call. = FALSE
            )
          }
        }
        inspected <<- running
        selected <<- state$conversion_config(identity)
        return(selected)
      }

      if (!is.null(selected)) {
        return(selected)
      }
      if (reticulate:::is_python_initialized()) {
        # A startup package may have initialized Python before this adapter
        # existed. Its running identity takes precedence over selection hints;
        # do not rediscover an executable or rerun environment activation.
        config <- globals$py_config
        sys <- reticulate::import("sys", convert = TRUE)
        inspected <<- jsonlite::toJSON(
          list(
            embedding = list(
              python = sys$executable,
              libpython = config$libpython,
              python_home = config$pythonhome
            ),
            prefix = sys$prefix,
            exec_prefix = sys$exec_prefix,
            base_prefix = sys$base_prefix,
            base_exec_prefix = sys$base_exec_prefix,
            metadata = list(
              base_executable = sys$`_base_executable`,
              pythonpath = paste(sys$path, collapse = .Platform$path.sep),
              version = gsub("\n", " ", sys$version, fixed = TRUE),
              version_number = as.character(config$version),
              architecture = config$architecture,
              conda = isTRUE(as.logical(config$conda)),
              numpy = if (is.null(config$numpy)) {
                NULL
              } else {
                list(
                  path = config$numpy$path,
                  version = as.character(config$numpy$version)
                )
              }
            )
          ),
          auto_unbox = TRUE,
          null = "null"
        )
        selected <<- config
        return(selected)
      }
      if (run_before_initialized) {
        # R-first calls arrive through reticulate::ensure_python_initialized(),
        # which has already invoked this callback.
        callback <- getOption("reticulate.python.beforeInitialized")
        if (is.function(callback)) {
          was_initializing <- initializing
          initializing <<- TRUE
          on.exit(initializing <<- was_initializing, add = TRUE)
          callback()
          selection_callback <<- callback
        }
      }

      # Keep reticulate's discovery and its R-side selection hints in one place.
      if (!is.null(required_module)) {
        required_module <- strsplit(required_module, ".", fixed = TRUE)[[1L]][[
          1L
        ]]
      }
      # Preserve the call name in R's discovery error diagnostics.
      py_discover_config <- reticulate:::py_discover_config
      config <- local({
        previous_options <- options(reticulate.python.initializing = TRUE)
        on.exit(options(previous_options), add = TRUE)
        py_discover_config(required_module, use_environment)
      })
      python_not_found <- function(message) {
        hint <- paste0(
          'See the Python "Order of Discovery" here: ',
          'https://rstudio.github.io/reticulate/articles/versions.html#order-of-discovery.'
        )
        stop(paste(message, hint, sep = "\n"), call. = FALSE)
      }
      if (is.null(config)) {
        # Background initialization may finish with R alone. Explicit hints,
        # incompatible interpreters, and discovery errors still fail normally.
        if (optional) {
          return(FALSE)
        }
        python_not_found(
          "Installation of Python not found, Python bindings not loaded."
        )
      } else if (reticulate:::is_incompatible_arch(config)) {
        fmt <- "Your current architecture is %s; however, this version of Python was compiled for %s."
        message <- sprintf(
          fmt,
          reticulate:::current_python_arch(),
          config$architecture
        )
        python_not_found(message)
      }
      state$check_python_version(config)
      # Discovery retains reticulate's selection precedence and metadata.
      # Console owns the embedding fields consumed by both native startup
      # and reticulate's later attachment to that same interpreter.
      inspected <<- tryCatch(
        .Call("mcp_console_inspect_python", config$python),
        error = function(error) {
          class(error) <- c("console_python_inspection_error", class(error))
          stop(error)
        }
      )
      embedding <- jsonlite::fromJSON(inspected)$embedding
      config$python <- embedding$python
      config$executable <- embedding$python
      config$libpython <- embedding$libpython
      config$pythonhome <- embedding$python_home
      selected <<- config
      config
    }

    cancel_selection <- function() {
      # Only called before CPython starts. Selection has no process mutations.
      inspected <<- NULL
      selected <<- NULL
      selection_callback <<- NULL
      invisible()
    }

    state$selected_python <- function(optional = FALSE) {
      # Extend a fresh selection's cleanup through serialization. A cached
      # selection may already belong to a running interpreter.
      pending <- is.null(selected) &&
        !reticulate::py_available(initialize = FALSE)
      on.exit(if (pending && !is.null(selected)) cancel_selection(), add = TRUE)
      config <- tryCatch(
        select_python(run_before_initialized = TRUE, optional = optional),
        console_python_inspection_error = function(error) {
          message("Error: ", conditionMessage(error))
          NULL
        }
      )
      if (identical(config, FALSE)) {
        return("null")
      }
      # Inspection has not committed an interpreter or environment. Report
      # its failure as cell output and leave the existing worker retryable.
      if (is.null(config)) {
        return("")
      }
      result <- inspected
      pending <- FALSE
      result
    }
    state$cancel_python_selection <- function() {
      cancel_selection()
      0L
    }

    finish_python_initialization <- function() {
      invisible(.Call("mcp_console_finish_python_initialization"))
    }

    install_console_services <- function() {
      invisible(.Call(
        "mcp_console_install_python_services",
        reticulate::py_config()$libpython
      ))
    }

    attach_python <- function(config) {
      numpy_load_error <- tryCatch(
        {
          if (is.null(config$numpy) || config$numpy$version < "1.6") {
            "installation of Numpy >= 1.6 not found"
          } else {
            ""
          }
        },
        error = function(error) "<unknown>"
      )
      # Console owns CPython's environment. Reticulate attaches conversion
      # and callbacks without activating or changing the running interpreter.
      on.exit(finish_python_initialization(), add = TRUE)
      reticulate:::py_initialize(
        config$python,
        config$libpython,
        config$pythonhome,
        "",
        config$version$major,
        config$version$minor,
        interactive(),
        numpy_load_error
      )

      # Reticulate owns conversion, cross-language calls, and event integration.
      reg.finalizer(
        globals,
        function(environment) {
          try(reticulate:::py_allow_threads_impl(FALSE))
          if (
            tolower(Sys.getenv("RETICULATE_ENABLE_PYTHON_FINALIZER")) %in%
              c("true", "1", "yes")
          ) {
            reticulate:::py_finalize()
          }
        },
        onexit = TRUE
      )
      config$available <- TRUE
      # Declaration diagnostics are reticulate compatibility, not selection
      # or activation. Preserve its opt-out and warning behavior.
      if (nzchar(config$virtualenv)) {
        check_packages <- tolower(Sys.getenv(
          "RETICULATE_CHECK_REQUIRED_PACKAGES",
          "true"
        )) %in%
          c("true", "1", "yes")
        if (check_packages) {
          tryCatch(
            reticulate:::check_virtualenv_required_packages(config),
            error = function(error) invisible()
          )
        }
      }
      config
    }

    initialize_python <- function(
      required_module = NULL,
      use_environment = NULL
    ) {
      config <- select_python(required_module, use_environment)
      invisible(.Call(
        "mcp_console_initialize_python",
        inspected,
        system.file("python", package = "reticulate")
      ))
      # Reticulate publishes .globals$py_config only after this returns, so
      # unfinished attachment remains eligible for its existing retry path.
      attach_python(config)
    }

    install_python_initializer <- function(...) {
      namespace <<- asNamespace("reticulate")
      globals <<- get(".globals", envir = namespace)
      replace_binding("initialize_python", initialize_python)
      original_ensure_initialized <- get(
        "ensure_python_initialized",
        envir = namespace
      )
      replace_binding("ensure_python_initialized", function(...) {
        if (incomplete_attachment) {
          stop(
            "R/Python attachment is incomplete; restart required",
            call. = FALSE
          )
        }
        was_initializing <- initializing
        initializing <<- TRUE
        on.exit(initializing <<- was_initializing, add = TRUE)
        completed <- FALSE
        on.exit(
          {
            # Before py_config publication, reticulate can retry attachment to
            # the same interpreter. Later hooks may have arbitrary partial effects.
            if (!completed && !is.null(globals$py_config)) {
              incomplete_attachment <<- TRUE
            }
          },
          add = TRUE
        )
        callback <- getOption("reticulate.python.beforeInitialized")
        if (
          !is.null(selection_callback) &&
            identical(callback, selection_callback) &&
            !is.null(.Call("mcp_console_running_python"))
        ) {
          options(reticulate.python.beforeInitialized = NULL)
          on.exit(
            {
              if (is.null(getOption("reticulate.python.beforeInitialized"))) {
                options(reticulate.python.beforeInitialized = callback)
              }
            },
            add = TRUE
          )
        }
        result <- original_ensure_initialized(...)
        completed <- TRUE
        invisible(result)
      })
      original_py <- activeBindingFunction("py", namespace)
      lazy_main <- NULL
      was_locked <- bindingIsLocked("py", namespace)
      if (was_locked) {
        unlockBinding("py", namespace)
      }
      makeActiveBinding(
        "py",
        function() {
          if (!is.null(globals$py_config)) {
            namespace$ensure_python_initialized()
            return(original_py())
          }
          # Initialization hooks retain reticulate's previous NULL behavior.
          if (initializing) {
            return(NULL)
          }
          # Package conflict checks read active bindings. A delayed module
          # attaches only on attribute access, through reticulate's own proxy.
          if (is.null(lazy_main)) {
            lazy_main <<- namespace$import_main(delay_load = TRUE)
          }
          lazy_main
        },
        namespace
      )
      if (was_locked) {
        lockBinding("py", namespace)
      }
      original_use_python <- get("use_python", envir = namespace)
      replace_binding("use_python", function(python, required = NULL) {
        running <- .Call("mcp_console_running_python")
        if (!is.null(running) && !identical(required, FALSE)) {
          identity <- jsonlite::fromJSON(running)
          if (!same_python_selection(python, identity$embedding$python)) {
            stop(
              "Python is already initialized with another selection; restart required",
              call. = FALSE
            )
          }
        }
        original_use_python(python, required)
      })
      original_inject_hooks <- get("py_inject_hooks", envir = namespace)
      inject_hooks <- function() {
        original_inject_hooks()
        # Install after reticulate's input hook, including R-first startup.
        install_console_services()
      }
      replace_binding("py_inject_hooks", inject_hooks)
      # Reticulate reinstalls its interrupt handler after injecting hooks.
      replace_binding("install_interrupt_handlers", install_console_services)

      if (reticulate:::is_python_initialized()) {
        select_python()
        invisible(.Call(
          "mcp_console_initialize_python",
          inspected,
          system.file("python", package = "reticulate")
        ))
        finish_python_initialization()
        install_console_services()
      }
      # Already-live interpreters need their retained identity registered
      # before the bridge's eager initialization hook enters common setup.
      state$install_python_hooks()
      invisible()
    }

    setHook(
      packageEvent("reticulate", "onLoad"),
      install_python_initializer,
      action = "append"
    )
    if ("reticulate" %in% loadedNamespaces()) {
      install_python_initializer()
    }
    invisible()
  },
  envir = base::new.env(parent = environment())
)
