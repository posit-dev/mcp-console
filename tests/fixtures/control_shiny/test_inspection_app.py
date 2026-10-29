"""Variable inspection keeps its structured payload separate from diagnostics."""

from playwright.sync_api import expect
from test_app import submit

pytest_plugins = ["test_app"]


def test_r_inspection_diagnostics_do_not_freeze_either_variable_pane(app):
    page, _ = app
    submit(page, "python", 'inspection_value = "python-before-diagnostic"')
    expect(page.locator("#python_variables")).to_contain_text(
        "python-before-diagnostic", timeout=30_000
    )
    submit(
        page,
        "r",
        """str.inspection_diagnostic <- function(object, ...) {
  message(strrep("private-inspection-diagnostic", 1000))
  cat("custom-inspection-preview\\n")
}
inspection_object <- structure(1, class = "inspection_diagnostic")
inspection_table <- data.frame(value = 1)
""",
    )
    expect(page.locator("#r_variables")).to_contain_text(
        "custom-inspection-preview", timeout=15_000
    )
    expect(
        page.locator("#r_tables").get_by_role("button", name="Open inspection_table")
    ).to_be_visible()

    submit(page, "python", 'inspection_value = "python-after-diagnostic"')
    expect(page.locator("#python_variables")).to_contain_text(
        "python-after-diagnostic", timeout=30_000
    )
    submit(
        page,
        "r",
        'str.inspection_diagnostic <- function(object, ...) stop("inspection-method-failed")',
    )
    expect(page.locator("#r_variables")).to_contain_text(
        "inspection-method-failed", timeout=30_000
    )
    expect(page.locator("#r_tables").get_by_role("button")).to_have_count(0)
    submit(page, "python", 'inspection_value = "python-after-inspection-error"')
    expect(page.locator("#python_variables")).to_contain_text(
        "python-after-inspection-error", timeout=30_000
    )
    submit(
        page,
        "r",
        'rm(inspection_object, str.inspection_diagnostic)\ninspection_after <- "r-after-diagnostic"',
    )
    expect(page.locator("#r_variables")).to_contain_text(
        "r-after-diagnostic", timeout=30_000
    )
    expect(page.locator("#r_variables")).not_to_contain_text("inspection_object")
    submit(page, "python", 'inspection_value = "python-after-removal"')
    expect(page.locator("#python_variables")).to_contain_text(
        "python-after-removal", timeout=30_000
    )
    expect(page.locator("#transcript .entry.complete")).to_have_count(7)
    expect(
        page.locator("#transcript .content").filter(
            has_text="private-inspection-diagnostic"
        )
    ).to_have_count(0)
