#[cfg(any(unix, windows))]
mod activity;
#[cfg(any(unix, windows))]
mod bootstrap;
#[cfg(any(unix, windows))]
mod coordinator;
#[cfg(any(unix, windows))]
mod core;
#[cfg(any(unix, windows))]
mod embedded_r;
#[cfg(any(unix, windows))]
mod input;
#[cfg(any(unix, windows))]
mod interrupt;
#[cfg(any(unix, windows))]
mod r_integration;

// Keep the rest of the crate dependent on the worker facade. The core owns
// runtime-neutral sideband state and host callbacks. The coordinator owns
// language dispatch; activity and bootstrap own native command waiting and
// process setup. The R backend owns its interpreter and native R events.
#[cfg(any(unix, windows))]
pub(crate) use coordinator::run;
#[cfg(unix)]
pub(crate) use core::mark_shutting_down;
#[cfg(any(unix, windows))]
pub(crate) use core::{
    bootstrapping, emit_output, is_shutting_down, publish_plot, publish_python_activation,
    publish_python_activation_failure, publish_r_activation, publish_r_activation_failure,
    record_bootstrap_interrupt, record_worker_failure, resolve_python, resolve_python_version,
    resolve_r,
};
#[cfg(unix)]
pub(crate) use input::python_interrupt_wakeup;
#[cfg(any(unix, windows))]
pub(crate) use input::{PythonInput, read_python_input};
#[cfg(windows)]
pub(crate) use interrupt::with_python_interrupt;
#[cfg(any(unix, windows))]
pub(crate) use interrupt::{
    acknowledge_python_interrupt, begin_python_commit, check_python_selection_interrupt,
    finish_python_commit, inspect_python, install_python_interrupt,
    pending as python_interrupt_pending,
};

#[cfg(not(any(unix, windows)))]
pub(crate) fn run(_bootstrap_runtimes: bool) -> Result<(), Box<dyn std::error::Error>> {
    Err(std::io::Error::new(
        std::io::ErrorKind::Unsupported,
        "embedded R workers are supported only on macOS",
    )
    .into())
}

#[cfg(any(unix, windows))]
pub(crate) use r_integration::{
    available as r_available, ensure_bridge, ensure_initialized as ensure_r,
    initialized as r_initialized,
};
