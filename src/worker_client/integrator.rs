//! Integrator requests share execution admission, but retain separate delivery ownership.

use std::time::{Duration, Instant};

use super::evaluation::Origin;
use super::lifecycle::LifecycleState;
use super::{Client, Content, Response};

#[derive(Debug)]
pub(crate) enum IntegratorError {
    Busy,
    Timeout,
    Cancelled,
    Failed(String),
}

#[derive(Clone, Copy)]
pub(crate) enum IntegratorWait {
    Reject,
    Timeout(Duration),
    Forever,
}

impl From<String> for IntegratorError {
    fn from(message: String) -> Self {
        Self::Failed(message)
    }
}

impl Client {
    /// The caller associates this reply with its outstanding managed-input event.
    /// Select and enqueue under lifecycle ownership so it cannot reach a replacement.
    pub(crate) fn integrator_stdin(&self, input: String) -> Result<(), String> {
        let active = self.evaluation()?;
        let lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        lifecycle.ensure_generation(&lifecycle.generation)?;
        if let Some(active) = active.as_ref() {
            lifecycle.ensure_generation(&active.evaluation.admission.generation)?;
            return active.evaluation.submit_integrator_stdin(input);
        }
        #[cfg(any(unix, windows))]
        if let Some(worker) = lifecycle.worker_connection()
            && worker.is_bootstrapping()?
        {
            return worker.write_startup_stdin(input);
        }
        Err("integrator worker is not waiting for startup input".into())
    }

    pub(crate) async fn integrator_evaluate(
        &self,
        cell: crate::cell::Cell,
        wait: IntegratorWait,
        transcript: crate::transcript::Transcript,
        call_id: Option<u64>,
        mut cancelled: tokio::sync::watch::Receiver<bool>,
    ) -> Result<Response, IntegratorError> {
        if *cancelled.borrow() {
            return Err(IntegratorError::Cancelled);
        }
        let (generation, mut connection) = {
            let lifecycle = self
                .0
                .lifecycle
                .lock()
                .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
            (lifecycle.generation.clone(), lifecycle.worker_connection())
        };
        let deadline = match wait {
            IntegratorWait::Timeout(duration) => Some(Instant::now() + duration),
            IntegratorWait::Reject | IntegratorWait::Forever => None,
        };
        if matches!(wait, IntegratorWait::Reject) {
            self.startup_result()
                .borrow()
                .clone()
                .ok_or(IntegratorError::Busy)??;
        } else {
            pending(deadline, &mut cancelled, self.ready()).await??;
        }
        // Readiness retains successful configuration even when prelaunch failed.
        // MCP owns that failure flag and its output; an integrator must neither
        // consume them nor admit a cell that the evaluator would skip as failed.
        if self
            .0
            .startup_failed
            .load(std::sync::atomic::Ordering::Acquire)
        {
            return Err(IntegratorError::Failed(
                "worker startup failed; code was not run".into(),
            ));
        }
        self.validate_cell(&cell)?;
        let mut first_attempt = true;
        let mut waiting_recorded = false;
        let (evaluation, claim) = loop {
            // Register before inspecting the slot; transport acknowledgment can
            // release it even when no subsequent MCP call reaps the old owner.
            let changed = self.0.admission_changed.notified();
            tokio::pin!(changed);
            changed.as_mut().enable();
            let operation = match wait {
                IntegratorWait::Timeout(_) | IntegratorWait::Forever => {
                    pending(deadline, &mut cancelled, self.0.admission.read()).await?
                }
                IntegratorWait::Reject => self
                    .0
                    .admission
                    .try_read()
                    .map_err(|_| IntegratorError::Busy)?,
            };
            let preparation = match wait {
                IntegratorWait::Timeout(_) | IntegratorWait::Forever => {
                    pending(deadline, &mut cancelled, self.0.preparation.read()).await?
                }
                IntegratorWait::Reject => self
                    .0
                    .preparation
                    .try_read()
                    .map_err(|_| IntegratorError::Busy)?,
            };
            self.ensure_generation(&generation)?;
            let bootstrap = {
                let lifecycle = self
                    .0
                    .lifecycle
                    .lock()
                    .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
                let current = lifecycle.worker_connection();
                // Automatic recovery can replace a process without replacing
                // its logical generation. Queued source still targets the
                // original interpreter state; initial startup may attach once.
                if let Some(expected) = &connection {
                    if current
                        .as_ref()
                        .is_none_or(|current| !current.is_same_connection(expected))
                    {
                        return Err(IntegratorError::Failed("worker connection changed before integrator admission; code was not run".into()));
                    }
                } else {
                    connection = current;
                }
                lifecycle.worker_bootstrapping()?
            };
            let busy = self.current_evaluation()?.is_some() || self.admit().is_err() || bootstrap;
            if !busy {
                if *cancelled.borrow() {
                    return Err(IntegratorError::Cancelled);
                }
                // Tokio timeouts can return an already-ready future after their
                // deadline. Only a zero-duration first attempt is an idle probe.
                if deadline.is_some_and(|deadline| Instant::now() >= deadline)
                    && (!matches!(wait, IntegratorWait::Timeout(Duration::ZERO)) || !first_attempt)
                {
                    return Err(IntegratorError::Timeout);
                }
                let result = self.start_evaluation_owned(
                    crate::cell::Cell {
                        language: cell.language,
                        source: cell.source.clone(),
                    },
                    None,
                    generation.clone(),
                    transcript.clone(),
                    call_id,
                    None,
                    &mut None,
                    None,
                    Origin::Integrator,
                );
                match result {
                    Ok(started) => break started,
                    Err(error) if self.current_evaluation()?.is_none() => return Err(error.into()),
                    Err(_) => {}
                }
            }
            drop(preparation);
            drop(operation);
            if matches!(wait, IntegratorWait::Reject) {
                return Err(IntegratorError::Busy);
            }
            first_attempt = false;
            if !waiting_recorded {
                transcript.protocol(
                    "integrator_waiting",
                    serde_json::json!({"call_id": call_id}),
                );
                waiting_recorded = true;
            }
            pending(deadline, &mut cancelled, changed).await?;
        };

        // Admission is synchronous above. Install the retained owner before the
        // next await, so cancellation cannot leave a sideband cell pollable by MCP.
        let client = self.clone();
        let collector = tokio::spawn(async move {
            let result = evaluation.wait_terminal(claim).await;
            let mut result = result.map(|wait| {
                super::output::render_response(super::send::send_response_from_wait(wait))
            });
            let mut active = client.0.evaluation.lock().expect("worker evaluation lock");
            evaluation.restore_idle_prelude();
            if let Ok(response) = &mut result {
                response.settle_delivery();
            }
            if active
                .as_ref()
                .is_some_and(|current| std::sync::Arc::ptr_eq(&current.evaluation, &evaluation))
            {
                *active = None;
            }
            client.0.admission_changed.notify_waiters();
            result
        });
        collector
            .await
            .map_err(|error| {
                IntegratorError::Failed(format!("integrator evaluation task failed: {error}"))
            })?
            .map_err(IntegratorError::Failed)
    }

    pub(crate) fn integrator_status(&self) -> serde_json::Value {
        let Ok(mut active) = self.0.evaluation.try_lock() else {
            return serde_json::json!({"state": "busy", "idle": false, "evaluation": null});
        };
        if active.as_ref().is_some_and(|active| {
            active
                .evaluation
                .reap_delivered_completion()
                .unwrap_or(false)
        }) {
            *active = None;
            self.0.admission_changed.notify_waiters();
        }
        let Ok(lifecycle) = self.0.lifecycle.try_lock() else {
            return serde_json::json!({"state": "busy", "idle": false, "evaluation": null});
        };
        let (state, evaluation) = match lifecycle.state {
            LifecycleState::Restarting { .. } => ("restarting", serde_json::Value::Null),
            LifecycleState::ShuttingDown { .. } => ("shutting_down", serde_json::Value::Null),
            LifecycleState::Ready => match active.as_ref() {
                Some(active) => {
                    let (state, started) = active.evaluation.observation();
                    (
                        state,
                        serde_json::json!({
                            "origin": if active.evaluation.is_integrator() { "integrator" } else { "mcp" },
                            "language": active.language,
                            "started": started,
                        }),
                    )
                }
                None if !self.startup_finished() || lifecycle.starting() => {
                    ("starting", serde_json::Value::Null)
                }
                None if self
                    .startup_result()
                    .borrow()
                    .as_ref()
                    .is_some_and(Result::is_err)
                    || self
                        .0
                        .startup_failed
                        .load(std::sync::atomic::Ordering::Acquire) =>
                {
                    ("failed", serde_json::Value::Null)
                }
                None if self.0.preparation.try_read().is_err() => {
                    ("preparing", serde_json::Value::Null)
                }
                None => (
                    lifecycle.integrator_phase().unwrap_or("idle"),
                    serde_json::Value::Null,
                ),
            },
        };
        serde_json::json!({"state": state, "idle": state == "idle", "evaluation": evaluation})
    }

    pub(crate) fn integrator_preview(&self) -> Result<Vec<Content>, IntegratorError> {
        let active = self
            .0
            .evaluation
            .try_lock()
            .map_err(|_| IntegratorError::Busy)?;
        match active.as_ref() {
            Some(active) => active.evaluation.preview().ok_or(IntegratorError::Busy),
            None => Ok(self.0.output.preview()),
        }
    }
}

async fn pending<T>(
    deadline: Option<Instant>,
    cancelled: &mut tokio::sync::watch::Receiver<bool>,
    future: impl std::future::Future<Output = T>,
) -> Result<T, IntegratorError> {
    if *cancelled.borrow() {
        return Err(IntegratorError::Cancelled);
    }
    tokio::select! {
        biased;
        _ = cancelled.changed() => Err(IntegratorError::Cancelled),
        result = async {
            match deadline {
                Some(deadline) => tokio::time::timeout_at(tokio::time::Instant::from_std(deadline), future)
                    .await.map_err(|_| IntegratorError::Timeout),
                None => Ok(future.await),
            }
        } => result,
    }
}
