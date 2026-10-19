use std::collections::BTreeSet;

use super::super::lifecycle::{
    FailedWorkerStop, GenerationStatus, LifecycleState, OldGenerationCommitDisposition,
    RequirementChangeState, WorkerGeneration,
};
use super::super::{
    Client, EnvironmentPreparationAdmissionFailure, PreparationOutcome, Response, WorkerState,
};
use super::requirements::{RequirementDelta, Requirements, push_duckdb_r_target};
use super::resolution::EnvironmentResolutionFailure;
use super::state::{Environment, commit_managed_r};

pub(in crate::worker_client) enum PrepareResult {
    Prepared,
    RestartRequired,
    Failed(Response),
    WorkerStopped(Response),
}

#[derive(Clone, Copy)]
pub(in crate::worker_client) enum PreparationIntent {
    Standalone,
    BeforeEvaluation,
    StartupEvaluation,
}

impl Client {
    /// Adds requirements to the managed environment.
    pub(in crate::worker_client) async fn prepare(
        &self,
        requirements: Requirements,
    ) -> Result<PrepareResult, String> {
        let client = self.clone();
        tokio::task::spawn_blocking(move || client.prepare_blocking(requirements))
            .await
            .map_err(|error| format!("requirement preparation task failed: {error}"))?
    }

    fn prepare_blocking(&self, requirements: Requirements) -> Result<PrepareResult, String> {
        let _operation = self.admit_operation()?;
        let generation = self.admit()?;
        let preparation = self.admit_preparation()?;
        self.prepare_admitted(
            requirements,
            &generation,
            &preparation,
            PreparationIntent::Standalone,
        )
    }

    pub(in crate::worker_client) fn prepare_admitted(
        &self,
        requirements: Requirements,
        generation: &WorkerGeneration,
        _preparation: &tokio::sync::RwLockWriteGuard<'_, ()>,
        intent: PreparationIntent,
    ) -> Result<PrepareResult, String> {
        let action = requirements.action;
        let call_id = requirements.call_id;
        let environment = self
            .0
            .environment
            .as_ref()
            .ok_or_else(|| "managed requirements are unavailable".to_string())?;
        let active_operation = self
            .evaluation()?
            .as_ref()
            .map(|active| active.evaluation.clone());
        if let Some(active) =
            active_operation.filter(|_| !matches!(intent, PreparationIntent::StartupEvaluation))
        {
            let environment = match environment.try_lock() {
                Ok(environment) => environment,
                Err(std::sync::TryLockError::WouldBlock) => {
                    return Err(active.reject_preparation_message().to_string());
                }
                Err(std::sync::TryLockError::Poisoned(_)) => {
                    return Err("worker environment lock poisoned".to_string());
                }
            };
            self.ensure_generation(generation)?;
            let delta = RequirementDelta::calculate(&environment, requirements)?;
            if delta.is_empty() {
                return Ok(PrepareResult::Prepared);
            }
            self.require_explicit_restart(&delta)?;
            if self.0.python_only {
                self.validate_live_native_delta(&environment, &delta)?;
            }
            if matches!(intent, PreparationIntent::Standalone)
                && self.requirement_change_state(generation)?
                    == RequirementChangeState::RestartRequired
            {
                return Ok(PrepareResult::RestartRequired);
            }
            return Err(active.reject_preparation_message().to_string());
        }
        // Unchanged declarations do not wait for bootstrap. Recalculate after
        // admission because bootstrap callbacks can change the environment.
        // An idle resolver callback can own this lock. Let operation admission
        // reject that conflict instead of waiting for the callback's resolution.
        match environment.try_lock() {
            Ok(environment) => {
                self.ensure_generation(generation)?;
                if RequirementDelta::calculate(&environment, requirements.clone())?.is_empty() {
                    return Ok(PrepareResult::Prepared);
                }
            }
            Err(std::sync::TryLockError::WouldBlock) => {}
            Err(std::sync::TryLockError::Poisoned(_)) => {
                return Err("worker environment lock poisoned".to_string());
            }
        }
        // Preparation owns admission and no evaluation is active. A delivered
        // completion can precede release of the evaluator's worker lock.
        let mut worker = self
            .0
            .worker
            .lock()
            .map_err(|_| "worker lock poisoned".to_string())?;
        // A worker with R must finish bootstrap before replacement can be
        // selected: suspending callbacks does not prevent native R startup.
        // Python-only workers retain replacement of a blocked bootstrap.
        let mut replace_default = self.0.python_only
            && self
                .0
                .unused_default
                .load(std::sync::atomic::Ordering::Acquire)
            && generation.r_startup_permitted()
            && matches!(&*worker, WorkerState::Running(_));
        let environment_preparation = if let WorkerState::Running(running) = &*worker {
            match running.reserve_environment_preparation(replace_default) {
                Ok(reservation) => Ok(Some(reservation)),
                Err(EnvironmentPreparationAdmissionFailure::Busy(error)) => {
                    return self.finish_environment_resolution_failure(
                        generation,
                        intent,
                        EnvironmentResolutionFailure::Host(error),
                    );
                }
                Err(EnvironmentPreparationAdmissionFailure::Infrastructure(error)) => Err(error),
            }
        } else {
            Ok(None)
        };
        // Non-replacing admission can wait for native R startup. Reconsider
        // the unused-worker exception only after that startup succeeded.
        if environment_preparation.is_ok() {
            replace_default = self
                .0
                .unused_default
                .load(std::sync::atomic::Ordering::Acquire)
                && generation.r_startup_permitted()
                && matches!(&*worker, WorkerState::Running(_));
        }
        let mut environment = environment
            .lock()
            .map_err(|_| "worker environment lock poisoned".to_string())?;
        self.ensure_generation(generation)?;
        let delta = RequirementDelta::calculate(&environment, requirements)?;
        if delta.is_empty() {
            return Ok(PrepareResult::Prepared);
        }
        self.require_explicit_restart(&delta)?;
        if self.0.python_only && !replace_default {
            match &*worker {
                WorkerState::Initial => {}
                WorkerState::Running(_) => {
                    self.validate_live_native_delta(&environment, &delta)?;
                }
                WorkerState::Stopped => {
                    return Err(crate::local_runtime::LIVE_PREPARATION_DISABLED.into());
                }
            }
        }
        let includes_r = delta.r_changed;
        let _environment_preparation = match environment_preparation {
            Ok(reservation) => reservation,
            Err(error) => {
                drop(environment);
                return self.fail_running_preparation(
                    &mut worker,
                    generation,
                    true,
                    error,
                    includes_r,
                );
            }
        };
        if !delta.restart_required
            && self.requirement_change_state(generation)? == RequirementChangeState::RestartRequired
        {
            return Ok(PrepareResult::RestartRequired);
        }
        if !delta.restart_required
            && matches!(*worker, WorkerState::Stopped)
            && matches!(intent, PreparationIntent::Standalone)
        {
            return Ok(PrepareResult::RestartRequired);
        }
        if matches!(*worker, WorkerState::Running(_)) && !replace_default {
            let RequirementDelta {
                duckdb_extensions,
                duckdb_changed,
                python_additions,
                python_candidate,
                restart_required: _,
                r_requirements,
                r_changed,
                ..
            } = delta;
            let managed_r = if r_changed {
                match self.resolve_managed_r(
                    generation,
                    &environment.r_resolver,
                    r_requirements,
                    Vec::new(),
                ) {
                    Ok(managed_r) => Some(managed_r),
                    Err(failure) => {
                        return self
                            .finish_environment_resolution_failure(generation, intent, failure);
                    }
                }
            } else {
                None
            };
            if self.0.python_only && duckdb_changed && python_candidate.is_none() {
                let extensions = duckdb_extensions.iter().cloned().collect::<Vec<_>>();
                if let Err(failure) = self.resolve_python_duckdb_extensions_for_environment(
                    generation,
                    &environment,
                    &extensions,
                    &[],
                ) {
                    return self.finish_environment_resolution_failure(generation, intent, failure);
                }
            } else if !self.0.python_only
                && !duckdb_extensions.is_empty()
                && (duckdb_changed || managed_r.is_some())
            {
                let mut targets = Vec::new();
                if duckdb_changed {
                    targets.extend(environment.duckdb_r_targets.iter().cloned());
                }
                if let Some(managed_r) = managed_r.as_ref() {
                    push_duckdb_r_target(&mut targets, managed_r.clone());
                }
                let duckdb_extensions = duckdb_extensions.iter().cloned().collect::<Vec<_>>();
                if let Err(failure) =
                    self.resolve_duckdb_extensions(generation, &targets, &duckdb_extensions, &[])
                {
                    return self.finish_environment_resolution_failure(generation, intent, failure);
                }
            }
            let python_packages = if python_candidate.is_some() {
                python_additions.into_iter().collect()
            } else {
                Vec::new()
            };
            let duckdb_candidate = duckdb_changed.then_some(duckdb_extensions);
            if python_packages.is_empty() && managed_r.is_none() {
                if let Some(duckdb_candidate) = duckdb_candidate {
                    self.commit_locked_duckdb_environment(
                        generation,
                        &mut environment,
                        duckdb_candidate,
                    )?;
                }
                return Ok(PrepareResult::Prepared);
            }
            drop(environment);
            return self.prepare_running(
                generation,
                worker,
                python_packages,
                python_candidate,
                managed_r,
                duckdb_candidate,
            );
        }

        let resolved = match self.resolve_prestart_environment(generation, &environment, delta) {
            Ok(resolved) => resolved,
            Err(failure) => {
                return self.finish_environment_resolution_failure(generation, intent, failure);
            }
        };

        if replace_default {
            // Resolve the complete candidate first: failed preparation must
            // preserve the prewarmed worker and committed declaration.
            match self
                .stop_failed_worker(&mut worker, generation)
                .map_err(|failure| failure.message)?
            {
                FailedWorkerStop::Stopped(_) => *worker = WorkerState::Initial,
                FailedWorkerStop::RestartOwnsWorker => {
                    return Err(preparation_cancelled(includes_r));
                }
            }
        }

        let mut lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        match lifecycle.state {
            LifecycleState::Ready if lifecycle.generation.is(generation) => {
                lifecycle.processes.resolver = None;
                *environment = resolved;
                self.record_accepted_python(&environment);
                self.publish_requirements(&environment);
                self.record_requirements(action, call_id, &environment);
                Ok(PrepareResult::Prepared)
            }
            LifecycleState::Ready => {
                Err("session restarted before the operation began".to_string())
            }
            LifecycleState::Restarting { .. } => Err("worker is restarting".to_string()),
            LifecycleState::ShuttingDown { .. } => Err("worker is shutting down".to_string()),
        }
    }

    fn require_explicit_restart(&self, delta: &RequirementDelta) -> Result<(), String> {
        if delta.restart_required
            && self.has_live_worker()?
            && !self
                .0
                .unused_default
                .load(std::sync::atomic::Ordering::Acquire)
        {
            return Err("changed requirements require an explicit restart; retry send(control=\"restart\", requirements={\"action\": \"set\", ...}) with the complete declaration (or action=\"reset\")".into());
        }
        Ok(())
    }

    fn validate_live_native_delta(
        &self,
        environment: &Environment,
        delta: &RequirementDelta,
    ) -> Result<(), String> {
        if delta.has_live_python_additions() {
            delta.validate_live_python_additions(environment)?;
        } else if !delta.is_live_duckdb_only() {
            return Err(crate::local_runtime::LIVE_PREPARATION_DISABLED.into());
        }
        Ok(())
    }

    fn finish_environment_resolution_failure(
        &self,
        generation: &WorkerGeneration,
        intent: PreparationIntent,
        failure: EnvironmentResolutionFailure,
    ) -> Result<PrepareResult, String> {
        match (intent, failure) {
            (
                PreparationIntent::BeforeEvaluation,
                EnvironmentResolutionFailure::Host(error)
                | EnvironmentResolutionFailure::Interrupted(error),
            ) => match self.generation_status(generation)? {
                GenerationStatus::CurrentReady => Ok(self.failed_pre_evaluation_resolution(error)),
                GenerationStatus::CurrentClosing | GenerationStatus::Changed => Err(error),
            },
            (_, failure) => Err(failure.into_message()),
        }
    }

    fn commit_locked_duckdb_environment(
        &self,
        generation: &WorkerGeneration,
        environment: &mut Environment,
        duckdb_extensions: BTreeSet<String>,
    ) -> Result<(), String> {
        let lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        match lifecycle.state {
            LifecycleState::Ready if lifecycle.generation.is(generation) => {
                environment.duckdb_extensions = duckdb_extensions;
                self.publish_requirements(environment);
                Ok(())
            }
            LifecycleState::Ready => {
                Err("session restarted before the operation began".to_string())
            }
            LifecycleState::Restarting { .. } => Err("worker is restarting".to_string()),
            LifecycleState::ShuttingDown { .. } => Err("worker is shutting down".to_string()),
        }
    }

    fn prepare_running(
        &self,
        generation: &WorkerGeneration,
        mut worker: std::sync::MutexGuard<'_, WorkerState>,
        python_packages: Vec<String>,
        python_candidate: Option<crate::worker_protocol::PythonRequirementManifest>,
        managed_r: Option<crate::resolver::ManagedR>,
        duckdb_extensions: Option<BTreeSet<String>>,
    ) -> Result<PrepareResult, String> {
        self.ensure_generation(generation)?;
        let WorkerState::Running(running) = &mut *worker else {
            return Err("worker state changed during requirement preparation".to_string());
        };
        let includes_r = managed_r.is_some();
        let includes_python = !python_packages.is_empty();
        if includes_python {
            // The live worker has not accepted `managed_r` yet. Its Python
            // activation commits independently if that later R update fails,
            // so nested resolution uses the retained R environment.
            let client = self.clone();
            let commit_generation = generation.clone();
            let python_duckdb = if includes_r {
                None
            } else {
                duckdb_extensions.clone()
            };
            let commit = Box::new(move |result| {
                let managed = match result {
                    Ok(managed) => managed,
                    Err(error) => {
                        let error = if client.requirement_change_state(&commit_generation)?
                            == RequirementChangeState::RestartRequired
                        {
                            requirement_restart_error(error)
                        } else {
                            error
                        };
                        return Ok(PreparationOutcome::Completed(Err(error)));
                    }
                };
                if client.old_generation_commit_disposition(&commit_generation)?
                    == OldGenerationCommitDisposition::DiscardForReplacement
                {
                    return Ok(PreparationOutcome::DiscardedByReplacement);
                }
                if let Some((managed, configuration)) = managed
                    && client.commit_runtime_python(
                        commit_generation.clone(),
                        managed,
                        configuration,
                    )? == OldGenerationCommitDisposition::DiscardForReplacement
                {
                    return Ok(PreparationOutcome::DiscardedByReplacement);
                }
                if let Some(duckdb_extensions) = python_duckdb
                    && client.commit_running_environment(
                        &commit_generation,
                        None,
                        Some(duckdb_extensions),
                    )? == OldGenerationCommitDisposition::DiscardForReplacement
                {
                    return Ok(PreparationOutcome::DiscardedByReplacement);
                }
                Ok(PreparationOutcome::Completed(Ok(())))
            });
            let result = running.prepare_python(
                python_packages,
                python_candidate.expect("Python additions retain their admitted candidate"),
                includes_r,
                self.0
                    .python_only
                    .then(|| duckdb_extensions.clone())
                    .flatten(),
                commit,
            );
            match result {
                Ok(PreparationOutcome::Completed(Ok(()))) => {}
                Ok(PreparationOutcome::Completed(Err(error))) => {
                    return self.fail_running_preparation(
                        &mut worker,
                        generation,
                        false,
                        error,
                        includes_r,
                    );
                }
                Ok(PreparationOutcome::DiscardedByReplacement) => {
                    return Err(preparation_cancelled(includes_r));
                }
                Err(error) => {
                    return self.fail_running_preparation(
                        &mut worker,
                        generation,
                        true,
                        error,
                        includes_r,
                    );
                }
            }
        }
        if let Some(managed_r) = managed_r {
            let library = managed_r.library().to_path_buf();
            let client = self.clone();
            let commit_generation = generation.clone();
            let commit = Box::new(move |result| match result {
                Ok(()) => client
                    .commit_running_environment(
                        &commit_generation,
                        Some(managed_r),
                        duckdb_extensions,
                    )
                    .map(|disposition| match disposition {
                        OldGenerationCommitDisposition::Commit => {
                            PreparationOutcome::Completed(Ok(()))
                        }
                        OldGenerationCommitDisposition::DiscardForReplacement => {
                            PreparationOutcome::DiscardedByReplacement
                        }
                    }),
                Err(error) => client
                    .require_restart_for_requirement_changes(&commit_generation)
                    .map(|_| PreparationOutcome::Completed(Err(error))),
            });
            let result = match running.prepare_r(&library, commit) {
                Ok(result) => result,
                Err(error) => {
                    return self.fail_running_preparation(
                        &mut worker,
                        generation,
                        true,
                        error,
                        true,
                    );
                }
            };
            return match result {
                PreparationOutcome::Completed(Ok(())) => Ok(PrepareResult::Prepared),
                PreparationOutcome::Completed(Err(error)) => {
                    Ok(self.failed_preparation_response(requirement_restart_error(error)))
                }
                PreparationOutcome::DiscardedByReplacement => Err(preparation_cancelled(true)),
            };
        }
        if !includes_python
            && let Err(error) = self.commit_running_environment(generation, None, duckdb_extensions)
        {
            return self.fail_running_preparation(&mut worker, generation, true, error, includes_r);
        }
        Ok(PrepareResult::Prepared)
    }

    fn fail_running_preparation(
        &self,
        worker: &mut std::sync::MutexGuard<'_, WorkerState>,
        generation: &WorkerGeneration,
        stop_worker: bool,
        error: String,
        includes_r: bool,
    ) -> Result<PrepareResult, String> {
        match self.generation_status(generation)? {
            GenerationStatus::Changed => Err(preparation_cancelled(includes_r)),
            GenerationStatus::CurrentReady => {
                if stop_worker {
                    return match self.stop_failed_worker(worker, generation) {
                        Ok(FailedWorkerStop::Stopped(outcome)) => {
                            self.0.output.push_failure(
                                super::super::output::SendFailure::from(error)
                                    .worker_outcome(outcome)
                                    .worker_stopped(),
                            );
                            Ok(PrepareResult::WorkerStopped(self.0.output.take()))
                        }
                        Ok(FailedWorkerStop::RestartOwnsWorker) => Err(error),
                        Err(stop_error) => {
                            self.0.output.push_failure(
                                stop_error
                                    .attach_to(super::super::output::SendFailure::from(error))
                                    .worker_stopped(),
                            );
                            Ok(PrepareResult::WorkerStopped(self.0.output.take()))
                        }
                    };
                }
                Ok(self.failed_preparation_response(error))
            }
            GenerationStatus::CurrentClosing => Err(error),
        }
    }

    fn failed_preparation_response(&self, error: String) -> PrepareResult {
        let mut response = self.0.output.take();
        response.push_tool_error(error);
        PrepareResult::Failed(response)
    }

    fn failed_pre_evaluation_resolution(&self, error: String) -> PrepareResult {
        let mut response = self.0.output.take_prelude();
        let mut failure = Response::default();
        failure.push_tool_error(error);
        response.extend_cell_after_idle_prelude(failure);
        PrepareResult::Failed(response)
    }

    fn commit_running_environment(
        &self,
        generation: &WorkerGeneration,
        managed_r: Option<crate::resolver::ManagedR>,
        duckdb_extensions: Option<BTreeSet<String>>,
    ) -> Result<OldGenerationCommitDisposition, String> {
        let environment = self
            .0
            .environment
            .as_ref()
            .expect("running managed preparation requires an environment");
        let mut environment = environment
            .lock()
            .map_err(|_| "worker environment lock poisoned".to_string())?;
        let lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        let disposition = lifecycle.old_generation_commit_disposition(generation)?;
        match disposition {
            OldGenerationCommitDisposition::Commit => {
                if let Some(managed_r) = managed_r {
                    commit_managed_r(&mut environment, managed_r);
                }
                if let Some(duckdb_extensions) = duckdb_extensions {
                    environment.duckdb_extensions = duckdb_extensions;
                }
                self.publish_requirements(&environment);
                Ok(disposition)
            }
            OldGenerationCommitDisposition::DiscardForReplacement => Ok(disposition),
        }
    }

    pub(super) fn requirement_change_state(
        &self,
        generation: &WorkerGeneration,
    ) -> Result<RequirementChangeState, String> {
        let lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        match lifecycle.state {
            LifecycleState::Ready if lifecycle.generation.is(generation) => {
                Ok(lifecycle.requirement_changes)
            }
            LifecycleState::Ready => {
                Err("session restarted before the operation began".to_string())
            }
            LifecycleState::Restarting { .. } => Err("worker is restarting".to_string()),
            LifecycleState::ShuttingDown { .. } => Err("worker is shutting down".to_string()),
        }
    }

    pub(in crate::worker_client) fn require_restart_for_requirement_changes(
        &self,
        generation: &WorkerGeneration,
    ) -> Result<OldGenerationCommitDisposition, String> {
        let mut lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        let disposition = lifecycle.old_generation_commit_disposition(generation)?;
        match disposition {
            OldGenerationCommitDisposition::Commit => {
                lifecycle.requirement_changes = RequirementChangeState::RestartRequired;
                Ok(disposition)
            }
            OldGenerationCommitDisposition::DiscardForReplacement => Ok(disposition),
        }
    }
}

fn preparation_cancelled(includes_r: bool) -> String {
    if includes_r {
        "R preparation cancelled by restart".to_string()
    } else {
        "Python preparation cancelled by restart".to_string()
    }
}

fn requirement_restart_error(error: String) -> String {
    format!("{error}; further requirement changes are unavailable until session restart")
}
