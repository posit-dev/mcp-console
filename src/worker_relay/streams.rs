use std::io::Read;
use std::os::fd::AsRawFd;
use std::sync::{Arc, OnceLock, mpsc};
use std::thread;
use std::time::Instant;

use super::event_writer::EventSender;
use super::io::{Cancellation, cancellation_pipe, set_nonblocking};
use super::supervisor::{Control, FailureReporter};
use crate::readiness::wait_for_io;
use crate::relay_protocol::{EncodedBytes, RelayEvent};
use crate::worker_protocol::WorkerMessage;

const READ_CHUNK_SIZE: usize = 8 * 1024;

// Keep readiness waits cancellable through retirement. A worker
// descendant can retain the pipe after writing only part of a frame, so
// cancellation bounds additional reads, forwards complete buffered frames,
// and abandons any incomplete tail.
pub(super) struct SidebandReader {
    pub(super) cancel: Cancellation,
    thread: thread::JoinHandle<()>,
}

impl SidebandReader {
    pub(super) fn start(
        mut reader: crate::sideband::Reader,
        events: EventSender,
        failures: FailureReporter,
        controls: mpsc::Sender<Control>,
        drain_deadline: Arc<OnceLock<Instant>>,
    ) -> Result<Self, (crate::sideband::Reader, String)> {
        let (cancelled, cancel) = match cancellation_pipe("worker sideband") {
            Ok(pipe) => pipe,
            Err(error) => return Err((reader, error)),
        };
        let thread = thread::spawn(move || {
            let mut ordinary_close = false;
            let mut sideband_failure = None;
            loop {
                match forward_buffered_sideband(&mut reader, &events) {
                    Ok(true) => {}
                    Ok(false) => break,
                    Err(error) => {
                        sideband_failure = Some(error);
                        break;
                    }
                }

                let ready = match wait_for_io(reader.as_raw_fd(), libc::POLLIN, Some(&cancelled)) {
                    Ok(ready) => ready,
                    Err(error) => {
                        sideband_failure = Some(format!("worker sideband read failed: {error}"));
                        break;
                    }
                };
                if ready.cancelled {
                    if let Err(error) = drain_retiring_sideband(
                        &mut reader,
                        &events,
                        *drain_deadline
                            .get()
                            .expect("retirement deadline should be set"),
                    ) {
                        sideband_failure = Some(error);
                    }
                    break;
                }
                if !ready.stream {
                    continue;
                }
                let had_buffered_data = reader.has_buffered_data();
                match reader.read_chunk() {
                    Ok(()) => {}
                    Err(error)
                        if matches!(
                            error.kind(),
                            std::io::ErrorKind::Interrupted | std::io::ErrorKind::WouldBlock
                        ) => {}
                    Err(error)
                        if error.kind() == std::io::ErrorKind::UnexpectedEof
                            && !had_buffered_data =>
                    {
                        ordinary_close = true;
                        break;
                    }
                    Err(error) => {
                        sideband_failure = Some(format!("worker sideband read failed: {error}"));
                        break;
                    }
                }
            }
            if let Some(error) = sideband_failure {
                failures.report(error);
            }
            if ordinary_close {
                let _ = controls.send(Control::SidebandEof);
            }
        });
        Ok(Self { cancel, thread })
    }

    pub(super) fn cancel_and_join(self) -> Result<(), String> {
        self.cancel.cancel();
        self.thread
            .join()
            .map_err(|_| "worker sideband reader task failed".to_string())
    }
}

fn forward_buffered_sideband(
    reader: &mut crate::sideband::Reader,
    events: &EventSender,
) -> Result<bool, String> {
    while let Some(message) = reader
        .receive_buffered::<WorkerMessage>()
        .map_err(|error| format!("worker sideband read failed: {error}"))?
    {
        if !events.send(message.into()) {
            return Ok(false);
        }
    }
    Ok(true)
}

fn drain_retiring_sideband(
    reader: &mut crate::sideband::Reader,
    events: &EventSender,
    deadline: Instant,
) -> Result<(), String> {
    loop {
        if !forward_buffered_sideband(reader, events)? || Instant::now() >= deadline {
            return Ok(());
        }
        match reader.read_chunk() {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::WouldBlock | std::io::ErrorKind::UnexpectedEof
                ) =>
            {
                return Ok(());
            }
            Err(error) => {
                return Err(format!("worker sideband read failed: {error}"));
            }
        }
    }
}

pub(super) struct OutputReader {
    pub(super) cancel: Cancellation,
    thread: thread::JoinHandle<()>,
}

#[derive(Clone, Copy)]
pub(super) enum OutputStream {
    Stdout,
    Stderr,
}

impl OutputReader {
    pub(super) fn start<Stream>(
        mut stream: Stream,
        kind: OutputStream,
        events: EventSender,
        failures: FailureReporter,
        drain_deadline: Arc<OnceLock<Instant>>,
    ) -> Result<Self, (Stream, String)>
    where
        Stream: Read + AsRawFd + Send + 'static,
    {
        if let Err(error) = set_nonblocking(&stream) {
            return Err((stream, error));
        }
        let (cancelled, cancel) = match cancellation_pipe("worker output") {
            Ok(pipe) => pipe,
            Err(error) => return Err((stream, error)),
        };
        let thread = thread::spawn(move || {
            let mut buffer = [0_u8; READ_CHUNK_SIZE];
            loop {
                let ready = match wait_for_io(stream.as_raw_fd(), libc::POLLIN, Some(&cancelled)) {
                    Ok(ready) => ready,
                    Err(error) => {
                        failures.report(format!("worker output read failed: {error}"));
                        break;
                    }
                };
                if ready.cancelled {
                    if let Err(error) = drain_buffered_output(
                        &mut stream,
                        kind,
                        &events,
                        &mut buffer,
                        *drain_deadline
                            .get()
                            .expect("retirement deadline should be set"),
                    ) {
                        failures.report(error);
                    }
                    break;
                }
                if ready.stream {
                    match stream.read(&mut buffer) {
                        Ok(0) => break,
                        Ok(length) => {
                            if !events.send(output_event(kind, &buffer[..length])) {
                                break;
                            }
                        }
                        Err(error)
                            if matches!(
                                error.kind(),
                                std::io::ErrorKind::Interrupted | std::io::ErrorKind::WouldBlock
                            ) => {}
                        Err(error) => {
                            failures.report(format!("worker output read failed: {error}"));
                            break;
                        }
                    }
                }
            }
        });
        Ok(Self { cancel, thread })
    }

    pub(super) fn cancel_and_join(self) -> Result<(), String> {
        self.cancel.cancel();
        self.thread
            .join()
            .map_err(|_| "worker output reader task failed".to_string())
    }
}

pub(super) fn drain_unstarted_output(
    mut stream: impl Read + AsRawFd,
    kind: OutputStream,
    events: &EventSender,
    deadline: Instant,
) -> Result<(), String> {
    set_nonblocking(&stream)?;
    let mut buffer = [0_u8; READ_CHUNK_SIZE];
    drain_buffered_output(&mut stream, kind, events, &mut buffer, deadline)
}

fn output_event(stream: OutputStream, bytes: &[u8]) -> RelayEvent {
    match (stream, std::str::from_utf8(bytes)) {
        (OutputStream::Stdout, Ok(data)) => RelayEvent::Stdout {
            data: data.to_string(),
        },
        (OutputStream::Stderr, Ok(data)) => RelayEvent::Stderr {
            data: data.to_string(),
        },
        (OutputStream::Stdout, Err(_)) => RelayEvent::StdoutBytes {
            data: EncodedBytes::from_bytes(bytes),
        },
        (OutputStream::Stderr, Err(_)) => RelayEvent::StderrBytes {
            data: EncodedBytes::from_bytes(bytes),
        },
    }
}

fn drain_buffered_output(
    stream: &mut (impl Read + AsRawFd),
    kind: OutputStream,
    events: &EventSender,
    buffer: &mut [u8],
    deadline: Instant,
) -> Result<(), String> {
    while Instant::now() < deadline {
        match stream.read(buffer) {
            Ok(0) => break,
            Ok(length) => {
                if !events.send(output_event(kind, &buffer[..length])) {
                    break;
                }
            }
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => break,
            Err(error) => return Err(format!("worker output read failed: {error}")),
        }
    }
    Ok(())
}
