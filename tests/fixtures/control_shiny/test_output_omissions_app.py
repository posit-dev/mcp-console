"""Omission notices follow their shared or private output history."""

import json

import pytest
from playwright.sync_api import expect
from test_app import submit

pytest_plugins = ["test_app"]


@pytest.fixture
def captured_snapshots():
    frames = []
    routes = []

    def intercept(route):
        routes.append(route)
        server = route.connect_to_server()

        def receive(message):
            snapshot = json.loads(message).get("custom", {}).get("output-snapshot")
            if snapshot is not None:
                frames.append((message, snapshot))
            route.send(message)

        server.on_message(receive)

    def replay(message):
        assert len(routes) == 1
        routes[0].send(message)

    return intercept, frames, replay


@pytest.fixture
def browser_setup(captured_snapshots):
    intercept, _, _ = captured_snapshots
    return lambda page: page.route_web_socket("**", intercept)


def test_shared_and_immediate_omissions_keep_their_own_history(app):
    page, _ = app
    entries = page.locator("#transcript .entry.complete")
    shared = page.locator("#output-omissions")
    submit(page, "python", 'print("x" * 70000)')
    expect(entries).to_have_count(1, timeout=30_000)
    expect(shared).to_contain_text("omitted")
    shared_warning = shared.inner_text()

    page.get_by_label("Immediate", exact=True).check()
    submit(page, "python", 'print("small-private-output")')
    expect(entries).to_have_count(2, timeout=30_000)
    expect(entries.nth(1).locator(".content")).to_have_text("small-private-output\n")
    expect(shared).to_have_text(shared_warning)

    submit(page, "python", 'print("y" * 70000)')
    expect(entries).to_have_count(3, timeout=30_000)
    private_warning = entries.nth(2).locator(".history-omissions")
    expect(private_warning).to_contain_text("omitted from this Immediate capture")
    expect(shared).to_have_text(shared_warning)
    private_text = private_warning.inner_text()
    entries.nth(2).get_by_label("Responses to agent", exact=True).check()
    expect(private_warning).to_be_hidden()
    entries.nth(2).get_by_label("Captured output", exact=True).check()
    expect(private_warning).to_be_visible()

    submit(page, "python", 'print("another-private-output")')
    expect(entries).to_have_count(4, timeout=30_000)
    expect(shared).to_have_text(shared_warning)
    expect(private_warning).to_have_text(private_text)

    page.locator("#restart").click()
    expect(page.locator("#transcript .notice")).to_contain_text(
        [
            "Human requested a session restart.",
            "Session restarted; previous in-memory state was cleared.",
        ],
        timeout=30_000,
    )
    expect(page.locator("#connection_state")).to_have_text("Ready", timeout=30_000)
    expect(shared).to_have_text("")
    expect(private_warning).to_have_text(private_text)


def test_delayed_shared_snapshot_cannot_erase_observed_omissions(
    app, captured_snapshots
):
    page, _ = app
    _, frames, replay = captured_snapshots
    entries = page.locator("#transcript .entry.complete")
    shared = page.locator("#output-omissions")
    submit(page, "python", 'print("before-shared-spill")')
    expect(entries).to_have_count(1, timeout=30_000)
    before, snapshot = next(
        (message, snapshot)
        for message, snapshot in frames
        if any(
            "before-shared-spill" in event.get("text", "")
            for event in snapshot["events"]
        )
    )
    assert snapshot["omitted_events"] == 0
    assert snapshot["omitted_bytes"] == 0

    submit(page, "python", 'print("z" * 70000)')
    expect(entries).to_have_count(2, timeout=30_000)
    expect(shared).to_contain_text("omitted")
    warning = shared.inner_text()
    replay(before)
    # A new private result establishes delivery after the replayed frame and
    # does not publish a newer shared snapshot that could hide a regression.
    page.get_by_label("Immediate", exact=True).check()
    submit(page, "python", 'print("omission-delivery-barrier")')
    expect(entries).to_have_count(3, timeout=30_000)
    expect(entries.last.locator(".content")).to_have_text("omission-delivery-barrier\n")
    expect(shared).to_have_text(warning)
