use std::sync::{Arc, Condvar, Mutex, mpsc};

use super::{PendingPythonCandidate, WorkerEvent};
use crate::relay_protocol::RelayEvent;
use crate::worker_client::{
    Evaluation, IdleResponseSnapshot, OutputTape, PreparationOutcome, PythonPreparationCommit,
    RPreparationCommit,
};

#[derive(Clone)]
pub(in crate::worker_client) struct WorkerOperationState(Arc<OperationStateCell>);

struct OperationState {
    bootstrap: Bootstrap,
    bootstrap_suspended: bool,
    operation: Option<Operation>,
    failure: Option<String>,
    relay_exit_caused_failure: bool,
    idle_input: Option<String>,
    runtime_r_callback: Option<RuntimeRCallbackPhase>,
    environment_preparation_reserved: bool,
    retiring: bool,
}

#[derive(Clone, Copy)]
enum Bootstrap {
    Disabled,
    Running,
    Finished,
    Interrupted,
}

struct OperationStateCell {
    state: Mutex<OperationState>,
    runtime_r_reply: Condvar,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum RuntimeRCallbackPhase {
    Resolving,
    AwaitingActivation,
}

pub(in crate::worker_client) struct EnvironmentPreparationReservation {
    operation: WorkerOperationState,
    bootstrap_events: Option<mpsc::Sender<WorkerEvent>>,
}

struct Operation {
    kind: OperationKind,
    result: Option<mpsc::Sender<Result<OperationResult, String>>>,
}

enum OperationKind {
    Cell(Arc<Evaluation>),
    PrepareR {
        library: String,
        commit: RPreparationCommit,
    },
    PreparePython {
        commit: PythonPreparationCommit,
        requirements: crate::worker_protocol::PythonRequirementManifest,
        continue_environment_preparation: bool,
        duckdb_extensions: Option<std::collections::BTreeSet<String>>,
    },
}

pub(super) enum Route {
    Cell(Arc<Evaluation>),
    Bootstrap,
    Preparation,
    Idle,
}

pub(super) enum RuntimeRCallbackAdmission {
    Admitted,
    Busy,
}

pub(in crate::worker_client) enum OperationResult {
    Completed,
    RPrepared(PreparationOutcome),
    PythonPrepared(PreparationOutcome),
}

impl WorkerOperationState {
    pub(in crate::worker_client) fn new(builtin: bool) -> Self {
        Self(Arc::new(OperationStateCell {
            state: Mutex::new(OperationState {
                bootstrap: if builtin {
                    Bootstrap::Running
                } else {
                    Bootstrap::Disabled
                },
                bootstrap_suspended: false,
                operation: None,
                failure: None,
                relay_exit_caused_failure: false,
                idle_input: None,
                runtime_r_callback: None,
                environment_preparation_reserved: false,
                retiring: false,
            }),
            runtime_r_reply: Condvar::new(),
        }))
    }

    pub(in crate::worker_client) fn wait_for_bootstrap(&self) -> Result<(), String> {
        let mut state = self.lock()?;
        while matches!(state.bootstrap, Bootstrap::Running) {
            state.ensure_running()?;
            state = self
                .0
                .runtime_r_reply
                .wait(state)
                .map_err(|_| "worker operation state lock poisoned".to_string())?;
        }
        state.ensure_running()
    }

    pub(in crate::worker_client) fn finish_bootstrap(
        &self,
        interrupted: bool,
        admitted: Option<&Evaluation>,
    ) -> Result<(), String> {
        let mut state = self.lock()?;
        if !matches!(state.bootstrap, Bootstrap::Running) {
            return Err("worker sent an unexpected runtime initialization result".into());
        }
        if state.idle_input.is_some() {
            return Err("worker completed with an outstanding input request".into());
        }
        if let Some(OperationKind::Cell(evaluation)) =
            state.operation.as_ref().map(|operation| &operation.kind)
        {
            evaluation.input_complete()?;
        }
        if state.runtime_r_callback.is_some() {
            return Err(
                "worker initialized runtimes before completing runtime R activation".into(),
            );
        }
        // The caller holds the active-cell publication guard. Include cells
        // whose evaluator has not attached yet, but never a later admission.
        if interrupted && let Some(evaluation) = admitted {
            evaluation.admission.interrupt(|| Ok(()))?;
        }
        state.bootstrap = if interrupted {
            Bootstrap::Interrupted
        } else {
            Bootstrap::Finished
        };
        drop(state);
        self.0.runtime_r_reply.notify_all();
        Ok(())
    }

    pub(in crate::worker_client) fn interrupt_cell<T>(
        &self,
        evaluation: Option<&Evaluation>,
        enqueue: impl FnOnce() -> Result<T, String>,
    ) -> Result<T, String> {
        // Lock order: active publication -> lifecycle -> operation -> admission
        // -> response/output. Never wait for a receipt while holding these.
        let _state = self.lock()?;
        match evaluation {
            Some(evaluation) => evaluation.admission.interrupt(enqueue),
            None => enqueue(),
        }
    }

    pub(in crate::worker_client) fn dispatch_cell(
        &self,
        evaluation: &Evaluation,
        enqueue: impl FnOnce() -> Result<(), String>,
    ) -> Result<bool, String> {
        let state = self.lock()?;
        state.ensure_running()?;
        assert!(!matches!(state.bootstrap, Bootstrap::Running));
        evaluation.admission.dispatch(enqueue)
    }

    pub(in crate::worker_client) fn is_bootstrapping(&self) -> Result<bool, String> {
        Ok(matches!(self.lock()?.bootstrap, Bootstrap::Running))
    }

    pub(in crate::worker_client) fn abort_bootstrap_cell(&self) -> Result<(), String> {
        let mut state = self.lock()?;
        let operation = state
            .operation
            .take()
            .ok_or("interrupted bootstrap has no waiting cell")?;
        let OperationKind::Cell(evaluation) = operation.kind else {
            return Err("interrupted bootstrap did not own a waiting cell".into());
        };
        // Withholding never ran this cell. Its input may still belong to an
        // idle callback that ignored interruption; return ownership before
        // another input event can observe the detached operation.
        assert!(state.idle_input.is_none());
        state.idle_input = evaluation.take_input_request()?;
        drop(state);
        evaluation.complete_cell_after_grace();
        Ok(())
    }

    pub(in crate::worker_client) fn reserve_environment_preparation(
        &self,
        replacing: bool,
        events: mpsc::Sender<WorkerEvent>,
    ) -> Result<
        EnvironmentPreparationReservation,
        crate::worker_client::EnvironmentPreparationAdmissionFailure,
    > {
        use crate::worker_client::EnvironmentPreparationAdmissionFailure::{Busy, Infrastructure};

        if replacing {
            // An ordered dispatcher barrier lets in-flight callbacks finish
            // before the preparation owner takes the environment lock.
            let (admitted, receiver) = mpsc::sync_channel(1);
            events
                .send(WorkerEvent::SuspendBootstrap { admitted })
                .map_err(|_| Infrastructure("worker event dispatcher stopped".into()))?;
            receiver
                .recv()
                .map_err(|_| Infrastructure("worker bootstrap reservation stopped".into()))?
                .map_err(Infrastructure)?;
            return Ok(EnvironmentPreparationReservation {
                operation: self.clone(),
                bootstrap_events: Some(events),
            });
        }
        let mut state = self.lock().map_err(Infrastructure)?;
        state.ensure_available().map_err(Infrastructure)?;
        while matches!(state.bootstrap, Bootstrap::Running) {
            if state.idle_input.is_some() {
                return Err(Busy(
                    "runtime startup requested input; supply stdin before preparing requirements"
                        .into(),
                ));
            }
            state = self
                .0
                .runtime_r_reply
                .wait(state)
                .map_err(|_| Infrastructure("worker operation state lock poisoned".into()))?;
            state.ensure_available().map_err(Infrastructure)?;
        }
        if state.runtime_r_callback.is_some() {
            return Err(Busy(
                "requirements were not prepared because an idle runtime R callback owns environment changes"
                    .to_string(),
            ));
        }
        if state.environment_preparation_reserved {
            return Err(Infrastructure(
                "worker environment preparation is already reserved".to_string(),
            ));
        }
        state.environment_preparation_reserved = true;
        Ok(EnvironmentPreparationReservation {
            operation: self.clone(),
            bootstrap_events: None,
        })
    }

    pub(super) fn bootstrap_suspended(&self) -> Result<bool, String> {
        Ok(self.lock()?.bootstrap_suspended)
    }

    pub(super) fn resume_bootstrap(&self) -> Result<(), String> {
        self.lock()?.bootstrap_suspended = false;
        Ok(())
    }

    pub(super) fn suspend_bootstrap(&self) -> Result<(), String> {
        let mut state = self.lock()?;
        state.ensure_available()?;
        if matches!(state.bootstrap, Bootstrap::Disabled) || state.environment_preparation_reserved
        {
            return Err("worker cannot reserve unused bootstrap replacement".into());
        }
        state.environment_preparation_reserved = true;
        state.bootstrap_suspended = true;
        Ok(())
    }

    pub(in crate::worker_client) fn begin_cell(
        &self,
        evaluation: Arc<Evaluation>,
        capture_idle_prelude: bool,
    ) -> Result<mpsc::Receiver<Result<OperationResult, String>>, String> {
        let (result, receiver) = mpsc::channel();
        let mut state = self.lock()?;
        loop {
            state.ensure_available()?;
            if state.runtime_r_callback != Some(RuntimeRCallbackPhase::Resolving) {
                break;
            }
            state = self
                .0
                .runtime_r_reply
                .wait(state)
                .map_err(|_| "worker operation state lock poisoned".to_string())?;
        }
        if let Some(prompt) = state.idle_input.take() {
            evaluation.resume_input_request(prompt)?;
        }
        let mut operation = Some(Operation {
            kind: OperationKind::Cell(evaluation.clone()),
            result: Some(result),
        });
        evaluation.capture_prelude_before(capture_idle_prelude, || {
            state.operation = operation.take();
        })?;
        Ok(receiver)
    }

    pub(in crate::worker_client) fn begin_r_preparation(
        &self,
        library: String,
        commit: RPreparationCommit,
    ) -> Result<mpsc::Receiver<Result<OperationResult, String>>, String> {
        self.begin_preparation(OperationKind::PrepareR { library, commit })
    }

    pub(in crate::worker_client) fn begin_python_preparation(
        &self,
        commit: PythonPreparationCommit,
        continue_environment_preparation: bool,
        duckdb_extensions: Option<std::collections::BTreeSet<String>>,
        requirements: crate::worker_protocol::PythonRequirementManifest,
    ) -> Result<mpsc::Receiver<Result<OperationResult, String>>, String> {
        self.begin_preparation(OperationKind::PreparePython {
            commit,
            continue_environment_preparation,
            duckdb_extensions,
            requirements,
        })
    }

    pub(super) fn python_preparation_extensions(
        &self,
    ) -> Result<Option<std::collections::BTreeSet<String>>, String> {
        let state = self.lock()?;
        Ok(
            match state.operation.as_ref().map(|operation| &operation.kind) {
                Some(OperationKind::PreparePython {
                    duckdb_extensions, ..
                }) => duckdb_extensions.clone(),
                _ => None,
            },
        )
    }

    pub(super) fn python_preparation_requirements(
        &self,
    ) -> Result<Option<crate::worker_protocol::PythonRequirementManifest>, String> {
        Ok(
            match self
                .lock()?
                .operation
                .as_ref()
                .map(|operation| &operation.kind)
            {
                Some(OperationKind::PreparePython { requirements, .. }) => {
                    Some(requirements.clone())
                }
                _ => None,
            },
        )
    }

    fn begin_preparation(
        &self,
        kind: OperationKind,
    ) -> Result<mpsc::Receiver<Result<OperationResult, String>>, String> {
        let (result, receiver) = mpsc::channel();
        let mut state = self.lock()?;
        state.ensure_available()?;
        if !state.environment_preparation_reserved {
            return Err("worker environment preparation was not reserved".to_string());
        }
        if let Some(prompt) = state.idle_input.as_ref() {
            return Err(format!(
                "idle R callback requested input {prompt} during requirement preparation; collect callback input with send before preparing requirements"
            ));
        }
        let continue_environment_preparation = matches!(
            kind,
            OperationKind::PreparePython {
                continue_environment_preparation: true,
                ..
            }
        );
        state.operation = Some(Operation {
            kind,
            result: Some(result),
        });
        if !continue_environment_preparation {
            state.environment_preparation_reserved = false;
        }
        Ok(receiver)
    }

    pub(in crate::worker_client) fn fail(&self, error: String) {
        self.fail_with_relay_exit(error, false);
    }

    pub(super) fn fail_from_relay_exit(&self, error: String) {
        self.fail_with_relay_exit(error, true);
    }

    fn fail_with_relay_exit(&self, error: String, relay_exit: bool) {
        let operation = {
            let Ok(mut state) = self.0.state.lock() else {
                return;
            };
            if state.failure.is_none() {
                state.failure = Some(error.clone());
                // A later relay exit must not turn an earlier protocol or worker
                // failure into launcher-recovery evidence.
                state.relay_exit_caused_failure = relay_exit;
            }
            state.runtime_r_callback = None;
            state.environment_preparation_reserved = false;
            state.operation.take()
        };
        self.0.runtime_r_reply.notify_all();
        if let Some(operation) = operation {
            if let OperationKind::Cell(evaluation) = &operation.kind {
                evaluation
                    .admission
                    .finish(crate::worker_client::admission::CellOutcome::Failed);
            }
            if let Some(result) = operation.result {
                let _ = result.send(Err(error));
            }
        }
    }

    pub(in crate::worker_client) fn retire_operation(&self, error: String) {
        let result = {
            let Ok(mut state) = self.0.state.lock() else {
                return;
            };
            state.retiring = true;
            state.bootstrap_suspended = false;
            state.runtime_r_callback = None;
            state.environment_preparation_reserved = false;
            if let Some(OperationKind::Cell(evaluation)) =
                state.operation.as_ref().map(|op| &op.kind)
            {
                evaluation
                    .admission
                    .finish(crate::worker_client::admission::CellOutcome::Failed);
            }
            state
                .operation
                .as_mut()
                .and_then(|operation| operation.result.take())
        };
        self.0.runtime_r_reply.notify_all();
        if let Some(result) = result {
            let _ = result.send(Err(error));
        }
    }

    pub(in crate::worker_client) fn has_failure(&self) -> Result<bool, String> {
        Ok(self.lock()?.failure.is_some())
    }

    pub(in crate::worker_client) fn relay_exit_caused_failure(&self) -> Result<bool, String> {
        Ok(self.lock()?.relay_exit_caused_failure)
    }

    pub(in crate::worker_client) fn idle_response_snapshot(
        &self,
        output: &OutputTape,
    ) -> Result<IdleResponseSnapshot, String> {
        let state = self.lock()?;
        Ok(IdleResponseSnapshot {
            cut: output.cut(),
            failure: state.failure.clone(),
            input_requested: state.idle_input.is_some(),
        })
    }

    pub(super) fn with_route<T>(
        &self,
        publish: impl FnOnce(Route) -> Result<T, String>,
    ) -> Result<T, String> {
        let state = self.lock()?;
        // Hold the operation lock through publication so admission captures
        // in-flight idle output as the next cell's prelude.
        let route = match state.operation.as_ref().map(|operation| &operation.kind) {
            Some(OperationKind::Cell(evaluation)) => Route::Cell(evaluation.clone()),
            Some(OperationKind::PrepareR { .. } | OperationKind::PreparePython { .. }) => {
                Route::Preparation
            }
            None if matches!(state.bootstrap, Bootstrap::Running) => Route::Bootstrap,
            None => Route::Idle,
        };
        publish(route)
    }

    pub(super) fn begin_runtime_r_callback(
        &self,
        reject_busy: impl FnOnce() -> Result<(), String>,
    ) -> Result<RuntimeRCallbackAdmission, String> {
        let mut state = self.lock()?;
        if let Some(error) = state.failure.as_ref() {
            return Err(error.clone());
        }
        if state.retiring {
            return Err("worker is retiring".to_string());
        }
        match state.operation.as_ref().map(|operation| &operation.kind) {
            Some(OperationKind::PrepareR { .. } | OperationKind::PreparePython { .. }) => {
                return Err(
                    "worker sent a runtime R callback during requirement preparation".to_string(),
                );
            }
            Some(OperationKind::Cell(_)) | None => {}
        }
        if state.runtime_r_callback.is_some() {
            return Err("worker sent a second runtime R callback before activation".to_string());
        }
        if state.environment_preparation_reserved {
            // Queue the rejection before preparation can turn this reservation
            // into a live operation and enqueue its command.
            reject_busy()?;
            return Ok(RuntimeRCallbackAdmission::Busy);
        }
        state.runtime_r_callback = Some(RuntimeRCallbackPhase::Resolving);
        Ok(RuntimeRCallbackAdmission::Admitted)
    }

    pub(super) fn runtime_r_reply_sent(&self, awaiting_activation: bool) -> Result<(), String> {
        let mut state = self.lock()?;
        if state.runtime_r_callback != Some(RuntimeRCallbackPhase::Resolving) {
            return Err("worker has no pending runtime R resolver reply".to_string());
        }
        state.runtime_r_callback =
            awaiting_activation.then_some(RuntimeRCallbackPhase::AwaitingActivation);
        drop(state);
        self.0.runtime_r_reply.notify_all();
        Ok(())
    }

    pub(super) fn ensure_runtime_r_activation_phase(&self) -> Result<(), String> {
        let state = self.lock()?;
        match state.operation.as_ref().map(|operation| &operation.kind) {
            Some(OperationKind::PrepareR { .. } | OperationKind::PreparePython { .. }) => {
                return Err(
                    "worker sent a runtime R callback during requirement preparation".to_string(),
                );
            }
            Some(OperationKind::Cell(_)) | None => {}
        }
        if state.runtime_r_callback != Some(RuntimeRCallbackPhase::AwaitingActivation) {
            return Err(
                "worker sent R activation without an active runtime R callback".to_string(),
            );
        }
        Ok(())
    }

    pub(super) fn finish_runtime_r_callback(&self) -> Result<(), String> {
        let mut state = self.lock()?;
        if state.runtime_r_callback != Some(RuntimeRCallbackPhase::AwaitingActivation) {
            return Err("worker has no active runtime R callback".to_string());
        }
        state.runtime_r_callback = None;
        Ok(())
    }

    pub(super) fn input_requested(
        &self,
        prompt: String,
        rendered: String,
        output: &OutputTape,
    ) -> Result<(), String> {
        let mut state = self.lock()?;
        match state.operation.as_ref().map(|operation| &operation.kind) {
            Some(OperationKind::Cell(evaluation)) => evaluation.input_requested(prompt),
            Some(OperationKind::PrepareR { .. } | OperationKind::PreparePython { .. }) => {
                output.push_notice_line(format!("input requested: {rendered}"));
                Err(format!(
                    "idle R callback requested input {rendered} during requirement preparation; collect callback input with send before preparing requirements"
                ))
            }
            None => {
                if state.idle_input.is_some() {
                    return Err(
                        "worker requested new input before receiving prior input".to_string()
                    );
                }
                state.idle_input = Some(rendered.clone());
                output.push_notice_line(format!("input requested: {rendered}"));
                self.0.runtime_r_reply.notify_all();
                Ok(())
            }
        }
    }

    pub(super) fn input_received(&self) -> Result<(), String> {
        let mut state = self.lock()?;
        match state.operation.as_ref().map(|operation| &operation.kind) {
            Some(OperationKind::Cell(evaluation)) => evaluation.input_received(),
            Some(OperationKind::PrepareR { .. } | OperationKind::PreparePython { .. }) => {
                Err("worker reported received input during requirement preparation".to_string())
            }
            None => {
                state.idle_input.take().ok_or_else(|| {
                    "worker reported received input without requesting it".to_string()
                })?;
                Ok(())
            }
        }
    }

    pub(super) fn complete(
        &self,
        event: RelayEvent,
        r_candidates: &mut Vec<crate::resolver::ManagedR>,
        python_candidates: &mut Vec<PendingPythonCandidate>,
    ) -> Result<(), String> {
        let Operation { kind, result } = {
            let mut state = self.lock()?;
            if state.runtime_r_callback.is_some() {
                return Err(
                    "worker sent an operation result before completing runtime R activation"
                        .to_string(),
                );
            }
            state.operation.take().ok_or_else(|| {
                "worker sent an operation result without an active operation".to_string()
            })?
        };

        if result.is_none() && kind.matches_result(&event) {
            r_candidates.clear();
            python_candidates.clear();
            return Ok(());
        }

        let continue_environment_preparation = matches!(
            kind,
            OperationKind::PreparePython {
                continue_environment_preparation: true,
                ..
            }
        );
        let committed = match (kind, event) {
            (OperationKind::Cell(evaluation), RelayEvent::Completed) => {
                match evaluation.input_complete() {
                    Ok(()) => {
                        r_candidates.clear();
                        python_candidates.clear();
                        evaluation.complete_cell_after_grace();
                        Ok(OperationResult::Completed)
                    }
                    Err(error) => Err(error),
                }
            }
            (
                OperationKind::PrepareR {
                    library: expected,
                    commit,
                },
                RelayEvent::RPrepared { library },
            ) if library == expected => {
                r_candidates.clear();
                python_candidates.clear();
                commit(Ok(())).map(OperationResult::RPrepared)
            }
            (
                OperationKind::PrepareR { commit, .. },
                RelayEvent::RPreparationFailed { message },
            ) => {
                r_candidates.clear();
                python_candidates.clear();
                commit(Err(message)).map(OperationResult::RPrepared)
            }
            (OperationKind::PreparePython { commit, .. }, RelayEvent::PythonPrepared) => {
                let candidate = python_candidates
                    .pop()
                    .map(|candidate| (candidate.managed, candidate.configuration));
                r_candidates.clear();
                python_candidates.clear();
                commit(Ok(candidate)).map(OperationResult::PythonPrepared)
            }
            (
                OperationKind::PreparePython { commit, .. },
                RelayEvent::PythonPreparationFailed { message },
            ) => {
                r_candidates.clear();
                python_candidates.clear();
                commit(Err(message)).map(OperationResult::PythonPrepared)
            }
            (
                OperationKind::PreparePython { .. },
                RelayEvent::PythonPreparationRejected { message },
            ) => {
                r_candidates.clear();
                python_candidates.clear();
                Ok(OperationResult::PythonPrepared(
                    PreparationOutcome::Completed(Err(message)),
                ))
            }
            (OperationKind::Cell(_), _) => {
                Err("worker sent an unexpected evaluation result".to_string())
            }
            (OperationKind::PrepareR { .. }, RelayEvent::RPrepared { .. }) => {
                Err("worker prepared an unexpected R library".to_string())
            }
            (OperationKind::PrepareR { .. }, _) => {
                Err("worker sent an unexpected R preparation message".to_string())
            }
            (OperationKind::PreparePython { .. }, _) => {
                Err("worker sent an unexpected Python preparation message".to_string())
            }
        };

        if continue_environment_preparation
            && !matches!(
                &committed,
                Ok(OperationResult::PythonPrepared(
                    PreparationOutcome::Completed(Ok(()))
                ))
            )
        {
            self.release_environment_preparation()?;
        }

        match (result, committed) {
            (Some(result), Ok(committed)) => result
                .send(Ok(committed))
                .map_err(|_| "worker operation receiver stopped".to_string()),
            (Some(result), Err(error)) => {
                let _ = result.send(Err(error.clone()));
                Err(error)
            }
            (None, Err(error)) => Err(error),
            (None, Ok(_)) => unreachable!("a matching cancelled operation result returned early"),
        }
    }

    fn lock(&self) -> Result<std::sync::MutexGuard<'_, OperationState>, String> {
        self.0
            .state
            .lock()
            .map_err(|_| "worker operation state lock poisoned".to_string())
    }

    fn release_environment_preparation(&self) -> Result<(), String> {
        let mut state = self.lock()?;
        state.environment_preparation_reserved = false;
        Ok(())
    }
}

impl Drop for EnvironmentPreparationReservation {
    fn drop(&mut self) {
        if let Ok(mut state) = self.operation.0.state.lock() {
            state.environment_preparation_reserved = false;
        }
        if let Some(events) = &self.bootstrap_events {
            let _ = events.send(WorkerEvent::ResumeBootstrap);
        }
    }
}

impl OperationKind {
    fn matches_result(&self, event: &RelayEvent) -> bool {
        match (self, event) {
            (Self::Cell(_), RelayEvent::Completed)
            | (Self::PrepareR { .. }, RelayEvent::RPreparationFailed { .. })
            | (
                Self::PreparePython { .. },
                RelayEvent::PythonPrepared
                | RelayEvent::PythonPreparationFailed { .. }
                | RelayEvent::PythonPreparationRejected { .. },
            ) => true,
            (
                Self::PrepareR {
                    library: expected, ..
                },
                RelayEvent::RPrepared { library },
            ) => library == expected,
            _ => false,
        }
    }
}

impl OperationState {
    fn ensure_available(&self) -> Result<(), String> {
        self.ensure_running()?;
        if self.operation.is_some() {
            return Err("worker already has an active operation".to_string());
        }
        Ok(())
    }

    fn ensure_running(&self) -> Result<(), String> {
        if let Some(error) = self.failure.as_ref() {
            return Err(error.clone());
        }
        if self.retiring {
            return Err("worker is retiring".to_string());
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::worker_client::evaluation::EvaluationWait;
    use crate::worker_client::output::{Content, Response, SendResponse, render_response};
    use crate::worker_protocol::ConsoleChannel::Output;
    use std::thread;

    #[tokio::test]
    async fn cell_admission_captures_an_inflight_idle_route_as_prelude() {
        let output = OutputTape::new();
        let operation = WorkerOperationState::new(false);
        let evaluation = Arc::new(Evaluation::new(
            crate::worker_client::lifecycle::WorkerGeneration::new(),
            crate::transcript::Transcript::new(true),
            None,
            output.clone(),
            Response::default(),
            Response::default(),
            false,
            0,
        ));
        let claim = evaluation.claim().unwrap();
        let (routed, routed_rx) = mpsc::sync_channel(0);
        let (release, release_rx) = mpsc::sync_channel(0);

        let idle_operation = operation.clone();
        let idle_output = output.clone();
        let idle = thread::spawn(move || {
            idle_operation.with_route(|route| {
                assert!(matches!(route, Route::Idle));
                routed.send(()).unwrap();
                release_rx.recv().unwrap();
                idle_output.push_console_text(Output, "idle output");
                Ok(())
            })
        });
        routed_rx.recv().unwrap();

        let admitting_operation = operation.clone();
        let admitting_evaluation = evaluation.clone();
        let (contending, contending_rx) = mpsc::sync_channel(0);
        let admission = thread::spawn(move || {
            assert!(matches!(
                admitting_operation.0.state.try_lock(),
                Err(std::sync::TryLockError::WouldBlock)
            ));
            contending.send(()).unwrap();
            admitting_operation.begin_cell(admitting_evaluation, true)
        });
        contending_rx.recv().unwrap();
        release.send(()).unwrap();
        idle.join().unwrap().unwrap();
        drop(admission.join().unwrap().unwrap());

        operation
            .with_route(|route| match route {
                Route::Cell(evaluation) => evaluation.output(Output, "cell output".to_string()),
                Route::Bootstrap | Route::Preparation | Route::Idle => {
                    panic!("cell route was not installed")
                }
            })
            .unwrap();
        evaluation.complete_cell(Ok(()));
        let EvaluationWait::Completed(response) = evaluation
            .wait(claim, std::time::Duration::ZERO)
            .await
            .unwrap()
        else {
            panic!("cell did not complete")
        };
        let response = render_response(SendResponse::Completed(response));
        let (content, is_error, delivery) = response.into_parts();
        assert!(!is_error);
        assert!(matches!(
            content.as_slice(),
            [Content::Text(text)]
                if text == "idle output\n[output produced while idle]\ncell output"
        ));
        delivery.unwrap().delivered();
    }
}
