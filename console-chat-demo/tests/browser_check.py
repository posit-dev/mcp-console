"""Drive the running app in Chromium, optionally with real model tool calls."""

import argparse
import asyncio
import json
import os
import re
from pathlib import Path

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8008")
    parser.add_argument("--browser", required=True, help="Path to Chrome or Chromium")
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--skip-dynamic",
        action="store_true",
        help="For deployments with CONSOLE_CHAT_DYNAMIC_PACKAGES=0",
    )
    parser.add_argument(
        "--lifecycle",
        action="store_true",
        help="Test live cancellation and concurrent progress",
    )
    args = parser.parse_args()
    artifacts = ROOT / ".runtime" / "browser"
    artifacts.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=args.browser)
        errors = []
        context = await browser.new_context(viewport={"width": 1440, "height": 1000})
        first = await context.new_page()
        first.on("pageerror", lambda error: errors.append(str(error)))
        await first.goto(args.url)
        await expect(first.locator("#connection_status")).to_have_text("Ready")
        await expect(first.get_by_text("Viewing rows", exact=False)).to_be_visible()
        await first.screenshot(path=str(artifacts / "initial.png"), full_page=True)

        async def submit(page, prompt):
            editor = page.locator('[contenteditable="true"]')
            await editor.fill(prompt)
            await editor.press("Enter")
            await expect(page.locator("#connection_status")).to_have_text(
                "Working", timeout=30000
            )

        async def completed(page):
            await expect(page.locator("#connection_status")).to_have_text(
                "Ready", timeout=180000
            )
            assert "could not complete" not in await page.locator("body").inner_text()

        if args.live:
            # Separate browser contexts are separate users on the same server.
            second_context = await browser.new_context()
            second = await second_context.new_page()
            second.on("pageerror", lambda error: errors.append(str(error)))
            await second.goto(args.url)
            await expect(second.locator("#connection_status")).to_have_text("Ready")
            await submit(
                first,
                "First execute this complete Python cell:\n"
                "import pandas as pd\n"
                "browser_marker = 'alpha'\n"
                "print(pd.read_csv('data.csv')['revenue'].sum())\n\n"
                "Then use base R graphics to plot monthly revenue, and use the SQL "
                "parameter to query total revenue from data.csv. "
                "You must execute Python, R and SQL. End your answer with ALPHA_DONE.",
            )
            await submit(
                second,
                "Use Python to verify that browser_marker is absent from globals(), "
                "then set browser_marker = 'beta' and calculate the total units in "
                "data.csv. Report the isolation check and total. End with BETA_DONE.",
            )
            await asyncio.gather(completed(first), completed(second))
            await expect(
                first.get_by_role("log").get_by_text("ALPHA_DONE", exact=True)
            ).to_be_visible()
            await expect(
                second.get_by_role("log").get_by_text("BETA_DONE", exact=True)
            ).to_be_visible()
            await expect(first.locator("#session_info")).to_contain_text(
                "prepared packages"
            )
            await expect(first.locator(".plot-gallery img")).to_have_count(1)
            assert await first.locator(".plot-gallery img").evaluate_all(
                "images => images.every(x => x.complete && x.naturalWidth > 0)"
            )
            await first.get_by_role(
                "button", name=re.compile(r"^Used send")
            ).first.click()
            await first.get_by_role("button", name="send", exact=True).first.click()
            await first.get_by_text("Tool call", exact=True).first.click()
            call = first.locator(".shiny-tool-result__request code").first
            assert (await call.inner_text()).startswith("python: |")
            suggestion = first.get_by_role("log").locator(".suggestion").last
            await expect(suggestion).to_be_visible()
            question = await suggestion.inner_text()
            await suggestion.click()
            await expect(first.locator("#connection_status")).to_have_text("Working")
            await completed(first)
            await expect(
                first.get_by_role("log").get_by_text(question, exact=True).last
            ).to_be_visible()
            await submit(
                first,
                "Use Python to print browser_marker from the previous turn. "
                "It must still be alpha. End with PERSISTENCE_DONE.",
            )
            await completed(first)
            await expect(
                first.get_by_role("log").get_by_text("PERSISTENCE_DONE", exact=True)
            ).to_be_visible()
            async with first.expect_download() as download:
                await first.get_by_role("link", name="Export", exact=True).click()
            await (await download.value).save_as(artifacts / "analysis.md")
            exported = (artifacts / "analysis.md").read_text()
            assert "ALPHA_DONE" in exported and "Code submitted" in exported
            assert "```python\nimport pandas as pd\n" in exported
            assert "```r\n" in exported
            assert "```sql\n" in exported
            assert "```json\n" not in exported
            await first.screenshot(path=str(artifacts / "analysis.png"), full_page=True)
            await second.screenshot(
                path=str(artifacts / "second-client.png"), full_page=True
            )
            await first.get_by_role("button", name="New session", exact=True).click()
            await expect(first.locator("#session_info")).to_contain_text(
                "next question"
            )
            await expect(first.locator(".plot-gallery img")).to_have_count(0)
            if args.skip_dynamic:
                await expect(first.get_by_label("Dynamic packages")).to_have_count(0)
            else:
                await first.get_by_label("Dynamic packages").check()
                await submit(
                    first,
                    "Use the requirements parameter to add the Python package humanize, "
                    "then run Python to print humanize.intcomma(12345).",
                )
                await completed(first)
                await expect(first.locator("#session_info")).to_contain_text(
                    "dynamic packages"
                )
                await expect(first.get_by_role("log")).to_contain_text("12,345")
            await second_context.close()
        if args.lifecycle:
            other_context = await browser.new_context()
            other = await other_context.new_page()
            await other.goto(args.url)
            await expect(other.locator("#connection_status")).to_have_text("Ready")
            await submit(
                first,
                "We are testing this app's Stop button. Use the send tool now to run "
                "exactly this Python cell with timeout_ms=1000. A human will press Stop "
                "to interrupt it. Do not create the release file. Keep polling after "
                "submission until it stops:\n"
                "from pathlib import Path\nimport os, time\n"
                "Path('browser-waiting').write_text(str(os.getpid()))\n"
                "while not Path('browser-release').exists():\n    time.sleep(0.05)\n",
            )
            async with asyncio.timeout(90):
                while True:
                    checkpoints = list(
                        (ROOT / ".runtime/server/slots").glob(
                            "*/workspace/browser-waiting"
                        )
                    )
                    if checkpoints:
                        break
                    if (
                        await first.locator("#connection_status").inner_text()
                        != "Working"
                    ):
                        await first.screenshot(
                            path=str(artifacts / "lifecycle-failure.png")
                        )
                        raise AssertionError(
                            await first.get_by_role("log").inner_text()
                        )
                    await asyncio.sleep(0.01)
            checkpoint = checkpoints[0]
            worker = int(checkpoint.read_text())
            await submit(
                other,
                "Use Python to calculate 123 * 456. End with a separate paragraph containing only CONCURRENT_DONE.",
            )
            await completed(other)
            await expect(
                other.get_by_role("log").get_by_text("CONCURRENT_DONE", exact=True)
            ).to_be_visible()
            await expect(first.locator("#connection_status")).to_have_text("Working")
            assert checkpoint.exists(), "The first client should still be gated"
            await first.get_by_role("button", name="Stop", exact=True).click()
            await expect(first.locator("#connection_status")).to_have_text(
                "Ready", timeout=30000
            )
            assert not checkpoint.parent.exists(), "Stopped workspace was retained"
            try:
                os.kill(worker, 0)
            except ProcessLookupError:
                pass
            else:
                raise AssertionError("Stopped Console worker is still alive")
            await first.screenshot(path=str(artifacts / "stopped.png"), full_page=True)
            await other_context.close()
        await first.set_viewport_size({"width": 390, "height": 844})
        await first.locator(".chat-card").scroll_into_view_if_needed()
        chat_box = await first.locator(".chat-card").bounding_box()
        data_box = await first.locator(".data-card").bounding_box()
        assert data_box["y"] >= chat_box["y"] + chat_box["height"] + 10
        editor = first.locator('[contenteditable="true"]')
        await editor.scroll_into_view_if_needed()
        assert await editor.evaluate("""el => {
            const r = el.getBoundingClientRect();
            const card = el.closest('.chat-card').getBoundingClientRect();
            const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
            return r.height > 0 && r.top >= card.top && r.bottom <= card.bottom
                && (hit === el || el.contains(hit));
        }""")
        await first.screenshot(path=str(artifacts / "mobile.png"), full_page=True)
        assert await first.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        ), "The mobile layout overflows horizontally"
        assert not errors, errors
        (
            artifacts / ("lifecycle-result.json" if args.lifecycle else "result.json")
        ).write_text(
            json.dumps(
                {
                    "live": args.live or args.lifecycle,
                    "model": "claude-sonnet-5-5",
                    "browser_errors": errors,
                }
            )
        )
        await browser.close()
        print(
            "Browser checks passed"
            + (" with live model tool calls" if args.live or args.lifecycle else "")
        )


if __name__ == "__main__":
    asyncio.run(main())
