use std::sync::atomic::Ordering;
use std::sync::{Arc, Mutex, MutexGuard};

use super::evaluation::{self, Evaluation};
use super::lifecycle::{self, ControlledSendAdmission, WorkerGeneration};
use super::output::SendFailure;
use super::{
    ActiveEvaluation, Client, RResolver, Requirements, Response, WorkerCallbacks, WorkerSpec,
    WorkerState, environment, output, platform,
};

impl Client {
    pub(super) fn start_evaluation(
        &self,
        cell: crate::cell::Cell,
        stdin: Option<String>,
        generation: WorkerGeneration,
        transcript: crate::transcript::Transcript,
        call_id: Option<u64>,
    ) -> Result<(Arc<Evaluation>, evaluation::WaitClaim), String> {
        let mut control_prelude = None;
        self.start_evaluation_admitted(
            cell,
            stdin,
            generation,
            transcript,
            call_id,
            None,
            &mut control_prelude,
            None,
        )
    }

    #[allow(clippy::too_many_arguments)]
    pub(super) fn start_evaluation_admitted(
        &self,
        cell: crate::cell::Cell,
        stdin: Option<String>,
        generation: WorkerGeneration,
        transcript: crate::transcript::Transcript,
        call_id: Option<u64>,
        control: Option<&ControlledSendAdmission>,
        control_prelude: &mut Option<Response>,
        initial_requirements: Option<Requirements>,
    ) -> Result<(Arc<Evaluation>, evaluation::WaitClaim), String> {
        self.ensure_evaluation_admission(&generation, control)?;

        let mut active = self.evaluation()?;
        if let Some(active) = active.as_ref() {
            return Err(active.evaluation.reject_new_cell_message().to_string());
        }
        self.ensure_evaluation_admission(&generation, control)?;
        let startup = self.reserve_worker_startup(&generation)?;
        // Serialize retry cancellation with publishing its accepted cell.
        // Its preparation has already committed and is not rolled back here.
        let retry_admission = match control {
            Some(control) => control.admit_retry_payload("cell")?,
            None => None,
        };
        let (idle_prelude, worker_revision) = self.0.output.take_admission_prelude();
        let evaluation = Arc::new(Evaluation::new(
            generation.clone(),
            transcript,
            call_id,
            self.0.output.clone(),
            control_prelude.take().unwrap_or_default(),
            idle_prelude,
            control.is_some(),
            worker_revision,
        ));
        let wait_claim = evaluation
            .claim()
            .expect("a new evaluation must accept its first wait claim");
        if let Some(stdin) = stdin {
            evaluation
                .submit_stdin(stdin)
                .expect("a new evaluation must accept initial stdin");
        }
        *active = Some(ActiveEvaluation {
            evaluation: evaluation.clone(),
            language: cell.language,
            initial_requirements: Arc::new(Mutex::new(initial_requirements)),
        });
        let initial_requirements = active.as_ref().unwrap().initial_requirements.clone();
        let readiness = self.startup_result();
        drop(retry_admission);
        drop(active);

        let client = self.clone();
        let evaluator = evaluation.clone();
        let evaluation_task = tokio::task::spawn_blocking(move || {
            client.evaluate_blocking(
                cell,
                evaluator,
                generation,
                startup,
                initial_requirements,
                readiness,
            );
        });
        let failed = evaluation.clone();
        let _completion_task = tokio::spawn(async move {
            if let Err(error) = evaluation_task.await {
                failed.complete_cell(Err(SendFailure::from(format!(
                    "worker task failed: {error}"
                ))));
            }
        });
        Ok((evaluation, wait_claim))
    }

    fn ensure_evaluation_admission(
        &self,
        generation: &WorkerGeneration,
        control: Option<&ControlledSendAdmission>,
    ) -> Result<(), String> {
        match control {
            Some(control) => self.ensure_controlled_generation(control, generation),
            None => self.ensure_ordinary_generation(generation),
        }
    }

    pub(super) fn current_evaluation(&self) -> Result<Option<ActiveEvaluation>, String> {
        Ok(self.evaluation()?.clone())
    }

    pub(super) fn evaluation(&self) -> Result<MutexGuard<'_, Option<ActiveEvaluation>>, String> {
        let mut active = self
            .0
            .evaluation
            .lock()
            .map_err(|_| "worker evaluation lock poisoned".to_string())?;
        let reap = active
            .as_ref()
            .map(|active| active.evaluation.reap_delivered_completion())
            .transpose()?
            .unwrap_or(false);
        if reap {
            *active = None;
        }
        Ok(active)
    }

    pub(super) fn admit_send(&self) -> Result<tokio::sync::RwLockReadGuard<'_, ()>, String> {
        self.0
            .preparation
            .try_read()
            .map_err(|_| "session is preparing requirements".to_string())
    }

    pub(super) fn admit_operation(&self) -> Result<tokio::sync::RwLockReadGuard<'_, ()>, String> {
        let operation = self
            .0
            .admission
            .try_read()
            .map_err(|_| "session control is in progress".to_string())?;
        self.admit()?;
        Ok(operation)
    }

    pub(super) fn admit_controlled_operation(&self) -> tokio::sync::RwLockWriteGuard<'_, ()> {
        self.0.admission.blocking_write()
    }

    pub(super) fn admit_preparation(
        &self,
    ) -> Result<tokio::sync::RwLockWriteGuard<'_, ()>, String> {
        match self.0.preparation.try_write() {
            Ok(preparation) => Ok(preparation),
            Err(_) if self.0.preparation.try_read().is_ok() => {
                Err("[requirements not prepared: worker is starting]".to_string())
            }
            Err(_) => Err("session is preparing requirements".to_string()),
        }
    }

    pub(super) async fn write_idle_stdin(
        &self,
        stdin: String,
        generation: WorkerGeneration,
    ) -> Result<(), SendFailure> {
        if stdin.is_empty() {
            return Ok(());
        }
        let client = self.clone();
        tokio::task::spawn_blocking(move || client.write_idle_stdin_blocking(stdin, generation))
            .await
            .map_err(|error| SendFailure::from(format!("worker stdin task failed: {error}")))?
    }

    pub(super) fn write_idle_stdin_blocking(
        &self,
        stdin: String,
        generation: WorkerGeneration,
    ) -> Result<(), SendFailure> {
        self.write_idle_stdin_admitted(stdin, generation, None)
    }

    pub(super) fn write_idle_stdin_admitted(
        &self,
        stdin: String,
        generation: WorkerGeneration,
        control: Option<&ControlledSendAdmission>,
    ) -> Result<(), SendFailure> {
        self.with_worker(&generation, |worker| {
            // Startup may block; accept stdin only after it settles, using the
            // same cancellation boundary as a retry's following cell.
            let _admission = match control {
                Some(control) => match control.admit_retry_payload("stdin") {
                    Ok(admission) => admission,
                    // Cancelled admission is not a worker transport failure.
                    Err(error) => return Ok(Err(SendFailure::from(error))),
                },
                None => None,
            };
            if !stdin.is_empty() {
                self.0.unused_default.store(false, Ordering::Release);
            }
            worker
                .write_stdin(stdin)
                .map(|()| Ok(()))
                .map_err(SendFailure::from)
        })?
    }

    fn evaluate_blocking(
        &self,
        cell: crate::cell::Cell,
        evaluation: Arc<Evaluation>,
        generation: WorkerGeneration,
        startup: Option<Arc<lifecycle::WorkerStartupAdmission>>,
        initial_requirements: Arc<Mutex<Option<Requirements>>>,
        readiness: tokio::sync::watch::Receiver<Option<Result<(), String>>>,
    ) {
        let result = (|| {
            let readiness =
                tokio::runtime::Handle::current().block_on(Self::wait_for_startup(readiness));
            self.ensure_generation(&generation)
                .map_err(SendFailure::from)?;
            if evaluation.admission.outcome()?.is_some() {
                return Ok(());
            }
            if let Err(error) = readiness.and_then(|()| self.validate_cell(&cell)) {
                Response::tool_error(error).recover_to(self.0.output.clone());
                // Completion admits the next cell; release startup ownership first.
                drop(startup);
                evaluation.complete_cell(Ok(()));
                return Ok(());
            }
            if self.take_startup_failure(&generation)? {
                drop(startup);
                evaluation.complete_cell(Ok(()));
                return Ok(());
            }
            if let Some(requirements) = initial_requirements
                .lock()
                .map_err(|_| "initial requirements lock poisoned".to_string())?
                .take()
            {
                self.prepare_cell_requirements(requirements, &generation)?;
            }
            let mut worker = self
                .0
                .worker
                .lock()
                .map_err(|_| SendFailure::from("worker lock poisoned".to_string()))?;
            let result = self.evaluate_with_worker(&mut worker, cell, &evaluation, generation);
            drop(startup);
            // Restart waits for this lock before taking the old output cut.
            // Publish failures before releasing it, including startup failures.
            if let Err(failure) = result {
                evaluation.complete_cell(Err(failure));
            }
            Ok(())
        })();
        if let Err(failure) = result {
            evaluation.complete_cell(Err(failure));
        }
    }

    fn evaluate_with_worker(
        &self,
        worker: &mut WorkerState,
        cell: crate::cell::Cell,
        evaluation: &Arc<Evaluation>,
        generation: WorkerGeneration,
    ) -> Result<(), SendFailure> {
        self.ensure_generation(&generation)
            .map_err(SendFailure::from)?;
        // Only an established worker can publish idle output in the admission
        // gap. A new worker's startup output remains part of this call.
        let capture_idle_prelude = matches!(worker, WorkerState::Running(_));
        self.start_ordinary_worker(worker, &generation)?;
        let WorkerState::Running(running) = worker else {
            return Err(SendFailure::from("worker is not running".to_string()));
        };
        self.0.unused_default.store(false, Ordering::Release);
        let result = running
            .evaluate(cell, evaluation.clone(), capture_idle_prelude)
            .map_err(|message| evaluation.classify_failure(message));
        let mut failure = match result {
            Ok(()) => return Ok(()),
            Err(failure) => failure,
        };
        match self.stop_failed_worker(worker, &generation) {
            Ok(lifecycle::FailedWorkerStop::Stopped(outcome)) => {
                failure = failure.worker_outcome(outcome);
            }
            Ok(lifecycle::FailedWorkerStop::RestartOwnsWorker) => return Err(failure),
            Err(stop_error) => {
                return Err(stop_error.attach_to(failure));
            }
        }

        let replacement_startup = self.0.preparation.blocking_read();
        evaluation.start_replacement(failure.worker_stopped());
        let replacement = self.start_ordinary_worker(worker, &generation);
        // A delivered replacement result must admit the next preparation.
        drop(replacement_startup);
        evaluation.finish_replacement(replacement);
        Ok(())
    }

    fn with_worker<T>(
        &self,
        generation: &WorkerGeneration,
        operation: impl FnOnce(&mut platform::Worker) -> Result<T, SendFailure>,
    ) -> Result<T, SendFailure> {
        self.ensure_generation(generation)
            .map_err(SendFailure::from)?;

        let mut worker = self
            .0
            .worker
            .lock()
            .map_err(|_| SendFailure::from("worker lock poisoned".to_string()))?;
        self.ensure_generation(generation)
            .map_err(SendFailure::from)?;

        self.start_ordinary_worker(&mut worker, generation)?;
        let WorkerState::Running(running) = &mut *worker else {
            unreachable!("worker should be running");
        };
        match operation(running) {
            Ok(result) => Ok(result),
            Err(failure) => match self.stop_failed_worker(&mut worker, generation) {
                Ok(lifecycle::FailedWorkerStop::Stopped(outcome)) => {
                    Err(failure.worker_outcome(outcome).worker_stopped())
                }
                Ok(lifecycle::FailedWorkerStop::RestartOwnsWorker) => Err(failure),
                Err(stop_error) => Err(stop_error.attach_to(failure)),
            },
        }
    }

    fn start_ordinary_worker(
        &self,
        worker: &mut WorkerState,
        generation: &WorkerGeneration,
    ) -> Result<(), SendFailure> {
        self.start_worker(
            worker,
            generation.clone(),
            true,
            |stop_handle| self.register_stop_handle(generation, stop_handle),
            || Ok(()),
        )
        .map_err(|mut failure| {
            if let Err(clear_error) = self.clear_worker_stop_handle(generation) {
                failure.message.push_str(&format!(
                    "; additionally failed to clear the worker shutdown handle: {clear_error}"
                ));
            }
            failure
        })
    }

    pub(super) fn start_worker(
        &self,
        worker: &mut WorkerState,
        generation: WorkerGeneration,
        announce_replacement: bool,
        on_started: impl FnOnce(platform::WorkerShutdownHandle) -> Result<(), String>,
        on_ready: impl FnOnce() -> Result<(), String>,
    ) -> Result<(), SendFailure> {
        let replacing = matches!(&*worker, WorkerState::Stopped);
        if !matches!(&*worker, WorkerState::Running(_)) {
            #[cfg(any(unix, windows))]
            if let Some(preparation) = &*self
                .0
                .resolver_preparation
                .lock()
                .map_err(|_| "preparation lock poisoned".to_string())?
            {
                preparation.check_ready()?;
            }
            let startup = self.reserve_worker_startup(&generation)?;
            let mut environment = match &self.0.environment {
                Some(environment) => Some(
                    environment
                        .lock()
                        .map_err(|_| "worker environment lock poisoned".to_string())?,
                ),
                None => None,
            };
            if let Some(environment) = environment.as_mut()
                && matches!(environment.r_resolver, RResolver::Pending(_))
            {
                let delta =
                    environment::RequirementDelta::calculate(environment, Requirements::default())?;
                let prepared = self
                    .resolve_prestart_environment(&generation, environment, delta)
                    .map_err(|failure| SendFailure::from(failure.into_message()))?;
                let lifecycle = self
                    .0
                    .lifecycle
                    .lock()
                    .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
                lifecycle.ensure_startup(&generation)?;
                **environment = prepared;
                self.publish_requirements(environment);
            }
            let python = environment
                .as_ref()
                .and_then(|environment| environment.python.as_ref());
            let managed_r = environment
                .as_ref()
                .and_then(|environment| environment.r.as_ref());
            let spec = WorkerSpec {
                builtin: environment
                    .as_ref()
                    .is_some_and(|environment| !environment.custom_worker),
                languages: self.0.languages,
                local_runtime: environment
                    .as_ref()
                    .and_then(|environment| environment.local_runtime.as_ref()),
                executable: &self.0.program,
                arguments: &self.0.arguments,
                relay: self.0.relay.as_deref(),
                no_sandbox: self.0.no_sandbox,
                sandbox_settings: &self.0.sandbox_settings,
                startup_source: self.0.startup_source.as_ref(),
                duckdb_extension_directory: self.0.duckdb_extension_directory.as_deref(),
                resolver_matplotlib_cache: self
                    .0
                    .resolver_settings
                    .get("environment")
                    .and_then(|environment| environment.get("MPLCONFIGDIR"))
                    .and_then(serde_json::Value::as_str),
                python,
                managed_r,
                dynamic_resolution: self.0.dynamic_resolution,
                callbacks: WorkerCallbacks {
                    client: self.clone(),
                    generation: generation.clone(),
                },
            };
            // A launched initializer may already have produced external effects.
            // Only explicit restart grants another attempt after confirmed retirement.
            if (self.0.startup_source.is_some()
                && !self.0.startup_permitted.swap(false, Ordering::AcqRel))
                || !generation.r_startup_permitted()
            {
                return Err("startup may have executed; explicit restart required"
                    .to_string()
                    .into());
            }
            if replacing && announce_replacement {
                self.0
                    .output
                    .push_notice_line(output::WORKER_STARTING_NOTICE);
            }
            let running = self
                .0
                .runtime
                .spawn(spec, self.0.output.clone(), on_started, || {
                    on_ready()?;
                    if let Some(startup) = startup.as_ref() {
                        startup.transport_ready.store(true, Ordering::Release);
                    }
                    Ok(())
                })?;
            if let Some(environment) = environment.as_mut() {
                // An external `--worker` must apply its first managed R layer before
                // loading DuckDB; arbitrary preloaded namespaces are not tracked.
                environment.duckdb_r_targets = environment.r.iter().cloned().collect();
            }
            *worker = WorkerState::Running(running);
        }
        Ok(())
    }
}
