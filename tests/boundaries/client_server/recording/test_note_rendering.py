"""Journal examples remain prose when the generated QMD is rendered."""

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.recording.test_intervals import events
from boundaries.client_server.sandbox.test_quarto import RenderedText
from support.assertions import wait_for_worker_ready
from support.client import McpClient
from support.execution import DIRECT
from support.normalization import code, normalize_recording
from support.r import r_test_environment
from support.records import TranscriptWithCompanions
from support.requirements import R, command, requires
from support.resolvers import ir_cache_directory


@requires(R, command("quarto"))
def test_renders_note_examples_without_evaluating_them(
    binary: Path,
) -> TranscriptWithCompanions:
    environment, _ = r_test_environment()
    prose = """## Journal

Keep **formatted prose** and `ordinary code`.

---

Observed group
---

<span class="journal-label">Observed cohort</span>.
"""
    note = """---
Observed total: 42
---

## Examples

---
Observed cohort: controls
...

> ---
> Observed replicate: 3
> ---

~~~yaml
---
execute:
  eval: false
---
~~~

```{r}
writeLines("fence", "note-fence-ran")
```

Legacy inline `r {writeLines("inline", "note-inline-ran"); "literal"}`.
Quarto inline `{r} {writeLines("inline", "note-quarto-inline-ran"); "literal"}`.
Nested backticks ``r cat("`nested`")``.

> ```{r}
> writeLines("quote", "note-quote-ran")
> ```

- A nested example:

  ```{r, example}
  writeLines("list", "note-list-ran")
  ```

~~~{r}
writeLines("tilde", "note-tilde-ran")
~~~

```text
An inline example: `r {writeLines("literal", "note-literal-ran"); "literal"}`.
{{< include included.qmd >}}
```

{{< include included.qmd >}}
"""
    options_note = """#| include: false
Keep this literal chunk option visible.

```{r}
writeLines("option", "note-option-ran")
```
"""
    metadata_note = "--- \r\nexecute:\r\n  eval: false\r\n---\t\r\n"
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        with McpClient(
            binary,
            DIRECT.serve("-c", "languages=[r]", "-c", "r.packages=[]"),
            environment,
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            wait_for_worker_ready(client, "worker acquired before journal examples")
            client.expect(note=prose)
            client.expect(note=note)
            client.expect(note=options_note)
            client.expect(r='writeLines("source", "source-ran")', note=metadata_note)
            transcript = client.finish()

        (recording,) = (workspace / ".agents/console/sessions").iterdir()
        notes = [
            event["request"]["arguments"]["note"]
            for event in events(recording)
            if event["event"] == "tool_call" and "note" in event["request"]["arguments"]
        ]
        assert notes == [prose, note, options_note, metadata_note], notes
        markdown = (recording / "transcript.md").read_bytes()
        assert all(text.encode() in markdown for text in notes), markdown
        assert not list(workspace.glob("note-*-ran"))

        report = workspace / "report"
        report.mkdir()
        document = report / "session.qmd"
        shutil.copyfile(recording / "sessions/session-001.qmd", document)
        (report / "included.qmd").write_text(
            '```{r}\nwriteLines("include", "note-include-ran")\n```\n',
            encoding="utf-8",
        )
        rendering = subprocess.run(
            ["quarto", "render", document.name, "--to", "html", "--quiet"],
            cwd=report,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert rendering.returncode == 0, {
            "stdout": rendering.stdout,
            "stderr": rendering.stderr,
        }
        assert not list(report.glob("note-*-ran")), sorted(
            path.name for path in report.glob("note-*-ran")
        )
        rendered = document.with_suffix(".html").read_text(encoding="utf-8")
        assert "<strong>formatted prose</strong>" in rendered
        assert '<span class="journal-label">Observed cohort</span>' in rendered
        visible = RenderedText()
        visible.feed(rendered)
        visible_text = "".join(visible.parts)
        observed = {
            "source_executed": (report / "source-ran").exists(),
            "annotation_visible": "Observed total: 42" in visible_text,
        }
        assert observed == {"source_executed": True, "annotation_visible": True}, (
            observed
        )
        assert (report / "source-ran").read_text() == "source\n"
        for contents in (
            "Observed cohort: controls",
            "Observed replicate: 3",
            "#| include: false",
            "Keep this literal chunk option visible.",
            "execute:",
            "eval: false",
            'writeLines("fence", "note-fence-ran")',
            'r {writeLines("inline", "note-inline-ran"); "literal"}',
            'writeLines("inline", "note-quarto-inline-ran")',
            'r cat("`nested`")',
            'writeLines("include", "note-include-ran")',
        ):
            assert contents in visible_text, contents
        return TranscriptWithCompanions(
            normalize_recording(transcript, workspace),
            {
                "qmd": normalize_recording(
                    [{"qmd": document.read_text(encoding="utf-8")}],
                    workspace,
                )[0]["qmd"]
            },
        )


@requires(R, command("ir"), command("quarto"))
def test_python_export_ignores_note_examples_when_selecting_engine(
    binary: Path,
) -> TranscriptWithCompanions:
    environment, _ = r_test_environment()
    note = """## Alternative R example

```{r}
writeLines("example", "note-ran")
```
"""
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        with McpClient(
            binary,
            DIRECT.serve(
                "-c",
                "languages=[python]",
                "-c",
                "r.packages=[]",
                "-c",
                "python.packages=[]",
            ),
            environment,
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            wait_for_worker_ready(client, "worker acquired before Python journal")
            client.expect(note=note)
            (recording,) = (workspace / ".agents/console/sessions").iterdir()
            note_only = (recording / "sessions/session-001.qmd").read_text(
                encoding="utf-8"
            )
            client.expect(
                "executed-python=42\n",
                # fmt: python
                python=code("""
                    from pathlib import Path

                    written = Path("source-ran").write_text("42", encoding="utf-8")
                    print("executed-python=42")
                    """),
            )
            transcript = client.finish()

        (recording,) = (workspace / ".agents/console/sessions").iterdir()
        report = workspace / "report"
        report.mkdir()
        document = report / "session.qmd"
        shutil.copyfile(recording / "sessions/session-001.qmd", document)
        document_text = document.read_text(encoding="utf-8")
        assert "  packages: []\n" in document_text, document_text
        (report / "note-only.qmd").write_text(note_only, encoding="utf-8")
        engines = {}
        for name in ("note-only.qmd", document.name):
            inspection = subprocess.run(
                ["quarto", "inspect", name],
                cwd=report,
                env=environment,
                capture_output=True,
                text=True,
                check=True,
                timeout=120,
            )
            engines[name] = json.loads(inspection.stdout)["engines"]
        assert engines == {"note-only.qmd": ["markdown"], "session.qmd": ["jupyter"]}, (
            engines
        )

        render_environment = environment.copy()
        for name in tuple(render_environment):
            if name.startswith("UV_") or name in ("RETICULATE_PYTHON", "RETICULATE_UV"):
                del render_environment[name]
        render_environment["IR_CACHE_DIR"] = ir_cache_directory(environment)
        render_environment["XDG_CONFIG_HOME"] = str(workspace / ".config")
        # IR prepares a separate Jupyter environment for Quarto. Give that
        # dependency work the remaining preparation budget before timing the
        # actual render, and prove that preparation executes neither source nor
        # the note's R example.
        preparation_timeout = client.response_deadline() - time.monotonic() - 120
        assert preparation_timeout > 0, "no case budget remains for preparation"
        try:
            prewarm = subprocess.run(
                [
                    "ir",
                    "render",
                    document.name,
                    "--to",
                    "html",
                    "--output",
                    "prewarm.html",
                    "--no-execute",
                ],
                cwd=report,
                env=render_environment,
                capture_output=True,
                text=True,
                timeout=preparation_timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise AssertionError(
                {"phase": "preparation", "stdout": error.stdout, "stderr": error.stderr}
            ) from error
        assert prewarm.returncode == 0, {
            "stdout": prewarm.stdout,
            "stderr": prewarm.stderr,
        }
        assert not (report / "source-ran").exists()
        assert not (report / "note-ran").exists()
        try:
            rendering = subprocess.run(
                ["ir", "render", document.name, "--to", "html", "--quiet"],
                cwd=report,
                env=render_environment,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired as error:
            raise AssertionError(
                {"phase": "rendering", "stdout": error.stdout, "stderr": error.stderr}
            ) from error
        assert rendering.returncode == 0, {
            "stdout": rendering.stdout,
            "stderr": rendering.stderr,
        }
        assert (report / "source-ran").read_text(encoding="utf-8") == "42"
        assert not (report / "note-ran").exists()
        visible = RenderedText()
        visible.feed(document.with_suffix(".html").read_text(encoding="utf-8"))
        visible_text = "".join(visible.parts)
        assert "executed-python=42" in visible_text, visible_text
        assert note.strip() in visible_text, visible_text
        transcript.append({"rendering": {"engines": engines, "source_executed": True}})
        return TranscriptWithCompanions(
            normalize_recording(transcript, workspace),
            {"qmd": normalize_recording([{"qmd": document_text}], workspace)[0]["qmd"]},
        )
