"""Native environment controls inspect and extend Console's accepted declaration."""

import re
import sys
from contextlib import closing
from pathlib import Path

from playwright.sync_api import expect
from shiny.playwright.controller import OutputDataFrame
from test_app import submit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.checkpoints import FifoCheckpoint

pytest_plugins = ["test_app"]


def open_environments(page):
    page.get_by_role("tab", name="Environments", exact=True).click(timeout=5_000)
    expect(page.locator("#environments-summary")).to_contain_text(
        "Prepared", timeout=30_000
    )
    return OutputDataFrame(page, "environments-accepted")


def test_environment_declaration_is_filterable_and_separate_from_draft(app):
    page, _ = app
    table = open_environments(page)
    expect(page.get_by_text("Accepted requirements", exact=True)).to_be_visible()
    expect(page.locator("#environments-help")).to_contain_text(
        "not an installed-package inventory"
    )
    expect(page.locator("#environments-help")).to_contain_text("pandas and duckdb")
    table.set_filter([{"col": 0, "value": "R"}, {"col": 1, "value": "reticulate"}])
    table.expect_nrow(1)
    table.expect_cell("Accepted · Console runtime", row=0, col=2)
    table.set_filter({"col": 0, "value": "Python"})
    expect(table.cell_locator(row=1, col=1)).to_be_visible()
    requirements = table.loc_body.locator("tr[data-index] > td:nth-child(2)")
    values = requirements.all_text_contents()
    assert len(values) > 1
    table.set_sort({"col": 1, "desc": True})
    expect(requirements).to_have_text(sorted(values, reverse=True))
    page.locator("#environments-draft").fill("new-draft-only")
    page.locator("#environments-refresh").click()
    expect(page.locator("#environments-draft")).to_have_value("new-draft-only")
    table.set_filter({"col": 1, "value": "new-draft-only"})
    table.expect_nrow(0)
    expect(page.locator("#transcript .entry")).to_have_count(0)


def test_environment_add_prepares_and_failed_draft_is_retained(app):
    page, _ = app
    table = open_environments(page)
    page.locator('#environments-language input[value="r"]').check()
    page.locator("#environments-draft").fill("cli")
    page.locator("#environments-add").click()
    expect(page.locator("#environments-operation")).to_contain_text(
        "Prepared", timeout=60_000
    )
    expect(page.locator("#environments-add")).to_be_enabled()
    table.set_filter({"col": 1, "value": "cli"})
    table.expect_nrow(1)
    table.expect_cell("Accepted", row=0, col=2)
    page.locator('#environments-language input[value="python"]').check()
    page.locator("#environments-draft").fill("not a valid requirement")
    page.locator("#environments-add").click()
    expect(page.locator("#environments-operation")).to_contain_text(
        "Failed", timeout=30_000
    )
    expect(page.locator("#environments-draft")).to_have_value("not a valid requirement")
    expect(page.locator("#environments-add")).to_be_enabled()
    table.set_filter({"col": 1, "value": "not a valid requirement"})
    table.expect_nrow(0)
    expect(page.locator("#transcript .entry")).to_have_count(0)


def test_environment_busy_preserves_draft_and_retry_does_not_add_transcript(app):
    page, workspace = app
    submit(
        page,
        "python",
        "import time\nfrom pathlib import Path\nprint('busy-environment', flush=True)\nwhile not Path('environment-release').exists(): time.sleep(0.02)",
    )
    expect(page.locator("#transcript .entry.running .content")).to_contain_text(
        "busy-environment", timeout=30_000
    )
    open_environments(page)
    page.locator('#environments-language input[value="r"]').check()
    page.locator("#environments-draft").fill("cli")
    page.locator("#environments-add").click()
    expect(page.locator("#environments-operation")).to_contain_text(
        "Busy", timeout=30_000
    )
    expect(page.locator("#environments-draft")).to_have_value("cli")
    expect(page.locator("#environments-add")).to_be_enabled()
    (workspace / "environment-release").touch()
    expect(page.locator("#transcript .entry.complete")).to_have_count(1, timeout=30_000)
    page.locator("#environments-add").click()
    expect(page.locator("#environments-operation")).to_contain_text(
        "Prepared", timeout=60_000
    )
    expect(page.locator("#transcript .entry")).to_have_count(1)


def test_pending_preparation_is_not_replayed_after_restart(app):
    page, workspace = app
    table = open_environments(page)
    page.locator('#environments-language input[value="r"]').check()
    page.locator("#environments-draft").fill("cli")
    with (
        closing(FifoCheckpoint.create(workspace / "environment-entered")) as entered,
        closing(FifoCheckpoint.create(workspace / "environment-release")) as release,
    ):
        page.get_by_label("Immediate", exact=True).check()
        submit(
            page,
            "python",
            "with open('environment-entered', 'wb', buffering=0) as gate:\n"
            "    gate.write(b'1')\n"
            "with open('environment-release', 'rb', buffering=0) as gate:\n"
            "    gate.read(1)",
        )
        try:
            entered.wait(timeout=30)
            expect(page.locator("#transcript .entry.running")).to_have_count(1)
            page.locator("#environments-add").click()
            expect(page.locator("#environments-operation")).to_have_text(
                "Preparing requirements…"
            )
            expect(
                page.locator("#transcript .notice.requirements").last
            ).to_contain_text("Human requested requirement preparation")
            page.get_by_role("button", name="Restart", exact=True).click()
            expect(page.locator("#transcript .notice.restart").last).to_contain_text(
                "Session restarted", timeout=60_000
            )
            expect(page.locator("#connection_state")).to_have_text(
                "Ready", timeout=60_000
            )
            expect(page.locator("#environments-operation")).to_contain_text(
                re.compile("Busy|Failed"), timeout=30_000
            )
            expect(page.locator("#environments-add")).to_be_enabled()
            expect(page.locator("#environments-draft")).to_have_value("cli")
            table.set_filter({"col": 1, "value": "cli"})
            table.expect_nrow(0)
            expect(page.locator("#transcript .entry")).to_have_count(1)
        finally:
            release.release()
