"""Self-contained inspection programs, evaluated through the control socket."""

import json
from pathlib import Path


def inspection_source(language: str, output: Path) -> str:
    """Write the bounded payload separately from user-defined method diagnostics."""
    if language == "r":
        return _R_SOURCE.replace("OUTPUT_PATH", json.dumps(str(output)))
    assert language == "python"
    return f"exec({_PYTHON_SOURCE!r}, {{'OUTPUT_PATH': {str(output)!r}}})"


_R_SOURCE = """
local({
  variables <- utils::ls.str(envir = globalenv(), all.names = FALSE)
  shown <- variables[seq_len(min(length(variables), 100L))]
  attributes(shown) <- attributes(variables)
  output <- capture.output({
    if (length(shown)) {
      print(shown, max.level = 1L, vec.len = 4L, list.len = 6L,
            nchar.max = 80L, give.attr = FALSE, width = 60L,
            strict.width = "cut", deparse.lines = 2L)
    } else {
      cat("(empty)\\n")
    }
    if (length(variables) > length(shown)) {
      cat("...", length(variables) - length(shown), "more objects\\n")
    }
  })
  tables <- Filter(function(name) {
    is.data.frame(get(name, envir = globalenv(), inherits = FALSE))
  }, as.character(shown))
  omitted_lines <- omitted_tables <- 0L
  repeat {
    suffix <- if (omitted_lines + omitted_tables) {
      paste("...", omitted_lines, "preview lines and", omitted_tables, "table names omitted")
    } else character()
    encoded <- jsonlite::toJSON(list(
      text = paste(c(output, suffix), collapse = "\\n"), tables = I(tables),
      omitted_lines = omitted_lines, omitted_tables = omitted_tables
    ), auto_unbox = TRUE)
    if (nchar(encoded, type = "bytes") <= 7000L) break
    if (length(output)) {
      output <- head(output, -1L)
      omitted_lines <- omitted_lines + 1L
    } else {
      tables <- head(tables, -1L)
      omitted_tables <- omitted_tables + 1L
    }
  }
  writeLines(enc2utf8(encoded), OUTPUT_PATH, useBytes = TRUE)
})
"""

# A private exec namespace keeps imports and helper bindings out of __main__.
# Exact builtin types avoid user repr/iteration methods, including in containers.
_PYTHON_SOURCE = """
import __main__
import json
from itertools import islice
from pathlib import Path
import pandas as pd

def preview(value: object, depth: int = 0) -> str:
    kind = type(value)
    name = type.__getattribute__(kind, "__name__")[:80]
    if value is None or kind is bool or kind is float or kind is complex:
        return repr(value)
    if kind is int:
        return repr(value) if value.bit_length() <= 256 else f"<int: {value.bit_length()} bits>"
    if kind is str or kind is bytes:
        return repr(value[:80]) + ("..." if len(value) > 80 else "")
    if kind is list or kind is tuple or kind is dict:
        if depth == 2:
            return f"<{name}: {len(value)} items>"
        if kind is dict:
            entries = list(islice(value.items(), 4))
            parts = [preview(key, depth + 1) + ": " + preview(item, depth + 1)
                     for key, item in entries]
            left, right = "{", "}"
        else:
            parts = [preview(item, depth + 1) for item in value[:4]]
            left, right = ("[", "]") if kind is list else ("(", ")")
        if len(value) > 4:
            parts.append("...")
        if kind is tuple and len(value) == 1:
            parts.append("")
        return left + ", ".join(parts) + right
    return f"<{name}>"

items = sorted((name, value) for name, value in vars(__main__).copy().items()
               if not name.startswith("__"))
lines = [name[:80] + " = " + preview(value) for name, value in items[:100]]
if not items:
    lines.append("(empty)")
if len(items) > 100:
    lines.append(f"... {len(items) - 100} more symbols")
tables = [name for name, value in items[:100] if isinstance(value, pd.DataFrame)]
omitted_lines = omitted_tables = 0
while True:
    suffix = ([f"... {omitted_lines} preview lines and {omitted_tables} table names omitted"]
              if omitted_lines + omitted_tables else [])
    encoded = json.dumps({"text": "\\n".join(lines + suffix), "tables": tables,
                          "omitted_lines": omitted_lines, "omitted_tables": omitted_tables})
    if len(encoded.encode()) <= 7000:
        break
    if lines:
        lines.pop()
        omitted_lines += 1
    else:
        tables.pop()
        omitted_tables += 1
Path(OUTPUT_PATH).write_text(encoded, encoding="utf-8")
"""
