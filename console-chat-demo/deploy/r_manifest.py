"""Add R packages to a Connect manifest so Connect restores them into R_LIBS.

Usage: python deploy/r_manifest.py manifest.json R_VERSION

Reads deploy/r-packages.txt and adds those packages and their CRAN
dependencies from a fixed Posit Package Manager snapshot. Base and
recommended packages come with the R installation.
"""

import json
import re
import sys
import urllib.request
from pathlib import Path

# Binaries for Ubuntu 22.04, the Connect host's platform; source builds take far longer.
SNAPSHOT = "https://p3m.dev/cran/__linux__/jammy/2026-10-28"
DEPENDENCIES = ("Depends", "Imports", "LinkingTo")
BASE = {
    "R", "base", "compiler", "datasets", "graphics", "grDevices", "grid", "methods",
    "parallel", "splines", "stats", "stats4", "tcltk", "tools", "utils",
}  # fmt: skip


def main(manifest_path: Path, r_version: str) -> None:
    with urllib.request.urlopen(f"{SNAPSHOT}/src/contrib/PACKAGES") as response:
        index = response.read().decode()
    available, recommended = {}, set()
    for block in index.strip().split("\n\n"):
        fields = dict(re.findall(r"^(\S+): (.*(?:\n\s+.*)*)", block, re.MULTILINE))
        if "Priority" in fields:
            recommended.add(fields["Package"])
        else:
            available[fields["Package"]] = fields
    wanted = (Path(__file__).parent / "r-packages.txt").read_text().split()
    closure: dict[str, dict[str, str]] = {}
    while wanted:
        name = wanted.pop()
        if name in closure or name in BASE or name in recommended:
            continue
        closure[name] = available[name]
        for key in DEPENDENCIES:
            dependencies = closure[name].get(key, "")
            wanted += [
                d.split("(")[0].strip() for d in dependencies.split(",") if d.strip()
            ]
    manifest = json.loads(manifest_path.read_text())
    manifest["platform"] = r_version
    manifest["packages"] = {
        name: {
            "Source": "CRAN",
            "Repository": SNAPSHOT,
            # packrat orders installation by these dependency fields.
            "description": {
                "Package": name,
                "Version": fields["Version"],
                "Repository": "CRAN",
            }
            | {key: fields[key] for key in DEPENDENCIES if key in fields},
        }
        for name, fields in sorted(closure.items())
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Added {len(closure)} R packages for R {r_version}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2])
