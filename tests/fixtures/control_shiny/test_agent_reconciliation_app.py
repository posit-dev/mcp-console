"""Reconcile real agent calls when independent output delivery is delayed."""

import json
import os

import pytest
from playwright.sync_api import expect
from shiny.playwright.controller import Chat

pytest_plugins = ["test_app"]


@pytest.fixture
def delayed_delivery(delivery_order):
    held = []
    routes = []
    responses = []
    released = False

    def intercept(route):
        routes.append(route)
        server = route.connect_to_server()

        def receive(message):
            custom = json.loads(message).get("custom", {})
            if "agent-response" in custom:
                responses.append(custom["agent-response"])
            snapshot = custom.get("output-snapshot")
            captured_agent = snapshot and any(
                item["origin"] == "mcp" for item in snapshot["evaluations"]
            )
            response = "agent-response" in custom or "agent-outcome" in custom
            delayed = (
                captured_agent or response
                if delivery_order == "reset_before_outcome"
                else captured_agent
                if delivery_order == "response_first"
                else response
            )
            if not released and delayed:
                held.append(message)
            else:
                route.send(message)

        server.on_message(receive)

    def release(*, discard_snapshots=False):
        nonlocal released
        assert len(routes) == 1
        assert held
        released = True
        for message in held:
            if not discard_snapshots or "output-snapshot" not in json.loads(
                message
            ).get("custom", {}):
                routes[0].send(message)
        held.clear()

    return intercept, release, responses


@pytest.fixture
def browser_setup(delayed_delivery):
    intercept, _, _ = delayed_delivery
    return lambda page: page.route_web_socket("**", intercept)


@pytest.mark.skipif(
    os.environ.get("MCP_CONSOLE_LIVE_AGENT") != "1",
    reason="Live agent inference is explicitly enabled separately",
)
@pytest.mark.parametrize("delivery_order", ["response_first", "snapshot_first"])
def test_agent_calls_reconcile_across_delivery_order(
    app, delayed_delivery, delivery_order
):
    page, _ = app
    chat = Chat(page, "agent_chat")
    chat.loc_input.fill(
        "Make exactly two console_fixture send calls in sequence, each with exactly "
        "the same Python code: "
        'reconcile_counter = globals().get("reconcile_counter", 0) + 1; '
        'print(f"agent-order-{reconcile_counter}"). '
        "Do not combine the calls, poll, or run other code. Reply done."
    )
    chat.send_user_input(method="click")
    expect(page.locator("#agent_state")).to_have_text("Completed", timeout=120_000)
    entries = page.locator("#transcript .entry.agent")
    expect(entries).to_have_count(2)
    states_before_release = entries.locator(".evaluation-state").all_text_contents()
    if delivery_order == "snapshot_first":
        expect(page.locator("#transcript .entry.agent.complete")).to_have_count(2)
        expect(entries.locator(".agent-response")).to_have_count(0)
    else:
        expect(entries.locator(".agent-response")).to_have_count(2)

    _, release, _ = delayed_delivery
    release()
    expect(page.locator("#transcript .entry.agent.complete")).to_have_count(2)
    expect(entries).to_have_count(2)
    if delivery_order == "response_first":
        assert states_before_release == ["Response received · awaiting capture"] * 2
    for index in range(2):
        entry = entries.nth(index)
        expected = f"agent-order-{index + 1}\n"
        expect(entry.locator(".content")).to_have_text(expected)
        entry.get_by_label("Responses to agent", exact=True).check()
        expect(entry.locator(".agent-response")).to_have_count(1)
        expect(entry.locator(".response-content")).to_have_text(expected)
    assert (
        entries.nth(0).locator(".source code").inner_text()
        == entries.nth(1).locator(".source code").inner_text()
    )


@pytest.mark.skipif(
    os.environ.get("MCP_CONSOLE_LIVE_AGENT") != "1",
    reason="Live agent inference is explicitly enabled separately",
)
@pytest.mark.parametrize("delivery_order", ["response_first", "reset_before_outcome"])
def test_pending_agent_response_is_retired_on_restart(
    app, delayed_delivery, delivery_order
):
    page, _ = app
    chat = Chat(page, "agent_chat")
    prompt = (
        "Make exactly one console_fixture send call with Python code "
        'print("agent-restart-order"). Do not run anything else. Reply done.'
    )
    chat.loc_input.fill(prompt)
    chat.send_user_input(method="click")
    expect(page.locator("#agent_state")).to_have_text("Completed", timeout=120_000)
    initial_state = (
        "running" if delivery_order == "reset_before_outcome" else "awaiting_capture"
    )
    expect(page.locator(f"#transcript .entry.agent.{initial_state}")).to_have_count(1)
    page.get_by_role("button", name="Restart", exact=True).click()
    expect(page.locator("#transcript .entry.agent.retired")).to_have_count(1)
    expect(page.locator("#connection_state")).to_have_text("Ready", timeout=60_000)
    # A replacement may coalesce the retired worker's final snapshot away.
    # Drop those held observations before allowing the new worker's delivery.
    _, release, responses = delayed_delivery
    release(discard_snapshots=True)
    expect(page.locator("#transcript .entry.agent.retired")).to_have_count(1)
    chat.loc_input.fill(prompt)
    chat.send_user_input(method="click")
    expect(page.locator("#transcript .entry.agent")).to_have_count(2, timeout=120_000)
    expect(page.locator("#agent_state")).to_have_text("Completed", timeout=120_000)
    expect(page.locator("#transcript .entry.agent.retired")).to_have_count(1)
    current = page.locator("#transcript .entry.agent.complete")
    expect(current).to_have_count(1)
    expect(current.locator(".content")).to_have_text("agent-restart-order\n")
    current.get_by_label("Responses to agent", exact=True).check()
    expect(current.locator(".agent-response")).to_have_count(1)
    assert len(responses) == 2
    expected = "".join(
        block["text"] for block in responses[-1]["content"] if block["type"] == "text"
    )
    # Restart notices remain MCP-owned and may accompany the next code result.
    # The renderer must preserve that actual response on the new worker's entry.
    expect(current.locator(".response-content")).to_have_text(expected)
    assert expected.count("agent-restart-order") == 1
