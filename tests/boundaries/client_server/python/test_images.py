"""Final-expression PNG display through the public MCP interface."""

import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.python.test_peer_runtime import without_r
from support.assertions import (
    assert_result_content,
    last_result_text,
    wait_for_worker_ready,
)
from support.checkpoints import wait_for_worker_file
from support.client import McpClient
from support.execution import RUNTIME, Execution, executions
from support.normalization import code
from support.python import virtualenv_python
from support.records import Transcript
from support.requirements import R, command, requires
from support.suites import run_this_suite


@contextmanager
def image_client(
    binary: Path, execution: Execution, *, with_r: bool = False
) -> Iterator[tuple[McpClient, Path]]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        selected = root / "selected"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(selected)],
            check=True,
            capture_output=True,
        )
        python = virtualenv_python(selected)
        subprocess.run(
            ["uv", "pip", "install", "--python", str(python), "pillow"],
            check=True,
            capture_output=True,
        )
        environment = dict(os.environ, RETICULATE_PYTHON=str(python), TMPDIR=str(root))
        environment.pop("PYTHONPATH", None)
        environment.pop("RETICULATE_PYTHONPATH", None)
        if not with_r:
            without_r(environment, root)
        with McpClient(binary, execution.serve(), environment) as client:
            client.initialize_and_list_tools()
            wait_for_worker_ready(client, "Pillow image readiness")
            yield client, root


def prepare_image(client: McpClient, root: Path) -> bytes:
    client.send(
        # fmt: python
        python=code("""
            import builtins
            import io
            import os
            from pathlib import Path
            import sys
            from PIL import Image

            assert "matplotlib.pyplot" not in sys.modules
            original_displayhook = sys.displayhook
            picture = Image.new("RGB", (3, 2), "red")
            picture.putpixel((1, 0), (0, 0, 255))
            png = picture._repr_png_()
            assert isinstance(png, bytes)
            with Image.open(io.BytesIO(png)) as decoded:
                assert decoded.format == "PNG"
                assert decoded.size == (3, 2)
                assert decoded.getpixel((0, 0)) == (255, 0, 0)
                assert decoded.getpixel((1, 0)) == (0, 0, 255)
            (Path(os.environ["TMPDIR"]) / "reference.png").write_bytes(png)
            print("before image")
            picture
            """),
        timeout_ms=600_000,
    )
    reference = wait_for_worker_file(root, "reference.png", client).read_bytes()
    assert reference.startswith(b"\x89PNG\r\n\x1a\n"), reference
    assert_result_content(
        client,
        ["before image\n", reference],
        image_reference="Pillow PNG representation",
    )
    return reference


@requires(command("uv"))
@executions(RUNTIME)
def test_displays_png_representations_without_r(
    binary: Path, execution: Execution
) -> Transcript:
    with image_client(binary, execution) as (client, root):
        reference = prepare_image(client, root)
        client.expect(python="assigned = picture")
        client.expect(
            "last image retained\n",
            python='assert builtins._ is picture; print("last image retained")',
        )
        client.expect(python="None")
        client.expect(
            "last image retained\n",
            python='assert builtins._ is picture; print("last image retained")',
        )
        client.expect(
            "declined PNG\n",
            # fmt: python
            python=code("""
                class Declined:
                    def _repr_png_(self):
                        assert builtins._ is None
                        return None

                    def __repr__(self):
                        return "declined PNG"


                declined = Declined()
                declined
                """),
        )
        client.expect(
            "42\n",
            python="assert builtins._ is declined; 42",
        )
        client.expect(
            "text only\n",
            # fmt: python
            python=code("""
                class TextOnly:
                    _repr_png_ = None

                    def __repr__(self):
                        return "text only"


                TextOnly()
                """),
        )
        client.send(
            # fmt: python
            python=code("""
                class Broken:
                    def _repr_png_(self):
                        assert builtins._ is None
                        raise ValueError("PNG rendering failed")

                    def __repr__(self):
                        raise AssertionError("must not retry text after PNG failure")


                Broken()
                """),
        )
        assert last_result_text(client).endswith("ValueError: PNG rendering failed\n")
        client.expect(
            "worker recovered\n",
            python='assert builtins._ is None; print("worker recovered")',
        )
        client.send(
            # fmt: python
            python=code("""
                class Invalid:
                    def _repr_png_(self):
                        return "not PNG bytes"


                Invalid()
                """),
        )
        assert last_result_text(client).endswith(
            "TypeError: _repr_png_() must return bytes or None\n"
        )
        client.expect(
            "custom display\n",
            # fmt: python
            python=code("""
                assert builtins._ is None


                def custom_display(value):
                    assert value is picture
                    print("custom display")


                sys.displayhook = custom_display
                picture
                """),
        )
        client.send(
            # fmt: python
            python=code("""
                assert builtins._ is None
                sys.displayhook = original_displayhook
                picture
                """),
        )
        assert_result_content(
            client, [reference], image_reference="Pillow PNG representation"
        )
        return client.finish()


@requires(R, command("uv"))
@executions(RUNTIME)
def test_preserves_png_display_after_r_attachment(
    binary: Path, execution: Execution
) -> Transcript:
    with image_client(binary, execution, with_r=True) as (client, root):
        reference = prepare_image(client, root)
        client.expect(
            r='stopifnot(reticulate::py_eval("builtins._ is picture"))',
        )
        client.send(
            # fmt: python
            python=code("""
                assert builtins._ is picture
                assert sys.displayhook is original_displayhook
                picture
                """),
        )
        assert_result_content(
            client, [reference], image_reference="Pillow PNG representation"
        )
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
