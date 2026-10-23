//! Non-reaping direct-child observation, independent of process retirement.
//!
//! Completion reports confirmed exit or an observation error. Owners retain
//! the unreaped child (Unix) or its process handle (Windows) and settle their
//! observer before reaping. Termination and descendant cleanup stay with them.

#[cfg(unix)]
mod unix;
#[cfg(windows)]
mod windows;
#[cfg(unix)]
use unix as native;
#[cfg(windows)]
use windows as native;

#[cfg(unix)]
pub(crate) use unix::{direct_child_has_exited, wait_for_direct_child_exit};

use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::thread::{self, JoinHandle};
use std::time::Duration;

pub(crate) struct ChildExitWaiter {
    completion: Receiver<Result<bool, String>>,
    result: Option<Result<bool, String>>,
    task: Option<JoinHandle<()>>,
    cancel: Option<native::Cancel>,
}

impl ChildExitWaiter {
    pub(crate) fn start(process_id: u32) -> Result<Self, String> {
        Self::start_notifying(process_id, || {})
    }

    /// Wake-only compatibility adapter. Invocation means observation settled;
    /// the owner must retrieve its exit/error result through `wait` or `finish`.
    pub(crate) fn start_notifying(
        process_id: u32,
        notify: impl FnOnce() + Send + 'static,
    ) -> Result<Self, String> {
        Self::start_observing(process_id, move |_| notify())
    }

    pub(crate) fn start_observing(
        process_id: u32,
        notify: impl FnOnce(Result<(), String>) + Send + 'static,
    ) -> Result<Self, String> {
        Self::start_task(process_id, notify, false)
    }

    /// An owner may settle this observer even when process retirement fails.
    /// Cancellation neither signals nor reaps the child and is not exit proof.
    pub(crate) fn start_cancellable(
        process_id: u32,
        notify: impl FnOnce(Result<(), String>) + Send + 'static,
    ) -> Result<Self, String> {
        Self::start_task(process_id, notify, true)
    }

    fn start_task(
        process_id: u32,
        notify: impl FnOnce(Result<(), String>) + Send + 'static,
        cancellable: bool,
    ) -> Result<Self, String> {
        let observer = native::Observer::new(process_id)?;
        let cancellation = cancellable.then(native::cancellation).transpose()?;
        let (cancelled, cancel) = match cancellation {
            Some((cancelled, cancel)) => (Some(cancelled), Some(cancel)),
            None => (None, None),
        };
        let (sender, completion) = mpsc::sync_channel(1);
        let task = thread::Builder::new()
            .name("child exit observer".to_string())
            .spawn(move || {
                let result = match cancelled {
                    Some(cancelled) => observer.wait_cancellable(cancelled),
                    None => observer.wait().map(|()| true),
                };
                let _ = sender.send(result.clone());
                notify(result.and_then(observed_exit));
            })
            .map_err(|error| format!("failed to start child exit observer: {error}"))?;
        Ok(Self {
            completion,
            result: None,
            task: Some(task),
            cancel,
        })
    }

    pub(crate) fn wait(&mut self, timeout: Duration) -> Result<bool, String> {
        if let Some(result) = self.result.as_ref() {
            return result.clone();
        }
        let result = match self.completion.recv_timeout(timeout) {
            Ok(result) => result,
            Err(RecvTimeoutError::Timeout) => return Ok(false),
            Err(RecvTimeoutError::Disconnected) => {
                Err("child exit observer stopped without a result".to_string())
            }
        };
        self.result = Some(result.clone());
        result
    }

    /// Settle observation and its notification before the owner reaps. The
    /// owner must first terminate a child that is not expected to exit itself.
    pub(crate) fn finish(&mut self) -> Result<(), String> {
        self.settle()?
            .then_some(())
            .ok_or_else(|| "child exit observation cancelled".to_string())
    }

    /// Join a cancellable observer, returning any native observation failure
    /// separately from task settlement. A cancelled wait is not an exit error.
    pub(crate) fn cancel_and_finish(&mut self) -> Result<Option<String>, String> {
        drop(self.cancel.take());
        self.receive_result();
        self.join()?;
        Ok(self
            .result
            .as_ref()
            .expect("settled observation")
            .as_ref()
            .err()
            .cloned())
    }

    fn receive_result(&mut self) {
        self.result.get_or_insert_with(|| {
            self.completion
                .recv()
                .unwrap_or_else(|_| Err("child exit observer stopped without a result".to_string()))
        });
    }

    fn join(&mut self) -> Result<(), String> {
        if let Some(task) = self.task.take() {
            task.join()
                .map_err(|_| "child exit observer task failed".to_string())?;
        }
        Ok(())
    }

    fn settle(&mut self) -> Result<bool, String> {
        self.receive_result();
        self.join()?;
        self.result.as_ref().expect("settled observation").clone()
    }
}

fn observed_exit(exited: bool) -> Result<(), String> {
    exited
        .then_some(())
        .ok_or_else(|| "child exit observation cancelled".to_string())
}
