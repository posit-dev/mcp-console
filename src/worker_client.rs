use std::ffi::OsString;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

mod admission;
mod configuration;
mod control;
mod environment;
mod evaluation;
mod execution;
mod lifecycle;
mod output;
mod send;
mod status;

#[cfg(any(unix, windows))]
mod events;

#[cfg(any(unix, windows))]
mod transport;

#[cfg(any(unix, windows))]
#[path = "worker_client/process.rs"]
mod platform;

#[cfg(not(any(unix, windows)))]
#[path = "worker_client/unsupported.rs"]
mod platform;

use crate::local_runtime::DEFAULT_DUCKDB_EXTENSIONS;
pub(crate) use configuration::ClientConfiguration;
use configuration::RResolver;
pub(crate) use environment::validate_r_requirements;
pub(crate) use environment::{Declaration, Requirements, RequirementsAction};
use environment::{Environment, PythonEnvironment, RuntimeRResolutionFailure};
use evaluation::Evaluation;
use lifecycle::{LifecycleControl, OldGenerationCommitDisposition, WorkerGeneration};
pub(crate) use output::{Content, Response, ResponseDelivery};
use output::{OutputTape, SendFailure};

pub(crate) const DEFAULT_R_REQUIREMENTS: &[&str] = &[
    "tidyverse",
    "dplyr",
    "dbplyr",
    "reticulate",
    "DBI",
    "duckdb",
    "arrow",
    "nanoarrow",
    "yyjsonr",
    "ggplot2",
];

const CUSTOM_DUCKDB_R_REQUIREMENTS: &[&str] = &["DBI", "duckdb", "jsonlite"];
pub(crate) const WORKER_SHUTDOWN_GRACE: Duration = Duration::from_secs(1);
const INTERRUPT_GRACE: Duration = Duration::from_millis(100);

#[derive(Clone, Copy)]
pub(crate) enum SendControl {
    Interrupt,
    Restart,
}

pub(crate) struct SendRequest {
    pub(crate) cell: Option<crate::cell::Cell>,
    pub(crate) stdin: Option<String>,
    pub(crate) requirements: Option<Requirements>,
    pub(crate) control: Option<SendControl>,
    pub(crate) deadline: Instant,
    pub(crate) transcript: crate::transcript::Transcript,
    pub(crate) call_id: Option<u64>,
}

impl SendRequest {
    pub(crate) fn validate(&self, requirements_available: bool) -> Result<(), String> {
        let Some(requirements) = &self.requirements else {
            return Ok(());
        };
        if matches!(
            requirements.action,
            RequirementsAction::Set | RequirementsAction::Reset
        ) && matches!(self.control, Some(SendControl::Interrupt))
        {
            return Err("requirements.action=set/reset cannot accompany interrupt; use control=\"restart\" to replace a live environment".into());
        }
        if !requirements_available {
            return Err(
                "dynamic environment resolution is unavailable; install `ir` or `uv` and restart MCP Console"
                    .to_string(),
            );
        }
        if self.cell.is_none()
            && self.control.is_none()
            && self.stdin.as_ref().is_some_and(|stdin| !stdin.is_empty())
        {
            return Err(
                "requirements-only `send` performs standalone preparation and cannot also queue stdin"
                    .to_string(),
            );
        }
        if self.cell.is_none() && matches!(self.control, Some(SendControl::Interrupt)) {
            return Err(
                "`requirements` with `control = \"interrupt\"` requires a code cell".to_string(),
            );
        }
        // An interrupt and its stdin precede requirement-content errors. Validate
        // those only after the previous evaluation settles, before the new cell.
        if !matches!(self.control, Some(SendControl::Interrupt)) {
            requirements.validate()?;
        }
        Ok(())
    }
}

/// A cloneable handle to the single implicit session, including its startup.
#[derive(Clone)]
pub(crate) struct Client(Arc<ClientInner>);

struct ClientInner {
    configuration: OnceLock<ClientConfiguration>,
    startup: Mutex<tokio::sync::watch::Sender<Option<Result<(), String>>>>,
    /// Identity of connection startup, including the interval before admission.
    startup_generation: WorkerGeneration,
    /// Disposable presentation fact; admission still uses the startup outcome.
    startup_observation_complete: AtomicBool,
    /// The one evaluation occupying this session, independently of who is polling it.
    evaluation: Mutex<Option<ActiveEvaluation>>,
    /// Settles operations admitted before inline control reserves its optional new cell.
    admission: tokio::sync::RwLock<()>,
    preparation: tokio::sync::RwLock<()>,
    output: OutputTape,
    lifecycle: Mutex<LifecycleControl>,
    recording: Mutex<Option<crate::transcript::Transcript>>,
    startup_failed: AtomicBool,
    startup_stdin: Mutex<String>,
}

impl std::ops::Deref for ClientInner {
    type Target = ClientConfiguration;

    fn deref(&self) -> &Self::Target {
        self.configuration
            .get()
            .expect("runtime configuration is ready")
    }
}

/// Describes one worker launch for the current runtime.
struct WorkerSpec<'a> {
    builtin: bool,
    languages: Option<crate::cell::Languages>,
    executable: &'a std::path::Path,
    arguments: &'a [OsString],
    relay: Option<&'a std::path::Path>,
    no_sandbox: bool,
    sandbox_settings: &'a crate::settings::SandboxSettings,
    startup_source: Option<&'a crate::settings::startup::Startup>,
    duckdb_extension_directory: Option<&'a std::path::Path>,
    resolver_matplotlib_cache: Option<&'a str>,
    python: Option<&'a PythonEnvironment>,
    managed_r: Option<&'a crate::resolver::ManagedR>,
    dynamic_resolution: bool,
    callbacks: WorkerCallbacks,
    local_runtime: Option<&'a crate::local_runtime::Selection>,
}

struct IdleResponseSnapshot {
    cut: output::OutputCut,
    failure: Option<String>,
    input_requested: bool,
}

type RPreparationCommit =
    Box<dyn FnOnce(Result<(), String>) -> Result<PreparationOutcome, String> + Send + 'static>;

type PythonCandidate = (crate::resolver::ManagedPython, crate::python::NativePython);

type PythonPreparationCommit = Box<
    dyn FnOnce(Result<Option<PythonCandidate>, String>) -> Result<PreparationOutcome, String>
        + Send
        + 'static,
>;

enum PreparationOutcome {
    Completed(Result<(), String>),
    DiscardedByReplacement,
}

enum EnvironmentPreparationAdmissionFailure {
    Busy(String),
    Infrastructure(String),
}

#[derive(Clone, Copy)]
enum WorkerProcessOutcome {
    Exited(i32),
    Signaled(i32),
}

impl WorkerProcessOutcome {
    fn diagnostic(self) -> String {
        match self {
            Self::Exited(code) => format!("worker exited with status {code}"),
            Self::Signaled(signal) => format!("worker terminated by signal {signal}"),
        }
    }
}

struct WorkerRetirementFailure {
    message: String,
    outcome: Option<WorkerProcessOutcome>,
    can_replace: bool,
}

impl WorkerRetirementFailure {
    fn new(message: String, outcome: Option<WorkerProcessOutcome>) -> Self {
        Self {
            message,
            outcome,
            can_replace: false,
        }
    }

    fn attach_to(self, mut failure: SendFailure) -> SendFailure {
        failure.message.push_str(&format!(
            "; additionally failed to stop the worker: {}",
            self.message
        ));
        failure.worker_outcome(self.outcome)
    }
}

impl From<String> for WorkerRetirementFailure {
    fn from(message: String) -> Self {
        Self::new(message, None)
    }
}

#[derive(Clone)]
struct WorkerCallbacks {
    client: Client,
    generation: WorkerGeneration,
}

enum WorkerState {
    Initial,
    Stopped,
    Running(platform::Worker),
}

#[derive(Clone, Copy)]
enum WorkerRetirement {
    NeverStarted,
    AlreadyStopped,
    Stopped {
        outcome: Option<WorkerProcessOutcome>,
        failed: bool,
    },
}

impl WorkerState {
    fn stop_failed(&mut self) -> Result<WorkerRetirement, WorkerRetirementFailure> {
        match self {
            Self::Running(worker) => {
                let retirement = worker.shutdown_after_failure();
                let failed = worker.has_failure();
                *self = Self::Stopped;
                let outcome = retirement?;
                let failed =
                    failed.map_err(|message| WorkerRetirementFailure::new(message, outcome))?;
                Ok(WorkerRetirement::Stopped { outcome, failed })
            }
            Self::Initial => Ok(WorkerRetirement::NeverStarted),
            Self::Stopped => Ok(WorkerRetirement::AlreadyStopped),
        }
    }

    fn finish_retirement(&mut self) -> Result<WorkerRetirement, String> {
        match self {
            Self::Running(worker) => {
                let outcome = worker.finish_retirement()?;
                let failed = worker.has_failure()?;
                *self = Self::Stopped;
                Ok(WorkerRetirement::Stopped { outcome, failed })
            }
            Self::Initial => Ok(WorkerRetirement::NeverStarted),
            Self::Stopped => Ok(WorkerRetirement::AlreadyStopped),
        }
    }
}

#[derive(Clone)]
struct ActiveEvaluation {
    evaluation: Arc<Evaluation>,
    language: crate::cell::Language,
    initial_requirements: Arc<Mutex<Option<Requirements>>>,
}

impl Client {
    pub(crate) fn pending() -> Self {
        let (startup, _) = tokio::sync::watch::channel(None);
        let lifecycle = LifecycleControl::new();
        let startup_generation = lifecycle.generation.clone();
        Self(Arc::new(ClientInner {
            configuration: OnceLock::new(),
            startup: Mutex::new(startup),
            startup_generation,
            startup_observation_complete: AtomicBool::new(false),
            evaluation: Mutex::new(None),
            admission: tokio::sync::RwLock::new(()),
            preparation: tokio::sync::RwLock::new(()),
            output: OutputTape::new(),
            lifecycle: Mutex::new(lifecycle),
            recording: Mutex::new(None),
            startup_failed: AtomicBool::new(false),
            startup_stdin: Mutex::new(String::new()),
        }))
    }

    pub(crate) fn configure(&self, configuration: ClientConfiguration) {
        assert!(self.0.configuration.set(configuration).is_ok());
        self.0.unused_default.store(
            self.0.startup_source.is_none()
                && self.0.environment.as_ref().is_some_and(|environment| {
                    !environment
                        .lock()
                        .expect("worker environment lock")
                        .custom_worker
                }),
            Ordering::Release,
        );
    }

    pub(crate) fn finish_startup(&self, result: Result<(), String>) {
        self.0
            .startup
            .lock()
            .expect("startup result lock")
            .send_if_modified(|outcome| {
                if outcome.is_some() {
                    return false;
                }
                *outcome = Some(result);
                self.0
                    .startup_observation_complete
                    .store(true, Ordering::Release);
                true
            });
    }

    fn take_startup_failure(&self, generation: &WorkerGeneration) -> Result<bool, String> {
        let lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned")?;
        lifecycle.ensure_generation(generation)?;
        Ok(self.0.startup_failed.swap(false, Ordering::AcqRel))
    }

    pub(crate) async fn ready(&self) -> Result<(), String> {
        Self::wait_for_startup(self.startup_result()).await
    }

    fn startup_result(&self) -> tokio::sync::watch::Receiver<Option<Result<(), String>>> {
        self.0
            .startup
            .lock()
            .expect("startup result lock")
            .subscribe()
    }

    async fn wait_for_startup(
        mut result: tokio::sync::watch::Receiver<Option<Result<(), String>>>,
    ) -> Result<(), String> {
        let ready = result
            .wait_for(Option::is_some)
            .await
            .map_err(|_| "runtime startup stopped without a result".to_string())?;
        ready.as_ref().expect("startup completed").clone()
    }

    pub(crate) fn is_configured(&self) -> bool {
        self.0.configuration.get().is_some()
    }

    pub(crate) fn startup_finished(&self) -> bool {
        self.0
            .startup
            .lock()
            .expect("startup result lock")
            .borrow()
            .is_some()
    }

    pub(crate) fn retry_failed_startup(&self) -> bool {
        assert!(!self.is_configured());
        let active = self.0.evaluation.lock().expect("worker evaluation lock");
        let mut startup = self.0.startup.lock().expect("startup result lock");
        if !matches!(*startup.borrow(), Some(Err(_))) {
            return false;
        }
        // Match early admission's evaluation/startup lock order. Keep the failed
        // cell pollable, but exclude its declaration from the new attempt.
        if let Some(active) = active.as_ref() {
            *active
                .initial_requirements
                .lock()
                .expect("initial requirements lock") = None;
        }
        // Existing observers keep the failed attempt. A rejected cell must
        // never become runnable when a new attempt is installed.
        *startup = tokio::sync::watch::channel(None).0;
        self.0
            .startup_observation_complete
            .store(false, Ordering::Release);
        self.0
            .startup_stdin
            .lock()
            .expect("startup stdin lock")
            .clear();
        true
    }

    /// Launch the default process through the ordinary readiness/retirement path.
    /// Language runtimes retain their existing first-use initialization.
    pub(crate) fn prelaunch(&self, generation: &WorkerGeneration) {
        let result = (|| -> Result<(), SendFailure> {
            self.ensure_startup(generation)?;
            // An already admitted declaration selects the initial candidate through
            // the same transaction the evaluator uses after readiness.
            if let Some(active) = self.current_evaluation()? {
                let requirements = active
                    .initial_requirements
                    .lock()
                    .map_err(|_| "initial requirements lock poisoned".to_string())?
                    .take();
                if let Some(requirements) = requirements {
                    if let Err(error) = self
                        .validate_language(active.language)
                        .and_then(|()| self.validate_requirements(&requirements))
                    {
                        Response::tool_error(error).recover_to(self.0.output.clone());
                        active.evaluation.complete_cell(Ok(()));
                    } else {
                        self.prepare_cell_requirements(requirements, generation)?;
                    }
                }
            }
            let _preparation = self.0.preparation.blocking_read();
            let mut worker = self
                .0
                .worker
                .lock()
                .map_err(|_| "worker lock poisoned".to_string())?;
            if let Err(mut failure) = self.start_worker(
                &mut worker,
                generation.clone(),
                false,
                |handle| self.register_stop_handle(generation, handle),
                || Ok(()),
            ) {
                if let Err(error) = self.clear_worker_stop_handle(generation) {
                    failure.message.push_str(&format!("; {error}"));
                }
                return Err(failure);
            }
            Ok(())
        })();
        if let Err(failure) = result {
            let lifecycle = self.0.lifecycle.lock().expect("worker lifecycle lock");
            if lifecycle.state == lifecycle::LifecycleState::Ready
                && lifecycle.generation.is(generation)
            {
                if let Some(recording) = self.0.recording.lock().expect("recording lock").as_ref() {
                    recording.startup_failed(&failure.message);
                }
                self.0.output.push_failure(failure);
                self.0.startup_failed.store(true, Ordering::Release);
            }
        }
    }

    pub(crate) fn diagnostics(&self) -> crate::process_output::Diagnostics {
        self.0.output.diagnostics()
    }

    pub(crate) fn startup_failure_response(&self, error: String) -> Response {
        let mut response = self.0.output.take();
        response.push_tool_error(error);
        response
    }

    pub(crate) fn finish_recording(&self) {
        self.0.output.finish_session_output();
    }

    pub(crate) fn record_with(&self, transcript: crate::transcript::Transcript) {
        self.0.output.record_session_output(transcript.clone());
        *self.0.recording.lock().expect("recording lock") = Some(transcript);
    }
}

impl WorkerCallbacks {
    fn finish_bootstrap(
        &self,
        operation: &events::WorkerOperationState,
        interrupted: bool,
    ) -> Result<(), String> {
        // Keep the receipt cutoff atomic with cell publication, including an
        // evaluator that has not attached to the worker operation yet.
        let active = self.client.evaluation()?;
        let evaluation = active
            .as_ref()
            .filter(|active| active.evaluation.admission.generation.is(&self.generation))
            .map(|active| active.evaluation.as_ref());
        operation.finish_bootstrap(interrupted, evaluation)
    }

    fn resolve_r(
        &self,
        packages: Vec<String>,
    ) -> Result<crate::resolver::ManagedR, RuntimeRResolutionFailure> {
        self.client
            .resolve_runtime_r(self.generation.clone(), packages)
    }

    fn activate_r(
        &self,
        library: String,
        candidates: &mut Vec<crate::resolver::ManagedR>,
    ) -> Result<OldGenerationCommitDisposition, String> {
        self.client
            .activate_runtime_r(self.generation.clone(), library, candidates)
    }

    fn fail_r_activation(
        &self,
        library: String,
        candidates: &mut Vec<crate::resolver::ManagedR>,
    ) -> Result<OldGenerationCommitDisposition, String> {
        self.client
            .fail_runtime_r_activation(self.generation.clone(), library, candidates)
    }

    fn resolve_python(
        &self,
        request: crate::worker_protocol::PythonResolveRequest,
        duckdb_extensions: Option<std::collections::BTreeSet<String>>,
        deliberate: Option<crate::worker_protocol::PythonRequirementManifest>,
    ) -> Result<PythonCandidate, String> {
        self.client.resolve_runtime_python(
            self.generation.clone(),
            request,
            duckdb_extensions,
            deliberate,
        )
    }

    fn fail_python_activation(&self) -> Result<OldGenerationCommitDisposition, String> {
        self.client
            .require_restart_for_requirement_changes(&self.generation)
    }

    fn resolve_python_version(
        &self,
        request: crate::worker_protocol::PythonVersionResolveRequest,
    ) -> Result<String, String> {
        self.client
            .resolve_runtime_python_version(self.generation.clone(), request)
    }

    fn activate_python(
        &self,
        requirements: crate::worker_protocol::PythonRequirementManifest,
        candidate: Option<crate::resolver::ManagedPython>,
        configuration: Option<crate::python::NativePython>,
        duckdb_extensions: Option<std::collections::BTreeSet<String>>,
    ) -> Result<OldGenerationCommitDisposition, String> {
        self.client.activate_runtime_python(
            self.generation.clone(),
            requirements,
            candidate,
            configuration,
            duckdb_extensions,
        )
    }
}
