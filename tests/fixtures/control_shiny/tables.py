"""App-side table actions using private immediate evaluations.

The app serializes these actions with variable inspection and direct Immediate
submissions. A private result file avoids Console's 8 KiB display-text budget;
only a bounded row block crosses the process boundary, never the entire table.
"""

import json
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

RUNTIME = Path(__file__).with_name("table_runtime.py").read_text()
Evaluator = Callable[[str, str], Awaitable[dict[str, Any]]]


class TableViewer:
    def __init__(self, evaluate: Evaluator) -> None:
        self.evaluate = evaluate
        self._directory: tempfile.TemporaryDirectory[str] | None = None
        self._module: str | None = None
        self._view: str | None = None

    async def _evaluate(self, language: str, source: str) -> None:
        result = await self.evaluate(language, source)
        if error := result.get("execution_error"):
            raise RuntimeError(f"{error['ename']}: {error['evalue']}")
        if result.get("isError"):
            raise RuntimeError("Table request failed")

    async def _initialize(self) -> None:
        if self._module is not None:
            return
        module = "_console_shiny_table_" + uuid.uuid4().hex
        body = f"import sys, types\nmodule = types.ModuleType({module!r})\nexec({RUNTIME!r}, module.__dict__)\nsys.modules[{module!r}] = module"
        await self._evaluate("python", f"exec({body!r}, {{}})")
        self._module = module
        self._directory = tempfile.TemporaryDirectory(
            prefix="console-table-view-", dir="/tmp"
        )

    async def _result(
        self, language: str, source: Callable[[str], str]
    ) -> dict[str, Any]:
        assert self._directory is not None
        output = Path(self._directory.name) / (uuid.uuid4().hex + ".json")
        try:
            await self._evaluate(language, source(str(output)))
            return json.loads(output.read_text())
        finally:
            output.unlink(missing_ok=True)
            output.with_suffix(".tmp").unlink(missing_ok=True)

    async def open(self, language: str, name: str) -> dict[str, Any]:
        assert language in {"r", "python"}
        await self._initialize()
        view = uuid.uuid4().hex
        if language == "r":

            def source(output: str) -> str:
                return f"""local({{
  had_seed <- exists(".Random.seed", envir = globalenv(), inherits = FALSE)
  if (had_seed) seed <- get(".Random.seed", envir = globalenv(), inherits = FALSE)
  # First bridge/library initialization can seed R's RNG. Inspection must
  # preserve both its state and the absence of a seed in the client namespace.
  on.exit({{
    if (had_seed) {{
      assign(".Random.seed", seed, envir = globalenv())
    }} else if (exists(".Random.seed", envir = globalenv(), inherits = FALSE)) {{
      rm(".Random.seed", envir = globalenv())
    }}
  }})
  frame <- get({json.dumps(name)}, envir = globalenv(), inherits = FALSE)
  stopifnot(is.data.frame(frame))
  viewer <- reticulate::import({json.dumps(self._module)}, convert = FALSE)
  viewer$capture(reticulate::r_to_py(frame), {json.dumps(view)}, {json.dumps(output)})
  invisible(NULL)
}})"""
        else:

            def source(output: str) -> str:
                body = f"import sys, __main__\nsys.modules[{self._module!r}].capture(vars(__main__)[{name!r}], {view!r}, {output!r})"
                return f"exec({body!r}, {{}})"

        result = await self._result(language, source)
        self._view = view
        return result

    async def rows(
        self,
        view_id: str,
        start: int,
        end: int,
        sort_model: list[dict[str, str]],
        filter_model: dict[str, Any],
    ) -> dict[str, Any]:
        if view_id != self._view or self._module is None:
            raise ValueError("Table was closed or replaced; reopen it")
        if not (0 <= start < end and end - start <= 200):
            raise ValueError("Request 1–200 rows")

        def source(output: str) -> str:
            body = f"import sys\nsys.modules[{self._module!r}].rows({view_id!r}, {start!r}, {end!r}, {sort_model!r}, {filter_model!r}, {output!r})"
            return f"exec({body!r}, {{}})"

        return await self._result("python", source)

    async def close(self) -> None:
        if self._module is not None:
            body = f"import sys\nmodule = sys.modules[{self._module!r}]\nmodule.connection.close()\ndel sys.modules[{self._module!r}]"
            await self._evaluate("python", f"exec({body!r}, {{}})")
        self.reset()

    def reset(self) -> None:
        """Forget private state after the owning worker has been replaced."""
        if self._directory is not None:
            self._directory.cleanup()
        self._directory = None
        self._module = None
        self._view = None
