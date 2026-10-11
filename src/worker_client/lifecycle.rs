use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, Weak};
use std::time::{Duration, Instant};

use super::environment::{Environment, RequirementDelta};
use super::evaluation::{EvaluationReservation, RestartDelivery};
use super::output::{Response, ResponseAcknowledgment, SendFailure};
use super::{Client, WorkerRetirement, WorkerRetirementFailure, WorkerState, platform};

/// Identifies admitted work and retains incomplete R startup across automatic
/// replacement. Explicit restart creates a fresh identity and startup attempt.
#[derive(Clone)]
pub(crate) struct WorkerGeneration(Arc<AtomicBool>);

impl WorkerGeneration {
    pub(super) fn new() -> Self {
        Self(Arc::new(AtomicBool::new(true)))
    }

    pub(super) fn is(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.0, &other.0)
    }

    pub(super) fn r_initialization(&self, complete: bool) {
        self.0.store(complete, Ordering::Release);
    }

    pub(super) fn r_startup_permitted(&self) -> bool {
        self.0.load(Ordering::Acquire)
    }
}

/// Owns admission and process cancellation for the implicit session.
pub(super) struct LifecycleControl {
    pub(super) state: LifecycleState,
    pub(super) generation: WorkerGeneration,
    controlled_send: Option<Arc<()>>,
    retiring_generation: Option<RetiringGeneration>,
    pub(super) requirement_changes: RequirementChangeState,
    pub(super) processes: ProcessStopHandles,
    startup: Option<WorkerStartup>,
    // Registration transfers interrupt ownership before transport readiness.
    startup_transport: Option<Weak<AtomicBool>>,
}

struct WorkerStartup {
    owner: Weak<WorkerStartupAdmission>,
    interrupted: bool,
}

/// Keeps startup interruptible before and between its resolver processes.
pub(crate) struct WorkerStartupAdmission {
    client: Client,
    generation: WorkerGeneration,
    pub(super) transport_ready: Arc<AtomicBool>,
}

pub(super) enum SelectedInterruptTarget {
    Resolver {
        resolver: crate::resolver::ResolverStopHandle,
        startup: Option<Arc<WorkerStartupAdmission>>,
        worker: Option<platform::WorkerShutdownHandle>,
        evaluation: Option<Arc<super::Evaluation>>,
    },
    Startup {
        _owner: Arc<WorkerStartupAdmission>,
        evaluation: Option<Arc<super::Evaluation>>,
    },
    Worker {
        worker: platform::WorkerShutdownHandle,
        _generation: WorkerGeneration,
        evaluation: Option<Arc<super::Evaluation>>,
    },
    Busy,
    NoTarget,
}

impl SelectedInterruptTarget {
    fn interrupt(&self) -> Result<(), String> {
        match self {
            Self::Resolver { resolver, .. } => resolver.interrupt().map(|_| ()),
            Self::Startup { .. } => Ok(()),
            Self::Worker {
                worker, evaluation, ..
            } => worker.interrupt(evaluation.as_deref()),
            Self::Busy => Err("session control is in progress".into()),
            Self::NoTarget => Err("worker is not running".into()),
        }
    }

    pub(super) fn evaluation(&self) -> Option<&Arc<super::Evaluation>> {
        match self {
            Self::Worker { evaluation, .. }
            | Self::Resolver { evaluation, .. }
            | Self::Startup { evaluation, .. } => evaluation.as_ref(),
            Self::Busy | Self::NoTarget => None,
        }
    }

    pub(super) fn worker(&self) -> Option<&platform::WorkerShutdownHandle> {
        match self {
            Self::Worker { worker, .. } => Some(worker),
            Self::Resolver { worker, .. } => worker.as_ref(),
            _ => None,
        }
    }

    pub(super) fn ensure_followup_connection(&self, client: &Client) -> Result<(), String> {
        let Some(selected) = self.worker() else {
            return Ok(());
        };
        let lifecycle = client
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned")?;
        if lifecycle
            .processes
            .worker
            .as_ref()
            .is_some_and(|current| current.is_same_connection(selected))
        {
            Ok(())
        } else {
            Err("interrupted worker connection changed; cell was not run".into())
        }
    }
}

struct RetiringGeneration {
    generation: WorkerGeneration,
    disposition: OldGenerationCommitDisposition,
}

#[derive(Clone, Copy, Eq, PartialEq)]
pub(super) enum OldGenerationCommitDisposition {
    Commit,
    DiscardForReplacement,
}

impl LifecycleControl {
    pub(super) fn new() -> Self {
        Self {
            state: LifecycleState::Ready,
            generation: WorkerGeneration::new(),
            controlled_send: None,
            retiring_generation: None,
            requirement_changes: RequirementChangeState::Available,
            processes: ProcessStopHandles::default(),
            startup: None,
            startup_transport: None,
        }
    }

    fn start_restart(
        &mut self,
        grace: Duration,
        disposition: OldGenerationCommitDisposition,
    ) -> (ProcessStopHandles, Instant, WorkerGeneration) {
        let deadline = Instant::now() + grace;
        let stop_handles = self.processes.clone();
        stop_handles.reserve_shutdown(deadline);
        self.retiring_generation = Some(RetiringGeneration {
            generation: self.generation.clone(),
            disposition,
        });
        self.state = LifecycleState::Restarting { deadline };
        self.generation = WorkerGeneration::new();
        self.processes.resolver = None;
        self.startup = None;
        self.startup_transport = None;
        (stop_handles, deadline, self.generation.clone())
    }

    pub(super) fn ensure_startup(&self, expected: &WorkerGeneration) -> Result<(), String> {
        if !self.generation.is(expected) {
            return Err("session restarted before the operation began".to_string());
        }
        if matches!(self.state, LifecycleState::ShuttingDown { .. }) {
            return Err("worker is shutting down".to_string());
        }
        if self.startup_interrupted() {
            return Err("worker startup interrupted".to_string());
        }
        Ok(())
    }

    pub(super) fn ensure_generation(&self, expected: &WorkerGeneration) -> Result<(), String> {
        match self.state {
            LifecycleState::Ready if self.generation.is(expected) => Ok(()),
            LifecycleState::Ready => {
                Err("session restarted before the operation began".to_string())
            }
            LifecycleState::Restarting { .. } => Err("worker is restarting".to_string()),
            LifecycleState::ShuttingDown { .. } => Err("worker is shutting down".to_string()),
        }
    }

    fn startup_interrupted(&self) -> bool {
        self.startup
            .as_ref()
            .is_some_and(|startup| startup.interrupted && startup.owner.strong_count() != 0)
    }

    pub(super) fn starting(&self) -> bool {
        self.startup_observation().is_some()
    }

    pub(super) fn startup_observation(&self) -> Option<Weak<AtomicBool>> {
        self.startup_transport
            .as_ref()
            .filter(|ready| {
                ready
                    .upgrade()
                    .is_some_and(|ready| !ready.load(Ordering::Acquire))
            })
            .cloned()
    }

    fn interrupt_startup(&mut self) -> Option<Arc<WorkerStartupAdmission>> {
        let startup = self.startup.as_mut()?;
        let owner = startup.owner.upgrade()?;
        startup.interrupted = true;
        Some(owner)
    }

    pub(super) fn old_generation_commit_disposition(
        &self,
        expected: &WorkerGeneration,
    ) -> Result<OldGenerationCommitDisposition, String> {
        match self.state {
            LifecycleState::Ready if self.generation.is(expected) => {
                Ok(OldGenerationCommitDisposition::Commit)
            }
            LifecycleState::Ready => {
                Err("session restarted before the operation began".to_string())
            }
            LifecycleState::Restarting { .. } => self
                .retiring_generation
                .as_ref()
                .filter(|retiring| retiring.generation.is(expected))
                .map(|retiring| retiring.disposition)
                .ok_or_else(|| "worker is restarting".to_string()),
            LifecycleState::ShuttingDown { .. } => Err("worker is shutting down".to_string()),
        }
    }
}

#[derive(Clone, Copy, Eq, PartialEq)]
/// Carries a deadline only while processes are being stopped.
pub(super) enum LifecycleState {
    Ready,
    Restarting { deadline: Instant },
    ShuttingDown { deadline: Instant },
}

#[derive(Clone, Copy, Eq, PartialEq)]
pub(super) enum RequirementChangeState {
    Available,
    RestartRequired,
}

pub(super) enum GenerationStatus {
    CurrentReady,
    CurrentClosing,
    Changed,
}

pub(super) enum FailedWorkerStop {
    Stopped(Option<super::WorkerProcessOutcome>),
    RestartOwnsWorker,
}

struct RestartContext {
    processes: ProcessStopHandles,
    deadline: Instant,
    generation: WorkerGeneration,
    evaluation: Option<EvaluationReservation>,
}

/// Reserves lifecycle admission for one inline-control `send` call.
pub(super) struct ControlledSendAdmission {
    client: Client,
    token: Arc<()>,
    generation: WorkerGeneration,
    pub(super) retry_cancelled: Option<Arc<Mutex<bool>>>,
}

impl ControlledSendAdmission {
    pub(super) fn generation(&self) -> WorkerGeneration {
        self.generation.clone()
    }

    /// Hold through code/input publication so cancellation cannot overtake admission.
    pub(super) fn admit_retry_payload(
        &self,
        payload: &str,
    ) -> Result<Option<MutexGuard<'_, bool>>, String> {
        let cancelled = self
            .retry_cancelled
            .as_ref()
            .map(|cancelled| cancelled.lock().expect("retry admission cancellation lock"));
        if cancelled.as_ref().is_some_and(|cancelled| **cancelled) {
            return Err(format!("request cancelled before {payload} admission"));
        }
        Ok(cancelled)
    }
}

pub(super) struct RestartFailure {
    pub(super) message: String,
    pub(super) response: Response,
}

pub(super) struct RestartAttempt {
    pub(super) response: Response,
    pub(super) generation: Option<WorkerGeneration>,
}

struct WorkerReplacement {
    response: Response,
    ready: bool,
}

impl RestartFailure {
    fn new(message: String) -> Self {
        Self {
            message,
            response: Response::default(),
        }
    }

    fn with_response(message: String, response: Response) -> Self {
        Self { message, response }
    }
}

#[derive(Clone, Default)]
pub(super) struct ProcessStopHandles {
    worker: Option<platform::WorkerShutdownHandle>,
    pub(super) resolver: Option<crate::resolver::ResolverStopHandle>,
}

impl ProcessStopHandles {
    fn reserve_shutdown(&self, deadline: Instant) {
        if let Some(worker) = &self.worker {
            worker.reserve_shutdown(deadline, deadline);
        }
    }
    fn shutdown(&self, deadline: Instant) -> Result<(), String> {
        let errors = self.request_shutdown(deadline);
        self.finish_shutdown(errors)
    }

    fn request_shutdown(&self, deadline: Instant) -> Vec<String> {
        let mut errors = Vec::new();
        // Queue worker shutdown before resolver cancellation can release a
        // response command onto the retiring relay connection.
        if let Some(worker) = &self.worker {
            worker.request_shutdown(deadline, deadline);
        }
        if let Some(resolver) = self.resolver.as_ref()
            && let Err(error) = resolver.stop()
        {
            errors.push(error);
        }
        errors
    }

    fn finish_shutdown(&self, mut errors: Vec<String>) -> Result<(), String> {
        // The barrier lets the ordered consumer apply failures and finish a
        // cancelled resolver callback before relay retirement is enforced.
        if let Some(worker) = &self.worker
            && let Err(error) = worker.finish_shutdown()
        {
            errors.push(error);
        }
        match errors.split_first() {
            None => Ok(()),
            Some((first, rest)) => Err(rest.iter().fold(first.clone(), |mut error, additional| {
                error.push_str("; additionally ");
                error.push_str(additional);
                error
            })),
        }
    }
}

impl Drop for ControlledSendAdmission {
    fn drop(&mut self) {
        let Ok(mut lifecycle) = self.client.0.lifecycle.lock() else {
            return;
        };
        if lifecycle
            .controlled_send
            .as_ref()
            .is_some_and(|token| Arc::ptr_eq(token, &self.token))
        {
            lifecycle.controlled_send = None;
        }
    }
}

impl Drop for WorkerStartupAdmission {
    fn drop(&mut self) {
        let Ok(mut lifecycle) = self.client.0.lifecycle.lock() else {
            return;
        };
        if lifecycle.generation.is(&self.generation)
            && lifecycle
                .startup
                .as_ref()
                .is_some_and(|startup| std::ptr::eq(startup.owner.as_ptr(), self))
        {
            lifecycle.startup = None;
        }
    }
}

impl Client {
    #[cfg(any(unix, windows))]
    pub(super) fn queue_startup_stdin(
        &self,
        generation: &WorkerGeneration,
        input: String,
    ) -> Result<(), String> {
        if input.is_empty() {
            return Ok(());
        }
        let lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned")?;
        lifecycle.ensure_startup(generation)?;
        if let Some(worker) = &lifecycle.processes.worker {
            self.0
                .unused_default
                .store(false, std::sync::atomic::Ordering::Release);
            worker.write_startup_stdin(input)
        } else {
            self.0
                .startup_stdin
                .lock()
                .map_err(|_| "startup stdin lock poisoned")?
                .push_str(&input);
            Ok(())
        }
    }
    pub(crate) async fn cancel_startup(&self, deadline: Instant) -> Result<(), String> {
        let processes = self.close_lifecycle(deadline)?.unwrap_or_default();
        tokio::task::spawn_blocking(move || {
            let errors = processes.request_shutdown(deadline);
            processes.finish_shutdown(errors)
        })
        .await
        .map_err(|error| format!("startup shutdown task failed: {error}"))?
    }

    pub(super) fn has_live_worker(&self) -> Result<bool, String> {
        Ok(self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned")?
            .processes
            .worker
            .is_some())
    }

    pub(crate) fn reserve_worker_startup(
        &self,
        generation: &WorkerGeneration,
    ) -> Result<Option<Arc<WorkerStartupAdmission>>, String> {
        let mut lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        lifecycle.ensure_startup(generation)?;
        if lifecycle.processes.worker.is_some() {
            return Ok(None);
        }
        if let Some(owner) = lifecycle
            .startup
            .as_ref()
            .and_then(|startup| startup.owner.upgrade())
        {
            return Ok(Some(owner));
        }
        let owner = Arc::new(WorkerStartupAdmission {
            client: self.clone(),
            generation: generation.clone(),
            transport_ready: Arc::new(AtomicBool::new(false)),
        });
        lifecycle.startup_transport = Some(Arc::downgrade(&owner.transport_ready));
        lifecycle.startup = Some(WorkerStartup {
            owner: Arc::downgrade(&owner),
            interrupted: false,
        });
        Ok(Some(owner))
    }

    pub(crate) fn ensure_startup(&self, generation: &WorkerGeneration) -> Result<(), String> {
        self.0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?
            .ensure_startup(generation)
    }

    pub(super) fn interrupt_standalone_blocking(&self) -> Result<(), String> {
        self.select_interrupt_target(false)?.interrupt()
    }

    pub(super) fn interrupt_blocking(&self) -> Result<SelectedInterruptTarget, String> {
        let target = self.select_interrupt_target(true)?;
        target.interrupt()?;
        Ok(target)
    }

    fn select_interrupt_target(&self, controlled: bool) -> Result<SelectedInterruptTarget, String> {
        // Select once under publication/lifecycle locks. The selected connection
        // survives completion, lost acknowledgment and same-generation replacement.
        // No receipt wait or resolver I/O may hold these guards.
        let active = self.evaluation()?;
        let mut lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        let evaluation = active
            .as_ref()
            .filter(|active| {
                active
                    .evaluation
                    .admission
                    .generation
                    .is(&lifecycle.generation)
            })
            .map(|active| active.evaluation.clone());
        let startup = lifecycle.interrupt_startup();
        if startup.is_some()
            && let Some(evaluation) = &evaluation
        {
            evaluation.admission.interrupt(|| Ok(()))?;
        }
        if let Some(resolver) = lifecycle.processes.resolver.clone() {
            return Ok(SelectedInterruptTarget::Resolver {
                resolver,
                startup,
                worker: lifecycle.processes.worker.clone(),
                evaluation,
            });
        }
        if let Some(startup) = startup {
            return Ok(SelectedInterruptTarget::Startup {
                _owner: startup,
                evaluation,
            });
        }
        let active_execution = evaluation
            .as_ref()
            .map(|evaluation| {
                evaluation
                    .admission
                    .outcome()
                    .map(|outcome| outcome.is_none())
            })
            .transpose()?
            .unwrap_or(false);
        let worker_starting = lifecycle
            .processes
            .worker
            .as_ref()
            .map(|worker| worker.is_bootstrapping())
            .transpose()?
            .unwrap_or(false);
        let worker_allowed = controlled
            || lifecycle.controlled_send.is_none()
            || (lifecycle.state == LifecycleState::Ready && (active_execution || worker_starting));
        Ok(if !worker_allowed {
            SelectedInterruptTarget::Busy
        } else if let Some(worker) = lifecycle.processes.worker.clone() {
            SelectedInterruptTarget::Worker {
                worker,
                _generation: lifecycle.generation.clone(),
                evaluation,
            }
        } else {
            SelectedInterruptTarget::NoTarget
        })
    }

    /// Defers the replacement-ready marker when this admission owns a follow-up operation.
    pub(super) fn restart_blocking(
        &self,
        requirements: super::Requirements,
        grace: Duration,
        defer_idle: bool,
        control: Option<&ControlledSendAdmission>,
    ) -> Result<RestartAttempt, String> {
        let mut restart = if requirements.action == super::RequirementsAction::Add
            && requirements.python_version.is_empty()
            && requirements.exclude_newer.is_none()
            && requirements.duckdb.is_empty()
            && requirements.python.is_empty()
            && requirements.r.is_empty()
        {
            self.begin_restart(grace, control)?
        } else {
            self.resolve_and_begin_restart(requirements, grace, control)?
        };
        if let Err(mut error) = restart.processes.shutdown(restart.deadline) {
            let retirement = self.finish_worker_retirement().and_then(|retirement| {
                if matches!(
                    retirement,
                    WorkerRetirement::NeverStarted | WorkerRetirement::AlreadyStopped
                ) && let Some(worker) = &restart.processes.worker
                {
                    // A failed launch may never become Running or may already
                    // be Stopped. Cleanup failure does not settle retained I/O.
                    worker.finish_retirement()?;
                }
                Ok(retirement)
            });
            let retired_worker = matches!(retirement, Ok(WorkerRetirement::Stopped { .. }));
            let outcome = match retirement {
                Ok(WorkerRetirement::Stopped {
                    outcome,
                    failed: true,
                }) => outcome,
                Ok(
                    WorkerRetirement::Stopped { .. }
                    | WorkerRetirement::NeverStarted
                    | WorkerRetirement::AlreadyStopped,
                ) => None,
                Err(retirement_error) => {
                    error.push_str(&format!(
                        "; additionally failed to retire worker I/O: {retirement_error}"
                    ));
                    None
                }
            };
            let mut response =
                match self.settle_reserved_evaluation(restart.evaluation.take(), retired_worker) {
                    Ok(response) => response,
                    Err(settlement) => {
                        error.push_str(&format!(
                            "; additionally failed to settle evaluation response ownership: {}",
                            settlement.message
                        ));
                        settlement.response
                    }
                };
            let transition = self.fail_restart(restart.deadline);
            self.0
                .output
                .push_failure(SendFailure::from(error).worker_outcome(outcome));
            response.extend_logical_region(self.0.output.take());
            return Ok(RestartAttempt {
                response: self.retain_transition_result(transition, response),
                generation: None,
            });
        }
        match self.replace_worker(
            &mut restart.evaluation,
            restart.generation.clone(),
            !defer_idle,
            restart.processes.worker.as_ref(),
        ) {
            Ok(replacement) => {
                let transition = self.finish_restart(&restart.generation);
                let ready = replacement.ready && transition.is_ok();
                Ok(RestartAttempt {
                    response: self.retain_transition_result(transition, replacement.response),
                    generation: ready.then_some(restart.generation),
                })
            }
            Err(mut failure) => {
                if restart.evaluation.is_some() {
                    match self.settle_reserved_evaluation(restart.evaluation.take(), false) {
                        Ok(response) => failure.response.extend(response),
                        Err(settlement) => {
                            failure.message.push_str(&format!(
                                "; additionally failed to settle evaluation response ownership: {}",
                                settlement.message
                            ));
                            failure.response.extend(settlement.response);
                        }
                    }
                }
                let transition = self.fail_restart(restart.deadline);
                self.0
                    .output
                    .push_failure(SendFailure::from(failure.message));
                failure.response.extend(self.0.output.take());
                Ok(RestartAttempt {
                    response: self.retain_transition_result(transition, failure.response),
                    generation: None,
                })
            }
        }
    }

    fn resolve_and_begin_restart(
        &self,
        requirements: super::Requirements,
        grace: Duration,
        control: Option<&ControlledSendAdmission>,
    ) -> Result<RestartContext, String> {
        let generation = match control {
            Some(control) => control.generation(),
            None => self.admit()?,
        };
        let environment = self
            .0
            .environment
            .as_ref()
            .ok_or_else(|| "managed requirements are unavailable".to_string())?;
        let mut environment = environment
            .lock()
            .map_err(|_| "worker environment lock poisoned".to_string())?;
        self.ensure_generation(&generation)?;
        let action = requirements.action;
        let call_id = requirements.call_id;
        let delta = RequirementDelta::calculate(&environment, requirements)?;
        let resolved = if delta.is_empty() {
            if action == super::RequirementsAction::Add {
                drop(environment);
                return self.begin_restart(grace, control);
            }
            // Even an unchanged replacement excludes uncommitted old-worker
            // activations. Keep the environment locked through generation change.
            environment.clone()
        } else {
            self.resolve_prestart_environment(&generation, &environment, delta)
                .map_err(|failure| failure.into_message())?
        };

        let restart = self.commit_environment_and_begin_restart(
            &generation,
            grace,
            &mut environment,
            resolved,
            control,
        )?;
        self.record_requirements(action, call_id, &environment);
        Ok(restart)
    }

    fn commit_environment_and_begin_restart(
        &self,
        expected: &WorkerGeneration,
        grace: Duration,
        environment: &mut Environment,
        resolved: Environment,
        control: Option<&ControlledSendAdmission>,
    ) -> Result<RestartContext, String> {
        let mut evaluation = self.evaluation()?;
        let mut lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        let owns_control = match control {
            Some(control) => lifecycle
                .controlled_send
                .as_ref()
                .is_some_and(|token| Arc::ptr_eq(token, &control.token)),
            None => lifecycle.controlled_send.is_none(),
        };
        match lifecycle.state {
            LifecycleState::Ready if lifecycle.generation.is(expected) && owns_control => {}
            LifecycleState::Ready => {
                return Err("session restarted before the operation began".to_string());
            }
            LifecycleState::Restarting { .. } => {
                return Err("worker is restarting".to_string());
            }
            LifecycleState::ShuttingDown { .. } => {
                return Err("worker is shutting down".to_string());
            }
        }
        lifecycle.processes.resolver = None;
        let evaluation = evaluation
            .take()
            .map(|active| active.evaluation.reserve_for_restart())
            .transpose()?;
        *environment = resolved;
        self.record_accepted_python(environment);
        self.publish_requirements(environment);
        let (processes, deadline, generation) =
            lifecycle.start_restart(grace, OldGenerationCommitDisposition::DiscardForReplacement);
        self.0
            .startup_stdin
            .lock()
            .map_err(|_| "startup stdin lock poisoned")?
            .clear();
        Ok(RestartContext {
            processes,
            deadline,
            generation,
            evaluation,
        })
    }

    /// Crosses the physical worker boundary before starting its replacement.
    ///
    /// Acquiring the worker waits for its active operation to end, and
    /// `finish_retirement()` joins its remaining I/O tasks. No old-worker output
    /// can be published after the stopped notice below.
    fn replace_worker(
        &self,
        evaluation: &mut Option<EvaluationReservation>,
        generation: WorkerGeneration,
        report_idle: bool,
        retiring_worker: Option<&platform::WorkerShutdownHandle>,
    ) -> Result<WorkerReplacement, RestartFailure> {
        let mut worker = self
            .0
            .worker
            .lock()
            .map_err(|_| RestartFailure::new("worker lock poisoned".to_string()))?;
        self.ensure_restarting().map_err(RestartFailure::new)?;
        let retirement = worker.finish_retirement().map_err(RestartFailure::new)?;
        if matches!(
            retirement,
            WorkerRetirement::NeverStarted | WorkerRetirement::AlreadyStopped
        ) && let Some(retiring_worker) = retiring_worker
        {
            // An initial launch can fail before becoming Running; a failed
            // evaluation can already have stopped it. Neither logical state
            // settles the launch's retained I/O result for this replacing caller.
            retiring_worker
                .finish_retirement()
                .map_err(RestartFailure::new)?;
        }
        self.clear_restart_stop_handle()
            .map_err(RestartFailure::new)?;
        if matches!(retirement, WorkerRetirement::NeverStarted) {
            *worker = WorkerState::Stopped;
        }
        let retired_worker = matches!(retirement, WorkerRetirement::Stopped { .. });
        if let WorkerRetirement::Stopped {
            outcome: Some(outcome),
            failed: true,
        } = retirement
        {
            self.0.output.push_notice_line(outcome.diagnostic());
        }
        drop(worker);

        let mut response = self.settle_reserved_evaluation(evaluation.take(), retired_worker)?;

        let mut worker = match self.0.worker.lock() {
            Ok(worker) => worker,
            Err(_) => {
                return Err(RestartFailure::with_response(
                    "worker lock poisoned".to_string(),
                    response,
                ));
            }
        };
        if let Err(error) = self.ensure_restarting() {
            return Err(RestartFailure::with_response(error, response));
        }
        response.push_notice_line(super::output::WORKER_STARTING_NOTICE);

        self.0
            .startup_permitted
            .store(true, std::sync::atomic::Ordering::Release);
        let completion_generation = generation.clone();
        if let Err(mut failure) = self.start_worker(
            &mut worker,
            generation,
            false,
            |stop_handle| self.register_restart_stop_handle(stop_handle),
            || self.finish_restart(&completion_generation),
        ) {
            if let Err(clear_error) = self.clear_restart_stop_handle() {
                failure.message.push_str(&format!(
                    "; additionally failed to clear the worker shutdown handle: {clear_error}"
                ));
            }
            self.0.output.push_failure(failure);
            response.extend(self.0.output.take());
            return Ok(WorkerReplacement {
                response,
                ready: false,
            });
        }
        response.extend(self.0.output.take());
        if report_idle {
            response.push_notice(super::output::WORKER_IDLE_NOTICE);
        }
        Ok(WorkerReplacement {
            response,
            ready: true,
        })
    }

    /// Settles one reserved evaluation before restart publishes its own response.
    pub(super) fn settle_reserved_evaluation(
        &self,
        mut evaluation: Option<EvaluationReservation>,
        retired_worker: bool,
    ) -> Result<Response, RestartFailure> {
        let (old_output, post_completion_output) = evaluation.as_mut().map_or_else(
            || (self.0.output.take(), Response::default()),
            |evaluation| evaluation.take_output(&self.0.output),
        );

        let mut response = Response::default();
        let mut wait_for_send = None;
        let mut output_after_delivery = Response::default();
        let mut waiting_response_includes_stopped = false;
        let mut reclaimed_worker_stopped = false;
        let mut interrupted_notice = None;
        let mut settlement_error = None;
        if let Some(mut evaluation) = evaluation {
            let unfinished = evaluation.unfinished();
            if unfinished {
                interrupted_notice = Some(evaluation.active_stopped_notice());
            }
            if let Some(delivery) = evaluation.take_pending_delivery() {
                wait_for_send = Some(delivery);
                output_after_delivery = old_output;
            } else if evaluation.waiting {
                let mut send_output = old_output;
                if unfinished {
                    send_output.push_notice(evaluation.stopped_notice());
                    if retired_worker {
                        send_output.push_notice(super::output::WORKER_STOPPED_NOTICE);
                        waiting_response_includes_stopped = true;
                    }
                    send_output.mark_error();
                }
                match evaluation.deliver(send_output) {
                    Ok(RestartDelivery::Waiting(acknowledged)) => {
                        wait_for_send = Some(acknowledged);
                    }
                    Ok(RestartDelivery::Unclaimed(output)) => {
                        response.extend(output);
                        reclaimed_worker_stopped = waiting_response_includes_stopped;
                    }
                    Err(failure) => {
                        response.extend(failure.response);
                        reclaimed_worker_stopped = waiting_response_includes_stopped;
                        settlement_error = Some(failure.message);
                    }
                }
            } else {
                response.extend(old_output);
            }
        } else {
            response.extend(old_output);
        }
        if let Some(acknowledged) = wait_for_send {
            match acknowledged.recv().expect(
                "a response delivery sender must return its owned response before disconnecting",
            ) {
                ResponseAcknowledgment::Delivered => {}
                ResponseAcknowledgment::Unclaimed(output) => {
                    response.extend(output);
                    reclaimed_worker_stopped = waiting_response_includes_stopped;
                }
            }
        }
        response.extend_logical_region(output_after_delivery);
        response.extend_logical_region(post_completion_output);
        if let Some(notice) = interrupted_notice {
            response.push_notice(notice);
        }
        if retired_worker && !reclaimed_worker_stopped {
            response.push_notice(super::output::WORKER_STOPPED_NOTICE);
        }
        match settlement_error {
            Some(error) => Err(RestartFailure::with_response(error, response)),
            None => Ok(response),
        }
    }

    fn retain_transition_result(
        &self,
        transition: Result<(), String>,
        mut response: Response,
    ) -> Response {
        if let Err(error) = transition {
            self.0.output.push_failure(SendFailure::from(error));
            response.extend(self.0.output.take());
        }
        response
    }

    fn finish_worker_retirement(&self) -> Result<WorkerRetirement, String> {
        let mut worker = self
            .0
            .worker
            .lock()
            .map_err(|_| "worker lock poisoned".to_string())?;
        worker.finish_retirement()
    }

    pub(crate) fn take_prelaunch_failure(&self) -> Result<Option<super::Response>, String> {
        // Admission and idle collection acquire evaluation before lifecycle.
        // Retain both guards so an accepted cell keeps ownership of its failure.
        let evaluation = self.evaluation()?;
        let lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned")?;
        if lifecycle.state != LifecycleState::Ready || lifecycle.controlled_send.is_some() {
            return Ok(None);
        }
        // An accepted cell owns its startup failure and the accompanying output.
        if evaluation.is_some()
            || !self
                .0
                .startup_failed
                .swap(false, std::sync::atomic::Ordering::AcqRel)
        {
            return Ok(None);
        }
        Ok(Some(self.0.output.take()))
    }

    pub(crate) fn admit(&self) -> Result<WorkerGeneration, String> {
        let lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        match lifecycle.state {
            LifecycleState::Ready if lifecycle.controlled_send.is_none() => {
                Ok(lifecycle.generation.clone())
            }
            LifecycleState::Ready => Err("session control is in progress".to_string()),
            LifecycleState::Restarting { .. } => Err("worker is restarting".to_string()),
            LifecycleState::ShuttingDown { .. } => Err("worker is shutting down".to_string()),
        }
    }

    pub(super) fn begin_controlled_send(&self) -> Result<ControlledSendAdmission, String> {
        let mut lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        match lifecycle.state {
            LifecycleState::Ready if lifecycle.controlled_send.is_none() => {}
            LifecycleState::Ready => {
                return Err("session control is already in progress".to_string());
            }
            LifecycleState::Restarting { .. } => {
                return Err("worker is restarting".to_string());
            }
            LifecycleState::ShuttingDown { .. } => {
                return Err("worker is shutting down".to_string());
            }
        }
        let token = Arc::new(());
        lifecycle.controlled_send = Some(token.clone());
        Ok(ControlledSendAdmission {
            client: self.clone(),
            token,
            generation: lifecycle.generation.clone(),
            retry_cancelled: None,
        })
    }

    pub(super) fn generation_status(
        &self,
        expected: &WorkerGeneration,
    ) -> Result<GenerationStatus, String> {
        let lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        Ok(if !lifecycle.generation.is(expected) {
            GenerationStatus::Changed
        } else if lifecycle.state == LifecycleState::Ready {
            GenerationStatus::CurrentReady
        } else {
            GenerationStatus::CurrentClosing
        })
    }

    pub(super) fn ensure_generation(&self, expected: &WorkerGeneration) -> Result<(), String> {
        let lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        lifecycle.ensure_generation(expected)
    }

    pub(super) fn ensure_ordinary_generation(
        &self,
        expected: &WorkerGeneration,
    ) -> Result<(), String> {
        let lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        match lifecycle.state {
            LifecycleState::Ready
                if lifecycle.generation.is(expected) && lifecycle.controlled_send.is_none() =>
            {
                Ok(())
            }
            LifecycleState::Ready if !lifecycle.generation.is(expected) => {
                Err("session restarted before the operation began".to_string())
            }
            LifecycleState::Ready => Err("session control is in progress".to_string()),
            LifecycleState::Restarting { .. } => Err("worker is restarting".to_string()),
            LifecycleState::ShuttingDown { .. } => Err("worker is shutting down".to_string()),
        }
    }

    pub(super) fn ensure_controlled_generation(
        &self,
        admission: &ControlledSendAdmission,
        expected: &WorkerGeneration,
    ) -> Result<(), String> {
        let lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        let owns_control = lifecycle
            .controlled_send
            .as_ref()
            .is_some_and(|token| Arc::ptr_eq(token, &admission.token));
        match lifecycle.state {
            LifecycleState::Ready if owns_control && lifecycle.generation.is(expected) => Ok(()),
            LifecycleState::Ready if !lifecycle.generation.is(expected) => {
                Err("session restarted before the operation began".to_string())
            }
            LifecycleState::Ready => Err("session control admission changed".to_string()),
            LifecycleState::Restarting { .. } => Err("worker is restarting".to_string()),
            LifecycleState::ShuttingDown { .. } => Err("worker is shutting down".to_string()),
        }
    }

    fn ensure_restarting(&self) -> Result<(), String> {
        let lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        match lifecycle.state {
            LifecycleState::Restarting { .. } => Ok(()),
            LifecycleState::ShuttingDown { .. } => Err("worker is shutting down".to_string()),
            LifecycleState::Ready => Err("worker restart state changed".to_string()),
        }
    }

    fn begin_restart(
        &self,
        grace: Duration,
        control: Option<&ControlledSendAdmission>,
    ) -> Result<RestartContext, String> {
        let mut evaluation = self.evaluation()?;
        let mut lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        match lifecycle.state {
            LifecycleState::Restarting { .. } => {
                return Err("worker is already restarting".to_string());
            }
            LifecycleState::ShuttingDown { .. } => {
                return Err("worker is shutting down".to_string());
            }
            LifecycleState::Ready
                if lifecycle.processes.worker.is_none()
                    && lifecycle.processes.resolver.is_some()
                    && self.0.preparation.try_read().is_err() =>
            {
                // Explicit preconditions own preparation admission. A resolver
                // preparing lazy worker startup belongs to the operation that
                // restart is retiring, even before a worker process exists.
                return Err("requirement preparation is still running".to_string());
            }
            LifecycleState::Ready => {}
        }
        let owns_control = match control {
            Some(control) => lifecycle
                .controlled_send
                .as_ref()
                .is_some_and(|token| Arc::ptr_eq(token, &control.token)),
            None => lifecycle.controlled_send.is_none(),
        };
        if !owns_control {
            return Err("session control is in progress".to_string());
        }
        let evaluation = evaluation
            .take()
            .map(|active| active.evaluation.reserve_for_restart())
            .transpose()?;
        let (processes, deadline, generation) =
            lifecycle.start_restart(grace, OldGenerationCommitDisposition::Commit);
        self.0
            .startup_stdin
            .lock()
            .map_err(|_| "startup stdin lock poisoned")?
            .clear();
        Ok(RestartContext {
            processes,
            deadline,
            generation,
            evaluation,
        })
    }

    fn finish_restart(&self, expected: &WorkerGeneration) -> Result<(), String> {
        let mut lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        if !lifecycle.generation.is(expected) {
            return Ok(());
        }
        match lifecycle.state {
            LifecycleState::Restarting { .. } => {
                lifecycle.state = LifecycleState::Ready;
                lifecycle.retiring_generation = None;
                lifecycle.requirement_changes = RequirementChangeState::Available;
                Ok(())
            }
            LifecycleState::ShuttingDown { .. } => Err("worker is shutting down".to_string()),
            LifecycleState::Ready => Ok(()),
        }
    }

    fn fail_restart(&self, deadline: Instant) -> Result<(), String> {
        let mut lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        match lifecycle.state {
            LifecycleState::Restarting { .. } => {
                lifecycle.state = LifecycleState::ShuttingDown { deadline };
                lifecycle.retiring_generation = None;
                Ok(())
            }
            LifecycleState::ShuttingDown { .. } => Ok(()),
            LifecycleState::Ready => Err("worker restart state changed".to_string()),
        }
    }

    pub(super) fn stop_failed_worker(
        &self,
        worker: &mut WorkerState,
        expected: &WorkerGeneration,
    ) -> Result<FailedWorkerStop, WorkerRetirementFailure> {
        let lifecycle = self.0.lifecycle.lock().map_err(|_| {
            WorkerRetirementFailure::from("worker lifecycle lock poisoned".to_string())
        })?;
        if lifecycle.state != LifecycleState::Ready || !lifecycle.generation.is(expected) {
            return Ok(FailedWorkerStop::RestartOwnsWorker);
        }
        if !matches!(worker, WorkerState::Running(_)) {
            return Err(WorkerRetirementFailure::from(
                "failed worker was not running".to_string(),
            ));
        }
        let processes = lifecycle.processes.clone();
        let deadline = Instant::now();
        // The failed-worker path retains its unconditional relay drainage
        // allowance. Reserve before releasing admission, then cancel the real
        // callback outside lifecycle before joining its dispatcher.
        if let Some(handle) = &processes.worker {
            handle.reserve_failed_shutdown(deadline);
        }
        drop(lifecycle);
        let requested = processes.request_shutdown(deadline);
        let retirement = worker.stop_failed();
        let mut lifecycle = self.0.lifecycle.lock().map_err(|_| {
            WorkerRetirementFailure::from("worker lifecycle lock poisoned".to_string())
        })?;
        // Restart/EOF may now have joined the same operation. They own the
        // transition if generation/admission changed during teardown.
        let current = lifecycle.generation.is(expected) && lifecycle.state == LifecycleState::Ready;
        if !current {
            return retirement.map(|retirement| {
                // This caller consumed the outcome before marking the worker
                // Stopped. Retain it in the old output region before restart
                // settles that region; its worker view is now AlreadyStopped.
                if let WorkerRetirement::Stopped {
                    outcome: Some(outcome),
                    ..
                } = retirement
                {
                    self.0.output.push_notice_line(outcome.diagnostic());
                }
                FailedWorkerStop::RestartOwnsWorker
            });
        }
        let outcome = match retirement {
            Ok(WorkerRetirement::Stopped { outcome, .. }) => outcome,
            Ok(WorkerRetirement::NeverStarted | WorkerRetirement::AlreadyStopped) => {
                unreachable!("a running failed worker should retire")
            }
            Err(error) => {
                if error.can_replace {
                    // A failed cleanup remains an error, but the old launcher's
                    // exit and retired I/O permit a new logical generation.
                    lifecycle.processes.worker = None;
                } else {
                    lifecycle.state = LifecycleState::ShuttingDown {
                        deadline: Instant::now(),
                    };
                }
                return Err(error);
            }
        };
        lifecycle.processes.worker = None;
        if !requested.is_empty() {
            return Err(WorkerRetirementFailure::new(
                requested.join("; additionally "),
                outcome,
            ));
        }
        Ok(FailedWorkerStop::Stopped(outcome))
    }

    pub(super) fn register_stop_handle(
        &self,
        expected: &WorkerGeneration,
        handle: platform::WorkerShutdownHandle,
    ) -> Result<(), String> {
        let (deadline, message) = {
            let mut lifecycle = self
                .0
                .lifecycle
                .lock()
                .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
            match lifecycle.state {
                LifecycleState::Ready if lifecycle.generation.is(expected) => {
                    if lifecycle.startup_interrupted() {
                        (Instant::now(), "worker startup interrupted")
                    } else {
                        lifecycle.processes.worker = Some(handle.clone());
                        lifecycle.startup = None;
                        #[cfg(any(unix, windows))]
                        {
                            let input = std::mem::take(
                                &mut *self
                                    .0
                                    .startup_stdin
                                    .lock()
                                    .map_err(|_| "startup stdin lock poisoned")?,
                            );
                            if !input.is_empty() {
                                self.0
                                    .unused_default
                                    .store(false, std::sync::atomic::Ordering::Release);
                                handle.write_startup_stdin(input)?;
                            }
                        }
                        self.0
                            .startup
                            .lock()
                            .expect("startup result lock")
                            .send_modify(|_| {});
                        return Ok(());
                    }
                }
                LifecycleState::Ready => (
                    Instant::now(),
                    "session restarted before the operation began",
                ),
                LifecycleState::Restarting { deadline } => (deadline, "worker is restarting"),
                LifecycleState::ShuttingDown { deadline } => (deadline, "worker is shutting down"),
            }
        };
        handle.shutdown(deadline)?;
        Err(message.to_string())
    }

    pub(super) fn clear_worker_stop_handle(
        &self,
        expected: &WorkerGeneration,
    ) -> Result<(), String> {
        let mut lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        if lifecycle.state == LifecycleState::Ready && lifecycle.generation.is(expected) {
            lifecycle.processes.worker = None;
        }
        Ok(())
    }

    fn register_restart_stop_handle(
        &self,
        handle: platform::WorkerShutdownHandle,
    ) -> Result<(), String> {
        let (deadline, message) = {
            let mut lifecycle = self
                .0
                .lifecycle
                .lock()
                .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
            match lifecycle.state {
                LifecycleState::Restarting { .. } => {
                    if lifecycle.startup_interrupted() {
                        (Instant::now(), "worker startup interrupted")
                    } else {
                        lifecycle.processes.worker = Some(handle.clone());
                        lifecycle.startup = None;
                        return Ok(());
                    }
                }
                LifecycleState::ShuttingDown { deadline } => (deadline, "worker is shutting down"),
                LifecycleState::Ready => (Instant::now(), "worker restart state changed"),
            }
        };
        handle.shutdown(deadline)?;
        Err(message.to_string())
    }

    fn clear_restart_stop_handle(&self) -> Result<(), String> {
        let mut lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        if matches!(lifecycle.state, LifecycleState::Restarting { .. }) {
            lifecycle.processes.worker = None;
        }
        Ok(())
    }

    pub(crate) fn register_resolver_stop_handle(
        &self,
        expected: &WorkerGeneration,
        handle: crate::resolver::ResolverStopHandle,
    ) -> Result<(), String> {
        let message = {
            let mut lifecycle = self
                .0
                .lifecycle
                .lock()
                .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
            match lifecycle.state {
                LifecycleState::Ready | LifecycleState::Restarting { .. }
                    if lifecycle.generation.is(expected) =>
                {
                    if lifecycle.startup_interrupted() {
                        "worker startup interrupted"
                    } else {
                        lifecycle.processes.resolver = Some(handle.clone());
                        return Ok(());
                    }
                }
                LifecycleState::Ready => "session restarted before the operation began",
                LifecycleState::Restarting { .. } => "worker is restarting",
                LifecycleState::ShuttingDown { .. } => "worker is shutting down",
            }
        };
        handle.stop()?;
        Err(message.to_string())
    }

    pub(crate) fn clear_resolver_stop_handle(
        &self,
        expected: &WorkerGeneration,
    ) -> Result<(), String> {
        let mut lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        if (matches!(
            lifecycle.state,
            LifecycleState::Ready | LifecycleState::Restarting { .. }
        ) && lifecycle.generation.is(expected))
            || matches!(lifecycle.state, LifecycleState::ShuttingDown { .. })
        {
            lifecycle.processes.resolver = None;
        }
        Ok(())
    }

    fn close_lifecycle(&self, deadline: Instant) -> Result<Option<ProcessStopHandles>, String> {
        let mut lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        if !matches!(lifecycle.state, LifecycleState::ShuttingDown { .. }) {
            lifecycle.state = LifecycleState::ShuttingDown { deadline };
        }
        let handles = lifecycle.processes.clone();
        let deadline = match lifecycle.state {
            LifecycleState::ShuttingDown { deadline } => deadline,
            _ => unreachable!("closed lifecycle"),
        };
        handles.reserve_shutdown(deadline);
        Ok(
            (handles.worker.is_some() || handles.resolver.is_some() || lifecycle.startup.is_some())
                .then_some(handles),
        )
    }

    /// Stops and reaps active worker and resolver process groups.
    pub(crate) async fn shutdown(&self, deadline: Instant) -> Result<(), String> {
        let stop_handles = self.close_lifecycle(deadline)?.unwrap_or_default();
        let client = self.clone();
        let result = tokio::task::spawn_blocking(move || {
            let preparation = client
                .0
                .resolver_preparation
                .lock()
                .expect("preparation lock")
                .clone();
            // Queue relay shutdown and resolver cancellation before Close can
            // retire the preparation host and its control-input pipe.
            let errors = stop_handles.request_shutdown(deadline);
            let preparation = preparation.map(|preparation| {
                // Resolver and worker retirement run together. A lost
                // preparation connection must not extend worker shutdown.
                std::thread::spawn(move || preparation.close())
            });
            let stopped = stop_handles.finish_shutdown(errors);
            let retired = client.finish_worker_retirement().and_then(|retirement| {
                if matches!(
                    retirement,
                    WorkerRetirement::NeverStarted | WorkerRetirement::AlreadyStopped
                ) && let Some(worker) = &stop_handles.worker
                {
                    // A failed launch may never become Running or may already
                    // be Stopped before EOF. Observe its retained I/O result;
                    // a running worker already reports that result above.
                    worker.finish_retirement()?;
                }
                Ok(())
            });
            let preparation = preparation.map_or(Ok(()), |task| {
                task.join()
                    .map_err(|_| "preparation shutdown task panicked")?
            });
            let worker = match (stopped, retired) {
                (Ok(()), Ok(())) => Ok(()),
                (Err(error), Ok(())) | (Ok(()), Err(error)) => Err(error),
                (Err(error), Err(retirement_error)) => Err(format!(
                    "{error}; additionally failed to retire worker I/O: {retirement_error}"
                )),
            };
            match (worker, preparation) {
                (Ok(()), Ok(())) => Ok(()),
                (Err(error), Ok(())) | (Ok(()), Err(error)) => Err(error),
                (Err(error), Err(preparation_error)) => Err(format!(
                    "{error}; additionally failed to retire preparation: {preparation_error}"
                )),
            }
        })
        .await
        .map_err(|error| format!("process shutdown task failed: {error}"))?;
        self.finish_recording();
        result
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::worker_client::evaluation::EvaluationWait;
    use crate::worker_client::output::{Content, SendResponse, render_response};
    use crate::worker_protocol::ConsoleChannel;

    #[tokio::test]
    async fn replacement_failure_preserves_an_unclaimed_assembled_response() {
        let client = Client::pending();
        client.configure(crate::worker_client::ClientConfiguration::new(
            std::path::PathBuf::from("unused-worker"),
            None,
            false,
            crate::settings::SandboxSettings::default(),
        ));
        client.finish_startup(Ok(()));
        let evaluation = Arc::new(super::super::Evaluation::new(
            WorkerGeneration::new(),
            crate::transcript::Transcript::new(true),
            None,
            client.0.output.clone(),
            Response::default(),
            Response::default(),
            false,
            0,
        ));
        evaluation.complete_cell(Ok(()));
        let claim = evaluation.claim().unwrap();
        let EvaluationWait::Completed(response) =
            evaluation.wait(claim, Duration::ZERO).await.unwrap()
        else {
            panic!("completed evaluation did not assemble its response")
        };
        let response = render_response(SendResponse::Completed(response));
        let mut reservation = Some(evaluation.reserve_for_restart().unwrap());
        let failure =
            match client.replace_worker(&mut reservation, WorkerGeneration::new(), true, None) {
                Ok(_) => panic!("replacement unexpectedly succeeded outside a restart"),
                Err(failure) => failure,
            };
        assert_eq!(failure.message, "worker restart state changed");
        assert!(reservation.is_some());
        let (_, _, delivery) = response.into_parts();
        delivery.unwrap().unclaimed();

        let recovered = client
            .settle_reserved_evaluation(reservation.take(), false)
            .unwrap_or_else(|failure| panic!("{}", failure.message));
        let (content, is_error, delivery) = recovered.into_parts();

        assert!(!is_error);
        assert!(delivery.is_none());
        assert!(matches!(content.as_slice(), [Content::Text(text)] if text == "[done]"));
        let (remaining, is_error, delivery) = client.0.output.take().into_parts();
        assert!(remaining.is_empty());
        assert!(!is_error);
        assert!(delivery.is_none());
    }

    #[tokio::test]
    async fn failed_restart_excludes_a_delivered_response_but_keeps_later_output() {
        let client = Client::pending();
        client.configure(crate::worker_client::ClientConfiguration::new(
            std::path::PathBuf::from("unused-worker"),
            None,
            false,
            crate::settings::SandboxSettings::default(),
        ));
        client.finish_startup(Ok(()));
        let evaluation = Arc::new(super::super::Evaluation::new(
            WorkerGeneration::new(),
            crate::transcript::Transcript::new(true),
            None,
            client.0.output.clone(),
            Response::default(),
            Response::default(),
            false,
            0,
        ));
        evaluation.complete_cell(Ok(()));
        let claim = evaluation.claim().unwrap();
        let EvaluationWait::Completed(response) =
            evaluation.wait(claim, Duration::ZERO).await.unwrap()
        else {
            panic!("completed evaluation did not assemble its response")
        };
        let response = render_response(SendResponse::Completed(response));
        client
            .0
            .output
            .push_console_text(ConsoleChannel::Output, "later output".to_string());
        let reservation = evaluation.reserve_for_restart().unwrap();
        let (_, _, delivery) = response.into_parts();
        delivery.unwrap().delivered();

        let recovered = client
            .settle_reserved_evaluation(Some(reservation), false)
            .unwrap_or_else(|failure| panic!("{}", failure.message));
        let (content, is_error, delivery) = recovered.into_parts();

        assert!(!is_error);
        assert!(delivery.is_none());
        assert!(matches!(content.as_slice(), [Content::Text(text)] if text == "later output"));
        let (remaining, is_error, delivery) = client.0.output.take().into_parts();
        assert!(remaining.is_empty());
        assert!(!is_error);
        assert!(delivery.is_none());
    }
}
