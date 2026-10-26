#!/usr/bin/env python3
"""Check the published migration redirects through the site conversion command."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent


class WebsiteRedirectTests(unittest.TestCase):
    def test_redirects_preserve_pages_queries_fragments_and_downloads(self) -> None:
        routes = (
            ("index.html", ""),
            ("getting-started/index.html", "getting-started/"),
            ("sandbox_configuration/index.html", "sandbox_configuration/"),
            ("r/reference/console_tool/index.html", "r/reference/console_tool/"),
            ("python/reference/mcpconsole/index.html", "python/reference/mcpconsole/"),
            ("guide-index.html", "guide-index.html"),
        )
        with tempfile.TemporaryDirectory(prefix="console-redirects-") as temporary:
            site = Path(temporary)
            for name, _ in routes:
                page = site / name
                page.parent.mkdir(parents=True, exist_ok=True)
                page.write_text("<!doctype html><title>Old documentation</title>")
            download = site / "python/objects.json"
            download.write_bytes(b'{"items": []}\n')
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts/redirect-site"), str(site)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(download.read_bytes(), b'{"items": []}\n')

            for name, route in routes:
                page = BeautifulSoup((site / name).read_text(), "html.parser")
                target = "https://posit-dev.github.io/mcp-console/" + route
                with self.subTest(page=name):
                    self.assertEqual(
                        page.select_one('link[rel="canonical"]')["href"], target
                    )
                    self.assertEqual(page.select_one("#destination")["href"], target)
                    self.assertIn("Documentation has moved", page.get_text())
                for suffix in (
                    "",
                    "?view=full&query=R%20Python#methods-send",
                    "#a%20b",
                ):
                    with self.subTest(page=name, suffix=suffix):
                        self.assert_browser_redirect(
                            page,
                            "https://t-kalinowski.github.io/mcp-console/"
                            + route
                            + suffix,
                            target + suffix,
                        )
                # Explicit index.html links remain usable too.
                self.assert_browser_redirect(
                    page,
                    "https://t-kalinowski.github.io/mcp-console/" + name + "#usage",
                    "https://posit-dev.github.io/mcp-console/" + name + "#usage",
                )

            missing = BeautifulSoup((site / "404.html").read_text(), "html.parser")
            self.assert_browser_redirect(
                missing,
                "https://t-kalinowski.github.io/mcp-console/new-guide/?plain=1#example",
                "https://posit-dev.github.io/mcp-console/new-guide/?plain=1#example",
            )

    def assert_browser_redirect(
        self, page: BeautifulSoup, source: str, expected: str
    ) -> None:
        # Execute the generated page's script with browser URL and navigation
        # surfaces. This checks the shipped JavaScript, not a Python translation.
        result = subprocess.run(
            [
                "node",
                "-e",
                """
                const fs = require("node:fs");
                const vm = require("node:vm");
                const location = new URL(process.argv[1]);
                let destination;
                location.replace = url => { destination = url; };
                const link = {};
                vm.runInNewContext(fs.readFileSync(0, "utf8"), {
                    URL,
                    window: { location },
                    document: { getElementById: () => link },
                });
                process.stdout.write(JSON.stringify({ destination, link: link.href }));
                """,
                source,
            ],
            input=page.script.string,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout), {"destination": expected, "link": expected}
        )


if __name__ == "__main__":
    unittest.main()
