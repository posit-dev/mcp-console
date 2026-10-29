# Install calling observers without enclosing the DLL REPL in an R function.
# withCallingHandlers() would add frames around top-level source, while
# globalCallingHandlers() would retain observers in its persistent registry.
# Use the primitive directly so its native context baseline is updated without
# changing globalCallingHandlers()'s registry. R_ToplevelExec restores the stack.
# An explicit globalCallingHandlers() registration can replace these observers;
# such an unclassified unwind is reported as an abort rather than a stale error.
quote(.Internal(.addGlobHands(
  c("interrupt", "error"),
  list(
    function(condition) .Call("mcp_console_repl_interrupt"),
    function(condition) {
      .Call(
        "mcp_console_repl_error",
        # Do not mistake output from options(error) for a hidden diagnostic.
        isTRUE(getOption("show.error.messages")) && sink.number("message") == 2L
      )
    }
  ),
  baseenv(),
  NULL,
  TRUE
)))
