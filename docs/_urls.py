#!/usr/bin/env python3
"""Publish lowercase directory URLs without changing documentation sources."""

import json
import os
from pathlib import Path
import posixpath
import re
from urllib.parse import urlsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup
from yaml12 import read_yaml

docs = Path(__file__).resolve().parent
site = Path(os.environ["QUARTO_PROJECT_OUTPUT_DIR"]).resolve()
base = read_yaml(docs / "_quarto.yml")["website"]["site-url"]
base_path = urlsplit(base).path
preview = os.environ.get("MCP_CONSOLE_WEBSITE_PREVIEW") == "1"
preview_client = (docs / "_preview.html").read_text() if preview else ""


def page_url(path: str) -> str:
    # The package overviews already occupy /r/ and /python/.
    if path in ("R.html", "PYTHON.html"):
        return path.removesuffix(".html").lower() + "-guide/"
    if path.endswith(".html") and posixpath.basename(path) != "404.html":
        path = path.lower().removesuffix(".html")
        if posixpath.basename(path) == "index":
            path = posixpath.dirname(path)
        return path + "/" if path else ""
    return path


def rewrite_url(value: str, source: str, destination: str) -> str:
    url = urlsplit(value)
    if not url.path:
        return value
    if url.scheme or url.netloc:
        if not value.startswith(base):
            return value
        path = page_url(url.path.removeprefix(base_path))
        return url._replace(path=base_path + path).geturl()
    if url.path.startswith("/"):
        path = url.path.removeprefix(base_path).lstrip("/")
        return url._replace(path=base_path + page_url(path)).geturl()
    path = posixpath.normpath(posixpath.join(posixpath.dirname(source), url.path))
    path = page_url(path)
    relative = posixpath.relpath(path or ".", posixpath.dirname(destination) or ".")
    if not path or path.endswith("/") or url.path.endswith("/"):
        relative += "/"
    return url._replace(path=relative).geturl()


def write_changed(path: Path, content: str) -> None:
    if not path.exists() or path.read_text() != content:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


# Quarto reports only this render's outputs. Do not rewrite unrelated pages
# during preview; pkgdown runs only for a full render.
pages = [
    Path(name).resolve()
    for name in os.environ["QUARTO_PROJECT_OUTPUT_FILES"].splitlines()
    if name.endswith(".html")
]
full = os.environ.get("QUARTO_PROJECT_RENDER_ALL") == "1"
if full:
    pages.extend(sorted((site / "r").rglob("*.html")))
rendered = {page_url(path.relative_to(site).as_posix()) for path in pages}
assert len(rendered) == len(pages), "Published page URLs must be unique"
for path in pages:
    source = path.relative_to(site).as_posix()
    route = page_url(source)
    destination = route + "index.html" if not route or route.endswith("/") else route
    soup = BeautifulSoup(path.read_text(), "html.parser")
    for element in soup.find_all(True):
        for attribute in ("href", "src", "data-original-href", "data-search-index"):
            if attribute in element.attrs:
                element[attribute] = rewrite_url(
                    element[attribute], source, destination
                )
        if element.name == "meta":
            if element.get("http-equiv") == "refresh":
                # pkgdown aliases point grouped symbols at their owning page.
                # Use local URLs so these work in preview as well as deployment.
                delay, target = element["content"].split(";URL=", 1)
                if target.startswith(base):
                    url = urlsplit(target)
                    target = url._replace(
                        scheme="",
                        netloc="",
                        path=posixpath.relpath(
                            url.path.removeprefix(base_path),
                            posixpath.dirname(source) or ".",
                        ),
                    ).geturl()
                element["content"] = (
                    delay + ";URL=" + rewrite_url(target, source, destination)
                )
            elif element.get("name") == "quarto:offset":
                element["content"] = (
                    posixpath.relpath(".", posixpath.dirname(destination) or ".") + "/"
                )
            elif element.get("property") in ("og:url", "og:image"):
                element["content"] = rewrite_url(
                    element["content"], source, destination
                )
    # Quarto highlights the first navbar link found anywhere in the sidebar.
    # Prefer the current page (a clean self-link is ./), then the sidebar index.
    # This lets Get started and Guides share a sidebar without sharing a highlight.
    navbar_links = soup.select("#quarto-header .navbar .nav-link[href]")
    sidebar_index = soup.select_one("#quarto-sidebar .sidebar-item a[href]")
    if any(link["href"] == "./" for link in navbar_links):
        active_href = "./"
    else:
        active_href = sidebar_index["href"] if sidebar_index else None
    for link in navbar_links:
        link["class"] = [name for name in link.get("class", []) if name != "active"]
        link.attrs.pop("aria-current", None)
        if link["href"] == active_href:
            link["class"].append("active")
            link["aria-current"] = "page" if active_href == "./" else "true"
    html = str(soup)
    # Quarto hashes its original outputs after post-render, including during
    # live updates. Keep them in preview and attach its reload client to the
    # relocated copies, which the server otherwise treats as static files.
    if preview and source != destination:
        html += "\n" + preview_client
    write_changed(site / destination, html)
    if not preview and source != destination:
        path.unlink()

# Quarto merges search and sitemap entries using its original output names.
# On partial renders, replace the previous clean entries for the rendered pages,
# including sections that have been removed since the previous render.
search_file = site / "search.json"
search = json.loads(search_file.read_text())
search = [
    entry
    for entry in search
    if urlsplit(entry["href"]).path.removeprefix("./") not in rendered
]
for entry in search:
    for key in ("href", "objectID"):
        entry[key] = rewrite_url(entry[key], "index.html", "index.html")
write_changed(search_file, json.dumps(search, ensure_ascii=False, indent=2) + "\n")

sitemaps = [site / "sitemap.xml"]
if full:
    sitemaps.append(site / "r/sitemap.xml")
for sitemap in sitemaps:
    tree = ElementTree.parse(sitemap)
    root = tree.getroot()
    namespace = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    for entry in list(root):
        location = entry.find(namespace + "loc")
        assert location is not None and location.text is not None
        old = location.text
        location.text = rewrite_url(old, "index.html", "index.html")
        if (
            sitemap.parent == site
            and old == location.text
            and old.removeprefix(base) in rendered
        ):
            root.remove(entry)
    ElementTree.register_namespace("", namespace[1:-1])
    write_changed(sitemap, ElementTree.tostring(root, encoding="unicode") + "\n")

if full:
    r_search_file = site / "r/search.json"
    r_search = json.loads(r_search_file.read_text())
    for entry in r_search:
        entry["path"] = rewrite_url(entry["path"], "index.html", "index.html")
    write_changed(
        r_search_file, json.dumps(r_search, ensure_ascii=False, indent=2) + "\n"
    )

inventory_file = site / "python/objects.json"
inventory = json.loads(inventory_file.read_text())
for item in inventory["items"]:
    item["uri"] = rewrite_url(item["uri"], "index.html", "index.html")
write_changed(inventory_file, json.dumps(inventory, ensure_ascii=False) + "\n")

# Keep the downloadable Python documentation's links aligned with the website.
for path in [*(site / "python").rglob("*.md"), *(site / "python").glob("llms*.txt")]:
    source = path.relative_to(site).as_posix()
    text = re.sub(
        r"(?<=\]\()([^)\s]+)(?=\))",
        lambda match: rewrite_url(match[0], source, source),
        path.read_text(),
    )
    write_changed(path, text)
