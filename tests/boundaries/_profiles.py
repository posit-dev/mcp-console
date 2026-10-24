"""Small local gate over existing boundary cases and their declared executions.

Keep exact case selectors: adding a case to a suite must not grow this profile.
Full and explicitly selected runs do not use this list.
"""

SMOKE = (
    # MCP and CLI admission, including the canonical tools/list snapshot.
    "client_server/server/test_tools::initializes_and_lists_tools",
    "client_server/server/test_tools::invalid_send_has_no_external_effects",
    "cli/interface/test_help::help",
    "cli/interface/test_warmup::checks_all_languages",
    "cli/interface/test_configure_warmup::starts_one_detached_warmup_with_the_selected_launch",
    "cli/test_local_host::removed_provider_commands_are_unavailable",
    "cli/test_config_overrides::layers_project_then_cli_in_order",
    "cli/test_documentation::preparation_defaults_match_public_configuration",
    "cli/test_documentation::current_companion_links_match_pin",
    # Real runtimes, persistent state, and mixed-language recording with a plot.
    "client_server/r/test_runtime::rejects_incomplete_and_invalid_source",
    "client_server/python/test_runtime::evaluates_cells_in_persistent_reticulate_state",
    "client_server/sql/test_catalog::evaluates_queries_in_a_persistent_catalog",
    "client_server/recording/test_markdown::records_real_mixed_language_session",
    # Interactive input and interruption, in direct and sandboxed execution.
    "client_server/r/test_stdin::routes_idle_and_timed_out_stdin",
    "client_server/lifecycle/test_interrupts::interrupts_running_worker_with_sigint",
    # Cross-platform native filesystem and network policy through the CLI.
    "cli/sandbox/test_writable_roots::writable_roots_augment_default_permissions",
    "cli/sandbox/test_execution::denies_network_access",
)

# Allocation ceilings and repeated overlap retain their original workloads.
# Short cases exercise the functional boundaries in --full; scale is explicit.
STRESS = (
    "client_server/requirements/test_actions::inspection_completes_under_repeated_overlap",
    "client_server/output/test_recovery::cancelled_control_recovery_keeps_bounded_allocations",
    "client_server/output/test_recovery::recovered_silent_cells_keep_bounded_allocations",
    "client_server/output/test_recovery::recovered_image_omissions_keep_bounded_state",
)
