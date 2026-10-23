//! Drain queued relay output when the ordinary launcher child exits.

#[cfg(unix)]
mod unix;
#[cfg(windows)]
mod windows;

#[cfg(unix)]
pub(crate) use unix::{RelayOutput, forward};
#[cfg(windows)]
pub(crate) use windows::RelayOutput;

/// Create one decoder for each process's diagnostic stream. An empty
/// publication closes only that producer, including its incomplete UTF-8.
pub(crate) type DiagnosticProducer = Box<dyn FnMut(&[u8]) + Send>;
pub(crate) type Diagnostics = std::sync::Arc<dyn Fn() -> DiagnosticProducer + Send + Sync>;
