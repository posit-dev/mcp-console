//! Priority results have their own bounded tape and connection identity. They
//! never reserve, drain, or acknowledge an ordinary MCP evaluation.

use std::sync::atomic::{AtomicU64, Ordering};

use super::integrator::IntegratorError;
use super::{Client, OutputTape, Response};
use crate::cell::{Cell, Language};
use crate::worker_protocol::{ExecutionError, PriorityEvent, PriorityExecution};

static NEXT_REQUEST: AtomicU64 = AtomicU64::new(1);

#[derive(Clone, Copy, Default, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum IntegratorExecution {
    #[default]
    Serial,
    Immediate,
}

pub(super) struct Request {
    id: u64,
    connection: super::platform::WorkerShutdownHandle,
    output: OutputTape,
    error: Option<ExecutionError>,
    completed: tokio::sync::oneshot::Sender<Result<Response, IntegratorError>>,
}

impl Client {
    pub(crate) async fn integrator_priority_evaluate(
        &self,
        cell: Cell,
        cancelled: tokio::sync::watch::Receiver<bool>,
    ) -> Result<Response, IntegratorError> {
        let execution = match cell.language {
            Language::R => PriorityExecution::RReentrant,
            Language::Python => PriorityExecution::PythonThread,
            Language::Sql => {
                return Err(IntegratorError::Failed(
                    "immediate execution supports only R and Python".into(),
                ));
            }
        };
        if *cancelled.borrow() {
            return Err(IntegratorError::Cancelled);
        }
        self.startup_result()
            .borrow()
            .clone()
            .ok_or(IntegratorError::Busy)??;
        self.validate_cell(&cell)?;
        let operation = self
            .0
            .admission
            .try_read()
            .map_err(|_| IntegratorError::Busy)?;
        // Exclude explicit preparation through completion. The integrator must
        // prepare dependencies before concurrent work; ordinary runtime callbacks
        // retain their existing environment ownership. Restart can retire us.
        let _preparation = self
            .0
            .preparation
            .try_read()
            .map_err(|_| IntegratorError::Busy)?;
        let result = {
            let lifecycle = self
                .0
                .lifecycle
                .lock()
                .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
            lifecycle.ensure_generation(&lifecycle.generation)?;
            if lifecycle.worker_bootstrapping()? || self.0.startup_failed.load(Ordering::Acquire) {
                return Err(IntegratorError::Busy);
            }
            let connection = lifecycle.worker_connection().ok_or_else(|| {
                IntegratorError::Failed(
                    "initialize the worker with a serial evaluation before priority execution"
                        .into(),
                )
            })?;
            let mut pending = self.0.priority.lock().expect("priority evaluation lock");
            if pending.is_some() {
                return Err(IntegratorError::Busy);
            }
            if *cancelled.borrow() {
                return Err(IntegratorError::Cancelled);
            }
            let id = NEXT_REQUEST.fetch_add(1, Ordering::Relaxed);
            let (completed, result) = tokio::sync::oneshot::channel();
            *pending = Some(Request {
                id,
                connection: connection.clone(),
                output: OutputTape::new(),
                error: None,
                completed,
            });
            if let Err(error) = connection.priority_evaluate(id, execution, cell.source) {
                *pending = None;
                return Err(IntegratorError::Failed(error));
            }
            result
        };
        drop(operation);
        // Socket closure does not abandon accepted work or transfer its output
        // to MCP. The control handler records this result before closing.
        result
            .await
            .map_err(|_| IntegratorError::Failed("priority result owner stopped".into()))?
    }

    pub(super) fn priority_event(
        &self,
        commands: &super::platform::RelayCommandSender,
        request_id: u64,
        event: PriorityEvent,
    ) -> Result<(), String> {
        let mut pending = self.0.priority.lock().expect("priority evaluation lock");
        let Some(request) = pending
            .as_mut()
            .filter(|request| request.connection.owns_commands(commands))
        else {
            return Ok(());
        };
        if request.id != request_id {
            return Err("worker sent a priority event for another request".into());
        }
        match event {
            PriorityEvent::Output { data, diagnostic } => request.output.push_console_text(
                if diagnostic {
                    crate::worker_protocol::ConsoleChannel::Diagnostic
                } else {
                    crate::worker_protocol::ConsoleChannel::Output
                },
                data,
            ),
            PriorityEvent::Image { data, mime_type } => {
                request
                    .output
                    .push_image_with_artifact(data, mime_type, |_, _| Ok(None))?
            }
            PriorityEvent::ExecutionError { error } => {
                request
                    .error
                    .get_or_insert_with(|| super::output::bounded_execution_error(error));
            }
            PriorityEvent::Completed { error } => {
                let request = pending.take().expect("priority request");
                let result = match error {
                    Some(error) => Err(IntegratorError::Failed(error)),
                    None => {
                        let mut response = request.output.take();
                        response.set_execution_error(request.error);
                        Ok(response)
                    }
                };
                let _ = request.completed.send(result);
            }
        }
        Ok(())
    }

    pub(super) fn fail_priority(
        &self,
        commands: &super::platform::RelayCommandSender,
        message: &str,
    ) {
        let mut pending = self.0.priority.lock().expect("priority evaluation lock");
        if pending
            .as_ref()
            .is_some_and(|request| request.connection.owns_commands(commands))
        {
            let request = pending.take().expect("priority request");
            let _ = request
                .completed
                .send(Err(IntegratorError::Failed(message.into())));
        }
    }
}
