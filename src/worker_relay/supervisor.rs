use std::os::unix::process::ExitStatusExt as _;
use std::process::{Child, ChildStderr, ChildStdin, ChildStdout, Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, OnceLock, mpsc};
use std::time::{Duration, Instant};

use super::commands::{CommandReader, SidebandWrite, SidebandWriter, StdinWrite, StdinWriter};
use super::event_writer::{self, EventSender};
use super::lifecycle::{
    self, FirstFailure, Retirement, WORKER_SHUTDOWN_GRACE, collect_error, report_startup_failure,
};
use super::streams::{OutputReader, OutputStream, SidebandReader, drain_unstarted_output};
use crate::process_exit::ChildExitWaiter;
use crate::relay_protocol::RelayEvent;
use crate::worker_protocol::ServerMessage;

const RETIREMENT_DRAIN_TIMEOUT: Duration = Duration::from_millis(100);
pub(super) fn run(mut command: Command) -> Result<(), String> {
    let (controls, control_receiver) = mpsc::channel();
    let writer_controls = controls.clone();
    let (events, mut event_writer) = event_writer::start(move |message| {
        let _ = writer_controls.send(Control::Stop { message });
    })?;
    let failures = FailureReporter::new(controls.clone());
    let stopping = Arc::new(AtomicBool::new(false));

    let (sideband_reader, sideband_writer, child_endpoints) = match crate::sideband::bind() {
        Ok(sideband) => sideband,
        Err(error) => {
            return report_startup_failure(
                &events,
                event_writer,
                format!("failed to create worker sideband: {error}"),
            );
        }
    };
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    child_endpoints.configure_process(&mut command);
    let child = match command.spawn() {
        Ok(child) => child,
        Err(error) => {
            return report_startup_failure(
                &events,
                event_writer,
                format!("failed to launch worker: {error}"),
            );
        }
    };
    events.send(RelayEvent::WorkerStarted { pid: child.id() });
    drop(command);
    drop(child_endpoints);

    let mut worker = WorkerLifecycle::new(child, sideband_reader);
    let setup = worker
        .start_io(sideband_writer, &events, &failures, &controls, &stopping)
        .and_then(|()| worker.start_exit_watcher(controls.clone()));
    let (status, retirement_error) = match setup {
        Ok(()) => {
            let worker_sideband = worker
                .sideband_writer
                .as_ref()
                .expect("worker sideband writer should be running")
                .sender();
            let worker_stdin = worker
                .stdin
                .as_ref()
                .expect("worker stdin writer should be running")
                .sender();
            let result = supervise_worker(
                &mut worker.child,
                worker
                    .exit_watcher
                    .as_mut()
                    .expect("worker exit observer should be running"),
                &control_receiver,
                &events,
                &stopping,
                &worker_sideband,
                &worker_stdin,
            );
            worker.retired = true;
            result
        }
        Err(error) => {
            failures.report(error.clone());
            stopping.store(true, Ordering::SeqCst);
            let result = force_stop_worker(&mut worker.child, worker.exit_watcher.as_mut(), error);
            worker.retired = true;
            result
        }
    };
    if let Some(error) = retirement_error.as_ref() {
        failures.report(error.clone());
    }

    stopping.store(true, Ordering::SeqCst);
    event_writer.begin_retirement();
    let mut finish_error = worker.cancel_and_join(&events);

    let reported_failure = failures.message();
    let outcome = status.map(|status| match (status.code(), status.signal()) {
        (Some(code), _) => RelayEvent::WorkerExited { code },
        (None, Some(signal)) => RelayEvent::WorkerSignaled { signal },
        (None, None) => RelayEvent::Fatal {
            message: "worker exited without an exit code or signal".to_string(),
        },
    });
    collect_error(
        &mut finish_error,
        lifecycle::finish(&events, event_writer, reported_failure, outcome),
    );

    // Do not repeat an exact retirement failure after publishing it as the
    // authoritative Fatal event. Preserve a richer cleanup error that the
    // first-failure reporter could not publish.
    if retirement_error.as_ref() != reported_failure {
        collect_error(&mut finish_error, retirement_error.map_or(Ok(()), Err));
    }
    finish_error.map_or(Ok(()), Err)
}

fn supervise_worker(
    child: &mut Child,
    exit: &mut ChildExitWaiter,
    controls: &mpsc::Receiver<Control>,
    events: &EventSender,
    stopping: &AtomicBool,
    sideband: &mpsc::Sender<SidebandWrite>,
    stdin: &mpsc::Sender<StdinWrite>,
) -> (Option<ExitStatus>, Option<String>) {
    let mut retirement = Retirement::default();
    loop {
        let control = match retirement.remaining() {
            Some(remaining) if remaining.is_zero() => {
                return force_stop_worker(child, Some(exit), String::new());
            }
            Some(remaining) => match controls.recv_timeout(remaining) {
                Ok(control) => control,
                Err(mpsc::RecvTimeoutError::Timeout) => {
                    return force_stop_worker(child, Some(exit), String::new());
                }
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    return force_stop_worker(
                        child,
                        Some(exit),
                        "relay control channel stopped".to_string(),
                    );
                }
            },
            None => match controls.recv() {
                Ok(control) => control,
                Err(_) => {
                    return force_stop_worker(
                        child,
                        Some(exit),
                        "relay control channel stopped".to_string(),
                    );
                }
            },
        };
        match control {
            Control::Worker(message) if retirement.accepts_commands() => {
                if sideband.send(SidebandWrite::Message(message)).is_err() {
                    return force_stop_worker(
                        child,
                        Some(exit),
                        "worker sideband writer stopped".to_string(),
                    );
                }
            }
            Control::Stdin { data } if retirement.accepts_commands() => {
                if stdin.send(StdinWrite::Write(data.into_bytes())).is_err() {
                    return force_stop_worker(
                        child,
                        Some(exit),
                        "worker stdin writer stopped".to_string(),
                    );
                }
            }
            Control::Interrupt { request_id } if retirement.accepts_commands() => {
                let error = interrupt_worker(child).err();
                if !events.send_supervisor(RelayEvent::InterruptResult { request_id, error }) {
                    return force_stop_worker(child, Some(exit), String::new());
                }
            }
            Control::Shutdown { deadline } | Control::ControllerEof { deadline } => {
                if !retirement.accepts_commands() {
                    continue;
                }
                if matches!(control, Control::Shutdown { .. }) {
                    if !retirement.accept_shutdown(events, || deadline) {
                        return force_stop_worker(child, Some(exit), String::new());
                    }
                } else {
                    // EOF keeps the deadline computed by the command reader
                    // and sends no acceptance event.
                    retirement.start_if_idle(|| deadline);
                }
                stopping.store(true, Ordering::SeqCst);
                let _ = stdin.send(StdinWrite::Close);
                let _ = sideband.send(SidebandWrite::Message(ServerMessage::Shutdown));
            }
            Control::Stop { message } => {
                stopping.store(true, Ordering::SeqCst);
                return force_stop_worker(child, Some(exit), message);
            }
            Control::SidebandEof => {
                stopping.store(true, Ordering::SeqCst);
                if retirement.start_if_idle(|| Instant::now() + WORKER_SHUTDOWN_GRACE) {
                    let _ = stdin.send(StdinWrite::Close);
                }
            }
            Control::WorkerExited(result) => {
                stopping.store(true, Ordering::SeqCst);
                return match result {
                    Ok(()) => finish_exited_worker(child, exit),
                    Err(error) => force_stop_worker(child, Some(exit), error),
                };
            }
            Control::Worker(_) | Control::Stdin { .. } | Control::Interrupt { .. } => {}
        }
    }
}

fn finish_exited_worker(
    child: &mut Child,
    exit: &mut ChildExitWaiter,
) -> (Option<ExitStatus>, Option<String>) {
    if let Err(error) = exit.finish() {
        return force_stop_worker(child, Some(exit), error);
    }
    match child.wait() {
        Ok(status) => (Some(status), None),
        Err(error) => (
            None,
            Some(format!("failed to reap the direct worker: {error}")),
        ),
    }
}

fn interrupt_worker(child: &mut Child) -> Result<(), String> {
    // SAFETY: the direct child remains unreaped here, so its PID cannot be
    // reused before `kill` returns.
    if unsafe { libc::kill(child.id() as libc::pid_t, libc::SIGINT) } == 0 {
        Ok(())
    } else {
        Err(format!(
            "failed to interrupt worker: {}",
            std::io::Error::last_os_error()
        ))
    }
}

fn force_stop_worker(
    child: &mut Child,
    exit: Option<&mut ChildExitWaiter>,
    prior_error: String,
) -> (Option<ExitStatus>, Option<String>) {
    let mut errors = Vec::new();
    if !prior_error.is_empty() {
        errors.push(prior_error);
    }
    // A try_wait can reap. Keep the child identity pinned while observation
    // is in flight, including when shutdown has to terminate it.
    let mut status = None;
    let should_kill_direct_worker = match crate::process_exit::direct_child_has_exited(child.id()) {
        Ok(exited) => !exited,
        Err(error) => {
            errors.push(format!("failed to read direct worker status: {error}"));
            // Failed observation does not establish exit. The unreaped child
            // still pins its PID, so attempt termination before waiting.
            true
        }
    };
    if should_kill_direct_worker
        && let Err(error) = child.kill()
        && error.raw_os_error() != Some(libc::ESRCH)
    {
        errors.push(format!("failed to stop the direct worker: {error}"));
    }
    if let Some(exit) = exit
        && let Err(error) = exit.finish()
        && !errors.contains(&error)
    {
        errors.push(error);
    }
    match child.wait() {
        Ok(exit_status) => status = Some(exit_status),
        Err(error) => {
            errors.push(format!("failed to reap the direct worker: {error}"));
        }
    }
    let error = (!errors.is_empty()).then(|| errors.join("; "));
    (status, error)
}

enum ReaderState<Raw, Task> {
    Unstarted(Raw),
    Running(Task),
    Retired,
}

impl<Raw, Task> ReaderState<Raw, Task> {
    fn start(
        &mut self,
        start: impl FnOnce(Raw) -> Result<Task, (Raw, String)>,
    ) -> Result<(), String> {
        let Self::Unstarted(raw) = self.take() else {
            panic!("worker reader should start only once");
        };
        match start(raw) {
            Ok(task) => {
                *self = Self::Running(task);
                Ok(())
            }
            Err((raw, error)) => {
                *self = Self::Unstarted(raw);
                Err(error)
            }
        }
    }

    fn take(&mut self) -> Self {
        std::mem::replace(self, Self::Retired)
    }
}

struct WorkerLifecycle {
    child: Child,
    retired: bool,
    drain_deadline: Arc<OnceLock<Instant>>,
    raw_stdin: Option<ChildStdin>,
    stdin: Option<StdinWriter>,
    sideband_writer: Option<SidebandWriter>,
    stdout: ReaderState<ChildStdout, OutputReader>,
    stderr: ReaderState<ChildStderr, OutputReader>,
    sideband_reader: ReaderState<crate::sideband::Reader, SidebandReader>,
    command_reader: Option<CommandReader>,
    exit_watcher: Option<ChildExitWaiter>,
}

impl WorkerLifecycle {
    fn new(mut child: Child, sideband_reader: crate::sideband::Reader) -> Self {
        let raw_stdin = child
            .stdin
            .take()
            .expect("piped worker stdin should be available");
        let raw_stdout = child
            .stdout
            .take()
            .expect("piped worker stdout should be available");
        let raw_stderr = child
            .stderr
            .take()
            .expect("piped worker stderr should be available");
        Self {
            child,
            retired: false,
            drain_deadline: Arc::new(OnceLock::new()),
            raw_stdin: Some(raw_stdin),
            stdin: None,
            sideband_writer: None,
            stdout: ReaderState::Unstarted(raw_stdout),
            stderr: ReaderState::Unstarted(raw_stderr),
            sideband_reader: ReaderState::Unstarted(sideband_reader),
            command_reader: None,
            exit_watcher: None,
        }
    }

    fn start_io(
        &mut self,
        sideband_writer: crate::sideband::Writer,
        events: &EventSender,
        failures: &FailureReporter,
        controls: &mpsc::Sender<Control>,
        stopping: &Arc<AtomicBool>,
    ) -> Result<(), String> {
        let stdin = self
            .raw_stdin
            .take()
            .expect("raw worker stdin should be available");
        self.stdin = Some(StdinWriter::start(
            stdin,
            failures.clone(),
            stopping.clone(),
        )?);
        self.sideband_writer = Some(SidebandWriter::start(
            sideband_writer,
            failures.clone(),
            stopping.clone(),
        )?);

        self.stdout.start(|stdout| {
            OutputReader::start(
                stdout,
                OutputStream::Stdout,
                events.clone(),
                failures.clone(),
                self.drain_deadline.clone(),
            )
        })?;
        self.stderr.start(|stderr| {
            OutputReader::start(
                stderr,
                OutputStream::Stderr,
                events.clone(),
                failures.clone(),
                self.drain_deadline.clone(),
            )
        })?;

        self.command_reader = Some(CommandReader::start(controls.clone(), failures.clone())?);

        self.sideband_reader.start(|sideband_reader| {
            SidebandReader::start(
                sideband_reader,
                events.clone(),
                failures.clone(),
                controls.clone(),
                self.drain_deadline.clone(),
            )
        })
    }

    fn start_exit_watcher(&mut self, controls: mpsc::Sender<Control>) -> Result<(), String> {
        self.exit_watcher = Some(ChildExitWaiter::start_observing(
            self.child.id(),
            move |result| {
                let _ = controls.send(Control::WorkerExited(result));
            },
        )?);
        Ok(())
    }

    fn cancel_and_join(&mut self, events: &EventSender) -> Option<String> {
        let mut error = None;
        let deadline = Instant::now() + RETIREMENT_DRAIN_TIMEOUT;
        self.drain_deadline
            .set(deadline)
            .expect("worker transports should retire only once");
        // Wake every reader before joining any of them: a continuously
        // readable stream must not extend the other readers' allowance.
        if let ReaderState::Running(reader) = &self.sideband_reader {
            reader.cancel.cancel();
        }
        if let ReaderState::Running(reader) = &self.stdout {
            reader.cancel.cancel();
        }
        if let ReaderState::Running(reader) = &self.stderr {
            reader.cancel.cancel();
        }
        drop(self.raw_stdin.take());
        if let Some(command_reader) = self.command_reader.take() {
            collect_error(&mut error, command_reader.cancel_and_join());
        }
        if let Some(stdin) = self.stdin.take() {
            collect_error(&mut error, stdin.cancel_and_join());
        }
        if let Some(sideband_writer) = self.sideband_writer.take() {
            collect_error(&mut error, sideband_writer.cancel_and_join());
        }
        if let ReaderState::Running(sideband_reader) = self.sideband_reader.take() {
            collect_error(&mut error, sideband_reader.cancel_and_join());
        }
        match self.stdout.take() {
            ReaderState::Running(stdout) => collect_error(&mut error, stdout.cancel_and_join()),
            ReaderState::Unstarted(stdout) => collect_error(
                &mut error,
                drain_unstarted_output(stdout, OutputStream::Stdout, events, deadline),
            ),
            ReaderState::Retired => {}
        }
        match self.stderr.take() {
            ReaderState::Running(stderr) => collect_error(&mut error, stderr.cancel_and_join()),
            ReaderState::Unstarted(stderr) => collect_error(
                &mut error,
                drain_unstarted_output(stderr, OutputStream::Stderr, events, deadline),
            ),
            ReaderState::Retired => {}
        }
        error
    }
}

impl Drop for WorkerLifecycle {
    fn drop(&mut self) {
        if !self.retired {
            let _ = force_stop_worker(
                &mut self.child,
                self.exit_watcher.as_mut(),
                "relay failed while starting worker I/O".to_string(),
            );
        }
    }
}

#[derive(Clone)]
pub(super) struct FailureReporter {
    controls: mpsc::Sender<Control>,
    failure: FirstFailure,
}

impl FailureReporter {
    fn new(controls: mpsc::Sender<Control>) -> Self {
        Self {
            controls,
            failure: FirstFailure::default(),
        }
    }

    pub(super) fn report(&self, message: String) {
        if self.failure.record(message.clone()) {
            let _ = self.controls.send(Control::Stop { message });
        }
    }

    fn message(&self) -> Option<&String> {
        self.failure.message()
    }
}

pub(super) enum Control {
    Worker(ServerMessage),
    Stdin { data: String },
    Interrupt { request_id: u64 },
    Shutdown { deadline: Instant },
    ControllerEof { deadline: Instant },
    SidebandEof,
    Stop { message: String },
    WorkerExited(Result<(), String>),
}
