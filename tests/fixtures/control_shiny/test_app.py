"""Exercise the fixture through its browser UI and the real Console process."""

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright
from shiny.playwright.controller import Chat, InputCodeEditor


@pytest.fixture
def chat_frames():
    return []


@pytest.fixture
def app(tmp_path, chat_frames):
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
            assert "ShinyDeprecationWarning" not in log.read_text(), log.read_text()
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page()
                browser_errors = []
                page.on("pageerror", lambda error: browser_errors.append(str(error)))
                page.on(
                    "websocket",
                    lambda socket: socket.on(
                        "framereceived",
                        lambda frame: (
                            chat_frames.append(frame)
                            if isinstance(frame, str) and "agent_chat" in frame
                            else None
                        ),
                    ),
                )
                page.goto(f"http://127.0.0.1:{port}")
                page.wait_for_function(
                    "() => /^(Ready|Error:)/.test(document.querySelector('#connection_state')?.textContent || '')",
                    timeout=120_000,
                )
                expect(page.locator("#connection_state")).to_have_text("Ready")
                yield page, tmp_path
                assert not browser_errors
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
    chat = Chat(page, "agent_chat")
    expect(chat.loc_input).to_be_in_viewport(ratio=1)
    expect(chat.loc_input_button).to_be_in_viewport(ratio=1)
    for selector in (
        "#source",
        "#evaluate_r",
        "#evaluate_python",
        "#evaluate_sql",
    ):
        expect(page.locator(selector)).to_be_in_viewport(ratio=1)
    assert page.evaluate("document.documentElement.scrollHeight <= window.innerHeight")
    submit(page, "python", "for i in range(100): print(f'line {i}')")
    expect(page.locator("#transcript .content")).to_contain_text(
        "line 99", timeout=30_000
    )
    history = page.locator("#transcript-history")
    assert history.evaluate("element => element.scrollHeight > element.clientHeight")
    page.wait_for_function(
        "() => { const h = document.querySelector('#transcript-history'); return h.scrollHeight - h.scrollTop - h.clientHeight < 8; }"
    )
    expect(page.locator("#evaluate_python")).to_be_in_viewport(ratio=1)


def submit(page, language, source):
    InputCodeEditor(page, "source").loc_textarea.fill(source)
    page.locator(f"#evaluate_{language}").click()


def test_native_chat_suggestions_prefill_without_submitting(app):
    page, _ = app
    chat = Chat(page, "agent_chat")
    chat.expect_greeting("You and the agent share one R, Python, and SQL session.")
    prompts = (
        "List objects in our shared session",
        "Create a small example table in R",
        "Create a small example table in Python",
    )
    for prompt in prompts:
        chat.loc_greeting.get_by_text(prompt, exact=True).click()
        expect(chat.loc_input).to_have_text(prompt)
        expect(page.locator("#agent_state")).to_have_text("Idle")
        expect(
            chat.loc.locator(".shiny-chat-message:not(.shiny-chat-greeting)")
        ).to_have_count(0)
        expect(page.locator("#transcript .entry")).to_have_count(0)
    submit(page, "sql", "SELECT 42 AS answer")
    expect(page.locator("#transcript .content")).to_contain_text("42", timeout=30_000)
    expect(chat.loc_input).to_have_text(prompts[-1])
    expect(page.locator("#agent_state")).to_have_text("Idle")


def test_language_buttons_and_sql_restriction(app):
    page, _ = app
    InputCodeEditor(page, "source").expect_language("python")
    for language in ("r", "python", "sql"):
        expect(page.locator(f"#evaluate_{language}")).to_be_visible()
    submit(page, "sql", "select 41 + 1 as answer")
    expect(page.locator("#transcript .content").last).to_contain_text(
        "42", timeout=60_000
    )
    expect(page.locator("#transcript .language").last).to_have_text("SQL")
    expect(page.locator("#transcript .origin").last).to_have_text("Human")
    InputCodeEditor(page, "source").expect_language("sql")
    expect(page.locator("#transcript .source .hljs-keyword").first).to_have_text(
        "select"
    )
    submit(page, "python", "import sys; print(sys.executable)")
    expect(page.locator("#transcript .entry.complete")).to_have_count(2, timeout=60_000)
    assert (
        str(Path(sys.executable).parent)
        not in page.locator("#transcript .content").last.inner_text()
    )
    page.get_by_label("Immediate", exact=True).check()
    expect(page.locator("#evaluate_sql")).to_be_disabled()
    expect(page.locator("#execution-help")).to_contain_text("SQL supports Ordinary")


def test_streams_pending_output_and_retains_state(app):
    page, workspace = app
    InputCodeEditor(page, "source").loc_textarea.fill(
        "import time\n"
        "from pathlib import Path\n"
        "shared_answer = 41\n"
        "print('pending-marker', flush=True)\n"
        "while not Path('release').exists():\n"
        "    time.sleep(0.02)\n"
        "print('completed-marker')"
    )
    page.locator("#evaluate_python").click()
    expect(page.locator("#transcript .entry.running .content")).to_contain_text(
        "pending-marker", timeout=60_000
    )
    expect(page.locator("#transcript .source")).to_contain_text("shared_answer = 41")
    expect(page.locator("#transcript .content")).not_to_contain_text("completed-marker")
    (workspace / "release").touch()
    expect(page.locator("#transcript .content")).to_contain_text(
        "completed-marker", timeout=30_000
    )
    expect(page.locator("#transcript .entry.running")).to_have_count(0)
    InputCodeEditor(page, "source").loc_textarea.fill("print(shared_answer + 1)")
    page.locator("#evaluate_python").click()
    expect(page.locator("#transcript .content").last).to_contain_text(
        "42", timeout=30_000
    )
    submit(page, "r", "set.seed(1); plot(rnorm(10000), rnorm(10000))")
    expect(page.locator("#transcript img")).to_have_count(1, timeout=60_000)
    page.wait_for_function(
        "() => document.querySelector('#transcript img').naturalWidth > 0"
    )


@pytest.mark.parametrize("language", ["python", "r"])
def test_immediate_execution_while_ordinary_cell_is_pending(app, language):
    page, workspace = app
    expect(page.locator("#execution input")).to_have_count(2)
    expect(page.locator("#execution-help")).to_contain_text("requires the GIL")
    if language == "python":
        source = (
            "import time\nfrom pathlib import Path\nshared_answer = 41\n"
            "print('waiting-marker', flush=True)\n"
            "while not Path('release').exists():\n    time.sleep(0.02)\n"
            "print('ordinary-completed', shared_answer)"
        )
        priority = "shared_answer += 1; print('priority-result', shared_answer)"
    else:
        source = (
            'shared_answer <- 41; cat("waiting-marker\\n"); '
            'while (!file.exists("release")) Sys.sleep(0.02); '
            'cat("ordinary-completed", shared_answer, "\\n")'
        )
        priority = 'shared_answer <- shared_answer + 1; cat("priority-result", shared_answer, "\\n")'
    InputCodeEditor(page, "source").loc_textarea.fill(source)
    page.locator(f"#evaluate_{language}").click()
    expect(page.locator("#transcript .entry.running .content")).to_contain_text(
        "waiting-marker", timeout=60_000
    )
    page.get_by_label("Immediate", exact=True).check()
    InputCodeEditor(page, "source").loc_textarea.fill(priority)
    page.locator(f"#evaluate_{language}").click()
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


@pytest.mark.parametrize("language", ["python", "r"])
def test_variables_update_while_ordinary_cell_is_pending(app, language):
    page, workspace = app
    expect(page.get_by_text("Variables", exact=True)).to_be_visible()
    variables = page.locator(f"#{language}_variables")
    if language == "python":
        source = (
            "import time\n"
            "from pathlib import Path\n"
            "inspected_value = 'variable-before'\n"
            "inspected_deleted = 'delete-before'\n"
            "repr_calls = 0\n"
            "class UnsafeRepr:\n"
            "    def __repr__(self):\n"
            "        global repr_calls\n"
            "        repr_calls += 1\n"
            "        raise RuntimeError('repr-was-executed')\n"
            "inspected_object = UnsafeRepr()\n"
            "print('variables-waiting', flush=True)\n"
            "while not Path('variables-update').exists():\n"
            "    time.sleep(0.02)\n"
            "inspected_value = 'variable-after'\n"
            "while not Path('variables-delete').exists():\n"
            "    time.sleep(0.02)\n"
            "del inspected_deleted\n"
            "while not Path('variables-release').exists():\n"
            "    time.sleep(0.02)\n"
            "print('variables-completed')"
        )
    else:
        source = (
            'inspected_value <- "variable-before"\n'
            'inspected_deleted <- "delete-before"\n'
            'cat("variables-waiting\\n")\n'
            'while (!file.exists("variables-update")) Sys.sleep(0.02)\n'
            'inspected_value <- "variable-after"\n'
            'while (!file.exists("variables-delete")) Sys.sleep(0.02)\n'
            "rm(inspected_deleted)\n"
            'while (!file.exists("variables-release")) Sys.sleep(0.02)\n'
            'cat("variables-completed\\n")'
        )
    InputCodeEditor(page, "source").loc_textarea.fill(source)
    page.locator(f"#evaluate_{language}").click()
    expect(page.locator("#transcript .entry.running .content")).to_have_text(
        "variables-waiting\n", timeout=60_000
    )
    expect(variables).to_contain_text("inspected_value", timeout=30_000)
    expect(variables).to_contain_text("variable-before")
    expect(variables).to_contain_text("inspected_deleted")
    if language == "python":
        expect(variables).to_contain_text("inspected_object")
        expect(variables).to_contain_text("<UnsafeRepr>")

    (workspace / "variables-update").touch()
    expect(variables).to_contain_text("variable-after", timeout=30_000)
    expect(page.locator("#transcript .entry.running .content")).to_have_text(
        "variables-waiting\n"
    )
    expect(page.locator("#transcript .content")).to_have_text("variables-waiting\n")

    (workspace / "variables-delete").touch()
    expect(variables).not_to_contain_text("inspected_deleted", timeout=30_000)
    expect(variables).to_contain_text("variable-after")
    expect(page.locator("#transcript .entry.running .content")).to_have_text(
        "variables-waiting\n"
    )
    expect(page.locator("#transcript .content")).to_have_text("variables-waiting\n")
    expect(page.locator("#transcript .entry")).to_have_count(1)

    (workspace / "variables-release").touch()
    expect(page.locator("#transcript .content")).to_have_text(
        "variables-waiting\nvariables-completed\n", timeout=30_000
    )
    expect(page.locator("#transcript .entry.running")).to_have_count(0)
    if language == "python":
        InputCodeEditor(page, "source").loc_textarea.fill(
            "print('repr-count', repr_calls)"
        )
        page.locator(f"#evaluate_{language}").click()
        expect(page.locator("#transcript .content").last).to_have_text(
            "repr-count 0\n", timeout=30_000
        )


@pytest.mark.skipif(
    os.environ.get("MCP_CONSOLE_LIVE_AGENT") != "1",
    reason="Live Luna inference is explicitly enabled separately",
)
def test_agent_uses_the_same_worker(app):
    page, _ = app
    InputCodeEditor(page, "source").loc_textarea.fill("shared_answer = 40")
    page.locator("#evaluate_python").click()
    expect(page.locator("#transcript .entry.complete")).to_have_count(1, timeout=60_000)
    Chat(page, "agent_chat").loc_input.fill(
        "Use the console_fixture send tool to execute exactly this Python code: "
        "shared_answer += 2; print(shared_answer). "
        "Then reply with only the printed number."
    )
    Chat(page, "agent_chat").send_user_input(method="click")
    expect(page.locator("#agent_state")).to_have_text("Completed", timeout=120_000)
    expect(page.locator("#transcript .agent").last).to_contain_text("42")
    InputCodeEditor(page, "source").loc_textarea.fill(
        "print('shared-state:', shared_answer)"
    )
    page.locator("#evaluate_python").click()
    expect(page.locator("#transcript")).to_contain_text(
        "shared-state: 42", timeout=30_000
    )
    if screenshot := os.environ.get("MCP_CONSOLE_FIXTURE_SCREENSHOT"):
        page.screenshot(path=screenshot, full_page=True)


def test_pending_completion_and_scroll_position(app):
    page, workspace = app
    submit(
        page,
        "python",
        (
            "import time\nfrom pathlib import Path\n"
            "for i in range(120): print(f'pending-line-{i}')\n"
            "while not Path('more').exists(): time.sleep(0.02)\n"
            "print('more-output', flush=True)\n"
            "while not Path('done').exists(): time.sleep(0.02)\n"
            "print('finished-output')"
        ),
    )
    content = page.locator("#transcript .content")
    expect(content).to_contain_text("pending-line-119", timeout=60_000)
    page.wait_for_function(
        "() => { const h = document.querySelector('#transcript-history'); return h.scrollHeight - h.scrollTop - h.clientHeight < 8; }"
    )
    history = page.locator("#transcript-history")
    history.evaluate("element => element.scrollTop = 0")
    (workspace / "more").touch()
    expect(content).to_contain_text("more-output")
    assert history.evaluate("element => element.scrollTop") == 0
    (workspace / "done").touch()
    expect(page.locator("#transcript .entry.complete")).to_have_count(1, timeout=30_000)
    expect(content).to_contain_text("finished-output")
    assert content.inner_text().count("pending-line-119") == 1
    assert history.evaluate("element => element.scrollTop") == 0
    history.evaluate("element => element.scrollTop = element.scrollHeight")
    submit(page, "r", "plot(1:10)")
    expect(page.locator("#transcript img")).to_have_count(1, timeout=60_000)
    page.wait_for_function(
        "() => document.querySelector('#transcript img').naturalWidth > 0"
    )
    page.wait_for_function(
        "() => { const h = document.querySelector('#transcript-history'); return h.scrollHeight - h.scrollTop - h.clientHeight < 8; }"
    )
    page.get_by_role("tab", name="Plots", exact=True).click()
    expect(page.locator("#plots img")).to_have_count(1)
    page.locator("#plots img").click()
    expect(page.locator("#plot-dialog")).to_be_visible()
    page.locator("#plot-close").click()
    expect(page.locator("#transcript .entry")).to_have_count(2)


def test_typed_diagnostics_and_private_inspection(app):
    page, _ = app
    submit(
        page,
        "r",
        'cat("ordinary-text\\n"); message("diagnostic-marker"); warning("warning-marker")',
    )
    expect(page.locator("#transcript .entry.complete")).to_have_count(1, timeout=60_000)
    expect(page.locator("#transcript .diagnostic")).to_contain_text(
        ["diagnostic-marker", "warning-marker"]
    )
    expect(page.locator("#transcript .content")).not_to_contain_text('"tables"')


@pytest.mark.skipif(
    os.environ.get("MCP_CONSOLE_LIVE_AGENT") != "1",
    reason="Live Luna inference is explicitly enabled separately",
)
def test_agent_interrupt_and_mixed_language_labels(app):
    page, _ = app
    submit(
        page,
        "python",
        "import time\nprint('interrupt-ready', flush=True)\nwhile True: time.sleep(0.02)",
    )
    expect(page.locator("#transcript .content")).to_contain_text(
        "interrupt-ready", timeout=60_000
    )
    Chat(page, "agent_chat").loc_input.fill(
        "First use console_fixture send with control='interrupt' to stop the running cell. "
        "Then make exactly one console_fixture send call containing all three language fields: "
        "r='cat(\"r-agent-marker\\n\")', python='print(\"python-agent-marker\")', "
        "sql=\"select 'sql-agent-marker' as marker\". Do not run anything else. Reply done."
    )
    Chat(page, "agent_chat").send_user_input(method="click")
    expect(page.locator("#agent_state")).to_have_text("Completed", timeout=120_000)
    expect(page.locator("#transcript .interrupt").first).to_contain_text(
        "requested interruption; execution may still be running"
    )
    expect(page.locator("#transcript .interrupt").last).to_contain_text(
        "interrupt request returned"
    )
    expect(page.locator("#transcript .agent .language")).to_have_text(
        ["R", "PYTHON", "SQL"]
    )
    expect(page.locator("#transcript .agent .origin")).to_have_text(["Agent"] * 3)
    expect(page.locator("#transcript .agent .content")).to_have_text(
        ["only one of `r`, `python`, or `sql` may be supplied"] * 3
    )
    expect(page.locator("#transcript .entry")).to_have_count(4)
    Chat(page, "agent_chat").loc_input.fill(
        "Now execute each of those three snippets, each in its own console_fixture send call, "
        "in R, Python, SQL order. Do not use a multi-language call. Then reply done."
    )
    Chat(page, "agent_chat").send_user_input(method="click")
    expect(page.locator("#transcript .agent.complete")).to_have_count(
        3, timeout=120_000
    )
    expect(page.locator("#agent_state")).to_have_text("Completed", timeout=120_000)
    expect(page.locator("#transcript .agent.complete .language")).to_have_text(
        ["R", "PYTHON", "SQL"]
    )
    expect(page.locator("#transcript .agent.complete .content")).to_contain_text(
        ["r-agent-marker", "python-agent-marker", "sql-agent-marker"]
    )
    expect(page.locator("#transcript .entry")).to_have_count(7)


@pytest.mark.skipif(
    os.environ.get("MCP_CONSOLE_LIVE_AGENT") != "1",
    reason="Live Luna inference is explicitly enabled separately",
)
def test_native_chat_streams_markdown_while_direct_controls_remain_live(
    app, chat_frames
):
    page, workspace = app
    chat = Chat(page, "agent_chat")
    assistant = chat.loc_messages.locator(
        ".shiny-chat-message:not(.shiny-chat-user-message):not(.shiny-chat-greeting)"
    )
    chat.loc_input.fill(
        "First reply with exactly **Streaming started** and `native chat` in Markdown, "
        "then use console_fixture send to execute exactly this Python program:\n"
        "import time\nfrom pathlib import Path\n"
        "print('chat-checkpoint', flush=True)\n"
        "while not Path('chat-release').exists(): time.sleep(0.02)\n"
        "Finally reply with exactly **Streaming finished**."
    )
    chat.send_user_input(method="click")
    expect(assistant.locator("strong")).to_contain_text(
        ["Streaming started"], timeout=120_000
    )
    expect(assistant.locator("code")).to_contain_text(["native chat"])
    expect(page.locator("#transcript .content")).to_contain_text(
        "chat-checkpoint", timeout=60_000
    )
    expect(page.locator("#agent_state")).to_have_text("Running")
    page.get_by_label("Immediate", exact=True).check()
    submit(page, "python", "print('direct-during-chat')")
    expect(page.locator("#transcript .content").last).to_contain_text(
        "direct-during-chat", timeout=30_000
    )
    (workspace / "chat-release").touch()
    expect(page.locator("#agent_state")).to_have_text("Completed", timeout=120_000)
    expect(assistant.locator("strong")).to_contain_text(
        ["Streaming started", "Streaming finished"]
    )
    assert assistant.inner_text().count("Streaming started") == 1
    assert any("chunk_start" in action for action in chat_frames)
    assert any("chunk_end" in action for action in chat_frames)


def test_native_submitted_code_preserves_backticks_and_html(app):
    page, _ = app
    source = "print('```<script>literal-code</script>```')"
    submit(page, "python", source)
    expect(page.locator("#transcript .entry.complete")).to_have_count(1, timeout=30_000)
    expect(page.locator("#transcript .source code")).to_have_text(source)
    expect(page.locator("#transcript .source .hljs-string")).to_contain_text(
        "literal-code"
    )
    expect(page.locator("#transcript .source script")).to_have_count(0)
    expect(page.locator("#transcript .content")).to_have_text(
        "```<script>literal-code</script>```\n"
    )
