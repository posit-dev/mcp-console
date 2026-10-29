"""Self-contained inspection programs, evaluated through the control socket."""

R_SOURCE = """
local({
  variables <- utils::ls.str(envir = globalenv(), all.names = FALSE)
  shown <- variables[seq_len(min(length(variables), 100L))]
  attributes(shown) <- attributes(variables)
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
"""

# A private exec namespace keeps imports and helper bindings out of __main__.
# Exact builtin types avoid user repr/iteration methods, including in containers.
PYTHON_SOURCE = (
    "exec("
    + repr("""
import __main__
from itertools import islice

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
for name, value in items[:100]:
    print(name[:80] + " = " + preview(value))
if not items:
    print("(empty)")
if len(items) > 100:
    print(f"... {len(items) - 100} more symbols")
""")
    + ", {})"
)
