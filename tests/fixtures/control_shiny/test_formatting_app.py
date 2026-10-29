"""Display formatting preserves submitted source, output, and transcript position."""

import os
import re

import pytest
from playwright.sync_api import expect
from shiny.playwright.controller import Chat
from test_app import submit
from test_export_app import copy_projection

pytest_plugins = ["test_app"]


def test_formatted_python_and_r_restore_original_and_keep_output(app):
    page, _ = app
    page.get_by_label("Original", exact=True).check(timeout=5_000)
    python = 'format_value=("A  B\\nC");print( format_value )'
    r = 'format_value<-"R  text";cat( format_value,"\\n",sep="" )'
    submit(page, "python", python)
    submit(page, "r", r)
    entries = page.locator("#transcript .entry.complete")
    expect(entries).to_have_count(2, timeout=30_000)
    expect(entries.locator(".language")).to_have_text(["PYTHON", "R"])
    expect(entries.locator(".origin")).to_have_text(["Human", "Human"])
    original = entries.locator(".source code").all_text_contents()
    assert [source.rstrip("\n") for source in original] == [python, r]
    output = entries.locator(".content").all_text_contents()
    assert output == ["A  B\nC\n", "R  text\n"]
    page.get_by_label("Formatted", exact=True).check()
    expect(entries.nth(0).locator(".format-status")).to_contain_text("Ruff")
    expect(entries.nth(1).locator(".format-status")).to_contain_text("Air")
    expect(entries.nth(0).locator(".source code")).to_contain_text(
        'format_value = "A  B\\nC"'
    )
    expect(entries.nth(1).locator(".source code")).to_contain_text(
        'format_value <- "R  text"'
    )
    formatted = entries.locator(".source code").all_text_contents()
    assert formatted != original
    assert 'format_value = "A  B\\nC"' in formatted[0]
    assert 'format_value <- "R  text"' in formatted[1]
    assert entries.locator(".content").all_text_contents() == output
    page.get_by_label("Original", exact=True).check()
    expect(entries.nth(0).locator(".source code")).to_have_text(python)
    assert entries.locator(".source code").all_text_contents() == original
    assert entries.locator(".content").all_text_contents() == output


def test_formatter_failure_and_sql_keep_original_source(app):
    page, _ = app
    expect(page.get_by_label("Formatted", exact=True)).to_be_checked()
    malformed = "broken = ["
    sql = "select  42 as sql_value"
    submit(page, "python", malformed)
    first = page.locator("#transcript .entry").first
    expect(first.locator(".execution-error")).to_be_visible(timeout=30_000)
    expect(first.locator(".format-status")).to_contain_text("could not format")
    expect(first.locator(".source code")).to_have_text(malformed)
    failure = first.locator(".content").text_content()
    submit(page, "sql", sql)
    second = page.locator("#transcript .entry").nth(1)
    expect(second.locator(".content")).to_contain_text("42", timeout=30_000)
    expect(second.locator(".format-status")).to_contain_text("SQL")
    expect(second.locator(".format-status")).to_contain_text(
        re.compile("original|unchanged")
    )
    expect(second.locator(".source code")).to_have_text(sql)
    page.get_by_label("Original", exact=True).check()
    assert first.locator(".source code").text_content().rstrip("\n") == malformed
    assert second.locator(".source code").text_content().rstrip("\n") == sql
    assert first.locator(".content").text_content() == failure


def test_native_copy_follows_view_but_qmd_keeps_submitted_source(app):
    page, _ = app
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    source = 'copy_value=[1,2,3];print( "literal  spaces",copy_value )'
    submit(page, "python", source)
    entries = page.locator("#transcript .entry.complete")
    expect(entries).to_have_count(1, timeout=30_000)
    submit(page, "python", source)
    expect(entries).to_have_count(2, timeout=30_000)
    for entry in (entries.nth(0), entries.nth(1)):
        expect(entry.locator(".source code")).to_contain_text("copy_value = [1, 2, 3]")
        formatted = entry.locator(".source code").text_content()
        entry.get_by_role("button", name="Copy to clipboard", exact=True).click()
        expect(entry.locator(".code-copy-button")).to_have_class(re.compile("checked"))
        assert page.evaluate("navigator.clipboard.readText()") == formatted
    native_sources = entries.locator(".source > [id]")
    expect(native_sources).to_have_count(2)
    source_ids = [node.get_attribute("id") for node in native_sources.all()]
    assert len(set(source_ids)) == 2
    qmd = copy_projection(page, "QMD")
    assert qmd.count(source) == 2
    assert formatted.rstrip("\n") not in qmd
    page.get_by_role("button", name="Export", exact=True).click()
    page.get_by_label("Original", exact=True).check()
    for entry in (entries.nth(0), entries.nth(1)):
        expect(entry.locator(".source code")).to_have_text(source)
        entry.get_by_role("button", name="Copy to clipboard", exact=True).click()
        expect(entry.locator(".code-copy-button")).to_have_class(re.compile("checked"))
        assert page.evaluate("navigator.clipboard.readText()").rstrip("\n") == source
    assert copy_projection(page, "QMD") == qmd


def test_pending_updates_keep_view_and_scroll_position(app):
    page, workspace = app
    source = (
        "import time;from pathlib import Path\n"
        'for i in range(120):print(f"pending-format-{i}",flush=True)\n'
        'while not Path("format-more").exists():time.sleep(0.02)\n'
        'print("format-more-output",flush=True)\n'
        'while not Path("format-done").exists():time.sleep(0.02)\n'
        'print("format-finished-output")'
    )
    second_source = "second_value=[1,2];print( second_value )"
    submit(page, "python", source)
    first = page.locator("#transcript .entry").first
    expect(first.locator(".content")).to_contain_text(
        "pending-format-119", timeout=30_000
    )
    expect(first.locator(".source code")).to_contain_text(
        "for i in range(120):\n    print"
    )
    page.wait_for_function(
        "() => { const h = document.querySelector('#transcript-history'); return h.scrollHeight - h.scrollTop - h.clientHeight < 8; }"
    )
    history = page.locator("#transcript-history")
    try:
        history.evaluate("element => element.scrollTop = 0")
        page.get_by_label("Original", exact=True).check()
        expect(first.locator(".source code")).to_have_text(source)
        assert history.evaluate("element => element.scrollTop") == 0
        (workspace / "format-more").touch()
        expect(first.locator(".content")).to_contain_text("format-more-output")
        expect(page.get_by_label("Original", exact=True)).to_be_checked()
        assert first.locator(".source code").text_content().rstrip("\n") == source
        assert history.evaluate("element => element.scrollTop") == 0
        page.get_by_label("Formatted", exact=True).check()
        expect(first.locator(".source code")).to_contain_text(
            "for i in range(120):\n    print"
        )
        assert history.evaluate("element => element.scrollTop") == 0
        page.get_by_label("Immediate", exact=True).check()
        submit(page, "python", second_source)
        second = page.locator("#transcript .entry").nth(1)
        expect(second.locator(".content")).to_have_text("[1, 2]\n", timeout=30_000)
        expect(second.locator(".source code")).to_contain_text("second_value = [1, 2]")
        expect(first.locator(".source code")).to_contain_text(
            "for i in range(120):\n    print"
        )
        assert history.evaluate("element => element.scrollTop") == 0
        (workspace / "format-done").touch()
        expect(page.locator("#transcript .entry.complete")).to_have_count(
            2, timeout=30_000
        )
        expect(first.locator(".content")).to_contain_text("format-finished-output")
        assert first.locator(".content").text_content().count("pending-format-119") == 1
        page.get_by_label("Original", exact=True).check()
        expect(second.locator(".source code")).to_have_text(second_source)
        assert first.locator(".source code").text_content().rstrip("\n") == source
        assert history.evaluate("element => element.scrollTop") == 0
        history.evaluate("element => element.scrollTop = element.scrollHeight")
        page.get_by_label("Formatted", exact=True).check()
        expect(second.locator(".source code")).to_contain_text("second_value = [1, 2]")
        page.wait_for_function(
            "() => { const h = document.querySelector('#transcript-history'); return h.scrollHeight - h.scrollTop - h.clientHeight < 8; }"
        )
    finally:
        (workspace / "format-more").touch()
        (workspace / "format-done").touch()


@pytest.mark.skipif(
    os.environ.get("MCP_CONSOLE_LIVE_AGENT") != "1",
    reason="Live Luna inference is explicitly enabled separately",
)
def test_agent_source_uses_the_same_format_toggle(app):
    page, _ = app
    page.get_by_label("Original", exact=True).check()
    Chat(page, "agent_chat").loc_input.fill(
        "Make exactly one console_fixture send call with this Python code: "
        "agent_format_value=[1,2,3];print( agent_format_value ). "
        "Then reply done."
    )
    Chat(page, "agent_chat").send_user_input(method="click")
    expect(page.locator("#agent_state")).to_have_text("Completed", timeout=120_000)
    entry = page.locator("#transcript .entry.agent.complete")
    expect(entry).to_have_count(1, timeout=30_000)
    expect(entry.locator(".origin")).to_have_text("Agent")
    expect(entry.locator(".language")).to_have_text("PYTHON")
    expect(entry.locator(".content")).to_contain_text("[1, 2, 3]")
    original = entry.locator(".source code").text_content()
    page.get_by_label("Formatted", exact=True).check()
    expect(entry.locator(".format-status")).to_contain_text("Ruff")
    expect(entry.locator(".source code")).to_contain_text(
        "agent_format_value = [1, 2, 3]"
    )
    page.get_by_label("Original", exact=True).check()
    expect(entry.locator(".source code")).to_have_text(original)
    assert entry.locator(".source code").text_content() == original
