"""Native inspection and additive preparation of Console requirements."""

from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any

import pandas as pd
from shiny import Inputs, Outputs, Session, module, reactive, render, ui

LANGUAGES = {"r": "R", "python": "Python", "duckdb": "DuckDB extensions"}
DIAGNOSTIC_LIMIT = 4000


@module.ui
def environments_ui() -> ui.TagList:
    return ui.TagList(
        ui.h6("Accepted requirements"),
        ui.output_text("summary"),
        ui.input_action_button("refresh", "Refresh", class_="btn-sm"),
        ui.output_data_frame("accepted"),
        ui.output_text("settings"),
        ui.div(ui.output_text("help"), class_="small text-muted"),
        ui.h6("Add requirements"),
        ui.input_radio_buttons(
            "language", "Environment", LANGUAGES, selected="r", inline=True
        ),
        ui.input_text_area(
            "draft", "Draft · one requirement per line", rows=3, width="100%"
        ),
        ui.input_action_button("add", "Add and prepare", class_="btn-primary btn-sm"),
        ui.output_text("operation"),
        ui.output_code("diagnostics", placeholder=False),
    )


@dataclass
class EnvironmentActions:
    refresh: Callable[[], Awaitable[None]]


@module.server
def environments_server(
    input: Inputs,
    output: Outputs,
    session: Session,
    request: Callable[..., Awaitable[dict[str, Any]]],
    prepare: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    start: Callable[[Coroutine[Any, Any, None]], None],
) -> EnvironmentActions:
    """The caller owns preparation admission and pauses its private inspectors."""
    displayed = reactive.value(
        pd.DataFrame(columns=["Language", "Requirement", "Role"])
    )
    summary_text = reactive.value("Waiting for Console requirements…")
    settings_text = reactive.value("")
    operation_text = reactive.value("")
    diagnostic_text = reactive.value("")
    active = False
    revision = 0

    async def change(value: reactive.Value[Any], item: Any) -> None:
        async with reactive.lock():
            value.set(item)
            await reactive.flush()

    async def display(snapshot: dict[str, Any]) -> None:
        declaration = snapshot["requirements"]
        runtime = snapshot["runtime_requirements"]
        rows = []
        count = 0
        for language, label in LANGUAGES.items():
            accepted = set(declaration[language])
            infrastructure = set(runtime.get(language, []))
            count += len(accepted)
            for requirement in sorted(accepted | infrastructure):
                roles = []
                if requirement in accepted:
                    roles.append("Accepted")
                if requirement in infrastructure:
                    roles.append("Console runtime")
                rows.append([label, requirement, " · ".join(roles)])
        await change(
            displayed, pd.DataFrame(rows, columns=["Language", "Requirement", "Role"])
        )
        await change(
            summary_text,
            f"Prepared: {'yes' if snapshot['prepared'] else 'no'} · {count} accepted requirements",
        )
        settings = []
        if declaration.get("python_version"):
            settings.append(
                f"Python version constraints: {declaration['python_version']}"
            )
        if declaration.get("exclude_newer"):
            settings.append(f"Exclude newer: {declaration['exclude_newer']}")
        await change(settings_text, " · ".join(settings))

    async def show_diagnostics(message: str, truncated: bool = False) -> None:
        suffix = (
            "\n[diagnostic shortened to 4,000 characters]"
            if len(message) > DIAGNOSTIC_LIMIT
            else ""
        )
        if truncated:
            suffix += "\n[Console shortened the diagnostic before delivery]"
        await change(diagnostic_text, message[:DIAGNOSTIC_LIMIT] + suffix)

    async def refresh() -> None:
        nonlocal revision
        if active:
            return
        revision += 1
        observed = revision
        try:
            snapshot = await request(op="requirements")
            if observed == revision:
                await display(snapshot)
        except (RuntimeError, ValueError, OSError) as error:
            if observed == revision:
                await change(operation_text, "Failed to refresh requirements.")
                await show_diagnostics(str(error))

    async def add(language: str, draft: str) -> None:
        nonlocal active, revision
        if active:
            return
        requirements = [line.strip() for line in draft.splitlines() if line.strip()]
        if not requirements:
            await change(operation_text, "Enter one requirement per line.")
            return
        active = True
        revision += 1
        async with reactive.lock():
            ui.update_action_button("add", disabled=True, session=session)
            await reactive.flush()
        await change(operation_text, "Preparing requirements…")
        await show_diagnostics("")
        try:
            result = await prepare({"action": "add", language: requirements})
            await display(result)
            messages = {
                "prepared": "Prepared. Console accepted the requirements.",
                "restart_required": (
                    "Restart required. Apply this change through Console's requirements API with restart. "
                    "The ordinary Restart button reloads accepted requirements; it does not apply this draft."
                ),
                "busy": "Busy. Wait for execution to finish, then retry Add and prepare.",
                "failed": "Failed to prepare requirements. The draft is retained.",
            }
            await change(operation_text, messages[result["status"]])
            await show_diagnostics(
                result.get("message", ""), result.get("message_truncated", False)
            )
        except (RuntimeError, ValueError, OSError) as error:
            await change(
                operation_text, "Failed to prepare requirements. The draft is retained."
            )
            await show_diagnostics(str(error))
        finally:
            active = False
            async with reactive.lock():
                ui.update_action_button("add", disabled=False, session=session)
                await reactive.flush()
            await refresh()

    @reactive.effect
    @reactive.event(input.refresh)
    def refresh_clicked() -> None:
        start(refresh())

    @reactive.effect
    @reactive.event(input.add)
    def add_clicked() -> None:
        start(add(input.language(), input.draft()))

    @render.data_frame
    def accepted():
        return render.DataTable(
            displayed.get(), filters=True, height="230px", width="100%"
        )

    @render.text
    def help() -> str:
        return (
            "This is an accepted declaration, not an installed-package inventory or resolved versions. "
            "Prepared is Console's aggregate environment flag, not a per-package check. "
            "Console runtime dependencies are listed separately by role. "
            "The app table viewer requests Python pandas and duckdb during session setup."
        )

    @render.text
    def summary() -> str:
        return summary_text.get()

    @render.text
    def settings() -> str:
        return settings_text.get()

    @render.text
    def operation() -> str:
        return operation_text.get()

    @render.code
    def diagnostics() -> str:
        return diagnostic_text.get()

    return EnvironmentActions(refresh)
