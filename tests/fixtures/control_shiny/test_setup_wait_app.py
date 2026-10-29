"""Session setup survives admission deadlines and cancels on retirement."""

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from playwright.sync_api import expect
from test_app import submit
from test_sessions_app import create_session

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from boundaries.client_server.integrations.test_control import ControlClient
from support.checkpoints import wait_for_checkpoint, wait_for_path

pytest_plugins = ["test_app"]


@pytest.fixture(autouse=True)
def capture_control_endpoint(tmp_path, monkeypatch):
    binary = Path(os.environ["MCP_CONSOLE_TEST_BINARY"]).resolve()
    launcher = tmp_path / "console-launcher"
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\nfrom pathlib import Path\n"
        'endpoint = sys.argv[sys.argv.index("--control-socket") + 1]\n'
        'Path("fixture-control-endpoint").write_text(endpoint)\n'
        f"os.execv({str(binary)!r}, [{str(binary)!r}, *sys.argv[1:]])\n"
    )
    launcher.chmod(0o755)
    monkeypatch.setenv("MCP_CONSOLE_TEST_BINARY", str(launcher))
    (tmp_path / ".agents/console").mkdir(parents=True)


def journal_events(path: Path) -> list[dict]:
    # Observe complete public recording events, excluding a concurrent write.
    return [
        json.loads(line)
        for line in path.read_bytes().splitlines(keepends=True)
        if line.endswith(b"\n")
    ]


def wait_for_event(
    path: Path, predicate: Callable[[dict], bool], *, timeout: float = 15
) -> dict:
    found = None

    def discover() -> Path | None:
        nonlocal found
        found = next(
            (event for event in journal_events(path) if predicate(event)), None
        )
        return path if found is not None else None

    wait_for_checkpoint(
        discover, "expected Console recording event", root=path.parent, timeout=timeout
    )
    assert found is not None
    return found


@pytest.fixture
def waiting_session(app):
    page, workspace = app
    endpoint = Path((workspace / "fixture-control-endpoint").read_text())
    journal = next(workspace.glob(".agents/console/sessions/*/capture/events.jsonl"))
    baseline = max(event.get("call_id") or 0 for event in journal_events(journal))
    with ControlClient(endpoint) as control:
        pending = control.start(
            op="evaluate",
            session="already_running",
            language="python",
            busy="wait",
            wait_ms=60000,
            code=(
                "from pathlib import Path\nimport time\nsetup_wait_value = 41\n"
                'Path("setup-wait-started").touch()\n'
                'while not Path("setup-wait-release").exists(): time.sleep(0.02)\n'
                'print("existing evaluation finished")'
            ),
        )
        try:
            wait_for_path(
                workspace / "setup-wait-started", "named evaluation started", timeout=30
            )
            expect(
                page.locator('#console_session option[value="already_running"]')
            ).to_have_count(1, timeout=30_000)
            page.locator("#console_session").select_option("already_running")
            expect(page.locator("#connection_state")).to_have_text(
                "Preparing managed environments…"
            )
            setup = wait_for_event(
                journal,
                lambda event: (
                    event["event"] == "tool_call"
                    and event["call_id"] > baseline
                    and event["request"]["arguments"]
                    .get("r", "")
                    .startswith("reticulate::py_require(")
                ),
            )
            call_id = setup["call_id"]
            wait_for_event(
                journal,
                lambda event: (
                    event.get("direction") == "integrator_waiting"
                    and event["message"]["call_id"] == call_id
                ),
            )
            yield page, workspace, control, pending, journal, call_id
        finally:
            (workspace / "setup-wait-release").touch()


def test_setup_renews_admission_wait_after_existing_execution_exceeds_deadline(
    waiting_session,
):
    page, workspace, control, pending, journal, call_id = waiting_session
    expired = wait_for_event(
        journal,
        lambda event: event["event"] == "tool_result" and event["call_id"] == call_id,
        timeout=75,
    )
    assert (
        expired["error"]["message"]
        == "runner did not become idle before the deadline; code was not run"
    )
    retry = wait_for_event(
        journal,
        lambda event: (
            event["event"] == "tool_call"
            and event["call_id"] > call_id
            and event["request"]["arguments"]
            .get("r", "")
            .startswith("reticulate::py_require(")
        ),
    )
    wait_for_event(
        journal,
        lambda event: (
            event.get("direction") == "integrator_waiting"
            and event["message"]["call_id"] == retry["call_id"]
        ),
    )
    expect(page.locator("#connection_state")).to_have_text(
        "Preparing managed environments…"
    )
    (workspace / "setup-wait-release").touch()
    assert "result" in control.receive(pending)
    expect(page.locator("#connection_state")).to_have_text("Ready", timeout=30_000)
    submit(page, "python", "print(setup_wait_value + 1)")
    expect(page.locator("#transcript .entry:visible .content").last).to_have_text(
        "42\n", timeout=30_000
    )


def test_close_cancels_setup_wait_before_reopening_named_session(waiting_session):
    page, _, control, pending, journal, call_id = waiting_session
    page.locator("#close_console_session").click()
    expect(
        page.locator('#console_session option[value="already_running"]')
    ).to_have_count(0, timeout=30_000)
    assert control.receive(pending)["result"]["isError"] is True
    cancelled = wait_for_event(
        journal,
        lambda event: event["event"] == "tool_result" and event["call_id"] == call_id,
    )
    assert "error" in cancelled
    assert not any(
        event["event"] == "source_admitted" and event["call_id"] == call_id
        for event in journal_events(journal)
    )
    create_session(page, "already_running")
    submit(page, "python", 'print("setup_wait_value" in globals())')
    expect(page.locator("#transcript .entry:visible .content").last).to_have_text(
        "False\n", timeout=30_000
    )
