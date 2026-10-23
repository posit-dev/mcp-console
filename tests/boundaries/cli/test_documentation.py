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
        r"https://github\.com/([^/\s]+/[^/\s]+)/(?:blob|tree)/([^/\s)#]+)"
    )
    # Retain the original companion name after a repository migration. Other
    # GitHub source citations, including upstream references, own their revisions.
    companion_names = {"cobox", pin["repository"].split("/")[-1]}
    docs = ROOT / "docs"
    documents = [ROOT / "README.md", ROOT / "RELEASE.md"]
    documents += sorted(
        path
        for path in docs.rglob("*")
        if path.suffix in {".md", ".qmd"}
        # Dated evidence retains its revision; rendered output is not source.
        and path.relative_to(docs).parts[0] not in {"benchmarks", "_site", ".quarto"}
        and path != docs / "RESOLVER_RELEASE_AUDIT.md"
    )
    for document in documents:
        for line_number, line in enumerate(
            document.read_text(encoding="utf-8").splitlines(), 1
        ):
            for match in reference.finditer(line):
                repository, revision = match.groups()
                if repository.split("/")[-1] not in companion_names:
                    continue
                assert (repository, revision) == (pin["repository"], pin["commit"]), (
                    f"{document.relative_to(ROOT)}:{line_number}: companion reference "
                    f"{repository}@{revision} differs from sandbox-runner.json "
                    f"({pin['repository']}@{pin['commit']}); "
                    "link to sandbox-runner.json or use its current repository and commit"
                )
    return [{"current_companion_links": "match sandbox-runner.json"}]
