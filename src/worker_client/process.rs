#[cfg(windows)]
use crate::windows::ExitStatusExt as _;
use std::collections::HashMap;
use std::ffi::OsString;
use std::io::BufReader;
#[cfg(unix)]
use std::os::unix::process::ExitStatusExt as _;
use std::process::{Child, Command, ExitStatus};
use std::sync::{Arc, Condvar, Mutex, mpsc};
use std::thread;
use std::time::{Duration, Instant};

use super::events::{
    OperationResult, ReadyCommitOutcome, WorkerEvent, WorkerEventDispatcher, WorkerOperationState,
};
use super::output::SendFailure;
use super::transport::{
    self, OutputNotifier, PreparedTransport, RelayInput, RelayOutput, WriterAbort,
};
use super::{
    PreparationOutcome, PythonPreparationCommit, RPreparationCommit, WorkerProcessOutcome,
};
use crate::process_exit::ChildExitWaiter;
use crate::relay_protocol::{JsonlReader, JsonlWriter, RelayCommand, RelayEvent};

/// Lets the relay finish direct-worker shutdown, stream draining, and protocol
/// flushing after the worker's deadline before the outer fail-safe stops it.
const RELAY_RETIREMENT_GRACE: Duration = Duration::from_secs(2);
/// Lets the owned launcher complete its bounded child cleanup after SIGTERM.
const LAUNCHER_RETIREMENT_GRACE: Duration = Duration::from_secs(6);
const LAUNCHER_KILL_GRACE: Duration = Duration::from_secs(1);
const CHILD_EXIT_FALLBACK_POLL_INTERVAL: Duration = Duration::from_millis(10);

#[derive(Clone, Copy, PartialEq, Eq)]
enum RelayRetirementAllowance {
    TimelyLocalObservation,
    Always,
}

/// Spawns workers through the platform's runtime boundary.
pub(super) struct WorkerRuntime;

pub(super) struct Worker {
    stdin: StdinSender,
    operation: WorkerOperationState,
    interrupts: InterruptRequests,
    shutdown_started: LocalShutdownObservation,
    ready_commit: ReadyCommit,
    relay: RelayConnection,
}

/// Requests deadline-bounded shutdown while `Worker` retains the I/O task joins.
#[derive(Clone)]
pub(super) struct WorkerShutdownHandle {
    stdin: StdinSender,
    commands: RelayCommandSender,
    operation: WorkerOperationState,
    interrupts: InterruptRequests,
    shutdown_started: LocalShutdownObservation,
    ready_commit: ReadyCommit,
    child: Arc<Mutex<RelayProcess>>,
    retirement: Arc<GenerationRetirement>,
    output_stop: RelayOutputStop,
}

struct RelayConnection {
    child: Arc<Mutex<RelayProcess>>,
    commands: RelayCommandSender,
    retirement: Arc<GenerationRetirement>,
    output_stop: RelayOutputStop,
}

struct RelayProcess {
    temporary: Option<crate::local_runtime::TemporaryDirectory>,
    temporary_retirement: Result<(), String>,
    child: Child,
    retirement_grace: Duration,
    no_sandbox: bool,
    exit: ChildExitWaiter,
    exited: bool,
    reaped: bool,
    ready_committed: bool,
    relay_exit_recovery_expected: bool,
    retirement_requested: bool,
    retirement_deadline: Option<Instant>,
    kill_deadline: Option<Instant>,
    retirement: Option<Result<(), String>>,
}

struct RelayTasks {
    dispatcher: WorkerEventDispatcher,
    command_writer: RelayCommandThread,
    event_reader: thread::JoinHandle<()>,
    diagnostic_reader: Option<thread::JoinHandle<()>>,
}

/// One operation for this launch, shared by lifecycle, startup and failure
/// consumers. Reserving it never blocks on a callback, child or task join.
struct GenerationRetirement {
    state: Mutex<RetirementState>,
    changed: Condvar,
}

#[derive(Default)]
struct RetirementState {
    budgets: Option<(Instant, Instant)>,
    requesting: bool,
    requested: Option<(RelayRetirementAllowance, Result<(), String>)>,
    finishing: bool,
    tasks: Option<Box<RelayTasks>>,
    result: Option<RetirementResult>,
}

#[derive(Clone)]
struct RetirementResult {
    // A failed normal barrier is safely superseded only after physical cleanup
    // and the dispatcher join settle it. Retain it separately from cleanup.
    normal: Result<(), String>,
    cleanup: Result<(), String>,
    io: Result<Option<WorkerProcessOutcome>, String>,
    launcher_reaped: bool,
    // Replacement evidence is separate from native launcher status policy.
    resources_confirmed: bool,
}

impl RetirementResult {
    fn process_result(&self) -> Result<(), String> {
        // Both consumers observe this same completed operation. The process
        // observer reports cleanup; Worker reports the retained I/O outcome.
        // Combining I/O here would report its failure again when Worker stops.
        // Reaping and successful I/O settlement also supersede the normal
        // barrier when launcher status or temporary-storage cleanup fails.
        // Those independent cleanup failures still retain their own evidence.
        if self.cleanup.is_ok() || self.launcher_reaped && self.io.is_ok() {
            self.cleanup.clone()
        } else {
            combine_shutdown_results(self.normal.clone(), self.cleanup.clone())
        }
    }
}

#[derive(Clone)]
struct RelayOutputStop(Arc<Mutex<Option<OutputNotifier>>>);

impl RelayOutputStop {
    fn stop(&self) {
        drop(self.0.lock().expect("relay output stop lock").take());
    }
}

struct RelayCommandThread {
    sender: RelayCommandSender,
    thread: thread::JoinHandle<()>,
}

#[derive(Clone)]
pub(super) struct RelayCommandSender {
    state: Arc<Mutex<RelayCommandState>>,
    events: mpsc::Sender<WorkerEvent>,
}

struct RelayCommandState {
    writer: Option<mpsc::Sender<RelayWriterMessage>>,
    abort: Option<WriterAbort>,
    failure: Option<String>,
}

enum RelayWriterMessage {
    Command(RelayCommand),
    Shutdown {
        deadline: Instant,
        completed: mpsc::SyncSender<Result<(), String>>,
    },
}

#[derive(Clone, Default)]
/// Times local dispatcher processing of ShutdownStarted. This is the existing
/// relay-drain allowance policy, not the relay's remote acceptance timestamp.
pub(super) struct LocalShutdownObservation(Arc<Mutex<Option<ShutdownRequest>>>);

struct ShutdownRequest {
    deadline: Instant,
    observed: Option<Instant>,
}

#[derive(Clone)]
pub(super) struct StdinSender(RelayCommandSender);

#[derive(Clone)]
/// Correlates concurrent relay interrupt commands with their completion events.
pub(super) struct InterruptRequests(Arc<Mutex<InterruptRequestState>>);

struct InterruptRequestState {
    next_request_id: u64,
    pending: HashMap<u64, mpsc::SyncSender<Result<(), String>>>,
    failure: Option<String>,
}

#[derive(Clone, Default)]
struct ReadyCommit(Arc<Mutex<Option<ReadyCommitSender>>>);

type ReadyCommitSender = mpsc::Sender<ReadyCommitOutcome>;

impl WorkerRuntime {
    /// Starts a relay and waits for its worker's ready message.
    pub(super) fn spawn(
        &self,
        spec: super::WorkerSpec<'_>,
        output: super::OutputTape,
        on_started: impl FnOnce(WorkerShutdownHandle) -> Result<(), String>,
        on_ready: impl FnOnce() -> Result<(), String>,
    ) -> Result<Worker, SendFailure> {
        let super::WorkerSpec {
            builtin,
            languages,
            local_runtime,
            executable,
            arguments,
            relay,
            no_sandbox,
            sandbox_settings,
            startup_source,
            duckdb_extension_directory,
            resolver_matplotlib_cache,
            python,
            managed_r,
            dynamic_resolution,
            callbacks,
        } = spec;

        let current_executable = std::env::current_exe()
            .map_err(|error| format!("failed to locate the current executable: {error}"))?;
        let relay_target = relay_command_line(&current_executable, executable, arguments, relay);
        let mut command = if no_sandbox {
            let mut command = Command::new(&relay_target[0]);
            crate::settings::configure_environment(&mut command, sandbox_settings);
            command
                .args(&relay_target[1..])
                .env_remove(crate::settings::ENVIRONMENT);
            command
        } else {
            let mut command = Command::new(&current_executable);
            command
                .arg("sandbox")
                .arg("--exit-with-parent")
                .arg(std::process::id().to_string());
            command.args(["--settings-env", crate::settings::ENVIRONMENT]);
            command.arg("--").args(relay_target);
            command
        };
        let temporary = if no_sandbox && local_runtime.is_some() {
            Some(crate::local_runtime::TemporaryDirectory::create()?)
        } else {
            None
        };
        if let Some(temporary) = &temporary {
            command.env("TMPDIR", temporary.path());
        }
        command.env_remove(crate::settings::startup::ENVIRONMENT);
        // Retain the private file through worker readiness; failures also drop
        // its owner. The worker consumes it before any interpreter can run.
        let startup_transport = if builtin && let Some(startup) = startup_source {
            Some(startup.configure(&mut command)?)
        } else {
            None
        };
        command.env_remove("MCP_CONSOLE_MATPLOTLIB_CACHE");
        if !no_sandbox
            && cfg!(unix)
            && python.is_none_or(|python| python.managed().is_some())
            && let Some(cache) = resolver_matplotlib_cache
        {
            command.env("MCP_CONSOLE_MATPLOTLIB_CACHE", cache);
        }
        // Never accept an ambient internal selection for custom workers.
        command.env_remove(crate::local_runtime::ENVIRONMENT);
        if let Some(python) = python {
            python.configure_worker(&mut command);
        }
        if let Some(runtime) = local_runtime {
            runtime.configure(&mut command)?;
        }
        command.env_remove("MCP_CONSOLE_R_LIBRARY");
        if let Some(managed_r) = managed_r {
            managed_r.configure_worker(&mut command)?;
        }
        // Managed Python carries its prepared cache in the runtime selection.
        // Managed R and custom workers need the startup-captured cache;
        // a custom worker may accept its first R layer after launch.
        if (!builtin || managed_r.is_some())
            && let Some(directory) = duckdb_extension_directory
        {
            command.env(crate::local_runtime::DUCKDB_EXTENSION_DIRECTORY, directory);
        }
        command.env(
            "MCP_CONSOLE_DYNAMIC_ENVIRONMENT_RESOLUTION",
            if dynamic_resolution { "1" } else { "0" },
        );
        if let Some(languages) = languages {
            languages.configure(&mut command);
        }
        if !no_sandbox {
            let mut settings = sandbox_settings.clone();
            if let Some(transport) = &startup_transport {
                transport.preserve_access(&mut settings)?;
            }
            crate::settings::preserve_environment(&mut settings, command.get_envs())?;
            command.env(
                crate::settings::ENVIRONMENT,
                serde_json::to_string(&settings)
                    .map_err(|error| format!("cannot encode sandbox settings: {error}"))?,
            );
        }
        transport::configure_stdio(&mut command);
        #[cfg(unix)]
        crate::process_descriptors::close_unlisted_from_multithreaded_parent(&mut command)?;

        let (worker_events, worker_event_receiver) = mpsc::channel();
        let (transport, notify_output_exit, abort_writer) = PreparedTransport::new(&mut command)?;
        let child = command
            .spawn()
            .map_err(|error| format!("failed to launch worker relay: {error}"))?;
        drop(command);
        let output_stop = RelayOutputStop(Arc::new(Mutex::new(Some(notify_output_exit))));
        let mut child = RelayProcess::new(
            child,
            no_sandbox,
            LAUNCHER_RETIREMENT_GRACE,
            output_stop.clone(),
        )
        .map_err(|error| format!("failed to monitor worker relay: {error}"))?;
        child.temporary = temporary;
        let events = worker_events.clone();
        let transport =
            transport.connect(&mut child.child, output.diagnostics(), move |error| {
                let _ = events.send(WorkerEvent::TransportFailure(format!(
                    "launcher stderr read failed: {error}"
                )));
            })?;
        let child = Arc::new(Mutex::new(child));

        let operation = WorkerOperationState::new(builtin);
        let interrupts = InterruptRequests::new();
        let (startup_sender, startup_receiver) = mpsc::sync_channel(1);
        let (ready_commit_sender, ready_commit_receiver) = mpsc::channel();
        let ready_commit = ReadyCommit(Arc::new(Mutex::new(Some(ready_commit_sender))));
        let shutdown_started = LocalShutdownObservation::default();

        let (commands, command_writer) =
            start_relay_command_writer(transport.input, abort_writer, worker_events.clone());
        let event_reader = start_relay_event_reader(transport.output, worker_events);
        let dispatcher = WorkerEventDispatcher::start(
            worker_event_receiver,
            operation.clone(),
            commands.clone(),
            output.clone(),
            callbacks,
            startup_sender,
            ready_commit_receiver,
            interrupts.clone(),
            shutdown_started.clone(),
        );

        let relay = RelayConnection {
            child,
            commands: commands.clone(),
            retirement: Arc::new(GenerationRetirement {
                state: Mutex::new(RetirementState {
                    tasks: Some(Box::new(RelayTasks {
                        dispatcher,
                        command_writer,
                        event_reader,
                        diagnostic_reader: transport.diagnostic_reader,
                    })),
                    ..Default::default()
                }),
                changed: Condvar::new(),
            }),
            output_stop,
        };
        let mut worker = Worker {
            stdin: StdinSender(commands.clone()),
            operation,
            interrupts,
            shutdown_started,
            ready_commit,
            relay,
        };

        if let Err(error) = on_started(worker.shutdown_handle()) {
            let error = worker.startup_failure(error);
            return Err(error);
        }
        let started = startup_receiver
            .recv()
            .map_err(|_| "worker event dispatcher stopped before readiness".to_string());
        match started {
            Ok(Ok(())) => {}
            Ok(Err(error)) => {
                return Err(worker.startup_failure(error));
            }
            Err(error) => {
                return Err(worker.startup_failure(error));
            }
        }
        if let Err(error) = on_ready() {
            let error = worker.startup_failure(error);
            return Err(error);
        }
        if !worker.ready_commit.finish(ReadyCommitOutcome::Committed) {
            return Err(
                worker.startup_failure("worker stopped before readiness was committed".to_string())
            );
        }
        if let Err(error) = worker.relay.mark_ready() {
            return Err(worker.startup_failure(error));
        }
        Ok(worker)
    }
}

fn relay_command_line(
    current_executable: &std::path::Path,
    worker_executable: &std::path::Path,
    worker_arguments: &[OsString],
    relay: Option<&std::path::Path>,
) -> Vec<OsString> {
    let mut target = match relay {
        Some(relay) => vec![relay.as_os_str().to_os_string()],
        None => vec![
            current_executable.as_os_str().to_os_string(),
            OsString::from("worker-relay"),
        ],
    };
    target.push(worker_executable.as_os_str().to_os_string());
    target.extend(worker_arguments.iter().cloned());
    target
}

impl RelayProcess {
    fn new(
        child: Child,
        no_sandbox: bool,
        retirement_grace: Duration,
        output_stop: RelayOutputStop,
    ) -> Result<Self, String> {
        let exit = match ChildExitWaiter::start_notifying(child.id(), move || {
            output_stop.stop();
        }) {
            Ok(exit) => exit,
            Err(error) => {
                return Err(retire_after_exit_observer_failure(child, error));
            }
        };
        Ok(Self {
            temporary: None,
            temporary_retirement: Ok(()),
            child,
            retirement_grace,
            no_sandbox,
            exit,
            exited: false,
            reaped: false,
            ready_committed: false,
            relay_exit_recovery_expected: false,
            retirement_requested: false,
            retirement_deadline: None,
            kill_deadline: None,
            retirement: None,
        })
    }

    fn wait_timeout_without_reaping(&mut self, timeout: Duration) -> Result<bool, String> {
        if self.has_exited()? {
            return Ok(true);
        }
        self.exited = self.exit.wait(timeout)?;
        Ok(self.exited)
    }

    fn request_retirement(&mut self) -> Result<(), String> {
        if self.retirement_deadline.is_some() {
            return Ok(());
        }
        self.retirement_deadline = Some(Instant::now() + self.retirement_grace);
        let mut errors = Vec::new();
        match self.has_exited() {
            Ok(true) => return Ok(()),
            Ok(false) => {}
            Err(error) => errors.push(error),
        }
        if cfg!(windows) && !self.no_sandbox {
            // Shutdown/EOF is already queued to the relay. Windows has no
            // SIGTERM equivalent: killing the waiting frontend would discard
            // the native runner's Job-retirement receipt. Wait for that receipt
            // within the existing deadline; forced termination remains failure.
            return collected_errors(errors);
        }
        // SAFETY: the direct child remains unreaped here, so its PID cannot be
        // reused before `kill` returns.
        if let Err(error) = request_child_retirement(&mut self.child) {
            if error.raw_os_error() != Some(libc::ESRCH) {
                errors.push(format!(
                    "failed to request worker launcher retirement: {error}"
                ));
            }
        } else {
            self.retirement_requested = true;
        }
        collected_errors(errors)
    }

    fn retire_launcher(
        &mut self,
        before_reap: impl FnOnce(&mut Self) -> Result<(), String>,
    ) -> Result<(), String> {
        if self.reaped {
            return self.retirement.clone().unwrap_or(Ok(()));
        }
        let requested = self.request_retirement();
        let observed = self.wait_timeout_without_reaping(
            self.retirement_deadline
                .expect("launcher retirement deadline")
                .saturating_duration_since(Instant::now()),
        );
        // Capture generation evidence at the original boundary: after launcher
        // grace and before reaping or final force-stop classification.
        let prepared = before_reap(self);
        let cleanup = match observed {
            Ok(true) => self.reap(),
            outcome => {
                let error = match outcome {
                    Ok(false) => format!(
                        "worker launcher did not retire within {} ms",
                        self.retirement_grace.as_millis()
                    ),
                    Err(error) => error,
                    Ok(true) => unreachable!(),
                };
                combine_shutdown_results(Err(error), self.force_stop_inner())
            }
        };
        let cleanup =
            combine_shutdown_results(combine_shutdown_results(requested, prepared), cleanup);
        let prior = self.retirement.take();
        let result = match (prior, cleanup) {
            (None | Some(Ok(())), cleanup) => cleanup,
            (Some(Err(error)), Ok(())) => Err(error),
            (Some(Err(error)), Err(cleanup_error)) => {
                Err(format!("{error}; additionally {cleanup_error}"))
            }
        };
        self.finish_retirement(result)
    }

    fn force_stop_inner(&mut self) -> Result<(), String> {
        let mut errors = Vec::new();
        match self.has_exited() {
            Ok(true) => return self.reap(),
            Ok(false) => {}
            Err(error) => errors.push(error),
        }
        if let Err(error) = kill_child(&mut self.child)
            && error.raw_os_error() != Some(libc::ESRCH)
        {
            errors.push(format!("failed to stop the worker launcher: {error}"));
            return collected_errors(errors);
        }
        let kill_deadline = *self
            .kill_deadline
            .get_or_insert_with(|| Instant::now() + LAUNCHER_KILL_GRACE);
        let mut recovered_status = None;
        let exited = match self
            .exit
            .wait(kill_deadline.saturating_duration_since(Instant::now()))
        {
            Ok(true) => {
                self.exited = true;
                true
            }
            Ok(false) => false,
            Err(error) => {
                errors.push(error);
                recovered_status = observe_and_reap_child(
                    &mut self.child,
                    kill_deadline.saturating_duration_since(Instant::now()),
                    &mut errors,
                );
                recovered_status.is_some()
            }
        };
        if let Some(status) = recovered_status {
            if let Err(error) = self.finish_reaped_status(status) {
                errors.push(error);
            }
        } else if exited {
            if let Err(error) = self.reap() {
                errors.push(error);
            }
        } else {
            errors.push(format!(
                "worker launcher remained live for {} ms after forced termination",
                LAUNCHER_KILL_GRACE.as_millis(),
            ));
            match self.child.try_wait() {
                Ok(Some(status)) => {
                    if let Err(error) = self.finish_reaped_status(status) {
                        errors.push(error);
                    }
                }
                Ok(None) => {}
                Err(error) => errors.push(format!(
                    "failed to inspect the worker launcher after forced termination: {error}"
                )),
            }
        }
        collected_errors(errors)
    }

    fn finish_retirement(&mut self, result: Result<(), String>) -> Result<(), String> {
        self.retirement = Some(result.clone());
        result
    }

    fn reap(&mut self) -> Result<(), String> {
        if self.reaped {
            return Ok(());
        }
        let status = self
            .child
            .wait()
            .map_err(|error| format!("failed to reap the worker launcher: {error}"))?;
        self.finish_reaped_status(status)
    }

    fn finish_reaped_status(&mut self, status: ExitStatus) -> Result<(), String> {
        self.exited = true;
        self.reaped = true;
        if let Some(mut temporary) = self.temporary.take() {
            self.temporary_retirement = temporary.retire();
            self.temporary_retirement.clone()?;
        }
        // A direct relay's exit is redundant when its EOF established the
        // worker failure. The sandbox runner still owes cleanup; status 137
        // records a target SIGKILL already reported by that same relay failure.
        // A direct relay also terminates normally under our own SIGTERM request
        // or Windows process-handle termination, which has no signal status.
        if !self.ready_committed
            || status.success()
            || self.no_sandbox
                && self.retirement_requested
                && (cfg!(windows) || status.signal() == Some(libc::SIGTERM))
            || self.relay_exit_recovery_expected
                && (self.no_sandbox || status.code() == Some(128 + 9))
        {
            Ok(())
        } else if let Some(code) = status.code() {
            Err(format!("worker launcher exited with status {code}"))
        } else if let Some(signal) = status.signal() {
            Err(format!("worker launcher terminated by signal {signal}"))
        } else {
            Err("worker launcher exited without a status code or signal".to_string())
        }
    }

    fn has_exited(&mut self) -> Result<bool, String> {
        if self.exited || self.reaped {
            return Ok(true);
        }
        self.exited = self.exit.wait(Duration::ZERO)?;
        Ok(self.exited)
    }

    fn is_reaped(&self) -> bool {
        self.reaped
    }
}

impl Drop for RelayProcess {
    fn drop(&mut self) {
        if self.reaped {
            return;
        }
        let _ = self.retire_launcher(|_| Ok(()));
    }
}

fn retire_after_exit_observer_failure(mut child: Child, error: String) -> String {
    let mut errors = vec![error];
    // SAFETY: the direct child remains unreaped, so its PID cannot be reused
    // before the signal is delivered.
    if let Err(signal_error) = request_child_retirement(&mut child)
        && signal_error.raw_os_error() != Some(libc::ESRCH)
    {
        errors.push(format!(
            "failed to request worker launcher retirement: {signal_error}"
        ));
    }

    let mut reaped =
        observe_and_reap_child(&mut child, LAUNCHER_RETIREMENT_GRACE, &mut errors).is_some();
    if !reaped {
        if let Err(kill_error) = kill_child(&mut child)
            && kill_error.raw_os_error() != Some(libc::ESRCH)
        {
            errors.push(format!("failed to stop the worker launcher: {kill_error}"));
        }
        reaped = observe_and_reap_child(&mut child, LAUNCHER_KILL_GRACE, &mut errors).is_some();
    }
    if !reaped {
        errors.push(format!(
            "worker launcher remained live for {} ms after forced termination",
            LAUNCHER_KILL_GRACE.as_millis(),
        ));
    }
    errors.join("; additionally ")
}

fn observe_and_reap_child(
    child: &mut Child,
    timeout: Duration,
    errors: &mut Vec<String>,
) -> Option<ExitStatus> {
    let deadline = Instant::now()
        .checked_add(timeout)
        .unwrap_or_else(Instant::now);
    match ChildExitWaiter::start(child.id()) {
        Ok(mut exit) => match exit.wait(timeout) {
            Ok(true) => {
                return match child.wait() {
                    Ok(status) => Some(status),
                    Err(error) => {
                        errors.push(format!("failed to reap the worker launcher: {error}"));
                        None
                    }
                };
            }
            Ok(false) => {}
            Err(error) => errors.push(error),
        },
        Err(error) => errors.push(error),
    }

    // Thread creation or exit observation failed. Poll only in this exceptional
    // path so the launcher still receives a bounded grace period and is reaped.
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return Some(status),
            Ok(None) => {}
            Err(error) => {
                errors.push(format!(
                    "failed to inspect the worker launcher before releasing it: {error}"
                ));
                return None;
            }
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return None;
        }
        thread::sleep(remaining.min(CHILD_EXIT_FALLBACK_POLL_INTERVAL));
    }
}

fn collected_errors(errors: Vec<String>) -> Result<(), String> {
    match errors.split_first() {
        None => Ok(()),
        Some((first, rest)) => Err(rest.iter().fold(first.clone(), |mut error, additional| {
            error.push_str("; additionally ");
            error.push_str(additional);
            error
        })),
    }
}

impl Worker {
    pub(super) fn reserve_environment_preparation(
        &self,
        replacing: bool,
    ) -> Result<
        super::events::EnvironmentPreparationReservation,
        super::EnvironmentPreparationAdmissionFailure,
    > {
        self.operation
            .reserve_environment_preparation(replacing, self.relay.commands.events.clone())
    }

    pub(super) fn prepare_r(
        &mut self,
        library: &std::path::Path,
        commit: RPreparationCommit,
    ) -> Result<PreparationOutcome, String> {
        let library = library
            .to_str()
            .ok_or_else(|| "resolved R library path is not UTF-8".to_string())?
            .to_string();
        let result = self
            .operation
            .begin_r_preparation(library.clone(), commit)?;
        self.relay
            .commands
            .send(RelayCommand::PrepareR { library })?;
        match receive_operation(result)? {
            OperationResult::RPrepared(result) => Ok(result),
            _ => Err("worker sent an unexpected R preparation message".to_string()),
        }
    }

    pub(super) fn prepare_python(
        &mut self,
        packages: Vec<String>,
        requirements: crate::worker_protocol::PythonRequirementManifest,
        continue_environment_preparation: bool,
        duckdb_extensions: Option<std::collections::BTreeSet<String>>,
        commit: PythonPreparationCommit,
    ) -> Result<PreparationOutcome, String> {
        let result = self.operation.begin_python_preparation(
            commit,
            continue_environment_preparation,
            duckdb_extensions,
            requirements,
        )?;
        self.relay
            .commands
            .send(RelayCommand::PreparePython { packages })?;
        match receive_operation(result)? {
            OperationResult::PythonPrepared(result) => Ok(result),
            _ => Err("worker sent an unexpected Python preparation message".to_string()),
        }
    }

    pub(super) fn wait_for_bootstrap(&self) -> Result<(), String> {
        self.operation.wait_for_bootstrap()
    }

    pub(super) fn evaluate(
        &mut self,
        cell: crate::cell::Cell,
        evaluation: Arc<super::Evaluation>,
        capture_idle_prelude: bool,
    ) -> Result<(), String> {
        let result = self
            .operation
            .begin_cell(evaluation.clone(), capture_idle_prelude)?;
        let crate::cell::Cell { language, source } = cell;
        // Attaching drains stdin bundled with this cell through the relay's sole
        // command sender. Do this before queuing Evaluate to preserve the public
        // stdin-then-evaluate transport order.
        if let Err(error) = evaluation.attach_writer(self.stdin.clone()) {
            self.operation.fail(error.clone());
            return Err(error);
        }
        self.operation.wait_for_bootstrap()?;
        let dispatched = self.operation.dispatch_cell(&evaluation, || {
            self.relay
                .commands
                .send(RelayCommand::Evaluate { language, source })
        });
        let dispatched = match dispatched {
            Ok(dispatched) => dispatched,
            Err(error) => {
                self.operation.fail(error.clone());
                return Err(error);
            }
        };
        if !dispatched {
            // Controller withholding survives receipt processing and delayed
            // evaluator attachment. No Evaluate was queued for this owner.
            return self.operation.abort_bootstrap_cell();
        }
        match receive_operation(result)? {
            OperationResult::Completed => Ok(()),
            _ => Err("worker sent an unexpected evaluation result".to_string()),
        }
    }

    pub(super) fn idle_response_snapshot(
        &self,
        output: &super::OutputTape,
    ) -> Result<super::IdleResponseSnapshot, String> {
        self.operation.idle_response_snapshot(output)
    }

    pub(super) fn has_failure(&self) -> Result<bool, String> {
        self.operation.has_failure()
    }

    pub(super) fn write_stdin(&self, stdin: String) -> Result<(), String> {
        self.stdin.send(stdin)
    }

    pub(super) fn shutdown_after_failure(
        &mut self,
    ) -> Result<Option<WorkerProcessOutcome>, super::WorkerRetirementFailure> {
        let deadline = Instant::now();
        let retirement_deadline = relay_retirement_deadline(deadline);
        let shutdown = self.shutdown_handle();
        shutdown.request_shutdown(deadline, retirement_deadline);
        let retirement = shutdown.retire();
        let can_replace = retirement.io.is_ok() && retirement.resources_confirmed;
        let result = match (retirement.process_result(), retirement.io) {
            (Ok(()), Ok(outcome)) => Ok(outcome),
            (Err(error), Ok(outcome)) => Err(super::WorkerRetirementFailure::new(error, outcome)),
            (Ok(()), Err(error)) => Err(super::WorkerRetirementFailure::new(error, None)),
            (Err(error), Err(retirement_error)) => Err(super::WorkerRetirementFailure::new(
                format!("{error}; additionally failed to retire worker I/O: {retirement_error}"),
                None,
            )),
        };
        result.map_err(|mut error| {
            error.can_replace = can_replace;
            error
        })
    }

    pub(super) fn finish_retirement(&mut self) -> Result<Option<WorkerProcessOutcome>, String> {
        let shutdown = self.shutdown_handle();
        let deadline = Instant::now();
        shutdown.request_shutdown(deadline, deadline);
        shutdown.finish_retirement()
    }

    pub(super) fn shutdown_handle(&self) -> WorkerShutdownHandle {
        WorkerShutdownHandle {
            stdin: self.stdin.clone(),
            commands: self.relay.commands(),
            operation: self.operation.clone(),
            interrupts: self.interrupts.clone(),
            shutdown_started: self.shutdown_started.clone(),
            ready_commit: self.ready_commit.clone(),
            child: self.relay.child.clone(),
            retirement: self.relay.retirement.clone(),
            output_stop: self.relay.output_stop.clone(),
        }
    }

    fn startup_failure(&mut self, message: String) -> SendFailure {
        self.ready_commit
            .finish(ReadyCommitOutcome::Failed(message.clone()));
        let retirement = self.shutdown_after_failure();
        match retirement {
            Ok(outcome) => SendFailure::from(message).worker_outcome(outcome),
            Err(error) => error.attach_to(SendFailure::from(message)),
        }
    }
}

fn receive_operation(
    receiver: mpsc::Receiver<Result<OperationResult, String>>,
) -> Result<OperationResult, String> {
    receiver
        .recv()
        .map_err(|_| "worker event dispatcher stopped".to_string())?
}

fn start_relay_command_writer(
    relay_stdin: RelayInput,
    abort: WriterAbort,
    events: mpsc::Sender<WorkerEvent>,
) -> (RelayCommandSender, RelayCommandThread) {
    let (writer, receiver) = mpsc::channel();
    let sender = RelayCommandSender {
        state: Arc::new(Mutex::new(RelayCommandState {
            writer: Some(writer),
            abort: Some(abort),
            failure: None,
        })),
        events: events.clone(),
    };
    let thread_sender = sender.clone();
    let thread = thread::spawn(move || {
        let result = (|| -> Result<(), String> {
            let mut writer = JsonlWriter::new(relay_stdin);
            while let Ok(message) = receiver.recv() {
                let (command, completed) = match message {
                    RelayWriterMessage::Command(command) => (command, None),
                    RelayWriterMessage::Shutdown {
                        deadline,
                        completed,
                    } => (
                        RelayCommand::Shutdown {
                            grace_millis: duration_millis_ceil(
                                deadline.saturating_duration_since(Instant::now()),
                            ),
                        },
                        Some(completed),
                    ),
                };
                let result = match thread_sender.failure() {
                    Some(error) => Err(error),
                    None => writer.send(&command).map_err(|error| {
                        thread_sender.report_command_transport_failure(format!(
                            "worker relay stdin write failed: {error}"
                        ))
                    }),
                };
                if let Some(completed) = completed {
                    let _ = completed.send(result.clone());
                }
                result?;
            }
            Ok(())
        })();
        let error = thread_sender.report_command_transport_failure(
            result
                .err()
                .unwrap_or_else(|| "worker relay command writer stopped".into()),
        );
        // Closing admission disconnects the queue. Write aborts must
        // also settle Shutdown receipts that never reached the serialization owner.
        for message in receiver.try_iter() {
            if let RelayWriterMessage::Shutdown { completed, .. } = message {
                let _ = completed.send(Err(error.clone()));
            }
        }
    });
    (sender.clone(), RelayCommandThread { sender, thread })
}

fn start_relay_event_reader(
    output: RelayOutput,
    events: mpsc::Sender<WorkerEvent>,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        let mut reader = JsonlReader::new(BufReader::new(output));
        let result = (|| -> Result<(), String> {
            while let Some(event) = reader
                .receive::<RelayEvent>()
                .map_err(|error| format!("worker relay stdout read failed: {error}"))?
            {
                events
                    .send(WorkerEvent::Relay(event))
                    .map_err(|_| "worker event dispatcher stopped".to_string())?;
            }
            Ok(())
        })();
        if let Err(error) = result {
            let _ = events.send(WorkerEvent::TransportFailure(error));
        }
        let _ = events.send(WorkerEvent::RelayClosed);
    })
}

impl RelayCommandSender {
    pub(super) fn send(&self, command: RelayCommand) -> Result<(), String> {
        self.enqueue(RelayWriterMessage::Command(command))
    }

    fn enqueue(&self, message: RelayWriterMessage) -> Result<(), String> {
        let result = {
            let state = self.state.lock().expect("relay command admission lock");
            let Some(writer) = &state.writer else {
                return Err(state
                    .failure
                    .clone()
                    .expect("closed relay command admission"));
            };
            writer.send(message)
        };
        result.map_err(|_| self.command_writer_stopped())
    }

    fn failure(&self) -> Option<String> {
        self.state
            .lock()
            .expect("relay command admission lock")
            .failure
            .clone()
    }

    pub(super) fn is_aborted(&self) -> bool {
        self.state
            .lock()
            .expect("relay command admission lock")
            .failure
            .is_some()
    }

    /// Abort this generation's descriptor independently of its queue. Taking
    /// the sole queue sender also wakes an idle writer and closes all clones'
    /// admission under the same boundary.
    pub(super) fn abort(&self, error: String) -> bool {
        let mut state = self.state.lock().expect("relay command admission lock");
        if state.failure.is_some() {
            return false;
        }
        state.failure = Some(error);
        drop(state.abort.take());
        drop(state.writer.take());
        true
    }

    fn shutdown(
        &self,
        worker_deadline: Instant,
        completion_deadline: Instant,
    ) -> Result<(), String> {
        let (completed, wait) = mpsc::sync_channel(1);
        self.enqueue(RelayWriterMessage::Shutdown {
            deadline: worker_deadline,
            completed,
        })?;
        match wait.recv_timeout(completion_deadline.saturating_duration_since(Instant::now())) {
            Ok(result) => result,
            Err(mpsc::RecvTimeoutError::Timeout) => Err(self.report_command_transport_failure(
                "worker relay shutdown command writer did not finish before the worker deadline"
                    .to_string(),
            )),
            Err(mpsc::RecvTimeoutError::Disconnected) => Err(self
                .report_command_transport_failure(
                    "worker relay shutdown command writer stopped before reporting completion"
                        .to_string(),
                )),
        }
    }

    fn retire_operation(&self, deadline: Instant, error: String) -> Result<(), String> {
        let (reached, wait) = mpsc::sync_channel(1);
        self.events
            .send(WorkerEvent::RetireOperation { error, reached })
            .map_err(|_| {
                "worker event dispatcher stopped before ordered operation retirement".to_string()
            })?;
        match wait.recv_timeout(deadline.saturating_duration_since(Instant::now())) {
            Ok(()) => Ok(()),
            Err(mpsc::RecvTimeoutError::Timeout) => Err(
                "worker event dispatcher did not retire the operation before the relay retirement deadline"
                    .to_string(),
            ),
            Err(mpsc::RecvTimeoutError::Disconnected) => Err(
                "worker event dispatcher stopped before retiring the operation".to_string(),
            ),
        }
    }

    fn command_writer_stopped(&self) -> String {
        self.report_command_transport_failure("worker relay command writer stopped".to_string())
    }

    fn report_command_transport_failure(&self, error: String) -> String {
        if self.abort(error.clone()) {
            let _ = self.events.send(WorkerEvent::TransportFailure(error));
        }
        self.failure().expect("failed relay command transport")
    }
}

impl StdinSender {
    pub(super) fn send(&self, data: String) -> Result<(), String> {
        self.0.send(RelayCommand::Stdin { data })
    }
}

impl InterruptRequests {
    fn new() -> Self {
        Self(Arc::new(Mutex::new(InterruptRequestState {
            next_request_id: 0,
            pending: HashMap::new(),
            failure: None,
        })))
    }

    fn enqueue(
        &self,
        commands: &RelayCommandSender,
    ) -> Result<mpsc::Receiver<Result<(), String>>, String> {
        let (result, receiver) = mpsc::sync_channel(1);
        let request_id = {
            let mut state = self
                .0
                .lock()
                .map_err(|_| "worker interrupt state lock poisoned".to_string())?;
            if let Some(error) = state.failure.as_ref() {
                return Err(error.clone());
            }
            let request_id = state.next_request_id;
            state.next_request_id = state.next_request_id.wrapping_add(1);
            state.pending.insert(request_id, result);
            request_id
        };
        if let Err(error) = commands.send(RelayCommand::Interrupt { request_id }) {
            self.remove(request_id);
            return Err(error);
        }
        Ok(receiver)
    }

    pub(super) fn complete(&self, request_id: u64, error: Option<String>) -> Result<(), String> {
        let result = self
            .0
            .lock()
            .map_err(|_| "worker interrupt state lock poisoned".to_string())?
            .pending
            .remove(&request_id)
            .ok_or_else(|| "worker relay sent an unexpected interrupt result".to_string())?;
        let _ = result.send(error.map_or(Ok(()), Err));
        Ok(())
    }

    pub(super) fn fail(&self, error: String) {
        let (pending, error) = match self.0.lock() {
            Ok(mut state) => {
                let error = state.failure.get_or_insert(error).clone();
                (std::mem::take(&mut state.pending), error)
            }
            Err(poisoned) => {
                let mut state = poisoned.into_inner();
                let error = state.failure.get_or_insert(error).clone();
                (std::mem::take(&mut state.pending), error)
            }
        };
        for (_, result) in pending {
            let _ = result.send(Err(error.clone()));
        }
    }

    fn remove(&self, request_id: u64) {
        let mut state = match self.0.lock() {
            Ok(state) => state,
            Err(poisoned) => poisoned.into_inner(),
        };
        state.pending.remove(&request_id);
    }
}

impl LocalShutdownObservation {
    fn request(&self, deadline: Instant) -> Result<(), String> {
        let mut request = self
            .0
            .lock()
            .map_err(|_| "worker relay shutdown state lock poisoned".to_string())?;
        if request.is_some() {
            return Err("worker relay shutdown was requested twice".to_string());
        }
        *request = Some(ShutdownRequest {
            deadline,
            observed: None,
        });
        Ok(())
    }

    pub(super) fn observe(&self) -> Result<(), String> {
        let mut request = self
            .0
            .lock()
            .map_err(|_| "worker relay shutdown state lock poisoned".to_string())?;
        let request = request.as_mut().ok_or_else(|| {
            "worker relay reported shutdown start before shutdown was requested".to_string()
        })?;
        if request.observed.replace(Instant::now()).is_some() {
            return Err("worker relay reported shutdown start twice".to_string());
        }
        Ok(())
    }

    fn observed_by_deadline(&self) -> Result<bool, String> {
        let request = self
            .0
            .lock()
            .map_err(|_| "worker relay shutdown state lock poisoned".to_string())?;
        let request = request
            .as_ref()
            .ok_or_else(|| "worker relay shutdown was not requested".to_string())?;
        Ok(request
            .observed
            .is_some_and(|observed| observed <= request.deadline))
    }
}

impl WorkerShutdownHandle {
    pub(super) fn is_same_connection(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.commands.state, &other.commands.state)
    }

    pub(super) fn reserve_failed_shutdown(&self, deadline: Instant) {
        self.reserve_shutdown(deadline, relay_retirement_deadline(deadline));
    }
    pub(super) fn write_startup_stdin(&self, data: String) -> Result<(), String> {
        self.stdin.send(data)
    }

    pub(super) fn interrupt(&self, evaluation: Option<&super::Evaluation>) -> Result<(), String> {
        let acknowledgment = self
            .operation
            .interrupt_cell(evaluation, || self.interrupts.enqueue(&self.commands))?;
        acknowledgment
            .recv()
            .map_err(|_| "worker interrupt response was not received".to_string())?
    }

    pub(super) fn is_bootstrapping(&self) -> Result<bool, String> {
        self.operation.is_bootstrapping()
    }

    /// Requests relay-owned worker shutdown and enforces bounded relay retirement.
    ///
    /// The shared generation operation joins all relay transport tasks once.
    pub(super) fn shutdown(&self, deadline: Instant) -> Result<(), String> {
        self.request_shutdown(deadline, deadline);
        self.finish_shutdown()
    }

    pub(super) fn reserve_shutdown(&self, worker_deadline: Instant, completion_deadline: Instant) {
        self.retirement
            .state
            .lock()
            .expect("generation retirement lock")
            .budgets
            .get_or_insert((worker_deadline, completion_deadline));
    }

    pub(super) fn request_shutdown(&self, worker_deadline: Instant, completion_deadline: Instant) {
        self.reserve_shutdown(worker_deadline, completion_deadline);
        let mut state = self
            .retirement
            .state
            .lock()
            .expect("generation retirement lock");
        while state.requesting {
            state = self
                .retirement
                .changed
                .wait(state)
                .expect("generation retirement lock");
        }
        if state.requested.is_some() {
            return;
        }
        state.requesting = true;
        let (worker_deadline, completion_deadline) = state.budgets.expect("reserved retirement");
        drop(state);
        let reserved = self.shutdown_started.request(worker_deadline);
        self.ready_commit.finish(ReadyCommitOutcome::Retiring);
        let requested = combine_shutdown_results(
            reserved,
            self.commands.shutdown(worker_deadline, completion_deadline),
        );
        let allowance = if requested.is_ok() && completion_deadline == worker_deadline {
            RelayRetirementAllowance::TimelyLocalObservation
        } else {
            RelayRetirementAllowance::Always
        };
        let mut state = self
            .retirement
            .state
            .lock()
            .expect("generation retirement lock");
        state.requested = Some((allowance, requested));
        state.requesting = false;
        self.retirement.changed.notify_all();
        // Transport failure is retained in the operation and dispatched to the
        // evaluation. finish_shutdown decides whether force retirement settled it.
    }

    pub(super) fn finish_shutdown(&self) -> Result<(), String> {
        self.retire().process_result()
    }

    pub(super) fn finish_retirement(&self) -> Result<Option<WorkerProcessOutcome>, String> {
        self.retire().io
    }

    fn retire(&self) -> RetirementResult {
        let mut state = self
            .retirement
            .state
            .lock()
            .expect("generation retirement lock");
        while state.requesting || state.finishing {
            state = self
                .retirement
                .changed
                .wait(state)
                .expect("generation retirement lock");
        }
        if let Some(result) = &state.result {
            return result.clone();
        }
        let (deadline, _) = state.budgets.expect("reserved retirement");
        let (allowance, requested) = state.requested.clone().expect("requested retirement");
        let tasks = state.tasks.take().expect("owned generation tasks");
        state.finishing = true;
        drop(state);
        let retirement_deadline = relay_retirement_deadline(deadline);
        let barrier_deadline = if allowance == RelayRetirementAllowance::Always {
            retirement_deadline
        } else {
            deadline
        };
        let normal = combine_shutdown_results(
            requested,
            self.commands.retire_operation(
                barrier_deadline,
                "worker stopped before operation completed".to_string(),
            ),
        );
        let cleanup = self.stop_relay(deadline, retirement_deadline, allowance);
        let (launcher_reaped, resources_confirmed) = self
            .child
            .lock()
            .map(|child| {
                (
                    child.is_reaped(),
                    child.is_reaped() && child.temporary_retirement.is_ok(),
                )
            })
            .unwrap_or((false, false));
        // Failed process cleanup must still drain available output and join all
        // owned I/O. Neither a normal-barrier timeout nor an I/O join confirms
        // native cleanup; the retained process result remains authoritative.
        self.output_stop.stop();
        let command_writer =
            join_worker_thread(tasks.command_writer.stop(), "relay command writer");
        let event_reader = join_worker_thread(tasks.event_reader, "relay event reader");
        let diagnostics = join_diagnostic_reader(tasks.diagnostic_reader);
        let outcome = tasks.dispatcher.join();
        let joined = combine_shutdown_results(
            combine_shutdown_results(command_writer, event_reader),
            diagnostics,
        );
        let io = match (joined, outcome) {
            (Ok(()), outcome) => outcome,
            (Err(error), Ok(_)) => Err(error),
            (Err(error), Err(outcome)) => Err(format!("{error}; additionally {outcome}")),
        };
        let result = RetirementResult {
            normal,
            cleanup,
            io,
            launcher_reaped,
            resources_confirmed,
        };
        let mut state = self
            .retirement
            .state
            .lock()
            .expect("generation retirement lock");
        state.result = Some(result.clone());
        state.finishing = false;
        self.retirement.changed.notify_all();
        result
    }

    fn stop_relay(
        &self,
        deadline: Instant,
        retirement_deadline: Instant,
        allowance: RelayRetirementAllowance,
    ) -> Result<(), String> {
        let mut child = self
            .child
            .lock()
            .map_err(|_| "worker child lock poisoned".to_string())?;
        if let Some(result) = child.retirement.as_ref() {
            return result.clone();
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        let mut errors = Vec::new();
        let mut exited = match child.wait_timeout_without_reaping(remaining) {
            Ok(exited) => exited,
            Err(error) => {
                errors.push(error);
                false
            }
        };
        if !exited {
            match self.should_wait_for_relay_retirement(allowance) {
                Ok(true) => {
                    match child.wait_timeout_without_reaping(
                        retirement_deadline.saturating_duration_since(Instant::now()),
                    ) {
                        Ok(observed) => exited = observed,
                        Err(error) => errors.push(error),
                    }
                }
                Ok(false) => {}
                Err(error) => errors.push(error),
            }
        }
        let cleanup = if exited {
            match self.operation.relay_exit_caused_failure() {
                Ok(expected) => child.relay_exit_recovery_expected = expected,
                Err(error) => errors.push(error),
            }
            child.reap()
        } else {
            self.commands.report_command_transport_failure(
                "worker relay command transport retired".to_string(),
            );
            child.retire_launcher(|child| {
                child.relay_exit_recovery_expected = self.operation.relay_exit_caused_failure()?;
                Ok(())
            })
        };
        if let Err(error) = cleanup {
            errors.push(error);
        }
        let result = if errors.is_empty() {
            Ok(())
        } else {
            Err(errors.join("; additionally "))
        };
        child.finish_retirement(result)
    }

    fn should_wait_for_relay_retirement(
        &self,
        allowance: RelayRetirementAllowance,
    ) -> Result<bool, String> {
        if allowance == RelayRetirementAllowance::Always || self.operation.has_failure()? {
            return Ok(true);
        }
        self.shutdown_started.observed_by_deadline()
    }
}

fn relay_retirement_deadline(deadline: Instant) -> Instant {
    deadline
        .checked_add(RELAY_RETIREMENT_GRACE)
        .unwrap_or(deadline)
}

fn combine_shutdown_results(
    first: Result<(), String>,
    second: Result<(), String>,
) -> Result<(), String> {
    match (first, second) {
        (Ok(()), Ok(())) => Ok(()),
        (Err(error), Ok(())) | (Ok(()), Err(error)) => Err(error),
        (Err(error), Err(additional)) => Err(format!("{error}; additionally {additional}")),
    }
}

fn duration_millis_ceil(duration: Duration) -> u64 {
    let millis = duration.as_millis();
    let rounded = millis + u128::from(!duration.subsec_nanos().is_multiple_of(1_000_000));
    u64::try_from(rounded).unwrap_or(u64::MAX)
}

impl RelayConnection {
    fn mark_ready(&self) -> Result<(), String> {
        self.child
            .lock()
            .map_err(|_| "worker child lock poisoned".to_string())?
            .ready_committed = true;
        Ok(())
    }

    fn commands(&self) -> RelayCommandSender {
        self.commands.clone()
    }
}

impl RelayCommandThread {
    fn stop(self) -> thread::JoinHandle<()> {
        self.sender
            .abort("worker relay command transport retired".to_string());
        self.thread
    }
}

impl ReadyCommit {
    fn finish(&self, outcome: ReadyCommitOutcome) -> bool {
        let sender = match self.0.lock() {
            Ok(mut sender) => sender.take(),
            Err(poisoned) => poisoned.into_inner().take(),
        };
        if let Some(sender) = sender {
            let _ = sender.send(outcome);
            true
        } else {
            false
        }
    }
}

fn join_diagnostic_reader(task: Option<thread::JoinHandle<()>>) -> Result<(), String> {
    task.map_or(Ok(()), |task| {
        join_worker_thread(task, "launcher diagnostics")
    })
}

fn join_worker_thread(thread: thread::JoinHandle<()>, name: &str) -> Result<(), String> {
    thread
        .join()
        .map_err(|_| format!("worker {name} task failed"))
}

#[cfg(unix)]
fn request_child_retirement(child: &mut Child) -> std::io::Result<()> {
    if unsafe { libc::kill(child.id() as libc::pid_t, libc::SIGTERM) } != 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(())
}
#[cfg(windows)]
fn request_child_retirement(child: &mut Child) -> std::io::Result<()> {
    kill_child(child)
}

fn kill_child(child: &mut Child) -> std::io::Result<()> {
    let result = child.kill();
    // TerminateProcess can report access denied if the process exited between
    // our last observation and the kill. Only confirmed exit makes that benign;
    // Child retains the status for the owner's subsequent wait and retirement.
    #[cfg(windows)]
    if result.is_err() && matches!(child.try_wait(), Ok(Some(_))) {
        return Ok(());
    }
    result
}
