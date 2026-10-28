"""Exercise the fixture through its browser UI and the real Console process."""

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright


@pytest.fixture
def app(tmp_path):
    binary = Path(os.environ["MCP_CONSOLE_TEST_BINARY"]).resolve()
    with socket.socket() as address:
        address.bind(("127.0.0.1", 0))
        port = address.getsockname()[1]
    log = tmp_path / "shiny.log"
    with log.open("w+") as output:
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("app.py")),
                "--binary",
                str(binary),
                "--workspace",
                str(tmp_path),
                "--port",
                str(port),
            ],
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            # The launcher's ready line follows Uvicorn's startup event, so
            # browser navigation never races listener binding.
            import time

            deadline = time.monotonic() + 30
            while "Application startup complete" not in log.read_text():
                assert process.poll() is None, log.read_text()
                assert time.monotonic() < deadline, log.read_text()
                time.sleep(0.05)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page()
                page.goto(f"http://127.0.0.1:{port}")
                page.wait_for_function(
                    "() => /^(Ready|Error:)/.test(document.querySelector('#connection_state')?.textContent || '')",
                    timeout=120_000,
                )
                expect(page.locator("#connection_state")).to_have_text("Ready")
                yield page, tmp_path
                page.close()
                browser.close()
        finally:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            assert process.returncode in (0, -15), log.read_text()


@pytest.mark.parametrize("viewport", [(1280, 720), (1440, 900)])
def test_fits_viewport_and_scrolls_transcript(app, viewport):
    page, _ = app
    page.set_viewport_size({"width": viewport[0], "height": viewport[1]})
    for selector in ("#prompt", "#ask", "#source", "#evaluate"):
        expect(page.locator(selector)).to_be_in_viewport(ratio=1)
    assert page.evaluate("document.documentElement.scrollHeight <= window.innerHeight")

    page.locator("#source").fill("for i in range(100): print(f'line {i}')")
    page.locator("#evaluate").click()
    expect(page.locator("#transcript .content")).to_contain_text(
        "line 99", timeout=30_000
    )
    history = page.locator(".history").filter(has=page.locator("#transcript"))
    assert history.evaluate("element => element.scrollHeight > element.clientHeight")
    history.evaluate("element => element.scrollTop = element.scrollHeight")
    assert history.evaluate("element => element.scrollTop > 0")
    expect(page.locator("#evaluate")).to_be_in_viewport(ratio=1)
    assert page.evaluate("document.documentElement.scrollHeight <= window.innerHeight")


def test_streams_pending_output_and_retains_state(app):
    page, workspace = app
    page.locator("#source").fill(
        "import time\n"
        "from pathlib import Path\n"
        "shared_answer = 41\n"
        "print('pending-marker', flush=True)\n"
        "while not Path('release').exists():\n"
        "    time.sleep(0.02)\n"
        "print('completed-marker')"
    )
    page.locator("#evaluate").click()
    expect(page.locator("#pending_output")).to_contain_text(
        "pending-marker", timeout=60_000
    )
    expect(page.locator("#transcript .source")).to_contain_text("shared_answer = 41")
    expect(page.locator("#transcript .content")).not_to_contain_text("completed-marker")
    (workspace / "release").touch()
    expect(page.locator("#transcript .content")).to_contain_text(
        "completed-marker", timeout=30_000
    )
    expect(page.locator("#pending_output")).to_be_empty()
    page.locator("#source").fill("print(shared_answer + 1)")
    page.locator("#evaluate").click()
    expect(page.locator("#transcript .content").last).to_contain_text(
        "42", timeout=30_000
    )
    page.locator("#language").select_option("r")
    page.locator("#source").fill("set.seed(1); plot(rnorm(10000), rnorm(10000))")
    page.locator("#evaluate").click()
    expect(page.locator("#transcript img")).to_have_count(1, timeout=60_000)
    page.wait_for_function(
        "() => document.querySelector('#transcript img').naturalWidth > 0"
    )


@pytest.mark.parametrize("language", ["python", "r"])
def test_priority_execution_while_ordinary_cell_is_pending(app, language):
    page, workspace = app
    page.locator("#language").select_option(language)
    if language == "python":
        source = (
            "import time\nfrom pathlib import Path\nshared_answer = 41\n"
            "print('waiting-marker', flush=True)\n"
            "while not Path('release').exists():\n    time.sleep(0.02)\n"
            "print('ordinary-completed', shared_answer)"
        )
        priority = "shared_answer += 1; print('priority-result', shared_answer)"
        execution = "python_thread"
    else:
        source = (
            'shared_answer <- 41; cat("waiting-marker\\n"); '
            'while (!file.exists("release")) Sys.sleep(0.02); '
            'cat("ordinary-completed", shared_answer, "\\n")'
        )
        priority = 'shared_answer <- shared_answer + 1; cat("priority-result", shared_answer, "\\n")'
        execution = "r_reentrant"
    page.locator("#source").fill(source)
    page.locator("#evaluate").click()
    expect(page.locator("#pending_output")).to_contain_text(
        "waiting-marker", timeout=60_000
    )
    page.locator("#execution").select_option(execution)
    page.locator("#source").fill(priority)
    page.locator("#evaluate").click()
    expect(page.locator("#transcript .content").last).to_contain_text(
        "priority-result 42", timeout=30_000
    )
    expect(page.locator("#transcript .content").first).not_to_contain_text(
        "ordinary-completed"
    )
    (workspace / "release").touch()
    expect(page.locator("#transcript .content").first).to_contain_text(
        "ordinary-completed 42", timeout=30_000
    )


@pytest.mark.skipif(
    os.environ.get("MCP_CONSOLE_LIVE_AGENT") != "1",
    reason="Live Luna inference is explicitly enabled separately",
)
def test_agent_uses_the_same_worker(app):
    page, _ = app
    page.locator("#source").fill("shared_answer = 40")
    page.locator("#evaluate").click()
    expect(page.locator("#transcript .entry.complete")).to_have_count(1, timeout=60_000)
    page.locator("#prompt").fill(
        "Use the console_fixture send tool to execute exactly this Python code: "
        "shared_answer += 2; print(shared_answer). "
        "Then reply with only the printed number."
    )
    page.locator("#ask").click()
    expect(page.locator("#agent_state")).to_have_text("Completed", timeout=120_000)
    expect(page.locator("#transcript .agent").last).to_contain_text("42")
    page.locator("#source").fill("print('shared-state:', shared_answer)")
    page.locator("#evaluate").click()
    expect(page.locator("#transcript")).to_contain_text(
        "shared-state: 42", timeout=30_000
    )
    if screenshot := os.environ.get("MCP_CONSOLE_FIXTURE_SCREENSHOT"):
        page.screenshot(path=screenshot, full_page=True)
