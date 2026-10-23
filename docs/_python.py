#!/usr/bin/env python3
"""Generate Python reference sources before Quarto discovers the site's pages."""

import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urljoin

from great_docs._apiref import APIReference
from yaml12 import read_yaml

docs = Path(__file__).resolve().parent
root = docs.parent
config = read_yaml(root / "great-docs.yml")
website = read_yaml(docs / "_quarto.yml")["website"]
assert config["inline_methods"] is True
assert config["dynamic"] is False

# APIReference is internal to the pinned Great Docs release. Its default child
# layout embeds methods; the high-level `members: false` spelling needs a list.
sections = config["reference"]["sections"]
for section in sections:
    for entry in section["contents"]:
        if isinstance(entry, dict) and entry.get("members") is False:
            entry["members"] = []

# The full Great Docs build normally supplies these to the source-link renderer.
os.environ.update(
    GITHUB_REPO_URL=website["repo-url"],
    GIT_REF=config["source"]["branch"],
    PACKAGE_ROOT=str(root),
    SOURCE_PATH="",
)


def add_anchors(page: Path) -> None:
    """Keep published symbol fragments that Great Docs 0.17 omits."""
    header, delimiter, body = page.read_text().partition("\n---\n")
    assert delimiter, f"Missing generated front matter: {page}"
    body = f'<span id="{config["module"]}.{page.stem}"></span>\n\n' + body

    def member_anchor(match: re.Match[str]) -> str:
        name = match[2].replace("\\", "").removesuffix("()")
        anchors = f'<span id="{config["module"]}.{page.stem}.{name}"></span>\n'
        # Quarto drops leading underscores from heading IDs. Keep the original
        # method fragment too, without copying either anchor into the TOC.
        if name.startswith("__"):
            anchors += f'<span id="{name}"></span>\n'
        return anchors + "\n" + match[0]

    body = re.sub(
        r"^(#+ )\[([^\]]+)\]\{([^}]*\bdoc-object-name\b[^}]*)\}",
        member_anchor,
        body,
        flags=re.MULTILINE,
    )
    page.write_text(header + delimiter + body)


# Generate away from the watched sources, then update only changed files.
# This also removes obsolete generated pages without touching the Python guide.
with tempfile.TemporaryDirectory(prefix="console-python-reference-") as workspace:
    os.chdir(workspace)
    APIReference(
        {
            "package": config["module"],
            "source_dir": str(root / "python"),
            "dir": "python/reference",
            "out_inventory": "python/objects.json",
            "title": config["reference"]["title"],
            "parser": config["parser"],
            "dynamic": config["dynamic"],
            "sections": sections,
        }
    ).build()
    generated = Path(workspace) / "python"
    for page in (generated / "reference").glob("*.qmd"):
        # Set the sidebar on each page: directory metadata treats "reference"
        # as the sibling directory and rewrites it as a relative path.
        page.write_text(
            page.read_text().replace("---\n", "---\nsidebar: reference\n", 1)
        )
        if page.stem != "index":
            add_anchors(page)
    # Preserve the package's Markdown and LLM download URLs. They are resources,
    # not a second set of pages for Quarto to render.
    sources = [
        docs / "python/index.qmd",
        *sorted((generated / "reference").glob("*.qmd")),
    ]
    full_text = []
    links = [f"# {config['display_name']}\n"]
    for page in sources:
        relative = (
            Path("index.md")
            if page == sources[0]
            else Path("reference") / page.with_suffix(".md").name
        )
        text = page.read_text()
        body = text.partition("\n---\n")[2]
        title = config["display_name"] if page == sources[0] else page.stem
        if relative == Path("reference/index.md"):
            title = config["reference"]["title"]
        markdown = re.sub(
            r"(\]\([^)\n]+)\.(?:qmd|md)([?#][^)]*)?\)",
            r"\1.html\2)",
            f"# {title}\n\n{body}",
        )
        (generated / relative).write_text(markdown)
        url = website["site-url"] + "python/" + relative.with_suffix(".html").as_posix()
        # The combined download lives above reference/. Resolve each page's
        # links against its own URL before moving its text into that download.
        full_text.append(
            re.sub(
                r"(?<=\]\()([^)\s]+)(?=\))",
                lambda match: urljoin(url, match[0]),
                markdown,
            )
        )
        links.append(f"- [{title}]({url})")
    (generated / "llms.txt").write_text("\n".join(links) + "\n")
    (generated / "llms-full.txt").write_text("\n\n".join(full_text))
    for source in generated.rglob("*"):
        if source.is_file():
            destination = docs / "python" / source.relative_to(generated)
            destination.parent.mkdir(parents=True, exist_ok=True)
            content = source.read_bytes()
            if not destination.exists() or destination.read_bytes() != content:
                destination.write_bytes(content)
    for page in (docs / "python/reference").glob("*.qmd"):
        if not (generated / "reference" / page.name).exists():
            page.unlink()
            page.with_suffix(".md").unlink()
