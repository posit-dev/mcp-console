//! Owned native endpoints for the shared worker-client launch path.
//!
//! Setup owns endpoints, not the child or generation. Install the common
//! process owner before connecting its streams. Dropping the output notifier
//! wakes bounded readers; dropping writer abort independently cancels input.
//! Neither signal confirms child exit or target cleanup.
//!
//! Preparation retains its own native setup under its separate process owner.
//! Migrating that consumer is outside this worker-client extraction.

#[cfg(unix)]
mod unix;
#[cfg(windows)]
mod windows;
#[cfg(unix)]
use unix as native;
#[cfg(windows)]
use windows as native;

pub(super) use native::{PreparedTransport, RelayInput, configure_stdio};
pub(super) type RelayOutput = crate::process_output::RelayOutput;

/// The sole wake handle for relay output and launcher diagnostics.
pub(super) struct OutputNotifier {
    _native: native::Notifier,
}

/// The sole cancellation handle for the generation's command input.
pub(super) struct WriterAbort {
    _native: native::Notifier,
}

pub(super) struct RelayTransport {
    pub(super) input: RelayInput,
    pub(super) output: RelayOutput,
    pub(super) diagnostic_reader: Option<std::thread::JoinHandle<()>>,
}
