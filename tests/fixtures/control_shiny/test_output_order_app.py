"""Reconcile real bounded output snapshots delivered out of sequence."""

import json

import pytest
from playwright.sync_api import expect
from test_app import submit

pytest_plugins = ["test_app"]


@pytest.fixture
def delayed_output(tmp_path):
    held = []
    completed = []
    routes = []
    released = False

    def intercept(route):
        routes.append(route)
        server = route.connect_to_server()

        def receive(message):
            snapshot = json.loads(message).get("custom", {}).get("output-snapshot")
            early = snapshot and any(
                "EARLY-output-order" in event.get("text", "")
                for event in snapshot["events"]
            )
            if snapshot and any(
                item["state"] == "complete" for item in snapshot["evaluations"]
            ):
                completed.append(snapshot)
            if early and not released:
                held.append(message)
                # Proceed only after an actual early subscription observation.
                # The bounded completion history must then evict this event.
                (tmp_path / "output-order-release").touch()
            else:
                route.send(message)

        server.on_message(receive)

    def release():
        nonlocal released
        assert len(routes) == 1
        assert held
        released = True
        for message in held:
            routes[0].send(message)

    return intercept, release, held, completed


@pytest.fixture
def browser_setup(delayed_output):
    intercept, _, _, _ = delayed_output
    return lambda page: page.route_web_socket("**", intercept)


def test_delayed_snapshots_insert_evicted_output_before_completed_tail(
    app, delayed_output
):
    page, workspace = app
    _, release, held, completed = delayed_output
    submit(
        page,
        "python",
        "import time\nfrom pathlib import Path\n"
        'print("EARLY-output-order", flush=True)\n'
        'while not Path("output-order-release").exists(): time.sleep(0.02)\n'
        'print("x" * 100000, flush=True)\n'
        'print("LATE-output-order", flush=True)',
    )
    entries = page.locator("#transcript .entry.complete")
    entry = entries.first
    content = entry.locator(".content")
    try:
        expect(entries).to_have_count(1, timeout=30_000)
        expect(content).to_contain_text("LATE-output-order")
        expect(content).not_to_contain_text("EARLY-output-order")
        early_snapshot = json.loads(held[0])["custom"]["output-snapshot"]
        early = next(
            event
            for event in early_snapshot["events"]
            if "EARLY-output-order" in event.get("text", "")
        )
        tail = completed[-1]
        assert tail["omitted_events"] > 0
        assert early["sequence"] < tail["first_sequence"]
        assert all(event["sequence"] != early["sequence"] for event in tail["events"])
        assert any(
            "LATE-output-order" in event.get("text", "") for event in tail["events"]
        )

        release()
        expect(content).to_contain_text("EARLY-output-order")
        expect(entries).to_have_count(1)
        rendered = content.inner_text()
        assert rendered.index("EARLY-output-order") < rendered.index(
            "LATE-output-order"
        )
        assert rendered.count("EARLY-output-order") == 1
        assert rendered.count("LATE-output-order") == 1

        # Snapshots overlap and may repeat; replaying an observed snapshot must
        # neither duplicate output nor move it back behind the completed tail.
        release()
        # The next real response is also a delivery barrier for the replayed
        # frames, so an unchanged-text assertion cannot pass before they arrive.
        submit(page, "python", 'print("output-order-delivery-barrier")')
        expect(entries).to_have_count(2, timeout=30_000)
        expect(entries.last.locator(".content")).to_have_text(
            "output-order-delivery-barrier\n"
        )
        expect(content).to_have_text(rendered)
    finally:
        (workspace / "output-order-release").touch()
