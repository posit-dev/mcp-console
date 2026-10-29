"""Journal examples remain prose when the generated QMD is rendered."""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.recording.test_intervals import events
from boundaries.client_server.sandbox.test_quarto import RenderedText
from support.assertions import wait_for_worker_ready
from support.client import McpClient
from support.execution import DIRECT
from support.normalization import normalize_recording
from support.r import r_test_environment
from support.records import TranscriptWithCompanions
from support.requirements import R, command, requires


@requires(R, command("quarto"))
def test_renders_note_examples_without_evaluating_them(
    binary: Path,
) -> TranscriptWithCompanions:
    environment, _ = r_test_environment()
    note = """## Journal

Keep **formatted prose** and `ordinary code`.

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
            client.expect(note=note)
            client.expect(r='writeLines("source", "source-ran")')
            transcript = client.finish()

        (recording,) = (workspace / ".agents/console/sessions").iterdir()
        notes = [
            event["request"]["arguments"]["note"]
            for event in events(recording)
            if event["event"] == "tool_call" and "note" in event["request"]["arguments"]
        ]
        assert notes == [note], notes
        assert note in (recording / "transcript.md").read_text(encoding="utf-8")
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
        assert (report / "source-ran").read_text() == "source\n"
        assert not list(report.glob("note-*-ran")), sorted(
            path.name for path in report.glob("note-*-ran")
        )
        rendered = document.with_suffix(".html").read_text(encoding="utf-8")
        assert "<strong>formatted prose</strong>" in rendered
        visible = RenderedText()
        visible.feed(rendered)
        visible_text = "".join(visible.parts)
        for contents in (
            'writeLines("fence", "note-fence-ran")',
            'r {writeLines("inline", "note-inline-ran"); "literal"}',
            '{r} {writeLines("inline", "note-quarto-inline-ran"); "literal"}',
            'r cat("`nested`")',
            "{{< include included.qmd >}}",
        ):
            assert contents in visible_text, contents
        return TranscriptWithCompanions(
            normalize_recording(transcript, workspace),
            {
                "qmd.yaml": normalize_recording(
                    [{"qmd": document.read_text(encoding="utf-8"), "rendered": True}],
                    workspace,
                )
            },
        )
