"""Human lifecycle controls operate on Console without an agent turn."""

from playwright.sync_api import expect
from test_app import submit

pytest_plugins = ["test_app"]


def test_human_interrupt_retains_session_and_reports_request(app):
    page, _ = app
    submit(
        page,
        "python",
        "import time\nkept_value = 41\nprint('interrupt-button-ready', flush=True)\nwhile True: time.sleep(0.02)",
    )
    expect(page.locator("#transcript .entry.running .content")).to_contain_text(
        "interrupt-button-ready", timeout=30_000
    )
    page.get_by_role("button", name="Interrupt", exact=True).click(timeout=5_000)
    expect(page.locator("#transcript .notice.interrupt").first).to_contain_text(
        "Human requested interruption"
    )
    expect(page.locator("#transcript .notice.interrupt").last).to_contain_text(
        "accepted; execution may still be running"
    )
    expect(page.locator("#transcript .entry.running")).to_have_count(0, timeout=30_000)
    submit(page, "python", "print(kept_value + 1)")
    expect(page.locator("#transcript .content").last).to_have_text(
        "42\n", timeout=30_000
    )
    expect(page.locator("#agent_state")).to_have_text("Idle")
    page.get_by_role("button", name="Interrupt", exact=True).click()
    expect(page.locator("#transcript .notice.interrupt").last).to_contain_text(
        "No ordinary evaluation is running"
    )
    expect(page.locator("#connection_state")).to_have_text("Ready")


def test_human_restart_replaces_running_session_and_table(app):
    page, _ = app
    submit(
        page,
        "python",
        "import pandas as pd\nold_table = pd.DataFrame({'value': range(10)})",
    )
    page.locator("#python_tables").get_by_role(
        "button", name="Open old_table", exact=True
    ).click(timeout=30_000)
    expect(page.locator("#table-page_data tbody tr")).to_have_count(10, timeout=30_000)
    submit(
        page,
        "python",
        "import time\nprint('restart-button-ready', flush=True)\nwhile True: time.sleep(0.02)",
    )
    expect(page.locator("#transcript .entry.running .content")).to_contain_text(
        "restart-button-ready", timeout=30_000
    )
    page.get_by_role("button", name="Restart", exact=True).click(timeout=5_000)
    expect(page.locator("#transcript .notice.restart").first).to_contain_text(
        "Human requested a session restart"
    )
    expect(page.locator("#transcript .notice.restart").last).to_contain_text(
        "Session restarted; previous in-memory state was cleared", timeout=60_000
    )
    expect(page.get_by_role("button", name="Restart", exact=True)).to_be_enabled()
    expect(page.locator("#table-status")).to_have_text(
        "Session replaced. Open a table again.", timeout=30_000
    )
    expect(page.locator("#table-page_data tbody tr")).to_have_count(0)
    expect(page.locator("#connection_state")).to_have_text("Ready", timeout=60_000)
    page.get_by_role("tab", name="Variables", exact=True).click()
    expect(page.locator("#python_variables")).not_to_contain_text("old_table")
    submit(page, "python", "after_restart = 43; print(after_restart)")
    expect(page.locator("#transcript .content").last).to_have_text(
        "43\n", timeout=30_000
    )
    expect(page.locator("#python_variables")).to_contain_text(
        "after_restart = 43", timeout=30_000
    )
    expect(page.locator("#agent_state")).to_have_text("Idle")
    expect(page.locator("#transcript .entry.running")).to_have_count(0)
