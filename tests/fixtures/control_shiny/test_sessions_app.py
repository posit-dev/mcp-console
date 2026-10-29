"""The native session selector routes human and agent work to isolated workers."""

import os

import pytest
from playwright.sync_api import expect
from shiny.playwright.controller import Chat, OutputDataFrame
from test_app import submit
from test_export_app import copy_projection

pytest_plugins = ["test_app"]


@pytest.fixture(autouse=True)
def project_recordings(tmp_path):
    (tmp_path / ".agents/console").mkdir(parents=True)


def select_session(page, name):
    page.locator("#console_session").select_option(name)
    # A newly selected session has an empty, zero-height transcript. Its active
    # DOM state is observable before the first submitted cell gives it a box.
    expect(
        page.locator(
            f'#transcript .session-transcript[data-session="{name}"]:not([hidden])'
        )
    ).to_have_count(1, timeout=60_000)
    expect(page.locator("#connection_state")).to_have_text("Ready", timeout=60_000)


def create_session(page, name):
    page.locator("#new_console_session").click()
    page.locator("#new_session_name").fill(name)
    page.locator("#create_console_session").click()
    expect(page.locator("#console_session")).to_have_value(name, timeout=60_000)
    select_session(page, name)


def complete(page, language, source):
    count = page.locator("#transcript .entry:visible").count()
    submit(page, language, source)
    entries = page.locator("#transcript .entry.complete:visible")
    expect(entries).to_have_count(count + 1, timeout=30_000)
    return entries.last


def inspector(page, name, kind):
    pane = page.locator(
        f'.session-inspector[data-session="{name}"][data-inspector="{kind}"]:visible'
    )
    expect(pane).to_be_visible(timeout=30_000)
    return pane


def test_session_selector_creates_and_switches_isolated_workers(app):
    page, _ = app
    expect(page.locator("#console_session")).to_have_value("default")
    expect(page.locator("#close_console_session")).to_be_disabled()
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    complete(
        page,
        "python",
        'import pandas as pd\nsession_value = "default-python"\n'
        'session_table = pd.DataFrame({"value": range(1, 102)})\nprint(session_value)',
    )
    complete(page, "r", 'session_value <- "default-r"; plot(1:3)')
    complete(
        page, "sql", "create table session_values as select 'default-sql' as value"
    )
    page.get_by_role("tab", name="Plots", exact=True).click()
    default_plots = page.locator(
        '#plots .session-plots[data-session="default"]:visible img'
    )
    expect(default_plots).to_have_count(1)
    default_plot = default_plots.get_attribute("src")
    page.get_by_role("tab", name="Variables", exact=True).click()
    page.locator("#python_tables").get_by_role(
        "button", name="Open session_table", exact=True
    ).click(timeout=30_000)
    expect(
        inspector(page, "default", "table").locator(
            '[id$="-page_data"] tbody tr:first-child td:first-child'
        )
    ).to_have_text("1", timeout=30_000)
    page.locator("#transcript .entry:visible").first.get_by_label(
        "Responses to agent", exact=True
    ).check()
    create_session(page, "analysis")
    expect(page.locator("#close_console_session")).to_be_enabled()
    expect(page.locator("#transcript .entry:visible")).to_have_count(0)
    page.get_by_role("tab", name="Table", exact=True).click()
    expect(
        inspector(page, "analysis", "table").locator('[id$="-page_data"] tbody tr')
    ).to_have_count(0)
    page.get_by_role("tab", name="Plots", exact=True).click()
    expect(page.locator("#plots img:visible")).to_have_count(0)
    complete(
        page,
        "python",
        'import pandas as pd\nsession_value = "analysis-python"\n'
        'session_table = pd.DataFrame({"value": range(1001, 1102)})\nprint(session_value)',
    )
    complete(page, "r", 'session_value <- "analysis-r"; plot(3:1)')
    sql = complete(
        page, "sql", "create table session_values as select 'analysis-sql' as value"
    )
    expect(sql.locator(".execution-error")).to_have_count(0)
    expect(page.locator("#plots img:visible")).to_have_count(1)
    assert page.locator("#plots img:visible").get_attribute("src") != default_plot
    page.get_by_role("tab", name="Variables", exact=True).click()
    expect(page.locator("#python_variables")).to_contain_text(
        "analysis-python", timeout=30_000
    )
    expect(page.locator("#r_variables")).to_contain_text("analysis-r", timeout=30_000)
    page.locator("#python_tables").get_by_role(
        "button", name="Open session_table", exact=True
    ).click()
    expect(
        inspector(page, "analysis", "table").locator(
            '[id$="-page_data"] tbody tr:first-child td:first-child'
        )
    ).to_have_text("1001", timeout=30_000)
    qmd = copy_projection(page, "QMD")
    assert "analysis-python" in qmd
    assert "default-python" not in qmd
    select_session(page, "default")
    expect(page.locator("#transcript .entry:visible")).to_have_count(3)
    expect(
        page.locator("#transcript .entry:visible").first.get_by_label(
            "Responses to agent", exact=True
        )
    ).to_be_checked()
    page.get_by_role("tab", name="Table", exact=True).click()
    expect(
        inspector(page, "default", "table").locator('[id$="-page_data"] tbody tr')
    ).to_have_count(0)
    qmd = copy_projection(page, "QMD")
    assert "default-python" in qmd
    assert "analysis-python" not in qmd
    expect(
        complete(page, "python", "print(session_value)").locator(".content")
    ).to_have_text("default-python\n")
    expect(complete(page, "r", "cat(session_value)").locator(".content")).to_have_text(
        "default-r"
    )
    expect(
        complete(page, "sql", "select value from session_values").locator(".content")
    ).to_contain_text("default-sql")
    page.get_by_role("tab", name="Plots", exact=True).click()
    expect(default_plots).to_have_count(1)
    assert default_plots.get_attribute("src") == default_plot
    page.get_by_role("tab", name="Variables", exact=True).click()
    page.locator("#python_tables").get_by_role(
        "button", name="Open session_table", exact=True
    ).click()
    expect(
        inspector(page, "default", "table").locator(
            '[id$="-page_data"] tbody tr:first-child td:first-child'
        )
    ).to_have_text("1", timeout=30_000)
    markdown = copy_projection(page, "Markdown")
    assert "default-python" in markdown
    assert "analysis-python" in markdown


def test_pending_output_and_requirements_survive_switch_close_and_recreate(app):
    page, workspace = app
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    submit(
        page,
        "python",
        'import time\nfrom pathlib import Path\nprint("default-pending", flush=True)\n'
        'while not Path("session-release").exists(): time.sleep(0.02)\n'
        'print("default-finished")',
    )
    expect(page.locator("#transcript .entry.running:visible .content")).to_contain_text(
        "default-pending", timeout=30_000
    )
    try:
        create_session(page, "scratch")
        expect(
            complete(
                page, "python", 'scratch_value = 123; print("scratch-output")'
            ).locator(".content")
        ).to_have_text("scratch-output\n")
        expect(page.locator("#transcript .content:visible")).not_to_contain_text(
            "default-pending"
        )
        page.get_by_role("tab", name="Environments", exact=True).click()
        environment = inspector(page, "scratch", "environments")
        environment.locator('[id$="-language"] input[value="r"]').check()
        environment.locator('[id$="-draft"]').fill("cli")
        environment.locator('[id$="-add"]').click()
        expect(environment.locator('[id$="-operation"]')).to_contain_text(
            "Prepared", timeout=60_000
        )
        accepted = OutputDataFrame(
            page, environment.locator('[id$="-accepted"]').get_attribute("id")
        )
        accepted.set_filter({"col": 1, "value": "cli"})
        accepted.expect_nrow(1)
        submit(
            page,
            "python",
            'import time\nfrom pathlib import Path\nprint("scratch-pending", flush=True)\n'
            'while not Path("scratch-release").exists(): time.sleep(0.02)',
        )
        expect(
            page.locator("#transcript .entry.running:visible .content")
        ).to_have_text("scratch-pending\n", timeout=30_000)
        (workspace / "session-release").touch()
        page.locator("#close_console_session").click()
        expect(page.locator("#console_session")).to_have_value(
            "default", timeout=60_000
        )
        select_session(page, "default")
        expect(page.locator('#console_session option[value="scratch"]')).to_have_count(
            0
        )
        expect(page.locator("#transcript .entry.complete:visible")).to_have_count(
            1, timeout=30_000
        )
        output = page.locator("#transcript .entry:visible .content").text_content()
        assert output.count("default-pending") == 1
        assert output.count("default-finished") == 1
        expect(page.locator("#close_console_session")).to_be_disabled()
        page.get_by_role("tab", name="Environments", exact=True).click()
        default_environment = inspector(page, "default", "environments")
        accepted = OutputDataFrame(
            page, default_environment.locator('[id$="-accepted"]').get_attribute("id")
        )
        accepted.set_filter({"col": 1, "value": "cli"})
        accepted.expect_nrow(0)
        create_session(page, "scratch")
        entry = complete(page, "python", 'print("scratch_value" in globals())')
        expect(entry.locator(".content")).to_have_text("False\n")
        qmd = copy_projection(page, "QMD")
        assert "scratch_value = 123" not in qmd
        assert 'print("scratch_value" in globals())' in qmd
        markdown = copy_projection(page, "Markdown")
        assert "scratch_value = 123" in markdown
        assert "default-finished" in markdown
    finally:
        (workspace / "session-release").touch()
        (workspace / "scratch-release").touch()


@pytest.mark.skipif(
    os.environ.get("MCP_CONSOLE_LIVE_AGENT") != "1",
    reason="Live agent inference is explicitly enabled separately",
)
def test_agent_created_session_is_discovered_and_selected_context_is_used(app):
    page, _ = app
    chat = Chat(page, "agent_chat")
    chat.loc_input.fill(
        "Make exactly one console_fixture send call with session='from_agent' and "
        "Python code agent_session_value = 64; print(agent_session_value). "
        "Do not use the default session. Reply done."
    )
    chat.send_user_input(method="click")
    expect(page.locator("#agent_state")).to_have_text("Completed", timeout=120_000)
    expect(page.locator('#console_session option[value="from_agent"]')).to_have_count(
        1, timeout=30_000
    )
    select_session(page, "from_agent")
    expect(page.locator("#transcript .entry.agent.complete:visible")).to_have_count(
        1, timeout=30_000
    )
    expect(page.locator("#transcript .entry.agent:visible .content")).to_contain_text(
        "64"
    )
    chat.loc_input.fill(
        "In the currently selected Console session, increment agent_session_value "
        "by one and print it. Make exactly one send call, then reply done."
    )
    chat.send_user_input(method="click")
    expect(page.locator("#transcript .entry.agent.complete:visible")).to_have_count(
        2, timeout=120_000
    )
    expect(page.locator("#agent_state")).to_have_text("Completed", timeout=120_000)
    expect(
        page.locator("#transcript .entry.agent.complete:visible .content").last
    ).to_contain_text("65")
    expect(
        complete(page, "python", "print(agent_session_value)").locator(".content")
    ).to_have_text("65\n")
    select_session(page, "default")
    expect(
        complete(page, "python", 'print("agent_session_value" in globals())').locator(
            ".content"
        )
    ).to_have_text("False\n")
