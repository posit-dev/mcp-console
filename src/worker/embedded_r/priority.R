function(source, interruptible) {
  # R updates its shared legacy error buffer even for caught errors. Restore
  # the foreground's message before its error handler or REPL resumes.
  previous_error <- geterrmessage()
  on.exit(.Internal(seterrmessage(previous_error)))

  # The ordinary REPL owns R's global deferred-warning queue. Keep this
  # evaluation's warn=0 conditions local, including when source raises an error.
  # Warning conditions expose no immediate.=TRUE flag, so warn=0 warnings are
  # delivered after the private expression even when immediate delivery was requested.
  # Local errors bypass options(error); neither path changes last.warning.
  warnings <- list()
  messages <- character()
  phase <- "SyntaxError"
  source_call <- quote(eval(expression, globalenv()))
  with_interrupts <- if (interruptible) allowInterrupts else force

  condition_call <- function(condition) {
    call <- conditionCall(condition)
    if (identical(call, source_call)) NULL else call
  }

  flush_warnings <- function() {
    if (length(messages)) {
      cat(
        ngettext(length(messages), "Warning message:\n", "Warning messages:\n"),
        file = stderr()
      )
      print.warnings(
        structure(warnings, names = messages, dots = list(file = stderr())),
        header = character()
      )
      warnings <<- list()
      messages <<- character()
    }
  }

  failure <- tryCatch(
    with_interrupts(withCallingHandlers(
      {
        expressions <- parse(text = source)
        phase <- "RError"
        for (expression in expressions) {
          value <- withVisible(eval(expression, globalenv()))
          if (value$visible) {
            print(value$value)
          }
          flush_warnings()
        }
        NULL
      },
      warning = function(condition) {
        warn <- getOption("warn", 0L)
        if (is.na(warn)) {
          warn <- 0L
        }
        restart <- findRestart("muffleWarning", condition)
        if (
          warn == 0L &&
            is.null(getOption("warning.expression")) &&
            !is.null(restart)
        ) {
          if (length(messages) < getOption("nwarnings")) {
            warnings[length(warnings) + 1L] <<- list(condition_call(condition))
            messages <<- c(messages, conditionMessage(condition))
          }
          invokeRestart(restart)
        }
      }
    )),
    error = identity,
    # An explicit handler in submitted code can consume the condition first.
    interrupt = identity
  )
  if (!is.null(failure)) {
    if (inherits(failure, "interrupt")) {
      flush_warnings()
      return(c("Interrupt", "R evaluation interrupted"))
    }
    call <- condition_call(failure)
    if (phase == "SyntaxError") {
      call <- NULL
    }
    prefix <- if (is.null(call)) {
      "Error: "
    } else {
      paste0("Error in ", paste(deparse(call), collapse = "\n"), " : ")
    }
    message <- paste0(prefix, conditionMessage(failure), "\n")
    cat(message, file = stderr())
    flush_warnings()
    return(c(phase, message))
  }
  NULL
}
