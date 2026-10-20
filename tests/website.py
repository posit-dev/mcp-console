#!/usr/bin/env python3
"""Render the documentation website and check its public pages and links."""

from html.parser import HTMLParser
import ast
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import urlopen
from xml.etree import ElementTree

from bs4 import BeautifulSoup
from websockets.sync.client import connect
from yaml12 import read_yaml

ROOT = Path(__file__).resolve().parent.parent


def linked_file(page: Path, link: str) -> Path:
    """Resolve a rendered local link, including directory index pages."""
    path = unquote(urlsplit(link).path)
    destination = (page.parent / path).resolve() if path else page
    return destination / "index.html" if destination.is_dir() else destination


class Page(HTMLParser):
    def __init__(self, path: Path) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.links: list[str] = []
        self.title = ""
        self.in_title = False
        self.code_blocks: list[str] = []
        self.in_pre = False
        self.code = ""
        self.feed(path.read_text())

    def handle_starttag(
        self, tag: str, attributes: list[tuple[str, str | None]]
    ) -> None:
        attrs = dict(attributes)
        if attrs.get("id"):
            self.ids.add(attrs["id"])
        for name in ("href", "src"):
            if attrs.get(name):
                self.links.append(attrs[name])
        if tag == "title":
            self.in_title = True
        if tag == "pre":
            self.in_pre = True
            self.code = ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if tag == "pre":
            self.in_pre = False
            self.code_blocks.append(self.code)

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title += data
        if self.in_pre:
            self.code += data


class WebsiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workspace = tempfile.TemporaryDirectory(prefix="console-website-")
        cls.addClassCleanup(cls.workspace.cleanup)
        docs = Path(cls.workspace.name) / "docs"
        shutil.copytree(
            ROOT / "docs",
            docs,
            ignore=shutil.ignore_patterns(
                ".quarto", "_site", "reference", "objects.json", "index.md", "llms*.txt"
            ),
        )
        shutil.copytree(
            ROOT / "r",
            docs.parent / "r",
            ignore=shutil.ignore_patterns("docs", "*.Rcheck", "*.tar.gz"),
        )
        shutil.copytree(ROOT / "python", docs.parent / "python")
        for suite in ("test_help", "test_config_init"):
            relative = Path("tests/snapshots/cli/interface") / suite
            shutil.copytree(ROOT / relative, docs.parent / relative)
        for name in ("pyproject.toml", "great-docs.yml", "LICENSE"):
            shutil.copy2(ROOT / name, docs.parent / name)
        for directory in (docs, docs / "benchmarks"):
            prefix = "" if directory == docs else "../"
            r_readme = prefix + "../r/README.md"
            (directory / "package-links.md").write_text(
                "# Package links\n\n"
                "[R reference](https://t-kalinowski.github.io/mcp-console/"
                "r/reference/console_tool.html?view=full#arguments)\n\n"
                "[Python reference](https://t-kalinowski.github.io/mcp-console/"
                "python/reference/MCPConsole.html?view=full#"
                "mcp_console.MCPConsole.send)\n\n"
                f"[R overview]({r_readme})\n\n"
                f"[R configuration]({r_readme}#inspect-and-edit-configuration)\n\n"
                f"[Source]({prefix}../src/main.rs?plain=1#L1)\n\n"
                f"[Template]({prefix}templates/task-checkpoint.md)\n\n"
                f"[Guide]({prefix}SEND_OPERATIONS.md#standard-input-and-managed-reads)\n"
                "\n[Send API](`mcp_console.MCPConsole.send`)\n"
                "\n[](`~mcp_console.AsyncMCPConsole`)\n"
            )
        # Quarto's Python has the page-filter dependencies but no Great Docs.
        # Reference generation must prepare its own pinned environment.
        result = subprocess.run(
            [
                "uv",
                "run",
                "--no-project",
                "--isolated",
                "--with",
                "pandocfilters",
                "--with",
                "py-yaml12>=0.2.0",
                "quarto",
                "render",
                str(docs),
                "--to",
                "html",
            ],
            env={**os.environ, "QUARTO_PYTHON": "python"},
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        cls.site = (docs / "_site").resolve()
        cls.pages = {path.resolve(): Page(path) for path in cls.site.rglob("*.html")}

    def assert_published_links(self, links: list[str]) -> None:
        base = read_yaml(ROOT / "docs/_quarto.yml")["website"]["site-url"]
        for link in links:
            url = urlsplit(link)
            if url.netloc != urlsplit(base).netloc:
                continue
            with self.subTest(link=link):
                self.assertTrue(link.startswith(base), "link escapes the site")
                target = self.site / unquote(url.path.removeprefix(urlsplit(base).path))
                if target.is_dir():
                    target /= "index.html"
                self.assertTrue(target.is_file(), "published target is missing")
                if url.fragment:
                    self.assertIn(unquote(url.fragment), self.pages[target].ids)

    def test_pages_use_lowercase_directory_urls(self) -> None:
        for route in (
            "builtin_runtime",
            "readme",
            "r-guide",
            "python-guide",
            "python/reference/mcpconsole",
            "r/reference/consoleconfig",
        ):
            with self.subTest(route=route):
                self.assertTrue((self.site / route / "index.html").is_file())
        self.assertFalse((self.site / "BUILTIN_RUNTIME.html").exists())
        search = json.loads((self.site / "search.json").read_text())
        indexed = {urlsplit(entry["href"]).path for entry in search}
        self.assertIn("builtin_runtime/", indexed)
        self.assertIn("python/reference/mcpconsole/", indexed)
        for path, page in self.pages.items():
            for link in page.links:
                url = urlsplit(link)
                if url.scheme or url.netloc or not url.path:
                    continue
                target = path.parent / unquote(url.path)
                if target.is_dir():
                    target /= "index.html"
                if target.resolve() in self.pages:
                    with self.subTest(page=str(path), link=link):
                        self.assertEqual(url.path, url.path.lower())
                        self.assertFalse(url.path.endswith(".html"))

    def test_guides_have_titles_navigation_and_search(self) -> None:
        search = json.loads((self.site / "search.json").read_text())
        indexed = {urlsplit(entry["href"]).path for entry in search}
        sources = [
            *(ROOT / "docs").glob("*.md"),
            *(ROOT / "docs/benchmarks").glob("*.md"),
        ]
        for source in sources:
            route = source.relative_to(ROOT / "docs").with_suffix("").as_posix().lower()
            if route in ("r", "python"):
                route += "-guide"
            relative = Path(route) / "index.html"
            with self.subTest(page=str(relative)):
                page = self.pages[(self.site / relative).resolve()]
                heading = (
                    next(
                        line
                        for line in source.read_text().splitlines()
                        if line.startswith("# ")
                    )
                    .removeprefix("# ")
                    .replace("`", "")
                )
                self.assertIn(heading, page.title)
                self.assertIn("quarto-sidebar", page.ids)
                self.assertIn(route + "/", indexed)
        for name in ("index.html", "getting-started/index.html"):
            self.assertIn((self.site / name).resolve(), self.pages)
        self.assertFalse((self.site / "templates").exists())

    def test_guides_detect_system_color_scheme(self) -> None:
        for name in (
            "index.html",
            "cli-help/index.html",
            "benchmarks/transcript-concurrency/index.html",
        ):
            with self.subTest(page=name):
                soup = BeautifulSoup((self.site / name).read_text(), "html.parser")
                script = soup.find("script", id="quarto-html-before-body")
                self.assertIsNotNone(script)
                self.assertIn("prefers-color-scheme: dark", script.get_text())
                self.assertIsNotNone(soup.select_one(".quarto-color-scheme-toggle"))

    def test_r_reference_offers_automatic_color_scheme(self) -> None:
        for name in (
            "r/index.html",
            "r/reference/console_tool/index.html",
            "r/articles/autonomous-report/index.html",
        ):
            with self.subTest(page=name):
                path = self.site / name
                soup = BeautifulSoup(path.read_text(), "html.parser")
                script = soup.select_one('script[src$="lightswitch.js"]')
                self.assertIsNotNone(script)
                self.assertIn(
                    "prefers-color-scheme: dark",
                    (path.parent / script["src"]).read_text(),
                )
                self.assertIsNotNone(soup.select_one('[data-bs-theme-value="auto"]'))

    def test_r_reference_aliases_use_local_directory_urls(self) -> None:
        for alias, target in (
            ("sandboxsockets", "sandboxpolicy"),
            ("existingpython", "pythonconfig"),
        ):
            with self.subTest(alias=alias):
                page = self.site / f"r/reference/{alias}/index.html"
                soup = BeautifulSoup(page.read_text(), "html.parser")
                redirect = soup.find("meta", attrs={"http-equiv": "refresh"})
                self.assertEqual(redirect["content"], f"0;URL=../{target}/")
                self.assertTrue(linked_file(page, f"../{target}/").is_file())

    def test_r_report_vignette_is_discoverable(self) -> None:
        article = self.site / "r/articles/autonomous-report/index.html"
        self.assertIn(article, self.pages, "autonomous report vignette is missing")
        self.assertIn("Giving an agent an R session", self.pages[article].title)
        page = self.site / "r-guide/index.html"
        guide = BeautifulSoup(page.read_text(), "html.parser")
        destinations = {
            linked_file(page, link["href"])
            for link in guide.select("main a[href]")
            if not urlsplit(link["href"]).scheme
        }
        self.assertIn(article, destinations)
        self.assertIn(self.site / "r/articles/configuration/index.html", destinations)
        search = json.loads((self.site / "r/search.json").read_text())
        self.assertIn(
            "https://t-kalinowski.github.io/mcp-console/r/articles/autonomous-report/",
            {entry["path"] for entry in search},
        )

    def test_cli_help_matches_tested_transcripts(self) -> None:
        path = self.site / "cli-help/index.html"
        self.assertIn(path, self.pages, "CLI help page is missing")
        soup = BeautifulSoup(path.read_text(), "html.parser")
        public_commands = [
            "mcp-console --help",
            "mcp-console serve --help",
            "mcp-console warmup --help",
            "mcp-console sandbox --help",
            "mcp-console config --help",
            "mcp-console config init --help",
            "mcp-console config resolve --help",
            "mcp-console configure-client --help",
        ]
        for platform, filename in (("all-platforms", "help.yaml"),):
            with self.subTest(platform=platform):
                entries = read_yaml(
                    ROOT / "tests/snapshots/cli/interface/test_help" / filename,
                    multi=True,
                )
                expected = {
                    entry["command"]: entry["stdout"].rstrip("\n")
                    for entry in entries
                    if entry["command"] in public_commands
                }
                self.assertEqual(set(expected), set(public_commands))
                panel = soup.find(id=platform)
                self.assertIsNotNone(panel, "platform panel is missing")
                actual = [
                    (pre["data-command"], pre.get_text().rstrip("\n"))
                    for pre in panel.select("pre.cli-help")
                ]
                self.assertEqual(
                    actual,
                    [(command, expected[command]) for command in public_commands],
                )
                for pre in panel.select("pre.cli-help"):
                    self.assertEqual(pre.get("tabindex"), "0")
                    self.assertEqual(pre.get("role"), "region")
                    self.assertEqual(
                        pre.get("aria-label"),
                        pre["data-command"].removesuffix(" --help"),
                    )
                headings = [
                    heading.get_text().strip() for heading in panel.select("h3")
                ]
                self.assertEqual(
                    headings,
                    [command.removesuffix(" --help") for command in public_commands],
                )
        search = json.loads((self.site / "search.json").read_text())
        self.assertIn("cli-help/", {urlsplit(entry["href"]).path for entry in search})
        guide = self.site / "cli/index.html"
        destinations = {
            linked_file(guide, link)
            for link in self.pages[guide].links
            if not urlsplit(link).scheme and not urlsplit(link).netloc
        }
        self.assertIn(path, destinations)

    def test_guides_keep_shared_navigation(self) -> None:
        pages = (
            "readme/index.html",
            "getting-started/index.html",
            "clients/index.html",
            "build_from_source/index.html",
            "linux_compatibility/index.html",
            "windows/index.html",
            "builtin_runtime/index.html",
            "requirements/index.html",
            "sandbox/index.html",
            "recording/index.html",
            "sandbox_command/index.html",
        )
        navigation = []
        for name in pages:
            with self.subTest(page=name):
                soup = BeautifulSoup((self.site / name).read_text(), "html.parser")
                sidebar = soup.find(id="quarto-sidebar")
                self.assertIsNotNone(sidebar)
                links = [
                    linked_file(self.site / name, link["href"])
                    .relative_to(self.site)
                    .as_posix()
                    for link in sidebar.select("a[href]")
                    if urlsplit(link["href"]).path
                ]
                navigation.append(links)
                self.assertEqual(links, navigation[0])
                self.assertIn("clients/index.html", links)
                self.assertIn("sandbox_command/index.html", links)
                self.assertNotIn("sandbox_configuration/index.html", links)
                self.assertNotIn("runtime_reference/index.html", links)
                self.assertNotIn("api/index.html", links)
                sections = sidebar.select(".sidebar-item-section")
                self.assertEqual(
                    [
                        section.select_one(".sidebar-item-text").get_text(strip=True)
                        for section in sections
                    ],
                    ["Learn Console", "Set up", "Other tasks"],
                )
                learning = sections[0]
                self.assertEqual(
                    [
                        linked_file(self.site / name, link["href"])
                        for link in sections[1].select("a[href]")
                    ],
                    [self.site / "clients/index.html"],
                )
                heading = learning.select_one(".sidebar-item-text")
                self.assertEqual(heading.get_text(strip=True), "Learn Console")
                self.assertFalse(heading.has_attr("href"))
                self.assertIn("show", learning.select_one("ul")["class"])
                chapters = [
                    linked_file(self.site / name, link["href"])
                    .relative_to(self.site)
                    .as_posix()
                    for link in learning.select("a[href]")
                ]
                self.assertEqual(
                    chapters,
                    [
                        "getting-started/index.html",
                        "builtin_runtime/index.html",
                        "requirements/index.html",
                        "sandbox/index.html",
                        "recording/index.html",
                    ],
                )
                labels = [
                    link.get_text(" ", strip=True)
                    for link in soup.select(".navbar-nav .nav-link")
                    if link.get_text(strip=True)
                ]
                self.assertEqual(
                    labels, ["Get started", "Guides", "Build", "Reference"]
                )

    def test_build_has_its_own_navigation_and_links_to_reference(self) -> None:
        navigation = []
        for route in (
            "build",
            "build_on_console",
            "r-guide",
            "python-guide",
            "send_operations",
        ):
            with self.subTest(page=route):
                path = self.site / route / "index.html"
                page = BeautifulSoup(path.read_text(), "html.parser")
                links = [
                    linked_file(path, link["href"]).relative_to(self.site).as_posix()
                    for link in page.select("#quarto-sidebar a[href]")
                    if urlsplit(link["href"]).path
                ]
                navigation.append(links)
                self.assertEqual(links, navigation[0])
                for target in (
                    "build",
                    "build_on_console",
                    "r-guide",
                    "python-guide",
                    "send_operations",
                    "tool_configuration",
                    "api",
                    "r/reference",
                    "python/reference",
                ):
                    self.assertIn(f"{target}/index.html", links)
                self.assertNotIn("getting-started/index.html", links)
                self.assertNotIn("requirements/index.html", links)

        home = BeautifulSoup((self.site / "index.html").read_text(), "html.parser")
        self.assertEqual(
            [link.get_text(strip=True) for link in home.select(".home-links a")],
            ["Get started", "Build on Console", "Reference"],
        )
        start = self.site / "getting-started/index.html"
        connections = {
            linked_file(start, link["href"])
            for link in BeautifulSoup(start.read_text(), "html.parser").select(
                "main a[href]"
            )
            if not urlsplit(link["href"]).scheme
        }
        for target in ("r-guide", "python-guide"):
            self.assertIn(self.site / target / "index.html", connections)

    def test_learning_chapters_have_previous_and_next_links(self) -> None:
        chapters = [
            "getting-started",
            "builtin_runtime",
            "requirements",
            "sandbox",
            "recording",
        ]
        for previous, following in zip(chapters, chapters[1:]):
            for current, direction, target in (
                (previous, "next", following),
                (following, "previous", previous),
            ):
                with self.subTest(chapter=current, direction=direction):
                    path = self.site / current / "index.html"
                    page = BeautifulSoup(path.read_text(), "html.parser")
                    link = page.select_one(f".nav-page-{direction} a[href]")
                    self.assertIsNotNone(link)
                    self.assertEqual(
                        linked_file(path, link["href"]),
                        self.site / target / "index.html",
                    )

    def test_navbar_tracks_the_current_page_or_section(self) -> None:
        for route, label, current in (
            ("getting-started", "Get started", "page"),
            ("readme", "Guides", "page"),
            ("builtin_runtime", "Guides", "true"),
            ("clients", "Guides", "true"),
            ("build", "Build", "page"),
            ("build_on_console", "Build", "true"),
            ("r-guide", "Build", "true"),
            ("python-guide", "Build", "true"),
            ("send_operations", "Build", "true"),
            ("reference", "Reference", "page"),
            ("configuration", "Reference", "true"),
            ("tool_configuration", "Reference", "true"),
            ("api", "Reference", "true"),
            ("python/reference/mcpconsole", "Reference", "true"),
        ):
            with self.subTest(page=route):
                page = BeautifulSoup(
                    (self.site / route / "index.html").read_text(), "html.parser"
                )
                active = page.select("#quarto-header .navbar .nav-link.active")
                self.assertEqual(
                    [link.get_text(strip=True) for link in active], [label]
                )
                self.assertEqual(active[0].get("aria-current"), current)

    def test_reference_has_one_shared_navigation(self) -> None:
        navigation = []
        for name in (
            "reference",
            "configuration",
            "runtime_configuration",
            "tool_configuration",
            "sandbox_configuration",
            "package_configuration",
            "environment",
            "runtime_reference",
            "recording_reference",
            "cli",
            "cli-help",
            "api",
            "glossary",
            "python/reference/mcpconsole",
        ):
            with self.subTest(page=name):
                path = self.site / name / "index.html"
                page = BeautifulSoup(path.read_text(), "html.parser")
                links = [
                    linked_file(path, link["href"]).relative_to(self.site).as_posix()
                    for link in page.select("#quarto-sidebar a[href]")
                    if urlsplit(link["href"]).path
                ]
                navigation.append(links)
                self.assertEqual(links, navigation[0])
                for target in (
                    "configuration",
                    "package_configuration",
                    "runtime_reference",
                    "recording_reference",
                    "cli",
                    "api",
                    "python/reference",
                    "r/reference",
                    "glossary",
                ):
                    self.assertIn(f"{target}/index.html", links)
                for target in (
                    "getting-started",
                    "installation",
                    "build_on_console",
                    "sandbox_command",
                ):
                    self.assertNotIn(f"{target}/index.html", links)

    def test_development_has_focused_navigation(self) -> None:
        paths = (
            (
                "architecture/index.html",
                "windows_development/index.html",
                "requirements/index.html",
            ),
        )
        for name, included, excluded in paths:
            with self.subTest(page=name):
                soup = BeautifulSoup((self.site / name).read_text(), "html.parser")
                sidebar = soup.find(id="quarto-sidebar")
                self.assertIsNotNone(sidebar)
                links = {
                    linked_file(self.site / name, link["href"])
                    .relative_to(self.site)
                    .as_posix()
                    for link in sidebar.select("a[href]")
                    if not urlsplit(link["href"]).scheme
                    and not urlsplit(link["href"]).netloc
                }
                self.assertIn(included, links)
                self.assertNotIn(excluded, links)
                navbar = soup.select_one(".navbar")
                for label in ("Guides", "Get started", "Reference"):
                    self.assertIn(label, navbar.get_text(" ", strip=True))
                self.assertNotIn("Packages", navbar.get_text(" ", strip=True))

    def test_documentation_links_to_learning_and_tasks(self) -> None:
        page = BeautifulSoup(
            (self.site / "readme/index.html").read_text(), "html.parser"
        )
        self.assertEqual(page.select_one("h1").get_text(strip=True), "Guides")
        self.assertEqual(
            [heading.get_text(strip=True) for heading in page.select("main h2")],
            ["Learn Console", "I want to…"],
        )
        self.assertEqual(
            [
                linked_file(self.site / "readme/index.html", link["href"]).parent.name
                for link in page.select("#learn-console ol a[href]")
            ],
            [
                "getting-started",
                "builtin_runtime",
                "requirements",
                "sandbox",
                "recording",
            ],
        )
        table = page.select_one("main table")
        self.assertIsNotNone(table)
        self.assertIn("I want to", table.get_text())
        links = {
            linked_file(self.site / "readme/index.html", link["href"])
            .relative_to(self.site)
            .as_posix()
            + (
                "#" + urlsplit(link["href"]).fragment
                if urlsplit(link["href"]).fragment
                else ""
            )
            for link in table.select("a[href]")
        }
        for target in (
            "clients/index.html",
            "r-guide/index.html",
            "python-guide/index.html",
            "requirements/index.html",
            "sandbox/index.html",
            "builtin_runtime/index.html",
            "build_on_console/index.html",
            "send_operations/index.html",
            "recording/index.html",
        ):
            self.assertIn(target, links)
        runtime = self.pages[self.site / "runtime_reference/index.html"]
        self.assertIn("Language behavior", runtime.title)
        for link in table.select("a[href]"):
            self.assertIn(
                linked_file(self.site / "readme/index.html", link["href"]).parent.name,
                {
                    "clients",
                    "r-guide",
                    "python-guide",
                    "builtin_runtime",
                    "requirements",
                    "sandbox",
                    "recording",
                    "build_on_console",
                    "send_operations",
                    "sandbox_command",
                },
            )

    def test_reference_indexes_settings_by_their_owning_page(self) -> None:
        path = self.site / "reference/index.html"
        reference = BeautifulSoup(path.read_text(), "html.parser")
        settings = {}
        for row in reference.select("#configuration table tbody tr"):
            names = row.select("td:first-child code")
            self.assertTrue(names, row.get_text())
            target = row.select_one("a[href]")
            self.assertIsNotNone(target)
            for name in names:
                settings[name.get_text()] = linked_file(path, target["href"])
        for key, owner in {
            "r.packages": "package_configuration",
            "python.packages": "package_configuration",
            "r.resolution": "package_configuration",
            "python.resolution": "package_configuration",
            "resolver": "package_configuration",
            "cache": "package_configuration",
            "python": "runtime_configuration",
            "startup": "runtime_configuration",
            "sandbox.profile": "sandbox_configuration",
            "languages": "tool_configuration",
        }.items():
            with self.subTest(setting=key):
                self.assertEqual(settings.get(key), self.site / owner / "index.html")

        configuration = BeautifulSoup(
            (self.site / "configuration/index.html").read_text(), "html.parser"
        )
        self.assertIsNone(configuration.select_one("section#settings-by-task"))
        packages = self.pages[self.site / "package_configuration/index.html"]
        self.assertLessEqual(
            {
                "startup-package-declarations",
                "default-startup-packages",
                "dependency-resolution-policies",
                "cache-locations",
                "configuration",
                "filesystem-grants",
                "trust-boundary",
            },
            packages.ids,
        )

    def test_home_title_has_one_introduction_and_description_metadata(self) -> None:
        soup = BeautifulSoup((self.site / "index.html").read_text(), "html.parser")
        title = soup.select_one("#title-block-header")
        self.assertIsNotNone(title.select_one(".subtitle"))
        self.assertIsNone(title.select_one(".description"))
        description = soup.find("meta", attrs={"name": "description"})
        self.assertIsNotNone(description)
        self.assertIn("data", description["content"])

    def test_single_page_render_preserves_other_pages_and_package_sites(self) -> None:
        with tempfile.TemporaryDirectory(prefix="console-page-render-") as directory:
            workspace = Path(directory) / "project"
            shutil.copytree(self.workspace.name, workspace)
            docs = workspace / "docs"
            site = docs / "_site"
            unchanged = [
                path
                for path in site.rglob("*")
                if path.is_file()
                and path != site / "index.html"
                and (path.suffix == ".html" or path.relative_to(site).parts[0] == "r")
            ]
            # Make rewrites visible even when their content and clock tick match.
            for path in unchanged:
                os.utime(path, (1, 1))
            timestamps = {path: path.stat().st_mtime_ns for path in unchanged}
            # Quarto recopies declared resources on partial renders. Their
            # contents must stay intact even when the copy updates timestamps.
            python_resources = {
                path: path.read_bytes()
                for path in (site / "python").rglob("*")
                if path.is_file() and path.suffix != ".html"
            }
            source = docs / "index.qmd"
            marker = "Single-page rendering preserves the rest of the website."
            source.write_text(source.read_text() + f"\n{marker}\n")
            result = subprocess.run(
                ["quarto", "render", str(source), "--to", "html"],
                capture_output=True,
                text=True,
                timeout=180,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(marker, (site / "index.html").read_text())
            changed = [
                str(path.relative_to(site))
                for path, timestamp in timestamps.items()
                if not path.is_file() or path.stat().st_mtime_ns != timestamp
            ]
            self.assertEqual(
                len(changed),
                0,
                f"Single-page render changed unrelated output: {changed[:5]}",
            )
            for path, content in python_resources.items():
                self.assertEqual(path.read_bytes(), content, str(path))

            source = docs / "BUILTIN_RUNTIME.md"
            original = source.read_text()
            for heading in ("Preview draft", "Preview revised"):
                source.write_text(original + f"\n## {heading}\n\n{marker}\n")
                result = subprocess.run(
                    ["quarto", "render", str(source), "--to", "html"],
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(
                    heading, (site / "builtin_runtime/index.html").read_text()
                )
                self.assertFalse((site / "BUILTIN_RUNTIME.html").exists())
            search = json.loads((site / "search.json").read_text())
            hrefs = [entry["href"] for entry in search]
            self.assertEqual(len(hrefs), len(set(hrefs)), "duplicate search results")
            self.assertIn("builtin_runtime/#preview-revised", hrefs)
            self.assertNotIn("builtin_runtime/#preview-draft", hrefs)
            sitemap = ElementTree.parse(site / "sitemap.xml")
            locations = [
                element.text
                for element in sitemap.findall(
                    ".//{http://www.sitemaps.org/schemas/sitemap/0.9}loc"
                )
            ]
            self.assertEqual(
                len(locations), len(set(locations)), "duplicate sitemap URLs"
            )

    def test_preview_serves_clean_urls_and_updates_changed_pages(self) -> None:
        with tempfile.TemporaryDirectory(prefix="console-preview-") as directory:
            workspace = Path(directory) / "project"
            shutil.copytree(self.workspace.name, workspace)
            routes = (
                "/readme/",
                "/builtin_runtime/",
                "/python/reference/openai.responses_tool/",
                "/r/reference/console_tool/",
            )
            # Start from a completed render, as when switching from render-site
            # to preview-site. Preview must retain the outputs Quarto tracks.
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "scripts/preview-site"),
                    "--port",
                    "0",
                    "--host",
                    "127.0.0.1",
                    "--no-browser",
                ],
                cwd=workspace,
                env={**os.environ, "NO_COLOR": "1"},
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=os.name != "nt",
            )
            output: queue.Queue[str | None] = queue.Queue()
            log: list[str] = []

            def read_output() -> None:
                assert process.stdout is not None
                for line in process.stdout:
                    output.put(line)
                output.put(None)

            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()

            def wait_for(message: str) -> str:
                deadline = time.monotonic() + 180
                while True:
                    try:
                        line = output.get(timeout=max(0, deadline - time.monotonic()))
                    except queue.Empty:
                        self.fail(
                            f"Preview did not report {message!r}:\n{''.join(log)}"
                        )
                    if line is None:
                        self.fail(f"Preview exited before {message!r}:\n{''.join(log)}")
                    log.append(line)
                    if message in line:
                        return line

            try:
                ready = wait_for("Browse at ")
                address = urlsplit(ready.split("Browse at ", 1)[1].strip())
                self.assertIsNotNone(address.port, ready)
                base = f"http://127.0.0.1:{address.port}"
                for route in routes:
                    with self.subTest(route=route):
                        with urlopen(base + route, timeout=15) as response:
                            html = response.read().decode()
                        self.assertIn("<h1", html)
                        script = BeautifulSoup(html, "html.parser").select_one(
                            'script[src$="quarto-preview.js"]'
                        )
                        self.assertIsNotNone(script)
                        with urlopen(
                            urljoin(base + route, script["src"]), timeout=15
                        ) as response:
                            self.assertEqual(response.status, 200)
                            self.assertIn("QuartoPreview", response.read().decode())

                source = workspace / "docs/BUILTIN_RUNTIME.md"
                marker = "Preview updates this guide without restarting the server."
                with connect(
                    base.replace("http:", "ws:") + "/builtin_runtime/",
                    proxy=None,
                    open_timeout=15,
                ) as client:
                    source.write_text(source.read_text() + f"\n{marker}\n")
                    deadline = time.monotonic() + 180
                    while True:
                        message = client.recv(
                            timeout=max(0, deadline - time.monotonic())
                        )
                        if message.startswith("reload"):
                            self.assertEqual(message, "reload")
                            break
                with urlopen(base + "/builtin_runtime/", timeout=15) as response:
                    html = response.read().decode()
                self.assertIn(marker, html)
                self.assertIn("quarto-preview.js", html)
                self.assertIsNone(process.poll(), "".join(log))
            finally:
                if process.poll() is None:
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            check=True,
                            capture_output=True,
                        )
                    else:
                        os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=15)
                reader.join(timeout=15)
                assert process.stdout is not None
                process.stdout.close()

            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts/render-site")],
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=180,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            site = workspace / "docs/_site"
            self.assertFalse((site / "BUILTIN_RUNTIME.html").exists())
            for route in routes:
                self.assertNotIn(
                    "quarto-preview.js",
                    (site / route.lstrip("/") / "index.html").read_text(),
                )

    def test_get_started_has_connection_navigation(self) -> None:
        soup = BeautifulSoup(
            (self.site / "getting-started/index.html").read_text(), "html.parser"
        )
        navbar = soup.select_one(".navbar")
        sidebar = soup.find(id="quarto-sidebar")
        for element, included, excluded in (
            (navbar, "getting-started/index.html", "windows/index.html"),
            (sidebar, "clients/index.html", "configuration/index.html"),
        ):
            links = {
                linked_file(self.site / "getting-started/index.html", link["href"])
                .relative_to(self.site)
                .as_posix()
                for link in element.select("a[href]")
                if not urlsplit(link["href"]).scheme
                and not urlsplit(link["href"]).netloc
            }
            self.assertIn(included, links)
            self.assertNotIn(excluded, links)

    def test_mcp_client_setup_uses_uvx_and_global_scope(self) -> None:
        for name in ("getting-started/index.html", "clients/index.html"):
            with self.subTest(page=name):
                commands = [
                    line.strip()
                    for code in self.pages[self.site / name].code_blocks
                    for line in code.splitlines()
                    if "configure-client" in line
                ]
                for client in ("codex", "claude"):
                    self.assertIn(
                        f"uvx mcp-console configure-client {client}", commands
                    )

    def test_other_client_configuration_uses_uvx(self) -> None:
        configuration = next(
            json.loads(code)
            for code in self.pages[self.site / "clients/index.html"].code_blocks
            if '"mcpServers"' in code
        )
        server = configuration["mcpServers"]["console"]
        self.assertEqual(server["command"], "uvx")
        self.assertEqual(server["args"], ["mcp-console", "serve"])

    def test_moved_reference_pages_and_historical_fragments(self) -> None:
        fragments = {
            "REFERENCE": ("find-a-task", "apis-and-package-manuals"),
            "README": (
                "get-connected",
                "learn-console",
                "reference",
                "configure-a-project",
                "platform-setup",
                "public-reference",
                "develop-console",
            ),
            "getting-started": (
                "install",
                "build-from-source",
                "connect-your-client",
                "codex-and-claude-code",
                "other-mcp-clients",
                "r-and-python-packages",
            ),
            "CLIENTS": (
                "mcp-clients",
                "supported-platforms",
                "r-python-and-sql",
                "build-from-source",
                "connect-from-application-code",
                "from-r",
                "from-python",
                "configure-the-models-interface",
            ),
            "CONFIGURATION": (
                "model-visible-languages",
                "tool-descriptions-and-parameters",
                "inspect-and-edit-resolved-configuration",
            ),
            "BUILTIN_RUNTIME": (
                "runtime-selection",
                "r",
                "python",
                "r-and-python-interoperability",
                "sql-and-duckdb",
                "plots-and-images",
                "output-and-notices",
                "current-limitations",
                "installed-documentation",
                "select-a-native-connection",
                "result-previews",
            ),
            "REQUIREMENTS": (
                "inspecting-and-replacing-requirements",
                "accepted-requirement-input",
                "requirements-for-a-cell",
                "automatic-r-package-resolution",
                "automatic-python-import-resolution",
                "live-preparation",
                "live-r-preparation",
                "live-python-preparation",
                "duckdb-extension-preparation",
                "restarting-with-requirements",
                "custom-workers",
            ),
            "WINDOWS": (
                "install-and-run",
                "native-sandbox",
                "build-from-source",
                "lifecycle-and-platform-differences",
                "validation",
                "testing-parity-and-remaining-gaps",
                "troubleshooting",
            ),
            "SANDBOX": (
                "application-policy-and-launch",
                "policy-extensions-and-compatibility",
            ),
            "LINUX_COMPATIBILITY": ("reproducing-the-comparisons",),
            "SEND_OPERATIONS": ("python-client-cancellation",),
            "API": (
                "server-readiness",
                "operations",
                "wait-timing-and-cancellation",
                "inspecting-and-replacing-requirements",
                "accepted-requirement-input",
                "requirements-for-a-cell",
                "live-preparation",
                "restarting-with-requirements",
                "resolution-policy-admission",
            ),
            "RECORDING": ("storage-and-retention", "recording-failures"),
            "RECORDING_REFERENCE": ("storage-and-retention", "recording-failures"),
            "PYTHON-GUIDE": (
                "results-and-lifetime",
                "interrupt-a-wait-or-stop-execution",
                "inspect-or-replace-requirements",
            ),
            "python": (
                "start-without-a-model",
                "add-a-model-framework",
                "calls-and-dependencies",
            ),
        }
        for name, identifiers in fragments.items():
            with self.subTest(page=name):
                self.assertLessEqual(
                    set(identifiers),
                    self.pages[self.site / f"{name.lower()}/index.html"].ids,
                )
        for name in (
            "RUNTIME_REFERENCE",
            "API",
            "TOOL_CONFIGURATION",
            "WINDOWS_DEVELOPMENT",
        ):
            with self.subTest(reference=name):
                soup = BeautifulSoup(
                    (self.site / f"{name.lower()}/index.html").read_text(),
                    "html.parser",
                )
                self.assertGreater(len(soup.select_one("main").get_text()), 1000)

    def test_workspace_profile_is_available_in_the_sandbox_guide(self) -> None:
        commands = [
            line.strip()
            for name in (
                "getting-started/index.html",
                "clients/index.html",
                "sandbox/index.html",
            )
            for code in self.pages[self.site / name].code_blocks
            for line in code.splitlines()
        ]
        self.assertIn("uvx mcp-console config init", commands)
        for command in commands:
            if "configure-client" in command:
                self.assertNotIn("sandbox.profile=workspace", command)
                self.assertNotIn("--writable-root", command)
        for name in ("CONFIGURATION", "SANDBOX_CONFIGURATION"):
            with self.subTest(page=name):
                self.assertTrue(
                    any(
                        line.strip() == "profile: workspace"
                        for code in self.pages[
                            self.site / f"{name.lower()}/index.html"
                        ].code_blocks
                        for line in code.splitlines()
                    )
                )

    def test_permission_examples_distinguish_global_and_project_scope(self) -> None:
        soup = BeautifulSoup(
            (self.site / "sandbox_configuration/index.html").read_text(), "html.parser"
        )
        examples = (
            ("allow-workspace-writes-across-projects", "global"),
            ("grant-writes-to-one-directory", "project"),
        )
        identifiers = [identifier for identifier, _ in examples]
        self.assertEqual(
            [section["id"] for section in soup.find_all("section", id=identifiers)],
            identifiers,
        )
        for identifier, scope in examples:
            with self.subTest(scope=scope):
                section = soup.find(id=identifier)
                commands = [
                    line.strip()
                    for code in section.select("pre code")
                    for line in code.get_text().splitlines()
                ]
                command = next(
                    command for command in commands if "configure-client" in command
                )
                arguments = command.split()
                self.assertEqual(
                    arguments[:3], ["uvx", "mcp-console", "configure-client"]
                )
                self.assertIn(arguments[3], ("codex", "claude"))
                self.assertNotIn("--scope", arguments)
                if scope == "project":
                    directory_option = arguments.index("--directory")
                    self.assertEqual(arguments[directory_option + 1], ".")
                    self.assertLess(directory_option, arguments.index("--"))
                    self.assertIn("sandbox.profile=read_only", arguments)
                    self.assertIn("--writable-root", arguments)
                    self.assertLess(
                        commands.index("mkdir output"), commands.index(command)
                    )
                else:
                    self.assertNotIn("--directory", arguments)
                    self.assertIn("sandbox.profile=workspace", arguments)

    def test_reference_tables_have_keyboard_accessible_scroll_regions(self) -> None:
        for name in (
            "configuration/index.html",
            "runtime_configuration/index.html",
            "python-guide/index.html",
        ):
            with self.subTest(page=name):
                soup = BeautifulSoup((self.site / name).read_text(), "html.parser")
                tables = soup.select("main table")
                self.assertTrue(tables)
                for table in tables:
                    region = table.find_parent("div", class_="table-responsive")
                    self.assertIsNotNone(region)
                    self.assertEqual(region.get("tabindex"), "0")
                    self.assertEqual(region.get("role"), "region")
                    self.assertTrue(region.get("aria-label"))

    def test_r_package_subsite(self) -> None:
        home = (self.site / "r/index.html").resolve()
        reference = (self.site / "r/reference/console_tool/index.html").resolve()
        self.assertIn(home, self.pages, "pkgdown home page is missing")
        self.assertIn(reference, self.pages, "console_tool reference is missing")
        self.assertIn("mcp.console", self.pages[home].title)
        self.assertIn("console_tool", self.pages[reference].title)
        sitemap = ElementTree.parse(self.site / "r/sitemap.xml")
        urls = {
            element.text
            for element in sitemap.findall(
                ".//{http://www.sitemaps.org/schemas/sitemap/0.9}loc"
            )
        }
        reference_url = (
            "https://t-kalinowski.github.io/mcp-console/r/reference/console_tool/"
        )
        self.assertIn(reference_url, urls)
        search = json.loads((self.site / "r/search.json").read_text())
        self.assertIn(reference_url, {entry["path"] for entry in search})
        self.assertIn("arguments", self.pages[reference].ids)
        navbar = BeautifulSoup(reference.read_text(), "html.parser").select_one(
            ".navbar"
        )
        self.assertEqual(
            [
                link.get_text(strip=True)
                for link in navbar.select(".nav-link")
                if link.get_text(strip=True)
            ],
            ["MCP Console", "Get started", "Guides", "Build", "Reference"],
        )
        self.assertIn("../../../", self.pages[reference].links)
        for target in ("getting-started/", "readme/", "build/", "reference/"):
            self.assertIn(f"../../../{target}", self.pages[reference].links)
        self.assertIn(
            "https://github.com/t-kalinowski/mcp-console/blob/main/r/R/console-tool.R",
            self.pages[reference].links,
        )
        for name in (
            "reference/index.html",
            "r-guide/index.html",
            "package-links/index.html",
            "benchmarks/package-links/index.html",
        ):
            with self.subTest(page=name):
                page = self.site / name
                destinations = {
                    linked_file(page, link)
                    for link in self.pages[page].links
                    if not urlsplit(link).scheme and not urlsplit(link).netloc
                }
                self.assertIn(home, destinations)
                self.assertNotIn(
                    "https://github.com/t-kalinowski/mcp-console/blob/main/r/README.md",
                    self.pages[page].links,
                )

    def test_standalone_r_vignettes_link_to_published_pages(self) -> None:
        with tempfile.TemporaryDirectory(prefix="console-vignettes-") as directory:
            output = Path(directory)
            script = output / "render.R"
            script.write_text(
                "args <- commandArgs(trailingOnly = TRUE)\n"
                'for (input in list.files(args[[1]], pattern = "[.]Rmd$", full.names = TRUE)) {\n'
                "  rmarkdown::render(input, output_dir = args[[2]], quiet = TRUE)\n"
                "}\n"
            )
            result = subprocess.run(
                [
                    "Rscript",
                    "--vanilla",
                    str(script),
                    str(Path(self.workspace.name) / "r/vignettes"),
                    str(output),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            rendered = list(output.glob("*.html"))
            self.assertEqual(
                {path.stem for path in rendered},
                {path.stem for path in (ROOT / "r/vignettes").glob("*.Rmd")},
            )
            self.assert_published_links(
                [link for path in rendered for link in Page(path).links]
            )

    def test_python_downloads_link_to_published_pages(self) -> None:
        base = read_yaml(ROOT / "docs/_quarto.yml")["website"]["site-url"]
        for name in ("llms.txt", "llms-full.txt"):
            with self.subTest(download=name):
                text = (self.site / "python" / name).read_text()
                links = [
                    urljoin(base + "python/" + name, link)
                    for link in re.findall(r"(?<=\]\()([^)\s]+)(?=\))", text)
                ]
                self.assertTrue(links)
                self.assert_published_links(links)
                if name == "llms-full.txt":
                    for target in (
                        "python/reference/chatlas.tool/",
                        "python/reference/asyncmcpconsole/#send",
                        "api/#requirements",
                    ):
                        self.assertIn(base + target, links)

    def test_python_package_reference(self) -> None:
        home = (self.site / "python/index.html").resolve()
        self.assertIn(home, self.pages, "Python package guide is missing")
        reference = (self.site / "python/reference/index.html").resolve()
        self.assertIn(reference, self.pages, "Python API index is missing")
        for name in (
            "MCPConsole",
            "AsyncMCPConsole",
            "Requirements",
            "chatlas.tool",
            "chatlas.register",
            "openai.responses_tool",
            "openai.ResponsesTool",
            "openai.AsyncResponsesTool",
            "openai.agents_tool",
            "openai.agents_server",
            "anthropic.tool",
            "anthropic.tools",
            "codex.server",
        ):
            with self.subTest(symbol=name):
                page = (
                    self.site / f"python/reference/{name.lower()}/index.html"
                ).resolve()
                self.assertIn(page, self.pages, "Python API page is missing")
                self.assertIn(name, self.pages[page].title)
        sitemap = ElementTree.parse(self.site / "sitemap.xml")
        urls = {
            element.text
            for element in sitemap.findall(
                ".//{http://www.sitemaps.org/schemas/sitemap/0.9}loc"
            )
        }
        self.assertIn(
            "https://t-kalinowski.github.io/mcp-console/python/reference/mcpconsole/",
            urls,
        )
        self.assertIn(
            "MCPConsole",
            (self.site / "python/llms-full.txt").read_text(),
        )
        self.assertIn(
            "reference/mcpconsole/",
            (self.site / "python/llms.txt").read_text(),
        )
        self.assertTrue((self.site / "python/reference/MCPConsole.md").is_file())
        inventory = json.loads((self.site / "python/objects.json").read_text())
        for item in inventory["items"]:
            url = urlsplit(item["uri"])
            target = self.site / url.path / "index.html"
            with self.subTest(symbol=item["name"]):
                self.assertIn(target, self.pages)
                self.assertIn(url.fragment, self.pages[target].ids)
        for name in ("index.html", "reference/mcpconsole/index.html"):
            with self.subTest(package_page=name):
                page = self.site / "python" / name
                destinations = {
                    linked_file(page, link)
                    for link in self.pages[page].links
                    if not urlsplit(link).scheme and not urlsplit(link).netloc
                }
                for target in (
                    "index.html",
                    "getting-started/index.html",
                    "readme/index.html",
                    "reference/index.html",
                ):
                    self.assertIn(self.site / target, destinations)
        self.assertIn(
            "https://github.com/t-kalinowski/mcp-console/blob/main/python/mcp_console/_sync.py",
            {
                link.split("#")[0]
                for link in self.pages[
                    (self.site / "python/reference/mcpconsole/index.html").resolve()
                ].links
            },
        )
        for name, target in (
            ("reference/index.html", home),
            ("python-guide/index.html", reference),
        ):
            with self.subTest(page=name):
                page = self.site / name
                destinations = {
                    linked_file(page, link)
                    for link in self.pages[page].links
                    if not urlsplit(link).scheme and not urlsplit(link).netloc
                }
                self.assertIn(target, destinations)

    def test_python_reference_uses_main_website(self) -> None:
        guide = BeautifulSoup(
            (self.site / "python-guide/index.html").read_text(), "html.parser"
        )
        for name in (
            "python/index.html",
            "python/reference/index.html",
            "python/reference/mcpconsole/index.html",
            "python/reference/openai.asyncresponsestool/index.html",
        ):
            with self.subTest(page=name):
                path = self.site / name
                page = BeautifulSoup(path.read_text(), "html.parser")
                navbar = page.select_one("#quarto-header .navbar")
                self.assertIsNotNone(navbar)
                self.assertEqual(
                    navbar.get_text(" ", strip=True),
                    guide.select_one("#quarto-header .navbar").get_text(
                        " ", strip=True
                    ),
                )
                sidebar = page.select_one("#quarto-sidebar")
                self.assertIsNotNone(sidebar)
                self.assertIn("Configuration", sidebar.get_text())
                self.assertIn("MCP send", sidebar.get_text())
                self.assertIn("Python API", sidebar.get_text())
                self.assertIsNotNone(page.select_one("#quarto-search"))
                self.assertIsNotNone(page.select_one(".quarto-color-scheme-toggle"))
                self.assertIsNone(
                    page.select_one('nav[aria-label="Console documentation"]')
                )
                self.assertNotIn("great-docs", str(page.select("link[rel=stylesheet]")))
        search = json.loads((self.site / "search.json").read_text())
        self.assertIn(
            "python/reference/mcpconsole/",
            {urlsplit(entry["href"]).path for entry in search},
        )
        self.assertTrue(
            any(
                "send_tool" in entry["text"]
                and urlsplit(entry["href"]).path == "python/reference/mcpconsole/"
                for entry in search
            ),
            "The shared search must include inline method documentation",
        )

    def test_python_integrations_are_grouped_by_package(self) -> None:
        groups = {
            "openai — Responses API": {
                "openai.responses_tool",
                "openai.ResponsesTool",
                "openai.AsyncResponsesTool",
            },
            "openai-agents — Agents SDK": {
                "openai.agents_tool",
                "openai.agents_server",
            },
            "openai-codex — Codex SDK": {"codex.server"},
        }
        reference = self.site / "python/reference/index.html"
        index = BeautifulSoup(reference.read_text(), "html.parser")
        sections = {
            heading.get_text(" ", strip=True): heading.find_parent("section")
            for heading in index.select("main h2")
        }
        for label, symbols in groups.items():
            with self.subTest(group=label):
                self.assertIn(label, sections.keys())
                links = {
                    linked_file(reference, link["href"])
                    for link in sections[label].select("a[href]")
                    if urlsplit(link["href"]).path
                }
                self.assertEqual(
                    links,
                    {
                        reference.parent / f"{symbol.lower()}/index.html"
                        for symbol in symbols
                    },
                )
        page = BeautifulSoup(
            (self.site / "python-guide/index.html").read_text(), "html.parser"
        )
        labels = {
            row.find("td").get_text(" ", strip=True)
            for row in page.select("table tbody tr")
        }
        self.assertEqual(groups.keys() & labels, groups.keys())
        adapter = (
            BeautifulSoup(
                (self.site / "python/reference/codex.server/index.html").read_text(),
                "html.parser",
            )
            .select_one("main")
            .get_text(" ", strip=True)
        )
        self.assertIn("Codex SDK", adapter)
        self.assertNotIn("thread SDK", adapter)

    def test_python_methods_are_inline(self) -> None:
        for name, context_methods in (
            ("MCPConsole", ("__enter__", "__exit__")),
            ("AsyncMCPConsole", ("__aenter__", "__aexit__")),
        ):
            with self.subTest(client=name):
                path = self.site / f"python/reference/{name.lower()}/index.html"
                page = self.pages[path]
                for method in (
                    "connect",
                    "close",
                    "send",
                    "send_tool",
                    *context_methods,
                ):
                    self.assertIn(f"mcp_console.{name}.{method}", page.ids)
                    self.assertEqual(
                        path.read_text().count(f'id="mcp_console.{name}.{method}"'), 1
                    )
                    self.assertFalse(
                        (
                            self.site
                            / f"python/reference/{name.lower()}.{method}/index.html"
                        ).exists(),
                        "methods should be documented on the class page",
                    )

    def test_package_links_are_relative_at_each_depth(self) -> None:
        for name, prefix in (
            ("package-links/index.html", "../"),
            ("benchmarks/package-links/index.html", "../../"),
        ):
            with self.subTest(page=name):
                page = self.pages[self.site / name]
                for target in (
                    "r/reference/console_tool/?view=full#arguments",
                    "python/reference/mcpconsole/?view=full#"
                    "mcp_console.MCPConsole.send",
                    "r/",
                    "r/#inspect-and-edit-configuration",
                ):
                    self.assertIn(prefix + target, page.links)

    def test_repository_links_are_classified_at_each_depth(self) -> None:
        for name, prefix in (
            ("package-links/index.html", "../"),
            ("benchmarks/package-links/index.html", "../../"),
        ):
            with self.subTest(page=name):
                page = self.pages[self.site / name]
                for source in (
                    "src/main.rs?plain=1#L1",
                    "docs/templates/task-checkpoint.md",
                ):
                    self.assertIn(
                        "https://github.com/t-kalinowski/mcp-console/blob/main/"
                        + source,
                        page.links,
                    )
                self.assertIn(
                    prefix + "send_operations/#standard-input-and-managed-reads",
                    page.links,
                )

    def test_python_adapters_show_complete_examples(self) -> None:
        for name, factory in (
            ("chatlas.tool", "chatlas.tool"),
            ("chatlas.register", "chatlas.register"),
            ("openai.responses_tool", "openai.responses_tool"),
            ("openai.ResponsesTool", "openai.responses_tool"),
            ("openai.AsyncResponsesTool", "openai.responses_tool"),
            ("openai.agents_tool", "openai.agents_tool"),
            ("openai.agents_server", "openai.agents_server"),
            ("anthropic.tool", "anthropic.tool"),
            ("anthropic.tools", "anthropic.tools"),
            ("codex.server", "codex.server"),
        ):
            with self.subTest(adapter=name):
                page = self.pages[
                    self.site / f"python/reference/{name.lower()}/index.html"
                ]
                examples = [
                    code
                    for code in page.code_blocks
                    if "import mcp_console" in code
                    and f"mcp_console.{factory}(" in code
                ]
                self.assertTrue(examples, "missing standalone usage example")
                for code in examples:
                    ast.parse(code)

    def test_local_links_and_anchors_resolve(self) -> None:
        self.assertTrue(self.pages, "the website has no rendered pages")
        for path, page in self.pages.items():
            for link in page.links:
                url = urlsplit(link)
                same_site = (
                    url.netloc == "t-kalinowski.github.io"
                    and url.path.startswith("/mcp-console/")
                )
                if (url.scheme or url.netloc) and not same_site:
                    if url.netloc == "github.com" and url.path.startswith(
                        "/t-kalinowski/mcp-console/blob/main/"
                    ):
                        with self.subTest(
                            page=str(path.relative_to(self.site)), link=link
                        ):
                            self.assertNotIn("/../", url.path)
                            source = url.path.removeprefix(
                                "/t-kalinowski/mcp-console/blob/main/"
                            )
                            self.assertTrue(
                                (ROOT / unquote(source)).exists(),
                                "missing repository target",
                            )
                    continue
                target = unquote(url.path)
                if target.startswith("/"):
                    destination = self.site / target.removeprefix(
                        "/mcp-console/"
                    ).lstrip("/")
                else:
                    destination = path.parent / target if target else path
                if destination.is_dir():
                    destination /= "index.html"
                destination = destination.resolve()
                with self.subTest(page=str(path.relative_to(self.site)), link=link):
                    self.assertTrue(
                        destination.is_relative_to(self.site), "link escapes the site"
                    )
                    self.assertTrue(destination.is_file(), "missing link target")
                    if url.fragment and destination in self.pages:
                        self.assertIn(
                            unquote(url.fragment), self.pages[destination].ids
                        )


if __name__ == "__main__":
    unittest.main()
