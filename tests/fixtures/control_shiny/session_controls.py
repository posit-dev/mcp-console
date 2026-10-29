"""Native controls for selecting and creating Console's named sessions."""

from shiny import ui


def session_controls_ui() -> ui.Tag:
    return ui.div(
        ui.input_select(
            "console_session",
            "Console session",
            {"default": "default"},
            selected="default",
        ),
        ui.input_action_button(
            "new_console_session", "+ New", class_="btn-sm", disabled=True
        ),
        ui.input_action_button(
            "close_console_session",
            "Close",
            class_="btn-sm",
            disabled=True,
            title="Stop the selected named session",
        ),
        class_="console-session-controls",
    )


def new_session_modal() -> ui.Tag:
    return ui.modal(
        ui.input_text("new_session_name", "Session name", placeholder="analysis"),
        ui.p(
            "Variables, environments, and SQL connections are separate. "
            "Files in this workspace are shared.",
            class_="small text-muted",
        ),
        title="New Console session",
        footer=ui.TagList(
            ui.modal_button("Cancel"),
            ui.input_action_button(
                "create_console_session", "Create", class_="btn-primary"
            ),
        ),
        easy_close=True,
    )
