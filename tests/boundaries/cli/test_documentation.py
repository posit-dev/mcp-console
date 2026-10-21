"""Keep current reference documentation tied to public configuration and pins."""

import json
import os
import re
import subprocess
import sys
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from yaml12 import read_yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.records import Transcript

ROOT = Path(__file__).resolve().parents[3]


def test_preparation_defaults_match_public_configuration(binary: Path) -> Transcript:
    document = ROOT / "docs/PACKAGE_CONFIGURATION.md"
    match = re.search(
        r"<!-- preparation-defaults -->\s*```yaml\n(.*?)\n```",
        document.read_text(encoding="utf-8"),
        re.DOTALL,
    )
    assert match is not None, f"{document}: missing preparation defaults example"
    documented = read_yaml(StringIO(match[1]))
    with TemporaryDirectory() as temporary:
        result = subprocess.run(
            [
                binary,
                "config",
                "resolve",
                "--no-config",
                "--full",
                "--format",
                "json",
                "-c",
                "python={}",
            ],
            cwd=temporary,
            env=os.environ | {"MCP_CONSOLE_HOME": str(Path(temporary) / "home")},
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
    assert result.returncode == 0 and result.stderr == "", result
    settings = json.loads(result.stdout)
    expected = {
        "r": {"resolution": settings["r"]["resolution"]},
        # Compare the complete managed selection: an added version constraint
        # or package default must also update the documented defaults.
        "python": settings["python"],
    }
    assert documented == expected, {"documented": documented, "resolved": expected}
    return [{"documented_preparation_defaults": expected}]


def test_current_companion_links_match_pin(binary: Path) -> Transcript:
    pin = json.loads((ROOT / "sandbox-runner.json").read_text(encoding="utf-8"))
    reference = re.compile(
        rf"https://github\.com/{re.escape(pin['repository'])}/(?:blob|tree)/([^/\s)#]+)"
    )
    documents = [ROOT / "README.md", ROOT / "RELEASE.md"]
    documents += sorted((ROOT / "docs").glob("*.md"))
    documents += sorted((ROOT / "docs").glob("*.qmd"))
    for document in documents:
        # Dated audit/benchmark evidence retains the revision it evaluated.
        # Benchmarks live below docs/benchmarks, outside this current-guide scan.
        if document.name == "RESOLVER_RELEASE_AUDIT.md":
            continue
        for line_number, line in enumerate(
            document.read_text(encoding="utf-8").splitlines(), 1
        ):
            for match in reference.finditer(line):
                assert match[1] == pin["commit"], (
                    f"{document.relative_to(ROOT)}:{line_number}: companion reference "
                    f"{match[1]!r} differs from sandbox-runner.json ({pin['commit']}); "
                    "link to sandbox-runner.json or use its current commit"
                )
    return [{"current_companion_links": "match sandbox-runner.json"}]
