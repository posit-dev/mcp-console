#!/usr/bin/env python3
"""Public command regressions for formatter reporting."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class FormatTests(unittest.TestCase):
    @unittest.skipUnless(
        shutil.which("ruff") and shutil.which("yamark"),
        "embedded Markdown formatting requires ruff and yamark",
    )
    def test_python_fences_preserve_closing_lines_with_crlf(self) -> None:
        with tempfile.TemporaryDirectory(prefix="console format ") as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            shutil.copy2(ROOT / "scripts/format", root / "scripts/format")
            shutil.copy2(ROOT / "pyproject.toml", root / "pyproject.toml")
            commands = root / "commands"
            commands.mkdir()
            for name in ("cargo", "air"):
                path = commands / (name + ".cmd" if os.name == "nt" else name)
                path.write_text(
                    "@echo off\nexit /b 0\n"
                    if os.name == "nt"
                    else "#!/bin/sh\nexit 0\n"
                )
                path.chmod(0o755)
            documents = [root / f"example.{suffix}" for suffix in ("md", "qmd")]
            for path in documents:
                path.write_bytes(b"```python\r\nprint( 42 )\r\n```\r\n")
            result = subprocess.run(
                [sys.executable, root / "scripts/format", "--strict"],
                cwd=root,
                env=os.environ
                | {"PATH": str(commands) + os.pathsep + os.environ["PATH"]},
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for path in documents:
                with self.subTest(document=path.name):
                    self.assertEqual(
                        path.read_bytes().replace(b"\r\n", b"\n"),
                        b"```python\nprint(42)\n```\n",
                    )

    def test_reports_every_formatter_and_strict_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / "scripts"
            scripts.mkdir()
            shutil.copy2(ROOT / "scripts/format", scripts / "format")
            commands = root / "commands"
            commands.mkdir()
            for failure in (False, True):
                for name in ("ruff", "yamark", "cargo", "air"):
                    path = commands / (name + ".cmd" if os.name == "nt" else name)
                    status = 7 if failure and name == "yamark" else 0
                    path.write_text(
                        f"@echo off\necho {name}>> attempts\nexit /b {status}\n"
                        if os.name == "nt"
                        else f"#!/bin/sh\necho {name} >> attempts\nexit {status}\n"
                    )
                    path.chmod(0o755)
                for arguments in ([], ["--strict"]):
                    with self.subTest(failure=failure, arguments=arguments):
                        (root / "attempts").write_text("")
                        result = subprocess.run(
                            [sys.executable, scripts / "format", *arguments],
                            cwd=root,
                            env=os.environ | {"PATH": str(commands)},
                            capture_output=True,
                            text=True,
                            timeout=10,
                        )
                        self.assertEqual(
                            result.returncode,
                            int(failure and bool(arguments)),
                            result.stdout + result.stderr,
                        )
                        self.assertEqual(
                            (root / "attempts").read_text().splitlines(),
                            ["ruff", "yamark", "cargo", "air"],
                        )
                        self.assertEqual(
                            result.stdout.splitlines(),
                            [
                                "ruff: ok",
                                "yamark: failed (7)" if failure else "yamark: ok",
                                "rustfmt: ok",
                                "air: ok",
                            ],
                        )


if __name__ == "__main__":
    unittest.main()
