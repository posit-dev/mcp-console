//! Shared relay policy; native owners retire their worker and I/O before finish.

use std::sync::{Arc, OnceLock};
use std::time::{Duration, Instant};

use super::event_writer::{EventSender, EventWriter};
use crate::relay_protocol::RelayEvent;

pub(super) const WORKER_SHUTDOWN_GRACE: Duration = Duration::from_secs(1);

#[derive(Default)]
pub(super) struct Retirement(Option<Instant>);

impl Retirement {
    pub(super) fn accepts_commands(&self) -> bool {
        self.0.is_none()
    }

    pub(super) fn remaining(&self) -> Option<Duration> {
        self.0
            .map(|deadline| deadline.saturating_duration_since(Instant::now()))
    }

    pub(super) fn start_if_idle(&mut self, deadline: impl FnOnce() -> Instant) -> bool {
        if self.0.is_some() {
            return false;
        }
        // Closing admission and retaining the first budget are one transition.
        // Later EOF or control observations cannot renew worker lifetime.
        self.0 = Some(deadline());
        true
    }

    pub(super) fn accept_shutdown(
        &mut self,
        events: &EventSender,
        deadline: impl FnOnce() -> Instant,
    ) -> bool {
        if !self.start_if_idle(deadline) {
            return true;
        }
        events.send_supervisor(RelayEvent::ShutdownStarted)
    }
}

#[derive(Clone, Default)]
pub(super) struct FirstFailure(Arc<OnceLock<String>>);

impl FirstFailure {
    // Recording is independent of control delivery: a failure discovered while
    // draining still belongs to the terminal sequence after the loop has ended.
    pub(super) fn record(&self, message: String) -> bool {
        self.0.set(message).is_ok()
    }

    pub(super) fn message(&self) -> Option<&String> {
        self.0.get()
    }
}

pub(super) fn finish(
    events: &EventSender,
    event_writer: EventWriter,
    failure: Option<&String>,
    outcome: Option<RelayEvent>,
) -> Result<(), String> {
    // Reader completion and descriptor closure are native boundaries. Publish
    // wire closures only after their owner has drained and joined every reader.
    events.send_supervisor(RelayEvent::StdoutClosed);
    events.send_supervisor(RelayEvent::StderrClosed);
    if let Some(message) = failure {
        events.send_supervisor(RelayEvent::Fatal {
            message: message.clone(),
        });
    }
    events.send_supervisor(RelayEvent::WorkerSidebandClosed);
    if let Some(outcome) = outcome {
        events.send_supervisor(outcome);
    }
    events.finish();
    event_writer.join()
}

pub(super) fn report_startup_failure(
    events: &EventSender,
    mut event_writer: EventWriter,
    error: String,
) -> Result<(), String> {
    // Native setup owns partial resources and retires any spawned child first.
    event_writer.begin_retirement();
    events.send_supervisor(RelayEvent::Fatal {
        message: error.clone(),
    });
    events.finish();
    let mut error = Some(error);
    collect_error(&mut error, event_writer.join());
    Err(error.expect("startup failure should be retained"))
}

// Accumulate cleanup diagnostics without abandoning later owned joins.
pub(super) fn collect_error(current: &mut Option<String>, result: Result<(), String>) {
    let Err(error) = result else {
        return;
    };
    match current {
        Some(current) => current.push_str(&format!("; additionally {error}")),
        None => *current = Some(error),
    }
}
