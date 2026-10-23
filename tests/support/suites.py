import subprocess
from pathlib import Path


def run_this_suite(suite_path: str) -> None:
    suite = Path(suite_path).resolve()
    directory = next(
        parent for parent in suite.parents if (parent / "_run.py").is_file()
    )
    root = directory.parents[1]
    suite_name = suite.relative_to(directory).with_suffix("").as_posix()
    subprocess.run([root / "scripts" / "test", suite_name], check=True)
