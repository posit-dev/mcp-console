use std::sync::Arc;
use std::time::Instant;

use super::environment::{PreparationIntent, PrepareResult};
use super::evaluation::{self, Evaluation, EvaluationWait};
use super::lifecycle::{self, WorkerGeneration};
use super::output::{self, Response, SendFailure, SendResponse};
use super::{
    Client, INTERRUPT_GRACE, IdleResponseSnapshot, Requirements, SendControl, SendRequest,
    WorkerState,
};

enum PreparedEvaluation {
    Started(Arc<Evaluation>, evaluation::WaitClaim),
    Failed(Response),
}

pub(super) fn send_response_from_wait(wait: EvaluationWait) -> SendResponse {
    match wait {
        EvaluationWait::Running(output) => SendResponse::Running(output),
        EvaluationWait::InputRequested(output) => SendResponse::InputRequested(output),
        EvaluationWait::ReplacementStarting(output) => SendResponse::ReplacementStarting(output),
        EvaluationWait::ReplacementReady(output) => SendResponse::ReplacementReady(output),
        EvaluationWait::Completed(output) => SendResponse::Completed(output),
        EvaluationWait::Reclaimed(output) | EvaluationWait::Restarted(output) => {
            SendResponse::Restarted(output)
        }
    }
}

impl Client {
    /// Interprets preparation, control, evaluation, stdin, and polling for the session.
    pub(crate) async fn send(
        &self,
        request: SendRequest,
        initial_restart: bool,
    ) -> Result<Response, String> {
        let replacing = matches!(request.control, Some(SendControl::Restart));
        let generation = self.status_generation();
        let mut response = self.send_observed(request, initial_restart).await?;
        response.observe_phase(self.status_source(if replacing {
            self.status_generation()
        } else {
            generation
        }));
        Ok(response)
    }

    async fn send_observed(
        &self,
        request: SendRequest,
        initial_restart: bool,
    ) -> Result<Response, String> {
        if self.is_configured()
            && let Some(cell) = &request.cell
        {
            self.validate_cell(cell)?;
        }
        // Admission does not depend on discovery or readiness. The established
        // evaluation slot owns an accepted early cell and all subsequent polls.
        if request.control.is_none()
            && (request.requirements.is_none()
                || (!self.startup_finished() && request.cell.is_some()))
        {
            request.validate(true)?;
            return Ok(
                match self
                    .send_inner(
                        request.cell,
                        request.stdin,
                        request.requirements,
                        request.deadline,
                        request.transcript,
                        request.call_id,
                    )
                    .await
                {
                    Ok(response) => output::render_response(response),
                    Err(failure) => {
                        let mut response = Response::default();
                        response.push_failure(failure);
                        response
                    }
                },
            );
        }
        if matches!(request.control, Some(SendControl::Interrupt))
            && request.cell.is_none()
            && request.requirements.is_none()
            && !self.startup_finished()
        {
            let startup_stdin = request
                .stdin
                .clone()
                .filter(|input| !input.is_empty())
                .map(|input| self.admit().map(|generation| (generation, input)))
                .transpose()?;
            let client = self.clone();
            tokio::task::spawn_blocking(move || client.interrupt_standalone_blocking())
                .await
                .map_err(|error| format!("startup interrupt task failed: {error}"))??;
            if let Some((generation, input)) = startup_stdin {
                #[cfg(any(unix, windows))]
                self.queue_startup_stdin(&generation, input)?;
            }
            tokio::time::sleep(INTERRUPT_GRACE).await;
            if let Some(active) = self.current_evaluation()? {
                let Some(claim) = active.evaluation.claim_for_interrupt()? else {
                    return Ok(output::render_response(SendResponse::Running(
                        active.evaluation.unobserved_progress(),
                    )));
                };
                return Ok(output::render_response(send_response_from_wait(
                    active
                        .evaluation
                        .wait(
                            claim,
                            request.deadline.saturating_duration_since(Instant::now()),
                        )
                        .await?,
                )));
            }
            return Ok(output::render_response(SendResponse::ReplacementStarting(
                self.0.output.take(),
            )));
        }
        if initial_restart
            || !matches!(request.control, Some(SendControl::Restart))
            || !self.is_configured()
        {
            match tokio::time::timeout(
                request.deadline.saturating_duration_since(Instant::now()),
                self.ready(),
            )
            .await
            {
                Ok(Ok(())) => {}
                Ok(Err(error)) => return Ok(self.startup_failure_response(error)),
                Err(_) => {
                    let mut response = output::render_response(SendResponse::ReplacementStarting(
                        self.0.output.take(),
                    ));
                    if request.cell.is_some() {
                        response.push_tool_error(
                            "startup is pending; control was not applied and cell was not run",
                        );
                    }
                    return Ok(response);
                }
            }
        }
        if let Some(requirements) = &request.requirements {
            self.validate_requirements(requirements)?;
        }
        if let Some(cell) = &request.cell {
            self.validate_cell(cell)?;
        }
        request.validate(
            self.0.r_resolution == crate::settings::Resolution::Disabled
                || self.0.dynamic_resolution
                || self.0.python_preparation,
        )?;
        if initial_restart && let Some(response) = self.take_prelaunch_failure()? {
            return Ok(response);
        }
        if let Some(control) = request.control {
            if initial_restart && request.cell.is_none() && request.requirements.is_none() {
                // Joining a retry is observation, not another generation change.
                // Ordinary ownership routes stdin through any accepted evaluation.
                return Ok(
                    match self
                        .send_inner(
                            None,
                            request.stdin,
                            None,
                            request.deadline,
                            request.transcript,
                            request.call_id,
                        )
                        .await
                    {
                        Ok(response) => output::render_response(response),
                        Err(failure) => {
                            let mut response = Response::default();
                            response.push_failure(failure);
                            response
                        }
                    },
                );
            }
            return self
                .send_controlled(control, request, initial_restart)
                .await;
        }
        let SendRequest {
            cell,
            stdin,
            requirements,
            control: _,
            deadline,
            transcript,
            call_id,
        } = request;
        let requirements = requirements.expect("ordinary sends were admitted before readiness");
        if let Some(cell) = cell {
            return Ok(self
                .send_with_requirements(cell, stdin, requirements, deadline, transcript, call_id)
                .await);
        }
        let notice = match self.prepare(requirements).await? {
            PrepareResult::Prepared => "prepared",
            PrepareResult::RestartRequired => "restart required",
            PrepareResult::Failed(response) | PrepareResult::WorkerStopped(response) => {
                return Ok(response);
            }
        };
        let mut response = Response::default();
        response.push_notice(notice);
        Ok(response)
    }

    async fn send_with_requirements(
        &self,
        cell: crate::cell::Cell,
        stdin: Option<String>,
        requirements: Requirements,
        deadline: Instant,
        transcript: crate::transcript::Transcript,
        call_id: Option<u64>,
    ) -> Response {
        let client = self.clone();
        let admission = tokio::task::spawn_blocking(move || {
            client.prepare_and_start_evaluation(cell, stdin, requirements, transcript, call_id)
        })
        .await;
        let admission = match admission {
            Ok(Ok(admission)) => admission,
            Ok(Err(error)) => return output::direct_failure(error),
            Err(error) => {
                return output::direct_failure(format!(
                    "requirement preparation task failed: {error}"
                ));
            }
        };
        let (evaluation, wait_claim) = match admission {
            PreparedEvaluation::Started(evaluation, wait_claim) => (evaluation, wait_claim),
            PreparedEvaluation::Failed(response) => return response,
        };
        let response = match evaluation
            .wait(
                wait_claim,
                deadline.saturating_duration_since(Instant::now()),
            )
            .await
        {
            Ok(wait) => send_response_from_wait(wait),
            Err(error) => return output::direct_failure(error),
        };
        output::render_response(response)
    }

    fn prepare_and_start_evaluation(
        &self,
        cell: crate::cell::Cell,
        stdin: Option<String>,
        requirements: Requirements,
        transcript: crate::transcript::Transcript,
        call_id: Option<u64>,
    ) -> Result<PreparedEvaluation, String> {
        let _operation = self.admit_operation()?;
        let generation = self.admit()?;
        let preparation = match self.admit_preparation() {
            Ok(preparation) => preparation,
            Err(error) => {
                let mut response = Response::default();
                response.push_tool_error(error);
                return Ok(PreparedEvaluation::Failed(response));
            }
        };
        let prepared = match self.prepare_admitted(
            requirements,
            &generation,
            &preparation,
            PreparationIntent::BeforeEvaluation,
        ) {
            Ok(prepared) => prepared,
            Err(error) => {
                let mut response = Response::default();
                response.push_tool_error(error);
                return Ok(PreparedEvaluation::Failed(response));
            }
        };
        let result = match prepared {
            PrepareResult::Prepared => {
                let (evaluation, wait_claim) =
                    self.start_evaluation(cell, stdin, generation, transcript, call_id)?;
                PreparedEvaluation::Started(evaluation, wait_claim)
            }
            PrepareResult::RestartRequired => {
                let mut response = self.0.output.take();
                response.push_tool_error("requirements require session restart; cell was not run");
                self.recover_failed_evaluation(response)
            }
            PrepareResult::Failed(response) | PrepareResult::WorkerStopped(response) => {
                self.recover_failed_evaluation(response)
            }
        };
        drop(preparation);
        Ok(result)
    }

    fn recover_failed_evaluation(&self, mut response: Response) -> PreparedEvaluation {
        response.recover_to(self.0.output.clone());
        PreparedEvaluation::Failed(response)
    }

    async fn send_inner(
        &self,
        cell: Option<crate::cell::Cell>,
        stdin: Option<String>,
        initial_requirements: Option<Requirements>,
        deadline: Instant,
        transcript: crate::transcript::Transcript,
        call_id: Option<u64>,
    ) -> Result<SendResponse, SendFailure> {
        let mut operation = Some(self.admit_operation()?);
        let generation = self.admit()?;
        let mut preparation = None;
        let (evaluation, wait_claim) = match cell {
            Some(cell) => {
                preparation = Some(self.admit_send()?);
                let mut prelude = None;
                self.start_evaluation_admitted(
                    cell,
                    stdin,
                    generation,
                    transcript,
                    call_id,
                    None,
                    &mut prelude,
                    initial_requirements,
                )?
            }
            None => match self.current_evaluation()? {
                Some(active) => {
                    // An accepted early cell may own preparation's write lock.
                    // Poll its evaluation without reserving another operation.
                    self.ensure_ordinary_generation(&generation)?;
                    if !active.evaluation.admission.generation.is(&generation) {
                        return Err("session restarted before the operation began"
                            .to_string()
                            .into());
                    }
                    let wait_claim = active.evaluation.claim()?;
                    if let Some(stdin) = stdin {
                        active.evaluation.submit_stdin(stdin)?;
                    }
                    (active.evaluation, wait_claim)
                }
                None => {
                    preparation = Some(self.admit_send()?);
                    self.ensure_ordinary_generation(&generation)?;
                    let mut startup = self.startup_result();
                    let mut stdin = stdin;
                    #[cfg(any(unix, windows))]
                    if !self.startup_finished()
                        && let Some(input) = stdin.take()
                    {
                        self.queue_startup_stdin(&generation, input)?;
                    }
                    // Waiting for shared startup is observation, not a
                    // reservation against its accepted cell's preparation.
                    if !self.startup_finished() {
                        drop(preparation.take());
                        drop(operation.take());
                    }
                    loop {
                        if let Some(active) = self.current_evaluation()? {
                            self.ensure_ordinary_generation(&generation)?;
                            if !active.evaluation.admission.generation.is(&generation) {
                                return Err("session restarted before the operation began"
                                    .to_string()
                                    .into());
                            }
                            let claim = active.evaluation.claim_after_delivery(deadline).await?;
                            drop(preparation.take());
                            drop(operation.take());
                            return Ok(send_response_from_wait(
                                active
                                    .evaluation
                                    .wait(claim, deadline.saturating_duration_since(Instant::now()))
                                    .await?,
                            ));
                        }
                        if let Some(result) = startup.borrow_and_update().as_ref() {
                            if let Err(error) = result {
                                return Ok(SendResponse::Failed(
                                    self.startup_failure_response(error.clone()),
                                ));
                            }
                            break;
                        }
                        if tokio::time::timeout(
                            deadline.saturating_duration_since(Instant::now()),
                            startup.changed(),
                        )
                        .await
                        .is_err()
                        {
                            return Ok(SendResponse::ReplacementStarting(self.0.output.take()));
                        }
                    }
                    if let Some(stdin) = stdin
                        && let Err(failure) = self.write_idle_stdin(stdin, generation.clone()).await
                    {
                        match self.generation_status(&generation)? {
                            lifecycle::GenerationStatus::CurrentReady => {
                                let active = self.evaluation()?;
                                if active.is_some() {
                                    return Err(failure);
                                }
                                // A later cell must capture this failure in its
                                // idle prelude when it is admitted.
                                self.0.output.push_failure(failure);
                            }
                            lifecycle::GenerationStatus::CurrentClosing
                            | lifecycle::GenerationStatus::Changed => {
                                return Err(failure);
                            }
                        }
                    }
                    return self.take_idle_response(&generation);
                }
            },
        };
        drop(preparation);
        drop(operation);

        Ok(send_response_from_wait(
            evaluation
                .wait(
                    wait_claim,
                    deadline.saturating_duration_since(Instant::now()),
                )
                .await?,
        ))
    }

    /// Drains through an idle-response cut while this call owns the generation.
    pub(super) fn take_idle_response(
        &self,
        generation: &WorkerGeneration,
    ) -> Result<SendResponse, SendFailure> {
        let evaluation = self.evaluation()?;
        if evaluation.is_some() {
            return Err("worker started evaluating before idle output was collected"
                .to_string()
                .into());
        }
        if self.take_startup_failure(generation)? {
            return Ok(SendResponse::Failed(self.0.output.take()));
        }
        drop(evaluation);
        self.ensure_generation(generation)?;

        let mut worker = self
            .0
            .worker
            .lock()
            .map_err(|_| "worker lock poisoned".to_string())?;
        self.ensure_generation(generation)?;
        let snapshot = match &mut *worker {
            WorkerState::Running(running) => running.idle_response_snapshot(&self.0.output)?,
            WorkerState::Initial | WorkerState::Stopped => IdleResponseSnapshot {
                cut: self.0.output.cut(),
                failure: None,
                input_requested: false,
            },
        };
        if let Some(message) = snapshot.failure {
            let mut failure = SendFailure::from(message);
            match self.stop_failed_worker(&mut worker, generation) {
                Ok(lifecycle::FailedWorkerStop::Stopped(outcome)) => {
                    failure = failure.worker_outcome(outcome).worker_stopped();
                }
                Ok(lifecycle::FailedWorkerStop::RestartOwnsWorker) => {}
                Err(stop_error) => {
                    failure = stop_error.attach_to(failure);
                }
            }
            self.0.output.push_failure(failure);
            return Ok(SendResponse::Failed(self.0.output.take()));
        }
        drop(worker);
        let output = self.0.output.drain_through(snapshot.cut);
        Ok(if snapshot.input_requested {
            SendResponse::InputRequested(output)
        } else {
            SendResponse::Idle(output)
        })
    }

    pub(super) fn validate_cell(&self, cell: &crate::cell::Cell) -> Result<(), String> {
        self.validate_language(cell.language)
    }

    pub(super) fn validate_language(&self, language: crate::cell::Language) -> Result<(), String> {
        if self.0.python_only && matches!(language, crate::cell::Language::R) {
            return Err("R cells are unavailable in Python sessions without R".into());
        }
        Ok(())
    }

    pub(super) fn prepare_cell_requirements(
        &self,
        requirements: Requirements,
        generation: &WorkerGeneration,
    ) -> Result<(), SendFailure> {
        self.validate_requirements(&requirements)?;
        // This accepted cell owns its preparation. Admission's short read
        // reservation must settle before its background owner can begin.
        let preparation = self.0.preparation.blocking_write();
        match self.prepare_admitted(
            requirements,
            generation,
            &preparation,
            PreparationIntent::StartupEvaluation,
        )? {
            PrepareResult::Prepared => Ok(()),
            PrepareResult::RestartRequired => {
                Err("requirements require session restart; cell was not run"
                    .to_string()
                    .into())
            }
            PrepareResult::Failed(mut response) | PrepareResult::WorkerStopped(mut response) => {
                response.recover_to(self.0.output.clone());
                Err("requirements were not prepared; cell was not run"
                    .to_string()
                    .into())
            }
        }
    }

    pub(super) fn validate_requirements(&self, requirements: &Requirements) -> Result<(), String> {
        if self.0.python_only {
            if !self.0.python_preparation {
                if !requirements.duckdb.is_empty() {
                    return Err("DuckDB extension preparation is unavailable with a user-selected Python environment; install extensions before starting the session".into());
                }
                return Err(crate::local_runtime::PREPARATION_DISABLED.into());
            }
            if !requirements.r.is_empty() {
                return Err("R requirements are unavailable in Python sessions without R".into());
            }
        }
        if self.0.r_resolution != crate::settings::Resolution::Disabled
            && !self.0.dynamic_resolution
            && !self.0.python_preparation
        {
            return Err(crate::local_runtime::RESOLUTION_UNAVAILABLE.into());
        }
        Ok(())
    }
}
