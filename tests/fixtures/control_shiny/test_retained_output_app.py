"""Retained output is inspected through the browser without Console evaluation."""

import json

import pytest
from playwright.sync_api import expect
from test_app import submit
from test_environments_app import open_environments

pytest_plugins = ["test_app"]


@pytest.fixture(autouse=True)
def project_recordings(tmp_path):
    (tmp_path / ".agents/console").mkdir(parents=True)


def test_retained_text_pages_recover_spill_without_new_submission(app):
    page, _ = app
    source = 'print("retained-start\\n" + "é🙂" * 18000 + "\\nretained-end")'
    submit(page, "python", source)
    entry = page.locator("#transcript .entry.complete")
    expect(entry).to_have_count(1, timeout=30_000)
    entry.get_by_role("button", name="Open retained output", exact=True).click(
        timeout=5_000
    )
    modal = page.get_by_role("dialog")
    expect(modal).to_be_visible()
    contents = modal.locator("#retained_output_content")
    expect(contents).to_contain_text("retained-start")
    assert len(modal.locator("#retained_output_content").inner_text()) < 20000
    expect(modal.locator("#retained_output_status")).to_contain_text("Page 1 of")
    chunks = [contents.inner_text()]
    while modal.get_by_role("button", name="Next", exact=True).is_enabled():
        before = modal.locator("#retained_output_status").inner_text()
        modal.get_by_role("button", name="Next", exact=True).click()
        expect(modal.locator("#retained_output_status")).not_to_have_text(before)
        chunks.append(contents.inner_text())
    expect(modal.locator("#retained_output_content")).to_contain_text("retained-end")
    assert "".join(chunks) == "retained-start\n" + "é🙂" * 18000 + "\nretained-end\n"
    expect(page.locator("#transcript .entry")).to_have_count(1)
    expect(page.locator("#agent_state")).to_have_text("Idle")
    modal.get_by_role("button", name="Close", exact=True).click()
    expect(modal).not_to_be_visible()
    page.get_by_role("button", name="Restart", exact=True).click()
    expect(page.locator("#transcript .notice.restart").last).to_contain_text(
        "Session restarted", timeout=60_000
    )
    entry.get_by_role("button", name="Open retained output", exact=True).click()
    expect(page.locator("#retained_output_content")).to_contain_text("retained-start")
    expect(page.locator("#agent_state")).to_have_text("Idle")


def test_retained_pending_output_refreshes_without_polling_console(app):
    page, workspace = app
    source = (
        "import time\nfrom pathlib import Path\n"
        'print("retained-before", flush=True)\n'
        'while not Path("retained-release").exists(): time.sleep(0.02)\n'
        'print("retained-after", flush=True)\n'
    )
    submit(page, "python", source)
    entry = page.locator("#transcript .entry")
    expect(entry.locator(".content")).to_contain_text("retained-before", timeout=30_000)
    try:
        entry.get_by_role("button", name="Open retained output", exact=True).click()
        modal = page.get_by_role("dialog")
        expect(modal.locator("#retained_output_content")).to_contain_text(
            "retained-before"
        )
        expect(modal.locator("#retained_output_status")).to_contain_text(
            "may still grow"
        )
        (workspace / "retained-release").touch()
        expect(page.locator("#transcript .entry.complete")).to_have_count(
            1, timeout=30_000
        )
        modal.get_by_role("button", name="Refresh", exact=True).click()
        expect(modal.locator("#retained_output_content")).to_have_text(
            "retained-before\nretained-after\n"
        )
        expect(modal.locator("#retained_output_status")).not_to_contain_text(
            "may still grow"
        )
        expect(page.locator("#transcript .entry")).to_have_count(1)
        expect(page.locator("#agent_state")).to_have_text("Idle")
    finally:
        (workspace / "retained-release").touch()


def test_retained_immediate_result_is_private_and_labels_its_limit(app):
    page, _ = app
    submit(page, "r", "plot(1:3)")
    ordinary = page.locator("#transcript .entry.complete").first
    expect(ordinary.locator(".content img")).to_be_visible(timeout=30_000)
    ordinary.get_by_role("button", name="Open retained output", exact=True).click()
    modal = page.get_by_role("dialog")
    expect(modal.locator("#retained_output_content img")).to_be_visible()
    expect(modal.locator("#retained_output_status")).to_contain_text(
        "Recorded raw output"
    )
    modal.get_by_role("button", name="Close", exact=True).click()
    page.get_by_label("Immediate", exact=True).check()
    submit(page, "r", 'cat("private-retained-text\\n")')
    expect(page.locator("#transcript .entry.complete")).to_have_count(2, timeout=30_000)
    entry = page.locator("#transcript .entry.complete").last
    entry.get_by_role("button", name="Open retained output", exact=True).click()
    expect(modal.locator("#retained_output_status")).to_contain_text(
        "Recorded bounded immediate response"
    )
    expect(modal.locator("#retained_output_status")).to_contain_text(
        "no separate raw log"
    )
    expect(modal.locator("#retained_output_content")).to_contain_text(
        "private-retained-text"
    )
    expect(modal.locator("#retained_output_content img")).to_have_count(0)
    expect(page.locator("#agent_state")).to_have_text("Idle")
    expect(page.locator("#transcript .entry")).to_have_count(2)


def test_retained_immediate_images_include_those_omitted_from_response(app):
    page, _ = app
    open_environments(page)
    page.locator('#environments-language input[value="python"]').check()
    page.locator("#environments-draft").fill("matplotlib")
    page.locator("#environments-add").click()
    expect(page.locator("#environments-operation")).to_contain_text(
        "Prepared", timeout=60_000
    )
    page.get_by_label("Immediate", exact=True).check()
    submit(
        page,
        "python",
        """import matplotlib.pyplot as plt
import numpy as np
for i in range(3):
    fig = plt.figure(figsize=(10, 10), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.imshow(np.random.default_rng(i).integers(0, 256, (1000, 1000, 3), dtype=np.uint8))
    ax.set_axis_off()
    plt.show()
""",
    )
    entry = page.locator("#transcript .entry.complete")
    expect(entry).to_have_count(1, timeout=60_000)
    entry.get_by_role("button", name="Open retained output", exact=True).click()
    modal = page.get_by_role("dialog")
    expect(modal.locator("#retained_output_status")).to_contain_text("Page 1 of")
    images = set()
    additional = 0
    while True:
        if modal.locator("#retained_output_content img").count():
            images.add(
                modal.locator("#retained_output_content img").get_attribute("src")
            )
        if (
            "Additional retained image"
            in modal.locator("#retained_output_content").inner_text()
        ):
            additional += 1
        if not modal.get_by_role("button", name="Next", exact=True).is_enabled():
            break
        before = modal.locator("#retained_output_status").inner_text()
        modal.get_by_role("button", name="Next", exact=True).click()
        expect(modal.locator("#retained_output_status")).not_to_have_text(before)
    assert len(images) == 3
    assert additional > 0
    expect(page.locator("#transcript .entry")).to_have_count(1)
    expect(page.locator("#agent_state")).to_have_text("Idle")


def test_retained_viewer_rejects_calls_outside_visible_transcript(app):
    page, workspace = app
    (recording,) = (workspace / ".agents/console/sessions").iterdir()
    events = [
        json.loads(line)
        for line in (recording / "capture/events.jsonl").read_text().splitlines()
    ]
    private = next(
        event["call_id"]
        for event in events
        if event["event"] == "tool_call" and event.get("omit_from_projections")
    )
    page.evaluate(
        "call_id => Shiny.setInputValue('retained_output_request', {call_id}, {priority:'event'})",
        private,
    )
    modal = page.get_by_role("dialog")
    expect(modal.locator("#retained_output_status")).to_contain_text(
        "This call is not part of the visible transcript"
    )
    expect(modal.locator("#retained_output_content")).to_be_empty()
    expect(modal.get_by_role("button", name="Next", exact=True)).to_be_disabled()
    expect(page.locator("#transcript .entry")).to_have_count(0)
