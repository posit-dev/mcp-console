use std::sync::atomic::Ordering;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use super::environment::{PreparationIntent, PrepareResult};
use super::evaluation::{self, Evaluation, EvaluationWait};
use super::lifecycle::{ControlledSendAdmission, SelectedInterruptTarget, WorkerGeneration};
use super::output::{self, Response, SendFailure, SendResponse};
use super::send::send_response_from_wait;
use super::{
    Client, INTERRUPT_GRACE, Requirements, SendControl, SendRequest, WORKER_SHUTDOWN_GRACE,
};

enum ControlledEvaluation {
    Started(Arc<Evaluation>, evaluation::WaitClaim),
    Returned(Response),
    Observe {
        evaluation: Arc<Evaluation>,
        wait_claim: evaluation::WaitClaim,
        cell_not_run: bool,
    },
}

enum PriorEvaluation {
    None,
    Completed(Response),
    Active(Arc<Evaluation>),
    Failed(Response),
}

enum ControlledStdinFailure {
    ActiveEvaluation(String),
    IdleWorker(SendFailure),
}

/// Dropping the retry's async wait closes pending code and stdin admission.
/// Preparation and already admitted work can finish independently.
struct RetryAdmissionCancellation(Arc<Mutex<bool>>);

impl Drop for RetryAdmissionCancellation {
    fn drop(&mut self) {
        *self.0.lock().expect("retry admission cancellation lock") = true;
    }
}

fn interrupted_cell_not_run_response(wait: EvaluationWait) -> Response {
    let mut response = send_response_from_wait(wait);
    match &mut response {
        SendResponse::Idle(output)
        | SendResponse::Failed(output)
        | SendResponse::Running(output)
        | SendResponse::InputRequested(output)
        | SendResponse::Completed(output)
        | SendResponse::ReplacementStarting(output)
        | SendResponse::ReplacementReady(output)
        | SendResponse::Restarted(output) => {
            output.push_tool_error("interrupted evaluation is still active; cell was not run");
        }
    }
    output::render_response(response)
}

impl Client {
    /// Keep signaling responsive without draining another call's output interval.
    fn unobserved_running_response(&self) -> Result<Response, String> {
        let response = self
            .current_evaluation()?
            .map_or_else(Response::default, |active| {
                active.evaluation.unobserved_progress()
            });
        Ok(output::render_response(SendResponse::Running(response)))
    }

    pub(super) async fn send_controlled(
        &self,
        control: SendControl,
        request: SendRequest,
        initial_restart: bool,
    ) -> Result<Response, String> {
        let deadline = request.deadline;
        let direct_restart_error = matches!(control, SendControl::Restart)
            && request.requirements.is_some()
            && request.cell.is_none();
        let admission_wait = (initial_restart
            && (request.cell.is_some()
                || request
                    .stdin
                    .as_ref()
                    .is_some_and(|stdin| !stdin.is_empty())))
        .then(|| RetryAdmissionCancellation(Arc::new(Mutex::new(false))));
        let retry_cancelled = admission_wait.as_ref().map(|wait| wait.0.clone());
        let client = self.clone();
        let admission = tokio::task::spawn_blocking(move || {
            client.control_and_start_evaluation(control, request, initial_restart, retry_cancelled)
        })
        .await;
        let admission = match admission {
            Ok(Ok(admission)) => admission,
            Ok(Err(error)) if direct_restart_error => return Err(error),
            Ok(Err(error)) => return Ok(output::direct_failure(error)),
            Err(error) => {
                return Ok(output::direct_failure(format!(
                    "session control task failed: {error}"
                )));
            }
        };
        Ok(match admission {
            ControlledEvaluation::Started(evaluation, wait_claim) => {
                match evaluation
                    .wait(
                        wait_claim,
                        deadline.saturating_duration_since(Instant::now()),
                    )
                    .await
                {
                    Ok(wait) => output::render_response(send_response_from_wait(wait)),
                    Err(error) => output::direct_failure(error),
                }
            }
            ControlledEvaluation::Returned(response) => response,
            ControlledEvaluation::Observe {
                evaluation,
                wait_claim,
                cell_not_run,
            } => match evaluation
                .wait(
                    wait_claim,
                    if cell_not_run {
                        Duration::ZERO
                    } else {
                        deadline.saturating_duration_since(Instant::now())
                    },
                )
                .await
            {
                Ok(wait) if cell_not_run => interrupted_cell_not_run_response(wait),
                Ok(wait) => output::render_response(send_response_from_wait(wait)),
                Err(error) => output::direct_failure(error),
            },
        })
    }

    fn control_and_start_evaluation(
        &self,
        requested: SendControl,
        request: SendRequest,
        initial_restart: bool,
        retry_cancelled: Option<Arc<Mutex<bool>>>,
    ) -> Result<ControlledEvaluation, String> {
        let SendRequest {
            cell,
            stdin,
            requirements,
            control: _,
            deadline: _,
            transcript,
            call_id,
        } = request;
        let standalone_interrupt = matches!(requested, SendControl::Interrupt)
            && cell.is_none()
            && requirements.is_none()
            && stdin.as_ref().is_none_or(String::is_empty);
        let mut control = match self.begin_controlled_send() {
            Ok(control) => control,
            Err(_) if standalone_interrupt => {
                // A controlled restart owns admission while its resolver is live.
                // Preserve resolver-first signaling for an empty interrupt.
                self.interrupt_standalone_blocking()?;
                std::thread::sleep(INTERRUPT_GRACE);
                let control = match self.begin_controlled_send() {
                    Ok(control) => control,
                    Err(_) => {
                        // The interrupted control retains output recovery ownership.
                        return Ok(ControlledEvaluation::Returned(
                            self.unobserved_running_response()?,
                        ));
                    }
                };
                let generation = control.generation();
                return self.continue_after_interrupt(
                    &control,
                    generation,
                    cell,
                    requirements,
                    transcript,
                    call_id,
                );
            }
            Err(error) => return Err(error),
        };
        control.retry_cancelled = retry_cancelled;
        match requested {
            SendControl::Interrupt => self.interrupt_and_start_evaluation(
                &control,
                cell,
                stdin,
                requirements,
                transcript,
                call_id,
            ),
            SendControl::Restart => self.restart_and_start_evaluation(
                &control,
                cell,
                stdin,
                requirements,
                transcript,
                call_id,
                initial_restart,
            ),
        }
    }

    fn return_controlled_response(&self, mut response: Response) -> ControlledEvaluation {
        response.recover_to(self.0.output.clone());
        ControlledEvaluation::Returned(response)
    }

    fn return_controlled_failure(
        &self,
        mut response: Response,
        failure: SendFailure,
    ) -> ControlledEvaluation {
        response.extend_logical_region(self.0.output.take());
        response.push_failure(failure);
        self.return_controlled_response(response)
    }

    fn observe_interrupted_evaluation(
        &self,
        evaluation: Arc<Evaluation>,
        cell_not_run: bool,
    ) -> ControlledEvaluation {
        let claim = if cell_not_run {
            evaluation.claim().map(Some)
        } else {
            evaluation.claim_for_interrupt()
        };
        match claim {
            Ok(Some(wait_claim)) => ControlledEvaluation::Observe {
                evaluation,
                wait_claim,
                cell_not_run,
            },
            Ok(None) => ControlledEvaluation::Returned(output::render_response(
                SendResponse::Running(evaluation.unobserved_progress()),
            )),
            Err(error) => {
                let mut response = Response::default();
                if cell_not_run {
                    response.push_tool_error(
                        "interrupted evaluation is still active; cell was not run",
                    );
                } else {
                    response.push_tool_error(error);
                }
                self.return_controlled_response(response)
            }
        }
    }

    fn interrupt_and_start_evaluation(
        &self,
        control: &ControlledSendAdmission,
        cell: Option<crate::cell::Cell>,
        stdin: Option<String>,
        requirements: Option<Requirements>,
        transcript: crate::transcript::Transcript,
        call_id: Option<u64>,
    ) -> Result<ControlledEvaluation, String> {
        let generation = control.generation();
        if self.0.python_only && requirements.is_some() {
            return Err("Python requirements cannot accompany control: interrupt; prepare before first use or with control: restart".into());
        }
        let target = self.interrupt_blocking()?;
        self.ensure_controlled_generation(control, &generation)?;
        match self.submit_controlled_stdin(stdin, &generation, control, &target) {
            Ok(()) => {}
            Err(ControlledStdinFailure::ActiveEvaluation(error)) => return Err(error),
            Err(ControlledStdinFailure::IdleWorker(failure)) => {
                return Ok(self.return_controlled_failure(Response::default(), failure));
            }
        }
        std::thread::sleep(INTERRUPT_GRACE);
        if cell.is_some() {
            // Keep the prior receipt/output for collection even if replacement
            // already finished. Its logical generation alone cannot admit code.
            target.ensure_followup_connection(self)?;
        }
        self.continue_after_interrupt(control, generation, cell, requirements, transcript, call_id)
    }

    fn continue_after_interrupt(
        &self,
        control: &ControlledSendAdmission,
        generation: WorkerGeneration,
        cell: Option<crate::cell::Cell>,
        requirements: Option<Requirements>,
        transcript: crate::transcript::Transcript,
        call_id: Option<u64>,
    ) -> Result<ControlledEvaluation, String> {
        self.ensure_controlled_generation(control, &generation)?;

        // An interrupted preparation retains read admission until its resolver
        // settles. A code-free interrupt must still answer after the grace.
        let _operation = if cell.is_none() {
            match self.0.admission.try_write() {
                Ok(operation) => operation,
                Err(_) => {
                    // The preparation retains output recovery ownership.
                    return Ok(ControlledEvaluation::Returned(
                        self.unobserved_running_response()?,
                    ));
                }
            }
        } else {
            self.admit_controlled_operation()
        };
        let prior = self.prior_evaluation_after_interrupt(&generation, control, cell.is_some())?;
        if cell.is_none() {
            return match prior {
                PriorEvaluation::Active(evaluation) => {
                    Ok(self.observe_interrupted_evaluation(evaluation, false))
                }
                PriorEvaluation::Completed(response) => {
                    Ok(self.return_controlled_response(response))
                }
                PriorEvaluation::Failed(response) => Ok(self.return_controlled_response(response)),
                PriorEvaluation::None => match self.take_idle_response(&generation) {
                    Ok(response) => {
                        Ok(self.return_controlled_response(output::render_response(response)))
                    }
                    Err(failure) => {
                        Ok(self.return_controlled_failure(Response::default(), failure))
                    }
                },
            };
        }
        let mut control_prelude = match prior {
            PriorEvaluation::Active(evaluation) => {
                return Ok(self.observe_interrupted_evaluation(evaluation, true));
            }
            PriorEvaluation::Completed(response) => response,
            PriorEvaluation::None => Response::default(),
            PriorEvaluation::Failed(mut response) => {
                response.push_tool_error(
                    "interrupted evaluation could not be settled; cell was not run",
                );
                return Ok(self.return_controlled_response(response));
            }
        };
        if let Some(requirements) = requirements {
            if let Err(error) = requirements.validate() {
                control_prelude.push_tool_error(error);
                return Ok(self.return_controlled_response(control_prelude));
            }
            let preparation = match self.admit_preparation() {
                Ok(preparation) => preparation,
                Err(error) => {
                    control_prelude.push_tool_error(error);
                    return Ok(self.return_controlled_response(control_prelude));
                }
            };
            let prepared = self.prepare_admitted(
                requirements,
                &generation,
                &preparation,
                PreparationIntent::BeforeEvaluation,
            );
            drop(preparation);
            match prepared {
                Err(error) => {
                    control_prelude.push_tool_error(error);
                    return Ok(self.return_controlled_response(control_prelude));
                }
                Ok(PrepareResult::Prepared) => {}
                Ok(PrepareResult::RestartRequired) => {
                    control_prelude
                        .push_tool_error("requirements require session restart; cell was not run");
                    return Ok(self.return_controlled_response(control_prelude));
                }
                Ok(PrepareResult::Failed(response) | PrepareResult::WorkerStopped(response)) => {
                    control_prelude.extend_logical_region(response);
                    return Ok(self.return_controlled_response(control_prelude));
                }
            }
        }
        self.ensure_controlled_generation(control, &generation)?;
        let mut prelude = Some(control_prelude);
        let (evaluation, wait_claim) = match self.start_evaluation_admitted(
            cell.expect("controlled cell presence was checked"),
            None,
            generation,
            transcript,
            call_id,
            Some(control),
            &mut prelude,
            None,
        ) {
            Ok(started) => started,
            Err(error) => {
                let mut response = prelude.take().unwrap_or_default();
                response.push_tool_error(format!("{error}; cell was not run"));
                return Ok(self.return_controlled_response(response));
            }
        };
        Ok(ControlledEvaluation::Started(evaluation, wait_claim))
    }

    #[allow(clippy::too_many_arguments)]
    fn restart_and_start_evaluation(
        &self,
        control: &ControlledSendAdmission,
        cell: Option<crate::cell::Cell>,
        stdin: Option<String>,
        requirements: Option<Requirements>,
        transcript: crate::transcript::Transcript,
        call_id: Option<u64>,
        initial_restart: bool,
    ) -> Result<ControlledEvaluation, String> {
        let stdin_follows = stdin.as_ref().is_some_and(|stdin| !stdin.is_empty());
        let operation = initial_restart.then(|| self.admit_controlled_operation());
        let restart = if initial_restart {
            let generation = control.generation();
            if let Some(requirements) = requirements {
                let preparation = self.admit_preparation()?;
                match self.prepare_admitted(
                    requirements,
                    &generation,
                    &preparation,
                    PreparationIntent::BeforeEvaluation,
                )? {
                    PrepareResult::Prepared => {}
                    PrepareResult::RestartRequired => {
                        return Err("requirements require session restart; cell was not run".into());
                    }
                    PrepareResult::Failed(response) | PrepareResult::WorkerStopped(response) => {
                        return Ok(self.return_controlled_response(response));
                    }
                }
            }
            if cell.is_none()
                && let Some(active) = self.current_evaluation()?
            {
                // No-op preparation preserves the accepted cell. Its input
                // and observation must not wait for its worker lock.
                self.ensure_controlled_generation(control, &generation)?;
                if !active.evaluation.admission.generation.is(&generation) {
                    return Err("session restarted before the operation began".into());
                }
                let wait_claim = active.evaluation.claim()?;
                if let Some(stdin) = stdin.filter(|stdin| !stdin.is_empty()) {
                    let _admission = match control.admit_retry_payload("stdin") {
                        Ok(admission) => admission,
                        Err(error) => {
                            return Ok(self.return_controlled_response(Response::tool_error(error)));
                        }
                    };
                    active.evaluation.submit_stdin(stdin)?;
                }
                return Ok(ControlledEvaluation::Observe {
                    evaluation: active.evaluation,
                    wait_claim,
                    cell_not_run: false,
                });
            }
            // Cell admission owns its idle output cut. Do not consume output
            // from an evaluation accepted by another caller during readiness.
            let mut response = Response::default();
            response.push_notice("runtime discovery retried");
            if cell.is_none() && !stdin_follows {
                // Changed requirements can retire the unused prewarmed worker.
                // Empty stdin starts its prepared replacement without using it.
                if let Err(failure) =
                    self.write_idle_stdin_blocking(String::new(), generation.clone())
                {
                    return Ok(self.return_controlled_failure(response, failure));
                }
                response = output::project_replacement_ready(response);
            }
            super::lifecycle::RestartAttempt {
                response,
                generation: Some(generation),
            }
        } else {
            self.restart_blocking(
                requirements.unwrap_or_default(),
                WORKER_SHUTDOWN_GRACE,
                cell.is_some() || stdin_follows,
                Some(control),
            )?
        };
        let _operation = operation.unwrap_or_else(|| self.admit_controlled_operation());
        let Some(generation) = restart.generation else {
            let mut response = restart.response;
            if cell.is_some() {
                response.push_tool_error("session restart did not complete; cell was not run");
            }
            return Ok(self.return_controlled_response(response));
        };
        if !initial_restart {
            self.0.startup_failed.store(false, Ordering::Release);
            self.finish_startup(Ok(()));
        }
        self.ensure_controlled_generation(control, &generation)?;
        let Some(cell) = cell else {
            let response = restart.response;
            if let Some(stdin) = stdin.filter(|stdin| !stdin.is_empty()) {
                if let Err(failure) =
                    self.write_idle_stdin_admitted(stdin, generation.clone(), Some(control))
                {
                    return Ok(self.return_controlled_failure(response, failure));
                }
                return Ok(self.return_controlled_response(output::render_response(
                    SendResponse::ReplacementReady(response),
                )));
            }
            return Ok(self.return_controlled_response(response));
        };
        let mut prelude = Some(restart.response);
        let (evaluation, wait_claim) = match self.start_evaluation_admitted(
            cell,
            stdin,
            generation,
            transcript,
            call_id,
            Some(control),
            &mut prelude,
            None,
        ) {
            Ok(started) => started,
            Err(error) => {
                let mut response = prelude.take().unwrap_or_default();
                response.push_tool_error(format!("{error}; cell was not run"));
                return Ok(self.return_controlled_response(response));
            }
        };
        Ok(ControlledEvaluation::Started(evaluation, wait_claim))
    }

    fn submit_controlled_stdin(
        &self,
        stdin: Option<String>,
        generation: &WorkerGeneration,
        control: &ControlledSendAdmission,
        target: &SelectedInterruptTarget,
    ) -> Result<(), ControlledStdinFailure> {
        let Some(stdin) = stdin.filter(|stdin| !stdin.is_empty()) else {
            return Ok(());
        };
        self.ensure_controlled_generation(control, generation)
            .map_err(ControlledStdinFailure::ActiveEvaluation)?;
        if let Some(evaluation) = target.evaluation() {
            if !evaluation.admission.generation.is(generation) {
                return Err(ControlledStdinFailure::ActiveEvaluation(
                    "session restarted before stdin was queued".to_string(),
                ));
            }
            evaluation
                .submit_stdin(stdin)
                .map_err(ControlledStdinFailure::ActiveEvaluation)
        } else if let Some(worker) = target.worker() {
            self.0.unused_default.store(false, Ordering::Release);
            worker
                .write_startup_stdin(stdin)
                .map_err(SendFailure::from)
                .map_err(ControlledStdinFailure::IdleWorker)
        } else if matches!(
            target,
            SelectedInterruptTarget::Resolver { startup: None, .. }
        ) {
            // A resolver selected before any worker startup may be followed by
            // lazy startup in the admitted generation. Preserve its terminal
            // failure; an established target never uses this path.
            self.write_idle_stdin_admitted(stdin, generation.clone(), Some(control))
                .map_err(ControlledStdinFailure::IdleWorker)
        } else {
            Err(ControlledStdinFailure::ActiveEvaluation(
                "worker startup interrupted".into(),
            ))
        }
    }

    fn prior_evaluation_after_interrupt(
        &self,
        generation: &WorkerGeneration,
        control: &ControlledSendAdmission,
        cell_follows: bool,
    ) -> Result<PriorEvaluation, String> {
        let mut active = self.evaluation()?;
        let Some(current) = active.as_ref() else {
            return Ok(PriorEvaluation::None);
        };
        self.ensure_controlled_generation(control, generation)?;
        if !current.evaluation.admission.generation.is(generation) {
            return Err("session restarted before the interrupted evaluation settled".to_string());
        }
        let evaluation = current.evaluation.clone();
        // A code-free interrupt observes through a wait claim, including after
        // completion. It must not retire another send's evaluation or delivery.
        if !cell_follows {
            return Ok(PriorEvaluation::Active(evaluation));
        }
        let outcome = evaluation.admission.outcome()?;
        if outcome.is_none() {
            return Ok(PriorEvaluation::Active(evaluation));
        }
        let reservation = evaluation.reserve_completed_for_handoff()?;
        let Some(reservation) = reservation else {
            return Ok(PriorEvaluation::Active(evaluation));
        };
        *active = None;
        drop(active);
        match self.settle_reserved_evaluation(Some(reservation), false) {
            Ok(response) => Ok(if outcome == Some(super::admission::CellOutcome::Failed) {
                PriorEvaluation::Failed(response)
            } else {
                PriorEvaluation::Completed(response)
            }),
            Err(mut failure) => {
                failure.response.push_tool_error(failure.message);
                Ok(PriorEvaluation::Failed(failure.response))
            }
        }
    }
}
