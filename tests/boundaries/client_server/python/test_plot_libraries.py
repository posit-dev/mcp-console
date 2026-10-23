#!/usr/bin/env -S uv run --script

import re
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import assert_result_content, wait_for_worker_ready
from support.checkpoints import wait_for_worker_file
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.records import Transcript
from support.resolvers import matplotlib_test_environment
from support.suites import run_this_suite


@contextmanager
def plot_client(
    binary: Path, execution: Execution, backend: str
) -> Iterator[tuple[McpClient, Path]]:
    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        workspace = temporary / "workspace"
        workspace.mkdir()
        config = temporary / "matplotlib"
        config.mkdir()
        (config / "matplotlibrc").write_text(
            "figure.dpi: 100\nsavefig.bbox: standard\n", encoding="utf-8"
        )
        environment = matplotlib_test_environment(temporary / "cache")
        # Reuse prepared package artifacts while isolating mutable plot caches.
        uv_cache = subprocess.run(
            ["uv", "cache", "dir"], check=True, capture_output=True, text=True
        ).stdout.strip()
        assert Path(uv_cache).is_absolute(), uv_cache
        environment["UV_CACHE_DIR"] = uv_cache
        environment.update(
            MPLCONFIGDIR=str(config),
            MPLBACKEND=backend,
            MPL_IGNORE_SYSTEM_FONTS="1",
            TMPDIR=directory,
        )
        environment.pop("MATPLOTLIBRC", None)
        with McpClient(
            binary,
            execution.serve("-c", "cache=host"),
            environment,
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            wait_for_worker_ready(client, "plot library preparation readiness")
            client.expect(
                "[prepared]", requirements={"python": ["seaborn", "plotnine"]}
            )
            yield client, temporary


@executions(DIRECT, SANDBOXED)
def test_captures_seaborn_axes_and_facets(
    binary: Path, execution: Execution
) -> Transcript:
    with plot_client(binary, execution, "svg") as (client, temporary):
        client.send(
            # fmt: python
            python=code("""
                import os
                from pathlib import Path

                import matplotlib
                import matplotlib.pyplot as plt
                import seaborn as sns
                from PIL import Image

                assert matplotlib.get_backend() == "svg"
                references = Path(os.environ["TMPDIR"])
                figure, axes = plt.subplots(figsize=(3, 2), dpi=100)
                sns.scatterplot(x=[1, 2, 3], y=[3, 1, 2], ax=axes)
                grid = sns.relplot(
                    x=[1, 2, 1, 2],
                    y=[1, 3, 3, 1],
                    col=["a", "a", "b", "b"],
                    height=2,
                    aspect=1,
                )
                grid.figure.set_size_inches(4, 2)
                grid.figure.set_dpi(100)

                for plot, filename, size in (
                    (figure, "scatter.png", (300, 200)),
                    (grid.figure, "facets.png", (400, 200)),
                ):
                    reference = references / filename
                    plot.savefig(reference, format="png")
                    with Image.open(reference) as image:
                        assert image.size == size
                        assert any(lo < hi for lo, hi in image.convert("RGB").getextrema())

                print("open figures:", len(plt.get_fignums()))
                print("facet axes:", len(grid.axes.flat))
                """),
            timeout_ms=600_000,
        )
        # One PNG per pyplot figure, not per facet or savefig call.
        assert_result_content(
            client,
            [
                "open figures: 2\nfacet axes: 2\n",
                wait_for_worker_file(temporary, "scatter.png", client).read_bytes(),
                wait_for_worker_file(temporary, "facets.png", client).read_bytes(),
            ],
            image_reference="live seaborn savefig {page}",
        )
        client.expect(
            "('svg', [])\n",
            python="(matplotlib.get_backend(), plt.get_fignums())",
        )
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_captures_only_pyplot_owned_plotnine_figures(
    binary: Path, execution: Execution
) -> Transcript:
    with plot_client(binary, execution, "Agg") as (client, temporary):
        client.send(
            # fmt: python
            python=code("""
                import os
                from pathlib import Path

                import matplotlib
                import matplotlib.pyplot as plt
                import pandas as pd
                from PIL import Image
                from plotnine import aes, geom_point, ggplot, theme

                assert matplotlib.get_backend().lower() == "agg"
                references = Path(os.environ["TMPDIR"])
                data = pd.DataFrame({"x": [1, 2, 3], "y": [3, 1, 2]})
                plot = ggplot(data, aes("x", "y")) + geom_point() + theme(figure_size=(3, 2), dpi=100)
                assert plt.get_fignums() == []
                plot
                """),
            timeout_ms=600_000,
        )
        result = client.transcript[-1]["result"]
        representation = result["content"][0]["text"]
        assert re.fullmatch(
            r"<plotnine\.ggplot\.ggplot object at 0x[0-9a-fA-F]+>\n", representation
        ), representation
        assert_result_content(client, [representation])
        result["content"][0]["text"] = "<unshown plotnine ggplot>\n"

        client.expect(
            "<Figure size 300x200 with 1 Axes>\n",
            # fmt: python
            python=code("""
                # Default draw returns a detached Figure, even after savefig.
                hidden = plot.draw()
                hidden.savefig(references / "hidden.png", format="png")
                with Image.open(references / "hidden.png") as image:
                    assert image.size == (300, 200)
                    assert any(lo < hi for lo, hi in image.convert("RGB").getextrema())
                assert plt.get_fignums() == []
                hidden
                """),
        )
        client.send(
            # fmt: python
            python=code("""
                # Use a fresh object: the previous draw cached a detached Figure.
                shown_plot = (
                    ggplot(data, aes("x", "y")) + geom_point() + theme(figure_size=(3, 2), dpi=100)
                )
                shown = shown_plot.draw(show=True)
                assert plt.get_fignums() == []
                shown.savefig(references / "shown.png", format="png")
                with Image.open(references / "shown.png") as image:
                    assert image.size == (300, 200)
                    assert any(lo < hi for lo, hi in image.convert("RGB").getextrema())
                shown
                """),
            timeout_ms=600_000,
        )
        assert_result_content(
            client,
            [
                wait_for_worker_file(temporary, "shown.png", client).read_bytes(),
                "<Figure size 300x200 with 1 Axes>\n",
            ],
            image_reference="live shown plotnine savefig {page}",
        )
        client.expect(
            "('agg', [])\n",
            python="(matplotlib.get_backend().lower(), plt.get_fignums())",
        )
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
