"""Exercise public packaging hooks with a controllable Maturin build."""

from importlib import import_module
from pathlib import Path
import sys
from types import ModuleType
import zipfile


def build(*args):
    print("building", flush=True)
    if sys.stdin.readline().strip() == "fail":
        raise RuntimeError("fixture build failed")
    entries = b"[console_scripts]\nOtherTool = other.module:main\n"
    if sys.argv[1].startswith("prepare_metadata"):
        metadata = Path(args[0]) / "fixture-1.dist-info"
        metadata.mkdir(exist_ok=True)
        (metadata / "entry_points.txt").write_bytes(entries)
        return metadata.name
    with zipfile.ZipFile("fixture.whl", "w") as archive:
        archive.writestr("fixture-1.data/scripts/mcp-console.exe", b"native fixture")
        archive.writestr("fixture-1.dist-info/entry_points.txt", entries)
        archive.writestr("fixture-1.dist-info/RECORD", b"")
    return "fixture.whl"


maturin = ModuleType("maturin")
for name in (
    "build_wheel",
    "build_editable",
    "build_sdist",
    "get_requires_for_build_editable",
    "get_requires_for_build_sdist",
    "get_requires_for_build_wheel",
    "prepare_metadata_for_build_editable",
    "prepare_metadata_for_build_wheel",
):
    setattr(maturin, name, build)
sys.modules["maturin"] = maturin

# The public hooks retain real checkout ownership while the companion build is
# replaced with an inert stage, just as Maturin is replaced above.
root = Path(__file__).resolve().parent
(root / "scripts").mkdir(exist_ok=True)
(root / "scripts/stage-sandbox-runner").write_text("def stage_locked(*args): pass\n")
(root / "sandbox-runner.json").write_text("{}")
sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
build_backend = import_module("build_backend")

print("ready", flush=True)
print(getattr(build_backend, sys.argv[1])("."), flush=True)
