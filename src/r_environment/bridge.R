base::local(
  {
    managed <- base::Sys.getenv("MCP_CONSOLE_R_LIBRARY", unset = "")
    if (base::nzchar(managed)) {
      managed <- base::normalizePath(managed, winslash = "/", mustWork = TRUE)
    }
    temporary_library <- NULL
    if (base::identical(base::Sys.getenv("MCP_CONSOLE_SANDBOX"), "1")) {
      temporary_library <- base::tempfile(
        "mcp-console-library-",
        tmpdir = base::tempdir()
      )
      base::stopifnot(base::dir.create(temporary_library))
      temporary_library <- base::normalizePath(
        temporary_library,
        winslash = "/",
        mustWork = TRUE
      )
      base::.libPaths(base::c(temporary_library, base::.libPaths()))
      base::stopifnot(base::identical(
        base::.libPaths()[[1L]],
        temporary_library
      ))
    }
    in_progress <- base::character()
    dynamic_resolution <- base::identical(
      base::Sys.getenv(
        "MCP_CONSOLE_DYNAMIC_ENVIRONMENT_RESOLUTION",
        unset = "1"
      ),
      "1"
    )

    original_library <- base::library
    original_load_namespace <- base::loadNamespace
    original_with_restarts <- base::withRestarts

    is_plain_package_name <- function(package) {
      base::is.character(package) &&
        base::length(package) == 1L &&
        !base::is.na(package) &&
        base::grepl(
          "^[A-Za-z](?:[A-Za-z0-9.]*[A-Za-z0-9])?\\z",
          package,
          perl = TRUE,
          useBytes = TRUE
        )
    }

    package_available <- function(package) {
      base::paste0("package:", package) %in%
        base::search() ||
        base::isNamespaceLoaded(package) ||
        base::length(base::find.package(package, quiet = TRUE)) != 0L
    }

    apply_managed_library <- function(library) {
      library <- base::normalizePath(
        library,
        winslash = "/",
        mustWork = TRUE
      )
      paths <- base::.libPaths()
      paths <- paths[!paths %in% temporary_library]
      index <- base::match(managed, paths)
      paths <- paths[!paths %in% managed]
      existing <- library %in% paths
      if (!existing) {
        paths <- base::append(
          paths,
          library,
          after = if (base::is.na(index)) 0L else index - 1L
        )
      }
      base::.libPaths(base::c(temporary_library, paths))
      if (!library %in% base::.libPaths()) {
        base::stop("resolved R library was not added to .libPaths()")
      }
      # R deduplicates library paths. Reusing a project entry does not give
      # Console ownership of that entry on the next activation.
      managed <<- if (existing) "" else library
      library
    }

    prepare <- function(library) {
      result <- base::tryCatch(
        {
          library <- apply_managed_library(library)
          base::list(kind = "prepared", library = library)
        },
        error = function(error) {
          base::list(kind = "failed", message = base::conditionMessage(error))
        }
      )
      jsonlite::toJSON(
        result,
        auto_unbox = TRUE,
        null = "null",
        na = "null"
      )
    }

    activate_managed_library <- function(library) {
      base::suspendInterrupts({
        applied <- base::tryCatch(
          apply_managed_library(library),
          error = base::identity
        )
        if (base::inherits(applied, "error")) {
          message <- base::conditionMessage(applied)
          base::invisible(
            .Call("mcp_console_r_activation_failed", library, message)
          )
          base::list(
            kind = "failed",
            failure = "activation",
            message = message
          )
        } else {
          base::invisible(.Call("mcp_console_r_activated", applied))
          base::list(kind = "ready")
        }
      })
    }

    ensure_r_package <- function(package) {
      base::stopifnot(is_plain_package_name(package))
      if (package_available(package) || package %in% in_progress) {
        return(base::list(kind = "ready"))
      }

      in_progress <<- base::c(in_progress, package)
      base::on.exit(
        in_progress <<- in_progress[in_progress != package],
        add = TRUE
      )

      response <- .Call("mcp_console_resolve_r", package)
      if (base::identical(response, "unavailable")) {
        return(base::list(kind = "ready"))
      }
      if (
        !base::is.character(response) ||
          base::anyNA(response) ||
          !base::length(response) %in% 2:3
      ) {
        base::stop("invalid R environment resolver response")
      }

      if (
        base::length(response) == 2L &&
          base::identical(response[[1L]], "resolved")
      ) {
        return(activate_managed_library(response[[2L]]))
      }

      if (
        base::length(response) == 3L &&
          base::identical(response[[1L]], "failed") &&
          response[[2L]] %in% base::c("host", "interrupted")
      ) {
        return(base::list(
          kind = "failed",
          failure = response[[2L]],
          message = response[[3L]]
        ))
      }

      base::stop("invalid R environment resolver response")
    }

    signal_resolution_failure <- function(outcome) {
      # Let the original base operation report an unresolved package.
      if (base::identical(outcome$failure, "host")) {
        return(base::invisible(NULL))
      }
      if (base::identical(outcome$failure, "interrupted")) {
        condition <- base::structure(
          base::list(message = outcome$message, call = NULL),
          class = base::c("interrupt", "condition")
        )
        base::stop(condition)
      }
      base::stop(outcome$message, call. = FALSE)
    }

    # Keep R's loadNamespace syntax intact for packages that inspect its body.
    # Intercept only the retryable missing-package path through lexical scope.
    prepare_namespace_package <- function(load_namespace_frame) {
      condition <- base::get("cond", load_namespace_frame, inherits = FALSE)
      partial <- base::get("partial", load_namespace_frame, inherits = FALSE)
      if (
        !base::is.null(condition$lib.loc) ||
          !base::identical(partial, FALSE) ||
          !is_plain_package_name(condition$package)
      ) {
        return(FALSE)
      }

      outcome <- ensure_r_package(condition$package)
      if (base::identical(outcome$kind, "failed")) {
        signal_resolution_failure(outcome)
      }
      package_available(condition$package)
    }

    make_managed_with_restarts <- function() {
      restart_environment <- base::new.env(
        parent = base::environment(original_with_restarts)
      )
      base::assign(
        ".mcp_console_prepare_namespace",
        prepare_namespace_package,
        envir = restart_environment
      )
      base::lockEnvironment(restart_environment, bindings = TRUE)
      managed_with_restarts <- base::`environment<-`(
        original_with_restarts,
        restart_environment
      )
      # Preparation returns before base signals the condition, preserving its
      # original restart frames and Calls output when the package stays missing.
      base::`body<-`(
        managed_with_restarts,
        value = base::substitute(
          {
            if (.mcp_console_prepare_namespace(base::parent.frame())) {
              return(NULL)
            }
            original_body
          },
          base::list(original_body = base::body(original_with_restarts))
        )
      )
    }

    make_managed_load_namespace <- function() {
      load_namespace_environment <- base::new.env(
        parent = base::environment(original_load_namespace)
      )
      base::assign(
        "withRestarts",
        make_managed_with_restarts(),
        envir = load_namespace_environment
      )

      managed_load_namespace <- base::`environment<-`(
        original_load_namespace,
        load_namespace_environment
      )
      base::assign(
        "loadNamespace",
        managed_load_namespace,
        envir = load_namespace_environment
      )
      base::lockEnvironment(load_namespace_environment, bindings = TRUE)
      base::stopifnot(
        !base::identical(
          base::environment(managed_load_namespace),
          base::environment(original_load_namespace)
        ),
        base::identical(
          base::formals(managed_load_namespace),
          base::formals(original_load_namespace)
        ),
        base::identical(
          base::body(managed_load_namespace),
          base::body(original_load_namespace)
        )
      )
      managed_load_namespace
    }

    prepare_library_package <- function(package) {
      if (!is_plain_package_name(package)) {
        return(base::invisible(NULL))
      }
      outcome <- ensure_r_package(package)
      if (base::identical(outcome$kind, "failed")) {
        signal_resolution_failure(outcome)
      }
      base::invisible(NULL)
    }

    make_managed_library <- function() {
      library_environment <- base::new.env(
        parent = base::environment(original_library)
      )
      base::assign(
        ".mcp_console_prepare_library",
        prepare_library_package,
        envir = library_environment
      )
      base::lockEnvironment(library_environment, bindings = TRUE)
      managed_library <- base::`environment<-`(
        original_library,
        library_environment
      )
      # Keep base's body in the caller-visible frame so sys.call(), promises,
      # and condition construction retain their ordinary R semantics.
      base::`body<-`(
        managed_library,
        value = base::substitute(
          {
            if (!base::missing(package) && base::is.null(lib.loc)) {
              .mcp_console_prepare_library(
                if (character.only) {
                  package
                } else {
                  base::as.character(base::substitute(package))
                }
              )
            }
            original_body
          },
          base::list(original_body = base::body(original_library))
        )
      )
    }

    replace_base_binding <- function(name, value) {
      environment <- base::baseenv()
      base::stopifnot(base::bindingIsLocked(name, environment))
      base::unlockBinding(name, environment)
      base::on.exit(base::lockBinding(name, environment))
      base::assign(name, value, envir = environment)
    }

    if (dynamic_resolution) {
      replace_base_binding("library", make_managed_library())
      replace_base_binding("loadNamespace", make_managed_load_namespace())
    }

    base::environment()
  },
  envir = base::new.env(parent = base::baseenv())
)
