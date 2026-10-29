"""Native Shiny controls for bounded pages of a private table snapshot."""

import asyncio
import math
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any

import pandas as pd
from shiny import Inputs, Outputs, Session, module, reactive, render, ui
from tables import Evaluator, TableViewer

PAGE_SIZE = 100
OPERATORS = {
    "text": {
        "contains": "Contains",
        "notContains": "Does not contain",
        "equals": "Equals",
        "notEqual": "Does not equal",
        "startsWith": "Starts with",
        "endsWith": "Ends with",
    },
    "number": {
        "equals": "Equals",
        "notEqual": "Does not equal",
        "lessThan": "Less than",
        "lessThanOrEqual": "At most",
        "greaterThan": "Greater than",
        "greaterThanOrEqual": "At least",
        "inRange": "Between",
    },
    "date": {
        "equals": "On date",
        "notEqual": "Not on date",
        "lessThan": "Before",
        "greaterThan": "After",
        "inRange": "Between",
    },
}


@module.ui
def table_ui() -> ui.TagList:
    return ui.TagList(
        ui.output_text("status"),
        ui.div(
            ui.input_action_button("refresh", "Refresh"),
            ui.input_action_button("close", "Close"),
            class_="btn-group",
        ),
        ui.p(
            "Snapshot: refresh to include source changes. Controls query the whole snapshot.",
            class_="small text-muted",
        ),
        ui.accordion(
            ui.accordion_panel(
                "Sort and filter",
                ui.layout_columns(
                    ui.input_select(
                        "sort_column", "Sort column", {"": "Original row order"}
                    ),
                    ui.input_select(
                        "sort_direction",
                        "Direction",
                        {"asc": "Ascending", "desc": "Descending"},
                    ),
                    col_widths=(7, 5),
                    fill=False,
                ),
                ui.input_action_button("apply_sort", "Sort"),
                ui.input_select("filter_column", "Filter column", {}),
                ui.output_ui("filter_controls"),
                ui.div(
                    ui.input_action_button("apply_filter", "Apply filter"),
                    ui.input_action_button("clear_filters", "Clear filters"),
                    class_="btn-group",
                ),
            ),
            open=False,
        ),
        ui.output_text("active_filters"),
        ui.div(
            ui.input_action_button("previous", "Previous", class_="btn-sm text-nowrap"),
            ui.input_numeric("page_number", "Page", 1, min=1, step=1, width="85px"),
            ui.input_action_button("go", "Go", class_="btn-sm text-nowrap"),
            ui.input_action_button("next", "Next", class_="btn-sm text-nowrap"),
            class_="d-flex align-items-center gap-2",
        ),
        ui.output_text("page_status"),
        ui.output_text("truncation"),
        ui.div(
            ui.output_table("page_data"),
            class_="table-page",
            style="overflow:auto; max-height:50vh; min-height:100px;",
        ),
    )


@dataclass
class TableActions:
    open: Callable[[str, str], Awaitable[bool]]
    reset: Callable[[], Awaitable[None]]
    dispose: Callable[[], None]


@module.server
def table_server(
    input: Inputs,
    output: Outputs,
    session: Session,
    evaluate: Evaluator,
    immediate: asyncio.Lock,
    ready: asyncio.Event,
    start: Callable[[Coroutine[Any, Any, Any]], None],
) -> TableActions:
    viewer = TableViewer(evaluate)
    status_text = reactive.value("Open a table from Variables.")
    page_text = reactive.value("")
    filter_text = reactive.value("")
    truncation_text = reactive.value("")
    displayed = reactive.value(pd.DataFrame())
    controls_epoch = reactive.value(0)
    metadata: dict[str, Any] | None = None
    selected: tuple[str, str] | None = None
    filters: dict[str, Any] = {}
    sorts: list[dict[str, str]] = []
    page = 1
    matching = 0
    generation = revision = 0
    filter_revision = 0

    async def change(value: reactive.Value[Any], item: Any) -> None:
        async with reactive.lock():
            value.set(item)
            await reactive.flush()

    async def clear_view(message: str) -> None:
        await change(status_text, message)
        await change(displayed, pd.DataFrame())
        await change(page_text, "")
        await change(filter_text, "")
        await change(truncation_text, "")
        async with reactive.lock():
            with reactive.isolate():
                controls_epoch.set(controls_epoch.get() + 1)

    def invalidate() -> int:
        nonlocal generation, revision, filter_revision
        generation += 1
        revision += 1
        filter_revision += 1
        return generation

    async def fetch_page(target: int, token: int, version: int) -> None:
        nonlocal page, matching
        while token == generation and version == revision and metadata is not None:
            try:
                async with immediate:
                    if token != generation or version != revision or metadata is None:
                        return
                    result = await viewer.rows(
                        metadata["id"],
                        (target - 1) * PAGE_SIZE,
                        target * PAGE_SIZE,
                        sorts,
                        filters,
                    )
                if token != generation or version != revision:
                    return
                page, matching = target, result["row_count"]
                columns = metadata["columns"]
                frame = pd.DataFrame(
                    [
                        [row[column["field"]] for column in columns]
                        for row in result["rows"]
                    ],
                    columns=[column["name"] for column in columns],
                    dtype=object,
                )
                await change(displayed, frame.fillna(""))
                count = len(result["rows"])
                first = (page - 1) * PAGE_SIZE + 1 if count else 0
                last = first + count - 1 if count else 0
                await change(
                    page_text,
                    f"Rows {first:,}–{last:,} · {matching:,} matching rows · page {page:,} of {max(1, math.ceil(matching / PAGE_SIZE)):,}",
                )
                await change(
                    truncation_text,
                    "Some cell text was shortened to 1,024 characters; filters use full values."
                    if result["truncated"]
                    else "",
                )
                async with reactive.lock():
                    ui.update_numeric(
                        "page_number",
                        value=page,
                        max=max(1, math.ceil(matching / PAGE_SIZE)),
                        session=session,
                    )
                assert selected is not None
                await change(
                    status_text,
                    f"{selected[0].upper()} · {selected[1]} · {metadata['row_count']:,} rows",
                )
                return
            except (RuntimeError, ValueError, OSError) as error:
                if token != generation or version != revision:
                    return
                if getattr(error, "code", None) != "busy":
                    await change(status_text, str(error))
                    return
                await change(status_text, "Waiting for the worker to read the table…")
                await asyncio.sleep(1)

    def query(target: int) -> None:
        nonlocal revision
        if metadata is None:
            return
        revision += 1
        start(fetch_page(max(1, target), generation, revision))

    async def open_table(language: str, name: str) -> bool:
        nonlocal metadata, selected, filters, sorts, matching, page
        token = invalidate()
        await ready.wait()
        await change(status_text, f"Opening {name}…")
        while token == generation:
            try:
                async with immediate:
                    if token != generation:
                        return False
                    opened = await viewer.open(language, name)
                if token != generation:
                    return False
                selected = (language, name)
                metadata, filters, sorts, page = opened, {}, [], 1
                matching = opened["row_count"]
                choices = {
                    column["field"]: column["name"] for column in opened["columns"]
                }
                async with reactive.lock():
                    ui.update_select(
                        "sort_column",
                        choices={"": "Original row order", **choices},
                        selected="",
                        session=session,
                    )
                    ui.update_select("sort_direction", selected="asc", session=session)
                    ui.update_select(
                        "filter_column",
                        choices=choices,
                        selected=next(iter(choices)),
                        session=session,
                    )
                    with reactive.isolate():
                        controls_epoch.set(controls_epoch.get() + 1)
                await change(filter_text, "")
                query(1)
                return True
            except (RuntimeError, ValueError, OSError) as error:
                if token != generation:
                    return False
                if getattr(error, "code", None) != "busy":
                    await change(status_text, str(error))
                    return False
                await change(status_text, f"Waiting for the worker to open {name}…")
                await asyncio.sleep(1)
        return False

    async def reset() -> None:
        nonlocal metadata, selected
        invalidate()
        metadata = selected = None
        async with immediate:
            viewer.reset()
        await clear_view("Session replaced. Open a table again.")

    async def close_table() -> None:
        nonlocal metadata, selected
        token = invalidate()
        metadata = selected = None
        await clear_view("Open a table from Variables.")
        await ready.wait()
        while token == generation:
            try:
                async with immediate:
                    if token != generation:
                        return
                    await viewer.close()
                return
            except (RuntimeError, ValueError, OSError) as error:
                if token != generation:
                    return
                if getattr(error, "code", None) != "busy":
                    await change(status_text, str(error))
                    return
                await asyncio.sleep(1)

    @render.ui
    def filter_controls() -> ui.TagList:
        epoch = controls_epoch.get()
        if metadata is None:
            return ui.TagList()
        field = input.filter_column()
        column = next(
            (item for item in metadata["columns"] if item["field"] == field), None
        )
        if column is None:
            return ui.TagList()
        options = {
            **OPERATORS[column["kind"]],
            "blank": "Blank",
            "notBlank": "Not blank",
        }
        current = filters.get(field, {})
        # Each column/capture owns its input bindings. A late update for the
        # previously selected column cannot overwrite the user's new input.
        prefix = f"filter_{field}_{epoch}"
        return ui.TagList(
            ui.input_select(
                prefix + "_operator",
                f"Condition · {column['name']}",
                options,
                selected=current.get("type", next(iter(options))),
            ),
            ui.layout_columns(
                ui.input_text(
                    prefix + "_value",
                    f"Value · {column['name']}",
                    value=str(current.get("dateFrom", current.get("filter", ""))),
                    placeholder="Dates: YYYY-MM-DD",
                ),
                ui.input_text(
                    prefix + "_to",
                    "Upper value (Between only)",
                    value=str(current.get("dateTo", current.get("filterTo", ""))),
                ),
                col_widths=(7, 5),
                fill=False,
            ),
        )

    def filter_values() -> tuple[str, str, str, str] | None:
        epoch = controls_epoch.get()
        if metadata is None:
            return None
        field = input.filter_column()
        if field not in {column["field"] for column in metadata["columns"]}:
            return None
        prefix = f"filter_{field}_{epoch}"
        return (
            field,
            input[prefix + "_operator"](),
            input[prefix + "_value"](),
            input[prefix + "_to"](),
        )

    async def query_filters(version: int, token: int, wait: bool) -> None:
        if wait:
            await asyncio.sleep(0.3)
        if version == filter_revision and token == generation:
            query(1)

    def filter_changed(wait: bool) -> None:
        nonlocal filters, filter_revision
        values = filter_values()
        if values is None or metadata is None:
            return
        # Save the edited filter immediately so switching columns preserves it;
        # debounce the query, not the user's form state.
        field, operation, value, upper = values
        column = next(
            (column for column in metadata["columns"] if column["field"] == field), None
        )
        if column is None:
            return
        model: dict[str, Any] = {"filterType": column["kind"], "type": operation}
        if operation not in {"blank", "notBlank"}:
            if not value:
                model = {}
            else:
                if operation == "inRange" and not upper:
                    return
                try:
                    if column["kind"] == "number":
                        model["filter"] = float(value)
                        if operation == "inRange":
                            model["filterTo"] = float(upper)
                    elif column["kind"] == "date":
                        model["dateFrom"] = value
                        if operation == "inRange":
                            model["dateTo"] = upper
                    else:
                        model["filter"] = value
                except ValueError:
                    status_text.set("Enter a number for this filter.")
                    return
        updated = dict(filters)
        if model:
            updated[field] = model
        else:
            updated.pop(field, None)
        if updated == filters and wait:
            return
        filters = updated
        labels = {column["field"]: column["name"] for column in metadata["columns"]}
        filter_text.set(
            "; ".join(
                f"{labels[key]}: {model['type']} {model.get('filter', model.get('dateFrom', ''))}"
                for key, model in filters.items()
            ),
        )
        filter_revision += 1
        start(query_filters(filter_revision, generation, wait))

    @reactive.effect
    def filter_edits() -> None:
        filter_changed(True)

    @reactive.effect
    @reactive.event(input.apply_filter)
    def apply_filter() -> None:
        filter_changed(False)

    @reactive.effect
    @reactive.event(input.clear_filters)
    def clear_filters() -> None:
        nonlocal filters, filter_revision
        filters = {}
        filter_revision += 1
        controls_epoch.set(controls_epoch.get() + 1)
        filter_text.set("")
        query(1)

    @reactive.effect
    @reactive.event(input.apply_sort)
    def apply_sort() -> None:
        nonlocal sorts
        sorts = (
            [{"colId": input.sort_column(), "sort": input.sort_direction()}]
            if input.sort_column()
            else []
        )
        query(1)

    @reactive.effect
    @reactive.event(input.go)
    def go() -> None:
        query(
            min(
                max(1, math.ceil(matching / PAGE_SIZE)),
                max(1, int(input.page_number() or 1)),
            )
        )

    @reactive.effect
    @reactive.event(input.previous)
    def previous() -> None:
        query(max(1, page - 1))

    @reactive.effect
    @reactive.event(input.next)
    def next_page() -> None:
        query(min(max(1, math.ceil(matching / PAGE_SIZE)), page + 1))

    @reactive.effect
    @reactive.event(input.refresh)
    def refresh() -> None:
        if selected is not None:
            start(open_table(*selected))

    @reactive.effect
    @reactive.event(input.close)
    def close() -> None:
        start(close_table())

    @render.text
    def status() -> str:
        return status_text.get()

    @render.text
    def page_status() -> str:
        return page_text.get()

    @render.text
    def active_filters() -> str:
        return filter_text.get()

    @render.text
    def truncation() -> str:
        return truncation_text.get()

    @render.table(index=False, classes="table table-sm table-striped", na_rep="")
    def page_data() -> pd.DataFrame:
        return displayed.get()

    return TableActions(open_table, reset, viewer.reset)
