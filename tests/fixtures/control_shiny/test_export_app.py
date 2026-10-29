"""Export copies Console's existing projections through the public browser UI."""

import pytest
from playwright.sync_api import expect
from test_app import submit

pytest_plugins = ["test_app"]


@pytest.fixture(autouse=True)
def project_recordings(tmp_path):
    # Exercise Console's project precedence over the fixture's private home.
    (tmp_path / ".agents/console").mkdir(parents=True)


def copy_projection(page, name):
    if not page.get_by_role("button", name=f"Copy {name}", exact=True).is_visible():
        page.get_by_role("button", name="Export", exact=True).click()
    page.get_by_role("button", name=f"Copy {name}", exact=True).click()
    expect(page.locator("#export_status")).to_have_text(f"Copied {name}.")
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    return page.evaluate("navigator.clipboard.readText()")


def test_export_copies_recorded_source_and_transcript_without_private_inspection(app):
    page, workspace = app
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    source = (
        "# "
        + "x" * 10000
        + "\nexport_value = 42\nprint('export-output-' + 'y' * 80000)"
    )
    submit(page, "python", source)
    expect(page.locator("#transcript .entry.complete")).to_have_count(1, timeout=30_000)
    expect(page.locator("#python_variables")).to_contain_text(
        "export_value = 42", timeout=30_000
    )
    submit(page, "r", "plot(1:3)")
    expect(page.locator("#transcript .entry.complete")).to_have_count(2, timeout=30_000)
    expect(page.locator("#transcript img")).to_have_count(1)
    (recording,) = (workspace / ".agents/console/sessions").iterdir()
    qmd = copy_projection(page, "QMD")
    assert qmd == (recording / "sessions/session-001.qmd").read_text()
    assert source in qmd
    assert "export-output-" in qmd
    assert "omitted_lines" not in qmd
    markdown = copy_projection(page, "Markdown")
    assert markdown == (recording / "transcript.md").read_text()
    assert "export_value = 42" in markdown
    assert "export-output-" in markdown
    assert "omitted_lines" not in markdown
    assert "data:image" not in markdown
    assert "capture/sessions/001/" in markdown
    expect(page.locator("#agent_state")).to_have_text("Idle")


def test_qmd_export_follows_current_worker_and_reports_missing_source(app):
    page, workspace = app
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    submit(page, "python", "before_export_restart = 41")
    expect(page.locator("#transcript .entry.complete")).to_have_count(1, timeout=30_000)
    page.get_by_role("button", name="Restart", exact=True).click()
    expect(page.locator("#transcript .notice.restart").last).to_contain_text(
        "Session restarted", timeout=60_000
    )
    page.get_by_role("button", name="Export", exact=True).click()
    page.get_by_role("button", name="Copy QMD", exact=True).click()
    expect(page.locator("#export_status")).to_contain_text(
        "No QMD exists for the current worker"
    )
    submit(page, "sql", "select 42 as after_export_restart")
    expect(page.locator("#transcript .entry.complete")).to_have_count(2, timeout=30_000)
    qmd = copy_projection(page, "QMD")
    (recording,) = (workspace / ".agents/console/sessions").iterdir()
    assert qmd == (recording / "sessions/session-002.qmd").read_text()
    assert "after_export_restart" in qmd
    assert "before_export_restart" not in qmd
    markdown = copy_projection(page, "Markdown")
    assert "before_export_restart" in markdown
    assert "after_export_restart" in markdown


def test_export_reports_clipboard_permission_failure(app):
    page, _ = app
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.evaluate("navigator.clipboard.writeText('unchanged clipboard')")
    submit(page, "python", "export_denied = 42")
    expect(page.locator("#transcript .entry.complete")).to_have_count(1, timeout=30_000)
    page.context.clear_permissions()
    page.context.grant_permissions([])
    page.get_by_role("button", name="Export", exact=True).click()
    page.get_by_role("button", name="Copy Markdown", exact=True).click()
    expect(page.locator("#export_status")).to_contain_text("Copy failed:")
    page.context.grant_permissions(["clipboard-read"])
    assert page.evaluate("navigator.clipboard.readText()") == "unchanged clipboard"
