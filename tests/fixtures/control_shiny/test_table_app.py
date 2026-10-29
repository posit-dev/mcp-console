"""Native Shiny table controls query complete snapshots and display bounded pages."""

import os

import pytest
from playwright.sync_api import expect
from shiny.playwright.controller import Chat
from test_app import submit

pytest_plugins = ["test_app"]


@pytest.mark.parametrize("language", ["r", "python"])
def test_native_table_pages_filters_sort_and_refresh(app, language):
    page, _ = app
    source = (
        'explore <- data.frame(value=1:1000, category=rep(c("odd", "even"),500))'
        if language == "r"
        else "import pandas as pd\nexplore = pd.DataFrame({'value':range(1,1001), 'category':['odd','even']*500})"
    )
    submit(page, language, source)
    page.locator(f"#{language}_tables").get_by_role(
        "button", name="Open explore", exact=True
    ).click(timeout=60_000)
    expect(page.locator("#table-status")).to_contain_text("1,000 rows", timeout=60_000)
    rows = page.locator("#table-page_data tbody tr")
    expect(rows).to_have_count(100, timeout=30_000)
    expect(rows.first.locator("td").first).to_have_text("1")
    page.locator("#table-page_number").fill("10")
    page.locator("#table-go").click()
    expect(rows.first.locator("td").first).to_have_text("901", timeout=30_000)
    page.get_by_role("button", name="Sort and filter", exact=True).click()
    page.locator("#table-filter_column").select_option("c1")
    page.get_by_label("Condition · category", exact=True).select_option("equals")
    page.get_by_label("Value · category", exact=True).fill("even")
    page.locator("#table-apply_filter").click()
    page.locator("#table-sort_column").select_option("c0")
    page.locator("#table-sort_direction").select_option("desc")
    page.locator("#table-apply_sort").click()
    expect(rows.first.locator("td").first).to_have_text("1000", timeout=30_000)
    expect(rows.nth(1).locator("td").first).to_have_text("998")
    expect(page.locator("#table-page_status")).to_contain_text("500 matching rows")
    expect(page.locator("#transcript .entry")).to_have_count(1)
    mutation = (
        "explore$value <- explore$value + 1000"
        if language == "r"
        else "explore['value'] += 1000"
    )
    submit(page, language, mutation)
    expect(page.locator("#transcript .entry.complete")).to_have_count(2, timeout=30_000)
    expect(rows.first.locator("td").first).to_have_text("1000")
    page.locator("#table-refresh").click()
    expect(rows.first.locator("td").first).to_have_text(
        "1001.0" if language == "r" else "1001", timeout=30_000
    )
    page.locator("#table-close").click()
    expect(rows).to_have_count(0)
    expect(page.locator("#transcript .entry")).to_have_count(2)


def test_native_table_exact_wide_numbers_and_full_text_filter(app):
    page, _ = app
    submit(
        page,
        "python",
        "import pandas as pd\nexplore = pd.DataFrame({'wide':pd.array([2**60,2**60+1,9],dtype='Int64'), 'text':['x'*2000+'needle','plain','plain']})",
    )
    page.locator("#python_tables").get_by_role(
        "button", name="Open explore", exact=True
    ).click(timeout=60_000)
    rows = page.locator("#table-page_data tbody tr")
    expect(rows).to_have_count(3, timeout=30_000)
    page.get_by_role("button", name="Sort and filter", exact=True).click()
    page.locator("#table-sort_column").select_option("c0")
    page.locator("#table-apply_sort").click()
    expect(rows.first.locator("td").first).to_have_text("9", timeout=30_000)
    page.locator("#table-filter_column").select_option("c1")
    page.get_by_label("Condition · text", exact=True).select_option("contains")
    page.get_by_label("Value · text", exact=True).fill("needle")
    page.locator("#table-apply_filter").click()
    expect(rows).to_have_count(1, timeout=30_000)
    expect(rows.first.locator("td").first).to_have_text(str(2**60))
    expect(page.locator("#table-truncation")).to_contain_text("1,024")
    # Editing another column adds a debounced filter; switching back retains the
    # first filter, which still searches the full, unshortened snapshot value.
    page.locator("#table-filter_column").select_option("c0")
    expect(page.get_by_label("Value · wide", exact=True)).to_have_value("")
    page.get_by_label("Condition · wide", exact=True).select_option("equals")
    page.get_by_label("Value · wide", exact=True).fill(str(2**60 + 1))
    expect(rows).to_have_count(0, timeout=30_000)
    page.locator("#table-filter_column").select_option("c1")
    expect(page.get_by_label("Value · text", exact=True)).to_have_value("needle")
    expect(rows).to_have_count(0)
    page.locator("#table-clear_filters").click()
    expect(rows).to_have_count(3, timeout=30_000)
    expect(page.locator("#transcript .entry")).to_have_count(1)


def test_table_waits_for_busy_worker_without_transcript_queries(app):
    page, workspace = app
    submit(page, "r", "waiting_table <- data.frame(value=1:10)")
    expect(
        page.locator("#r_tables").get_by_role(
            "button", name="Open waiting_table", exact=True
        )
    ).to_be_visible(timeout=60_000)
    submit(
        page,
        "python",
        "import time\nfrom pathlib import Path\nprint('busy-table', flush=True)\nwhile not Path('table-release').exists(): time.sleep(0.02)",
    )
    expect(page.locator("#transcript .entry.running .content")).to_contain_text(
        "busy-table", timeout=30_000
    )
    page.locator("#r_tables").get_by_role(
        "button", name="Open waiting_table", exact=True
    ).click()
    expect(page.locator("#table-status")).to_contain_text(
        "Waiting for the worker", timeout=30_000
    )
    expect(page.locator("#transcript .entry")).to_have_count(2)
    (workspace / "table-release").touch()
    expect(
        page.locator("#table-page_data tbody tr:first-child td:first-child")
    ).to_have_text("1", timeout=30_000)
    expect(page.locator("#transcript .entry")).to_have_count(2)


@pytest.mark.skipif(
    os.environ.get("MCP_CONSOLE_LIVE_AGENT") != "1",
    reason="Live Luna inference is explicitly enabled separately",
)
def test_agent_session_replacement_clears_table(app):
    page, _ = app
    submit(
        page,
        "python",
        "import pandas as pd\nold_table = pd.DataFrame({'value': range(10)})",
    )
    page.locator("#python_tables").get_by_role(
        "button", name="Open old_table", exact=True
    ).click(timeout=30_000)
    expect(
        page.locator("#table-page_data tbody tr:first-child td:first-child")
    ).to_have_text("0", timeout=30_000)
    Chat(page, "agent_chat").loc_input.fill(
        "Use console_fixture send with control='restart' and no other arguments. Do not run code. Reply done."
    )
    Chat(page, "agent_chat").send_user_input(method="click")
    expect(page.locator("#agent_state")).to_have_text("Completed", timeout=120_000)
    expect(page.locator("#table-status")).to_have_text(
        "Session replaced. Open a table again.", timeout=30_000
    )
    expect(page.locator("#table-page_data tbody tr")).to_have_count(0)
    expect(page.locator("#connection_state")).to_have_text("Ready", timeout=30_000)
    page.get_by_role("tab", name="Variables", exact=True).click()
    expect(page.locator("#python_variables")).not_to_contain_text(
        "old_table", timeout=30_000
    )
    submit(page, "sql", "select 42 as after_restart")
    expect(page.locator("#transcript .entry.complete .content").last).to_contain_text(
        "42", timeout=30_000
    )


def test_failed_open_keeps_previous_table_identity(app):
    page, _ = app
    submit(
        page,
        "python",
        "import pandas as pd\nvalid = pd.DataFrame({'value': range(1,201)})\nunsupported = pd.DataFrame({'items': [[1,2]]})",
    )
    page.locator("#python_tables").get_by_role(
        "button", name="Open valid", exact=True
    ).click(timeout=60_000)
    rows = page.locator("#table-page_data tbody tr")
    expect(rows.first.locator("td").first).to_have_text("1", timeout=30_000)
    page.get_by_role("tab", name="Variables", exact=True).click()
    page.locator("#python_tables").get_by_role(
        "button", name="Open unsupported", exact=True
    ).click()
    expect(page.locator("#table-status")).to_contain_text(
        "Unsupported object column", timeout=30_000
    )
    page.locator("#table-next").click()
    expect(rows.first.locator("td").first).to_have_text("101", timeout=30_000)
    expect(page.locator("#table-status")).to_have_text("PYTHON · valid · 200 rows")
    page.locator("#table-refresh").click()
    expect(rows.first.locator("td").first).to_have_text("1", timeout=30_000)
