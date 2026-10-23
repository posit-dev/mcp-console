#!/usr/bin/env -S uv run --script

import shutil
import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.execution import SANDBOXED
from support.normalization import code
from support.r import r_test_environment
from support.records import Transcript
from support.requirements import SANDBOX, command, requires
from support.resolvers import ir_cache_directory
from support.suites import run_this_suite


class RenderedText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


@requires(SANDBOX, command("ir"), command("quarto"))
def test_renders_generated_document(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        workspace = Path(temporary_directory)
        environment, _ = r_test_environment()
        environment.pop("RETICULATE_PYTHON", None)
        ir_cache = ir_cache_directory(environment)
        client = McpClient(
            binary,
            SANDBOXED.serve("-c", "cache=host"),
            environment,
            current_directory=workspace,
        )
        client.initialize_and_list_tools()
        (workspace / "render-value.txt").write_text("40\n", encoding="utf-8")
        # fmt: r
        r_source = code(r"""
              #| eval: false
            echo <- 0L
            render_value <- as.integer(readLines("render-value.txt"))
            cat("executed-r=40\n")
            """).removesuffix("\n")
        client.send(r=r_source)
        assert client.transcript[-1]["result"]["content"] == [
            {"type": "text", "text": "executed-r=40\n"}
        ]
        # fmt: python
        source = code(r'''
              #| eval: false
            echo = """before
            ````
            <div>not markdown</div>
            after"""
            print(f"executed-python={int(r.render_value) + 2}")
            print(echo)
            ''').removesuffix("\n")
        client.send(python=source)
        python_result = client.transcript[-1]["result"]["content"][0]["text"]
        assert "executed-python=42" in python_result, python_result
        assert "<div>not markdown</div>" in python_result, python_result
        transcript = client.finish()

        (recording,) = (workspace / ".agents/console/sessions").iterdir()
        report = workspace / "report"
        report.mkdir()
        document = report / "session.qmd"
        shutil.copyfile(recording / "sessions/session-001.qmd", document)
        shutil.copy2(
            workspace / "render-value.txt", document.parent / "render-value.txt"
        )
        document_text = document.read_text(encoding="utf-8")
        assert f"```{{r}}\n#| error: true\n\n{r_source}\n```" in document_text
        assert f"`````{{python}}\n#| error: true\n\n{source}\n`````" in document_text
        assert "zod:" not in document_text
        assert "zod python:" not in document_text
        assert "execute:" not in document_text
        assert "  python-version: []" in document_text
        assert "  python-packages:" in document_text
        assert f"  original-working-directory: {workspace.resolve()}" in document_text
        assert "root.dir:" not in document_text
        render_environment = environment.copy()
        render_environment.pop("RETICULATE_UV", None)
        for name in tuple(render_environment):
            if name.startswith("UV_"):
                del render_environment[name]
        render_environment["IR_CACHE_DIR"] = ir_cache
        render_environment["XDG_CONFIG_HOME"] = str(workspace / ".config")
        # Dependency resolution runs outside the network-denied sandbox. Prime
        # the exact Quarto environment, then exercise rendering inside it.
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
            cwd=document.parent,
            env=render_environment,
            check=False,
            capture_output=True,
            text=True,
        )
        assert prewarm.returncode == 0, {
            "exit_code": prewarm.returncode,
            "stdout": prewarm.stdout,
            "stderr": prewarm.stderr,
            "document": document.read_text(encoding="utf-8"),
        }

        render_script = code(r"""
            set -eu
            cp "$1" "$TMPDIR/session.qmd"
            cp "$2" "$TMPDIR/render-value.txt"
            cd "$TMPDIR"
            export HOME="$TMPDIR"
            exec ir render session.qmd --to html --output - --quiet
            """)
        rendering = subprocess.run(
            [
                binary,
                "sandbox",
                "--",
                "/bin/sh",
                "-c",
                render_script,
                "mcp-console-quarto-render",
                document,
                document.parent / "render-value.txt",
            ],
            cwd=document.parent,
            env=render_environment,
            check=False,
            capture_output=True,
            text=True,
        )
        assert rendering.returncode == 0, {
            "exit_code": rendering.returncode,
            "stdout": rendering.stdout,
            "stderr": rendering.stderr,
            "document": document.read_text(encoding="utf-8"),
        }
        rendered = rendering.stdout
        rendered_text = RenderedText()
        rendered_text.feed(rendered)
        visible_text = "".join(rendered_text.parts)
        assert "MCP Console session" in rendered
        assert "executed-r=40" in visible_text
        assert "executed-python=42" in visible_text
        assert "not markdown" in visible_text
        assert "<div>not markdown</div>" not in rendered
        assert "zod:" not in rendered
        assert "zod python:" not in rendered
        transcript.append(
            {
                "quarto document": {
                    "executed client-authored R and Python cells through `ir` inside the sandbox": True,
                    "selected Python through reticulate defaults": True,
                    "omitted recorded runtime results": True,
                    "kept Markdown-looking source inside a code block": True,
                }
            }
        )
        return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
