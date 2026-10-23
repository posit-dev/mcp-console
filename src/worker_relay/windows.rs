//! Windows direct-worker owner. Process and pipe events wake blocking waits.
use std::io::{self, Read, Write};
use std::os::windows::io::{AsRawHandle, OwnedHandle};
use std::os::windows::process::CommandExt;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, mpsc};
use std::thread;
use std::time::{Duration, Instant};

use super::event_writer;
use super::lifecycle::{self, FirstFailure, Retirement, WORKER_SHUTDOWN_GRACE};
use super::routing::Operation;
use crate::jsonl::JsonlBuffer;
use crate::relay_protocol::{EncodedBytes, RelayCommand, RelayEvent};
use crate::windows::{Event, Pipe};
use crate::worker_protocol::{ServerMessage, WorkerMessage};

enum Control {
    Command(RelayCommand),
    ControllerEof,
    SidebandEof,
    SidebandReaderFinished,
    SidebandForwardingFailed,
    Exited,
    Failed(String),
}

#[derive(Clone)]
struct Controls {
    sender: mpsc::Sender<Control>,
    failure: FirstFailure,
}

impl Controls {
    fn send(&self, control: Control) -> Result<(), mpsc::SendError<Control>> {
        if let Control::Failed(message) = &control {
            // Preserve the first error even when a reader reports it during
            // retirement. Collecting it must not drain a live command queue.
            self.failure.record(message.clone());
        }
        self.sender.send(control)
    }
}

pub(super) fn run(command: Command) -> Result<(), String> {
    let (sender, commands) = mpsc::channel();
    let controls = Controls {
        sender,
        failure: FirstFailure::default(),
    };
    let failed = controls.clone();
    let (events, mut event_writer) = event_writer::start(move |message| {
        let _ = failed.send(Control::Failed(message));
    })?;
    let stopping = Arc::new(AtomicBool::new(false));
    let StartedWorker {
        mut child,
        mut sideband,
        sideband_writer,
        stdin: writer,
        stdout,
        stderr,
        cancel,
        input_ready,
        interrupt,
        mut exit,
    } = match start_worker(command, controls.clone()) {
        Ok(worker) => worker,
        Err(message) => {
            return lifecycle::report_startup_failure(&events, event_writer, message);
        }
    };
    events.send(RelayEvent::WorkerStarted { pid: child.id() });
    let input_controls = controls.clone();
    // This sole inherited-stdin reader ends with the relay process if the worker
    // exits while its caller still owns stdin. It never outlives a generation.
    thread::spawn(move || {
        let mut input = io::stdin().lock();
        let mut buffer = JsonlBuffer::default();
        loop {
            let message = match buffer.next_line() {
                Ok(Some(command)) => Ok(command),
                Err(error) => Err(io::Error::other(error)),
                Ok(None) => {
                    let mut chunk = [0; 8192];
                    match input.read(&mut chunk) {
                        Ok(0) if !buffer.has_buffered_data() => break,
                        Ok(0) => Err(io::Error::new(
                            io::ErrorKind::UnexpectedEof,
                            crate::relay_protocol::PARTIAL_COMMAND_EOF,
                        )),
                        Ok(length) => {
                            buffer.append(&chunk[..length]);
                            continue;
                        }
                        Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                        Err(error) => Err(error),
                    }
                }
            };
            match message {
                Ok(command) => {
                    let shutdown = matches!(command, RelayCommand::Shutdown { .. });
                    if input_controls.send(Control::Command(command)).is_err() {
                        return;
                    }
                    if shutdown {
                        return;
                    }
                }
                Err(error) => {
                    let _ = input_controls.send(Control::Failed(format!(
                        "relay stdin frame is invalid: {error}"
                    )));
                    return;
                }
            }
        }
        let _ = input_controls.send(Control::ControllerEof);
    });
    let mut tasks = Vec::new();
    for (handle, is_error) in [(stdout, false), (stderr, true)] {
        let mut stream = Pipe::from(handle).with_cancel(cancel.clone());
        let output_events = events.clone();
        let output_controls = controls.clone();
        tasks.push(thread::spawn(move || {
            let mut bytes = [0; 8192];
            loop {
                match stream.read(&mut bytes) {
                    Ok(0) => break,
                    Err(error) => {
                        if error.kind() != io::ErrorKind::ConnectionAborted {
                            let _ = output_controls.send(Control::Failed(format!(
                                "worker output read failed: {error}"
                            )));
                        }
                        let mut remaining = stream.available().unwrap_or(0);
                        stream.clear_cancel();
                        while remaining > 0 {
                            let limit = remaining.min(bytes.len());
                            let Ok(n) = stream.read(&mut bytes[..limit]) else {
                                break;
                            };
                            if n == 0 {
                                break;
                            }
                            remaining -= n;
                            let data = EncodedBytes::from_bytes(&bytes[..n]);
                            if !output_events.send(if is_error {
                                RelayEvent::StderrBytes { data }
                            } else {
                                RelayEvent::StdoutBytes { data }
                            }) {
                                break;
                            }
                        }
                        break;
                    }
                    Ok(n) => {
                        let data = EncodedBytes::from_bytes(&bytes[..n]);
                        if !output_events.send(if is_error {
                            RelayEvent::StderrBytes { data }
                        } else {
                            RelayEvent::StdoutBytes { data }
                        }) {
                            break;
                        }
                    }
                }
            }
        }));
    }
    let sideband_events = events.clone();
    let sideband_controls = controls.clone();
    tasks.push(thread::spawn(move || {
        let mut completion = Control::SidebandReaderFinished;
        loop {
            match sideband.receive::<WorkerMessage>() {
                Ok(message) => {
                    if !sideband_events.send(message.into()) {
                        completion = Control::SidebandForwardingFailed;
                        break;
                    }
                }
                Err(error) if error.kind() == io::ErrorKind::ConnectionAborted => {
                    if let Err(error) = sideband.drain_available::<WorkerMessage>(|message| {
                        sideband_events.send(message.into())
                    }) {
                        let _ = sideband_controls.send(Control::Failed(format!(
                            "worker sideband read failed: {error}"
                        )));
                    }
                    break;
                }
                Err(error) if error.kind() == io::ErrorKind::UnexpectedEof => {
                    completion = Control::SidebandEof;
                    break;
                }
                Err(error) => {
                    let _ = sideband_controls.send(Control::Failed(format!(
                        "worker sideband read failed: {error}"
                    )));
                    return;
                }
            }
        }
        let _ = sideband_controls.send(completion);
    }));
    let (send_sideband, messages) = mpsc::channel::<ServerMessage>();
    let writer_controls = controls.clone();
    let writer_stopping = stopping.clone();
    tasks.push(thread::spawn(move || {
        for message in messages {
            if let Err(error) = sideband_writer.send(&message) {
                if !writer_stopping.load(Ordering::SeqCst) {
                    let _ = writer_controls.send(Control::Failed(format!(
                        "worker sideband write failed: {error}"
                    )));
                }
                break;
            }
        }
    }));
    let (send_stdin, input) = mpsc::channel::<String>();
    let mut stdin = Pipe::from(writer).with_cancel(cancel.clone());
    let stdin_controls = controls.clone();
    let stdin_stopping = stopping.clone();
    tasks.push(thread::spawn(move || {
        'input: for bytes in input {
            // Wake both before a potentially blocking write and after bytes have
            // arrived. Small chunks let callbacks make progress on a full pipe.
            for chunk in bytes.as_bytes().chunks(8192) {
                input_ready.set();
                let written = stdin.write_all(chunk);
                input_ready.set();
                if let Err(error) = written {
                    if !stdin_stopping.load(Ordering::SeqCst) {
                        let _ = stdin_controls.send(Control::Failed(format!(
                            "worker stdin write failed: {error}"
                        )));
                    }
                    break 'input;
                }
            }
        }
        drop(stdin);
        input_ready.set();
    }));
    let mut send_stdin = Some(send_stdin);
    let mut retirement = Retirement::default();
    loop {
        let next = match retirement.remaining() {
            Some(remaining) if remaining.is_zero() => {
                let _ = child.kill();
                break;
            }
            Some(remaining) => commands.recv_timeout(remaining),
            None => commands
                .recv()
                .map_err(|_| mpsc::RecvTimeoutError::Disconnected),
        };
        match next {
            Ok(Control::Exited) => break,
            Err(_) => {
                let _ = child.kill();
                break;
            }
            Ok(Control::Failed(_) | Control::SidebandForwardingFailed) => {
                // Failed event admission can precede the writer's failure
                // callback when stdout is blocked. Retire the direct child
                // before cancelling/joining its I/O, just as for other failures.
                let _ = child.kill();
                break;
            }
            Ok(Control::ControllerEof | Control::SidebandEof) => {
                if retirement.start_if_idle(|| {
                    stopping.store(true, Ordering::SeqCst);
                    Instant::now() + WORKER_SHUTDOWN_GRACE
                }) {
                    send_stdin.take();
                    let _ = send_sideband.send(ServerMessage::Shutdown);
                }
            }
            // Cancellation completion is not observed sideband EOF. Its owner
            // has already begun retirement before setting the cancellation.
            Ok(Control::SidebandReaderFinished) => {}
            Ok(Control::Command(command)) if retirement.accepts_commands() => {
                match Operation::from(command) {
                    Operation::Interrupt { request_id } => {
                        interrupt.set();
                        events.send_supervisor(RelayEvent::InterruptResult {
                            request_id,
                            error: None,
                        });
                    }
                    Operation::Shutdown { grace_millis } => {
                        stopping.store(true, Ordering::SeqCst);
                        if !retirement.accept_shutdown(&events, || {
                            Instant::now() + Duration::from_millis(grace_millis)
                        }) {
                            let _ = child.kill();
                            break;
                        }
                        send_stdin.take();
                        let _ = send_sideband.send(ServerMessage::Shutdown);
                    }
                    Operation::Stdin { data } => {
                        if let Some(stdin) = &send_stdin {
                            let _ = stdin.send(data);
                        }
                    }
                    Operation::Worker(message) => {
                        let _ = send_sideband.send(message);
                    }
                }
            }
            Ok(Control::Command(_)) => {}
        }
    }
    if let Err(message) = exit.finish() {
        let _ = controls.send(Control::Failed(message));
    }
    let status = child.wait().map_err(|e| e.to_string())?;
    event_writer.begin_retirement();
    stopping.store(true, Ordering::SeqCst);
    cancel.set();
    drop(send_stdin);
    drop(send_sideband);
    for task in tasks {
        task.join().map_err(|_| "worker I/O task panicked")?;
    }
    lifecycle::finish(
        &events,
        event_writer,
        controls.failure.message(),
        Some(RelayEvent::WorkerExited {
            code: status.code().unwrap_or(1),
        }),
    )
}

struct StartedWorker {
    child: ChildOwner,
    sideband: crate::sideband::Reader,
    sideband_writer: crate::sideband::Writer,
    stdin: OwnedHandle,
    stdout: OwnedHandle,
    stderr: OwnedHandle,
    cancel: Event,
    input_ready: Event,
    interrupt: Event,
    exit: crate::process_exit::ChildExitWaiter,
}

fn start_worker(mut command: Command, controls: Controls) -> Result<StartedWorker, String> {
    // Keep setup in one fallible scope. On failure, all partial resources and
    // any spawned child retire before the caller publishes the Fatal frame.
    let cancel =
        Event::new().map_err(|e| format!("failed to create worker cancellation event: {e}"))?;
    let input_ready =
        Event::new().map_err(|e| format!("failed to create worker input event: {e}"))?;
    crate::windows::inherit(input_ready.as_raw_handle(), true)
        .map_err(|e| format!("failed to inherit worker input event: {e}"))?;
    let interrupt =
        Event::new().map_err(|e| format!("failed to create worker interrupt event: {e}"))?;
    crate::windows::inherit(interrupt.as_raw_handle(), true)
        .map_err(|e| format!("failed to inherit worker interrupt event: {e}"))?;
    let (sideband, sideband_writer, endpoints) = crate::sideband::bind(cancel.clone())
        .map_err(|e| format!("failed to create worker sideband: {e}"))?;
    command
        .creation_flags(windows_sys::Win32::System::Threading::CREATE_NO_WINDOW)
        .env(
            "MCP_CONSOLE_INTERRUPT_HANDLE",
            (interrupt.as_raw_handle() as usize).to_string(),
        )
        .env(
            "MCP_CONSOLE_INPUT_READY_HANDLE",
            (input_ready.as_raw_handle() as usize).to_string(),
        );
    let (input, stdin) = crate::windows::pipe(false, true)
        .map_err(|e| format!("failed to create worker stdin pipe: {e}"))?;
    let (stdout, output) = crate::windows::pipe(true, false)
        .map_err(|e| format!("failed to create worker stdout pipe: {e}"))?;
    let (stderr, diagnostic) = crate::windows::pipe(true, false)
        .map_err(|e| format!("failed to create worker stderr pipe: {e}"))?;
    command
        .stdin(Stdio::from(input))
        .stdout(Stdio::from(output))
        .stderr(Stdio::from(diagnostic));
    endpoints.configure_process(&mut command);
    let child = ChildOwner(
        command
            .spawn()
            .map_err(|e| format!("failed to launch worker: {e}"))?,
    );
    drop(command);
    drop(endpoints);
    crate::windows::inherit(interrupt.as_raw_handle(), false)
        .map_err(|e| format!("failed to clear worker interrupt event inheritance: {e}"))?;
    crate::windows::inherit(input_ready.as_raw_handle(), false)
        .map_err(|e| format!("failed to clear worker input event inheritance: {e}"))?;
    let exit = crate::process_exit::ChildExitWaiter::start_observing(child.id(), move |result| {
        let control = match result {
            Ok(()) => Control::Exited,
            Err(message) => Control::Failed(message),
        };
        let _ = controls.send(control);
    })
    .map_err(|e| format!("failed to observe worker exit: {e}"))?;
    Ok(StartedWorker {
        child,
        sideband,
        sideband_writer,
        stdin,
        stdout,
        stderr,
        cancel,
        input_ready,
        interrupt,
        exit,
    })
}

// Any error after spawning must still retire the one directly owned worker.
struct ChildOwner(std::process::Child);
impl std::ops::Deref for ChildOwner {
    type Target = std::process::Child;
    fn deref(&self) -> &Self::Target {
        &self.0
    }
}
impl std::ops::DerefMut for ChildOwner {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.0
    }
}
impl Drop for ChildOwner {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
