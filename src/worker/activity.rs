//! Native command waiting; each host keeps its own wait and wakeup ordering.

#[cfg(unix)]
mod unix;
#[cfg(windows)]
mod windows;

#[cfg(unix)]
pub(crate) use unix::observe_stdin_shutdown;
#[cfg(unix)]
pub(super) use unix::wait_for_message;
#[cfg(all(test, unix))]
pub(super) use unix::{CommandReadiness, next_command};
#[cfg(windows)]
pub(crate) use windows::observe_stdin_shutdown;
#[cfg(windows)]
pub(super) use windows::wait_for_message;
