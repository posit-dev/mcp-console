"""Native Shiny controls for each transcript evaluation's output views."""

from shiny import ui


def _expand_button() -> ui.Tag:
    return ui.tags.button(
        "Expand",
        type="button",
        class_="output-expand btn btn-sm btn-outline-secondary",
        hidden=True,
        **{"aria-expanded": "false"},
    )


def response_controls(identifier: str) -> str:
    return ui.div(
        _expand_button(),
        id="response_output_" + identifier.encode().hex(),
        class_="response-controls",
    ).render()["html"]


def output_controls(identifier: str) -> str:
    return ui.div(
        ui.input_radio_buttons(
            "output_view_" + identifier.encode().hex(),
            "Output view",
            {"captured": "Captured output", "agent": "Responses to agent"},
            selected="captured",
            inline=True,
        ),
        ui.div(
            _expand_button(),
            ui.tags.button(
                "Open retained output",
                type="button",
                class_="output-retained btn btn-sm btn-outline-secondary",
                disabled=True,
            ),
            class_="output-actions",
        ),
        class_="output-controls",
    ).render()["html"]
