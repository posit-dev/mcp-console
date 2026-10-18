#!/usr/bin/env python3
"""Build the Python package reference during full website renders."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

if os.environ.get("QUARTO_PROJECT_RENDER_ALL") != "1":
    sys.exit(0)

root = Path(__file__).resolve().parent.parent
destination = Path(os.environ["QUARTO_PROJECT_OUTPUT_DIR"]).resolve() / "python"
# The nested Quarto project must discover its own project and output directory.
env = {
    name: value
    for name, value in os.environ.items()
    if not name.startswith("QUARTO_PROJECT_")
}
# Quarto's default discovery skips sources beneath hidden parent directories.
# Build outside the checkout so hidden worktrees also produce the complete site.
with tempfile.TemporaryDirectory(prefix="console-python-docs-") as workspace:
    project = Path(workspace)
    for name in ("pyproject.toml", "great-docs.yml", "LICENSE"):
        shutil.copy2(root / name, project / name)
    shutil.copytree(root / "python", project / "python")
    shutil.copytree(root / "docs/python", project / "docs/python")
    env["PYTHONPATH"] = os.pathsep.join(
        [str(project / "python"), *([env["PYTHONPATH"]] if "PYTHONPATH" in env else [])]
    )
    subprocess.run(
        [str(Path(sys.executable).parent / "great-docs"), "build", "--no-refresh"],
        cwd=project,
        env=env,
        check=True,
    )
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(project / "great-docs/_site", destination)
