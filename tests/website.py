#!/usr/bin/env python3
"""Render the documentation website and check its public pages and links."""

from html.parser import HTMLParser
import ast
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup
from yaml12 import read_yaml

ROOT = Path(__file__).resolve().parent.parent


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
            ROOT / "docs", docs, ignore=shutil.ignore_patterns(".quarto", "_site")
        )
        shutil.copytree(
            ROOT / "r",
            docs.parent / "r",
            ignore=shutil.ignore_patterns("docs", "*.Rcheck", "*.tar.gz"),
        )
        shutil.copytree(ROOT / "python", docs.parent / "python")
        shutil.copytree(
            ROOT / "tests/snapshots/cli/interface/test_help",
            docs.parent / "tests/snapshots/cli/interface/test_help",
        )
        for name in ("pyproject.toml", "great-docs.yml", "LICENSE"):
            shutil.copy2(ROOT / name, docs.parent / name)
        for directory in (docs, docs / "benchmarks"):
            r_readme = "../r/README.md" if directory == docs else "../../r/README.md"
            (directory / "package-links.md").write_text(
                "# Package links\n\n"
                "[R reference](https://t-kalinowski.github.io/mcp-console/"
                "r/reference/console_tool.html?view=full#arguments)\n\n"
                "[Python reference](https://t-kalinowski.github.io/mcp-console/"
                "python/reference/MCPConsole.html?view=full#"
                "mcp_console.MCPConsole.send)\n\n"
                f"[R overview]({r_readme})\n\n"
                f"[R configuration]({r_readme}#inspect-and-edit-configuration)\n"
            )
        result = subprocess.run(
            ["quarto", "render", str(docs), "--to", "html"],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        cls.site = (docs / "_site").resolve()
        cls.pages = {path.resolve(): Page(path) for path in cls.site.rglob("*.html")}

    def test_guides_have_titles_navigation_and_search(self) -> None:
        search = json.loads((self.site / "search.json").read_text())
        indexed = {urlsplit(entry["href"]).path for entry in search}
        sources = [
            *(ROOT / "docs").glob("*.md"),
            *(ROOT / "docs/benchmarks").glob("*.md"),
        ]
        for source in sources:
            relative = source.relative_to(ROOT / "docs").with_suffix(".html")
            with self.subTest(page=str(relative)):
                page = self.pages[(self.site / relative).resolve()]
                heading = (
                    source.read_text()
                    .splitlines()[0]
                    .removeprefix("# ")
                    .replace("`", "")
                )
                self.assertIn(heading, page.title)
                self.assertIn("quarto-sidebar", page.ids)
                self.assertIn(relative.as_posix(), indexed)
        for name in ("index.html", "getting-started.html"):
            self.assertIn((self.site / name).resolve(), self.pages)
        self.assertFalse((self.site / "templates").exists())

    def test_guides_detect_system_color_scheme(self) -> None:
        for name in (
            "index.html",
            "cli-help.html",
            "benchmarks/transcript-concurrency.html",
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
            "r/reference/console_tool.html",
            "r/articles/autonomous-report.html",
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

    def test_r_report_vignette_is_discoverable(self) -> None:
        article = self.site / "r/articles/autonomous-report.html"
        self.assertIn(article, self.pages, "autonomous report vignette is missing")
        self.assertIn("Giving an agent an R session", self.pages[article].title)
        for source in ("index", "reference/console_tool", "reference/config-workflow"):
            page = self.site / f"r/{source}.html"
            with self.subTest(source=source):
                destinations = {
                    (page.parent / unquote(urlsplit(link).path)).resolve()
                    for link in self.pages[page].links
                    if not urlsplit(link).scheme and not urlsplit(link).netloc
                }
                self.assertIn(article, destinations)
        search = json.loads((self.site / "r/search.json").read_text())
        self.assertIn(
            "https://t-kalinowski.github.io/mcp-console/r/articles/autonomous-report.html",
            {entry["path"] for entry in search},
        )

    def test_cli_help_matches_tested_transcripts(self) -> None:
        path = self.site / "cli-help.html"
        self.assertIn(path, self.pages, "CLI help page is missing")
        soup = BeautifulSoup(path.read_text(), "html.parser")
        public_commands = [
            "mcp-console --help",
            "mcp-console serve --help",
            "mcp-console sandbox --help",
            "mcp-console config --help",
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
                    label = soup.find(id=pre.get("aria-labelledby"))
                    self.assertIsNotNone(label)
                    self.assertEqual(
                        label.get_text().strip(),
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
        self.assertIn(
            "cli-help.html", {urlsplit(entry["href"]).path for entry in search}
        )
        guide = self.site / "CLI.html"
        destinations = {
            (guide.parent / unquote(urlsplit(link).path)).resolve()
            for link in self.pages[guide].links
            if not urlsplit(link).scheme and not urlsplit(link).netloc
        }
        self.assertIn(path, destinations)

    def test_reading_paths_have_their_own_navigation(self) -> None:
        paths = (
            ("CONFIGURATION.html", "REQUIREMENTS.html", "SEND_OPERATIONS.html"),
            ("API.html", "REQUIREMENTS_API.html", "SANDBOX_CONFIGURATION.html"),
            ("ARCHITECTURE.html", "WINDOWS_DEVELOPMENT.html", "REQUIREMENTS.html"),
        )
        for name, included, excluded in paths:
            with self.subTest(page=name):
                soup = BeautifulSoup((self.site / name).read_text(), "html.parser")
                sidebar = soup.find(id="quarto-sidebar")
                self.assertIsNotNone(sidebar)
                links = {
                    urlsplit(link["href"]).path.removeprefix("./")
                    for link in sidebar.select("a[href]")
                }
                self.assertIn(included, links)
                self.assertNotIn(excluded, links)
                navbar = soup.select_one(".navbar")
                for label in (
                    "User guides",
                    "Client integration",
                    "Development",
                    "Packages",
                ):
                    self.assertIn(label, navbar.get_text(" ", strip=True))

    def test_home_title_has_one_introduction_and_description_metadata(self) -> None:
        soup = BeautifulSoup((self.site / "index.html").read_text(), "html.parser")
        title = soup.select_one("#title-block-header")
        self.assertIsNotNone(title.select_one(".subtitle"))
        self.assertIsNone(title.select_one(".description"))
        description = soup.find("meta", attrs={"name": "description"})
        self.assertIsNotNone(description)
        self.assertIn("data", description["content"])

    def test_moved_reference_pages_and_historical_fragments(self) -> None:
        fragments = {
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
                "build-from-source",
                "validation",
                "testing-parity-and-remaining-gaps",
            ),
            "SANDBOX": (
                "application-policy-and-launch",
                "policy-extensions-and-compatibility",
            ),
            "LINUX_COMPATIBILITY": ("reproducing-the-comparisons",),
            "SEND_OPERATIONS": ("python-client-cancellation",),
        }
        for name, identifiers in fragments.items():
            with self.subTest(page=name):
                self.assertLessEqual(
                    set(identifiers), self.pages[self.site / f"{name}.html"].ids
                )
        for name in (
            "RUNTIME_REFERENCE",
            "REQUIREMENTS_API",
            "TOOL_CONFIGURATION",
            "WINDOWS_DEVELOPMENT",
        ):
            with self.subTest(reference=name):
                soup = BeautifulSoup(
                    (self.site / f"{name}.html").read_text(), "html.parser"
                )
                self.assertGreater(len(soup.select_one("main").get_text()), 1000)

    def test_workspace_profile_is_available_in_setup_guides(self) -> None:
        for name in ("getting-started", "CONFIGURATION", "SANDBOX_CONFIGURATION"):
            with self.subTest(page=name):
                self.assertTrue(
                    any(
                        line.strip() == "profile: workspace"
                        for code in self.pages[self.site / f"{name}.html"].code_blocks
                        for line in code.splitlines()
                    )
                )

    def test_reference_tables_have_keyboard_accessible_scroll_regions(self) -> None:
        for name in ("CONFIGURATION.html", "REQUIREMENTS.html", "python/index.html"):
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
        reference = (self.site / "r/reference/console_tool.html").resolve()
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
            "https://t-kalinowski.github.io/mcp-console/r/reference/console_tool.html"
        )
        self.assertIn(reference_url, urls)
        search = json.loads((self.site / "r/search.json").read_text())
        self.assertIn(reference_url, {entry["path"] for entry in search})
        self.assertIn("arguments", self.pages[reference].ids)
        self.assertIn("../../index.html", self.pages[reference].links)
        self.assertIn(
            "https://github.com/t-kalinowski/mcp-console/blob/main/r/R/console-tool.R",
            self.pages[reference].links,
        )
        for name in (
            "index.html",
            "README.html",
            "DEVELOPMENT.html",
            "benchmarks/transcript-concurrency.html",
        ):
            with self.subTest(page=name):
                page = self.site / name
                destinations = {
                    (page.parent / unquote(urlsplit(link).path)).resolve()
                    for link in self.pages[page].links
                    if not urlsplit(link).scheme and not urlsplit(link).netloc
                }
                self.assertIn(home, destinations)
                self.assertNotIn(
                    "https://github.com/t-kalinowski/mcp-console/blob/main/r/README.md",
                    self.pages[page].links,
                )

    def test_python_package_subsite(self) -> None:
        home = (self.site / "python/index.html").resolve()
        self.assertIn(home, self.pages, "Great Docs home page is missing")
        reference = (self.site / "python/reference/index.html").resolve()
        self.assertIn(reference, self.pages, "Python API index is missing")
        for name in (
            "MCPConsole",
            "AsyncMCPConsole",
            "Requirements",
            "chatlas.tool",
            "chatlas.register",
            "openai.responses_tool",
            "openai.agents_tool",
            "openai.agents_server",
            "anthropic.tool",
            "anthropic.tools",
            "codex.server",
        ):
            with self.subTest(symbol=name):
                page = (self.site / f"python/reference/{name}.html").resolve()
                self.assertIn(page, self.pages, "Python API page is missing")
                self.assertIn(name, self.pages[page].title)
        search = json.loads((self.site / "python/search.json").read_text())
        self.assertIn("reference/MCPConsole.html", {entry["href"] for entry in search})
        sitemap = ElementTree.parse(self.site / "python/sitemap.xml")
        urls = {
            element.text
            for element in sitemap.findall(
                ".//{http://www.sitemaps.org/schemas/sitemap/0.9}loc"
            )
        }
        self.assertIn(
            "https://t-kalinowski.github.io/mcp-console/python/reference/MCPConsole.html",
            urls,
        )
        self.assertTrue((self.site / "python/llms-full.txt").is_file())
        for name in ("index.html", "reference/MCPConsole.html"):
            with self.subTest(package_page=name):
                page = self.site / "python" / name
                destinations = {
                    (page.parent / unquote(urlsplit(link).path)).resolve()
                    for link in self.pages[page].links
                    if not urlsplit(link).scheme and not urlsplit(link).netloc
                }
                for target in ("index.html", "PYTHON.html", "r/index.html"):
                    self.assertIn(self.site / target, destinations)
        self.assertIn(
            "https://github.com/t-kalinowski/mcp-console/blob/main/python/mcp_console/_sync.py",
            {
                link.split("#")[0]
                for link in self.pages[
                    (self.site / "python/reference/MCPConsole.html").resolve()
                ].links
            },
        )
        for name in ("index.html", "README.html", "PYTHON.html"):
            with self.subTest(page=name):
                page = self.site / name
                destinations = {
                    (page.parent / unquote(urlsplit(link).path)).resolve()
                    for link in self.pages[page].links
                    if not urlsplit(link).scheme and not urlsplit(link).netloc
                }
                self.assertIn(home, destinations)

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
                    (reference.parent / unquote(urlsplit(link["href"]).path)).resolve()
                    for link in sections[label].select("a[href]")
                    if urlsplit(link["href"]).path.endswith(".html")
                }
                self.assertEqual(
                    links,
                    {reference.parent / f"{symbol}.html" for symbol in symbols},
                )
        for name in ("python/index.html", "PYTHON.html"):
            with self.subTest(guide=name):
                page = BeautifulSoup((self.site / name).read_text(), "html.parser")
                labels = {
                    row.find("td").get_text(" ", strip=True)
                    for row in page.select("table tbody tr")
                }
                self.assertEqual(groups.keys() & labels, groups.keys())
        adapter = (
            BeautifulSoup(
                (self.site / "python/reference/codex.server.html").read_text(),
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
                path = self.site / f"python/reference/{name}.html"
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
                        (self.site / f"python/reference/{name}.{method}.html").exists(),
                        "methods should be documented on the class page",
                    )

    def test_package_links_are_relative_at_each_depth(self) -> None:
        for name, prefix in (
            ("package-links.html", ""),
            ("benchmarks/package-links.html", "../"),
        ):
            with self.subTest(page=name):
                page = self.pages[self.site / name]
                for target in (
                    "r/reference/console_tool.html?view=full#arguments",
                    "python/reference/MCPConsole.html?view=full#"
                    "mcp_console.MCPConsole.send",
                    "r/index.html",
                    "r/index.html#inspect-and-edit-configuration",
                ):
                    self.assertIn(prefix + target, page.links)

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
                page = self.pages[self.site / f"python/reference/{name}.html"]
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
