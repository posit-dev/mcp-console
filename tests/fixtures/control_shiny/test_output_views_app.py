"""Output views distinguish captured streams from delivered agent responses."""

import base64
import json
import os

import pytest
from playwright.sync_api import expect
from shiny.playwright.controller import Chat
from test_app import submit

pytest_plugins = ["test_app"]


@pytest.fixture(autouse=True)
def project_recordings(tmp_path):
    (tmp_path / ".agents/console").mkdir(parents=True)


def test_long_human_output_can_expand_and_is_not_an_agent_response(app):
    page, _ = app
    submit(page, "python", "for i in range(120): print(f'output-row-{i:03d}')")
    entries = page.locator("#transcript .entry.complete")
    expect(entries).to_have_count(1, timeout=30_000)
    entry = entries.first
    expect(entry.get_by_label("Captured output", exact=True)).to_be_checked()
    expect(entry.get_by_role("button", name="Expand", exact=True)).to_be_visible()
    clip = entry.locator(".output-clip")
    assert clip.evaluate("node => node.clientHeight < node.scrollHeight")
    entry.get_by_role("button", name="Expand", exact=True).click()
    expect(entry.locator(".content")).to_contain_text("output-row-119")
    expect(entry.get_by_role("button", name="Collapse", exact=True)).to_be_visible()
    assert clip.evaluate("node => node.clientHeight >= node.scrollHeight")
    entry.get_by_label("Responses to agent", exact=True).check()
    expect(entry.locator(".agent-responses")).to_have_text("Not sent to agent.")
    entry.get_by_label("Captured output", exact=True).check()
    expect(entry.locator(".content")).to_contain_text("output-row-000")
    expect(entry.locator(".content")).to_contain_text("output-row-119")
    entry.get_by_role("button", name="Collapse", exact=True).click()
    assert clip.evaluate("node => node.clientHeight < node.scrollHeight")
    page.get_by_label("Immediate", exact=True).check()
    submit(page, "python", "print('human-immediate-private-output')")
    expect(entries).to_have_count(2, timeout=30_000)
    private = entries.nth(1)
    expect(private.locator(".content")).to_have_text("human-immediate-private-output\n")
    private.get_by_label("Responses to agent", exact=True).check()
    expect(private.locator(".agent-responses")).to_have_text("Not sent to agent.")
    expect(page.locator("#agent_state")).to_have_text("Idle")


def test_captured_output_keeps_diagnostics_omissions_and_traceback(app):
    page, _ = app
    submit(
        page,
        "r",
        'cat("regular-output\\n"); message("view-diagnostic"); warning("view-warning")',
    )
    entries = page.locator("#transcript .entry.complete")
    expect(entries).to_have_count(1, timeout=30_000)
    expect(entries.first.locator(".diagnostic")).to_contain_text(
        ["view-diagnostic", "view-warning"]
    )
    submit(
        page,
        "python",
        'print("large-view-output-" + "x" * 70000)\n'
        "def fail_output_view():\n"
        '    raise RuntimeError("view-traceback-marker")\n'
        "fail_output_view()",
    )
    expect(entries).to_have_count(2, timeout=30_000)
    failure = entries.nth(1)
    # History eviction is a shared snapshot fact, not evidence that a specific
    # cell lost text. Its aggregate notice must stay separate from cell output.
    expect(page.locator("#output-omissions")).to_contain_text("omitted")
    expect(failure.locator(".execution-error")).to_contain_text("view-traceback-marker")
    expect(failure.locator(".execution-error")).to_contain_text("fail_output_view")
    failure.get_by_label("Responses to agent", exact=True).check()
    expect(failure.locator(".agent-responses")).to_have_text("Not sent to agent.")


def test_pending_output_preserves_selected_view_and_scroll_position(app):
    page, workspace = app
    page.set_viewport_size({"width": 1440, "height": 1000})
    source = (
        "import time\nfrom pathlib import Path\n"
        'for phase in ("pending", "more", "follow", "hidden"):\n'
        '    for i in range(120): print(f"output-view-{phase}-{i}", flush=True)\n'
        '    while not Path(f"output-view-{phase}").exists(): time.sleep(0.02)\n'
        'print("output-view-done-marker")'
    )
    submit(page, "python", source)
    entry = page.locator("#transcript .entry")
    history = page.locator("#transcript-history")
    clip = entry.locator(".output-clip")
    try:
        expect(entry.locator(".content")).to_contain_text(
            "output-view-pending-119", timeout=30_000
        )
        page.wait_for_function(
            "() => { const c = document.querySelector('#transcript .output-clip'); return c.scrollHeight - c.scrollTop - c.clientHeight <= 8; }",
            timeout=5_000,
        )
        assert clip.evaluate("node => getComputedStyle(node).overflowY") == "auto"
        clip.evaluate("node => node.scrollTop = 0")
        (workspace / "output-view-pending").touch()
        expect(entry.locator(".content")).to_contain_text("output-view-more-119")
        assert clip.evaluate("node => node.scrollTop") == 0
        clip.evaluate("node => node.scrollTop = node.scrollHeight")
        (workspace / "output-view-more").touch()
        expect(entry.locator(".content")).to_contain_text("output-view-follow-119")
        page.wait_for_function(
            "() => { const c = document.querySelector('#transcript .output-clip'); return c.scrollHeight - c.scrollTop - c.clientHeight <= 8; }"
        )
        entry.get_by_role("button", name="Expand", exact=True).click()
        history.evaluate("node => node.scrollTop = 0")
        entry.get_by_label("Responses to agent", exact=True).check()
        expect(entry.locator(".agent-responses")).to_have_text("Not sent to agent.")
        assert history.evaluate("node => node.scrollTop") == 0
        (workspace / "output-view-follow").touch()
        expect(entry.locator(".content")).to_contain_text("output-view-hidden-119")
        expect(entry.get_by_label("Responses to agent", exact=True)).to_be_checked()
        assert history.evaluate("node => node.scrollTop") == 0
        entry.get_by_label("Captured output", exact=True).check()
        assert history.evaluate("node => node.scrollTop") == 0
        (workspace / "output-view-hidden").touch()
        expect(page.locator("#transcript .entry.complete")).to_have_count(
            1, timeout=30_000
        )
        expect(entry.locator(".content")).to_contain_text("output-view-done-marker")
        assert history.evaluate("node => node.scrollTop") == 0
        output = entry.locator(".content").text_content()
        for phase in ("pending", "more", "follow", "hidden"):
            assert output.count(f"output-view-{phase}-119") == 1
        assert output.count("output-view-done-marker") == 1
    finally:
        for phase in ("pending", "more", "follow", "hidden"):
            (workspace / f"output-view-{phase}").touch()


@pytest.mark.skipif(
    os.environ.get("MCP_CONSOLE_LIVE_AGENT") != "1",
    reason="Live agent inference is explicitly enabled separately",
)
def test_agent_response_preserves_actual_preview_and_poll_boundaries(app):
    page, workspace = app
    Chat(page, "agent_chat").loc_input.fill(
        "Make exactly two console_fixture send tool calls. First execute this Python code "
        "with timeout_ms=10000: "
        'print("\\n".join(f"agent-output-view-{i:03d}-" + "x" * 100 for i in range(300))). '
        "Then call send with only timeout_ms=1000 and no code to poll once. "
        "Do not execute any other code. Reply done."
    )
    Chat(page, "agent_chat").send_user_input(method="click")
    expect(page.locator("#agent_state")).to_have_text("Completed", timeout=120_000)
    entry = page.locator("#transcript .entry.agent.complete")
    expect(entry).to_have_count(1, timeout=30_000)
    expect(entry.locator(".content")).to_contain_text("agent-output-view-150-")
    entry.get_by_label("Responses to agent", exact=True).check()
    response = entry.locator(".agent-response")
    expect(response).to_have_count(1)
    expect(response.locator(".response-content")).to_contain_text("output omitted")
    expect(response.locator(".response-content")).not_to_contain_text(
        "agent-output-view-150-"
    )
    (recording,) = (workspace / ".agents/console/sessions").iterdir()
    journal = (recording / "capture/events.jsonl").read_text().splitlines()
    events = [json.loads(line) for line in journal if line.endswith("}")]
    calls = [
        event
        for event in events
        if event["event"] == "tool_call" and "origin" not in event
    ]
    assert len(calls) == 2
    responses = [
        event["result"]["content"]
        for event in events
        if event["event"] == "tool_result"
        and event["call_id"] in {call["call_id"] for call in calls}
    ]
    assert len(responses) == 2
    expected_text = [
        "".join(block["text"] for block in result if block["type"] == "text")
        for result in responses
    ]
    assert response.locator(".response-content").text_content() == expected_text[0]
    poll = page.locator("#transcript .response-entry")
    expect(poll).to_have_count(1)
    expect(poll).to_contain_text("Poll")
    assert poll.locator(".response-content").text_content() == expected_text[1]
    assert (
        response.locator(".response-content").text_content().count("output omitted")
        == 1
    )
    entry.get_by_label("Captured output", exact=True).check()
    expect(entry.locator(".content")).to_contain_text("agent-output-view-150-")
    Chat(page, "agent_chat").loc_input.fill(
        "Make exactly one console_fixture send tool call with R code plot(1:3). "
        "Do not execute any other code. Reply done."
    )
    Chat(page, "agent_chat").send_user_input(method="click")
    entries = page.locator("#transcript .entry.agent.complete")
    expect(entries).to_have_count(2, timeout=120_000)
    expect(page.locator("#agent_state")).to_have_text("Completed", timeout=120_000)
    plot = entries.nth(1)
    expect(plot.locator(".language")).to_have_text("R")
    expect(plot.locator(".content img")).to_have_count(1)
    plot.get_by_label("Responses to agent", exact=True).check()
    expect(plot.locator(".agent-response")).to_have_count(1)
    image = plot.locator(".response-content img")
    expect(image).to_have_count(1)
    journal = (recording / "capture/events.jsonl").read_text().splitlines()
    events = [json.loads(line) for line in journal if line.endswith("}")]
    calls = [
        event
        for event in events
        if event["event"] == "tool_call" and "origin" not in event
    ]
    assert len(calls) == 3
    (result,) = [
        event["result"]
        for event in events
        if event["event"] == "tool_result" and event["call_id"] == calls[-1]["call_id"]
    ]
    (artifact,) = [block for block in result["content"] if block["type"] == "image"]
    mime, encoded = image.get_attribute("src").split(",", 1)
    assert mime == f"data:{artifact['mimeType']};base64"
    assert base64.b64decode(encoded) == (recording / artifact["path"]).read_bytes()
    assert image.get_attribute("src") == plot.locator(".content img").get_attribute(
        "src"
    )
    expect(page.locator("#transcript .response-entry")).to_have_count(1)
