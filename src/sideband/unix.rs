use std::io::{self, PipeReader, PipeWriter, Read, Write};
use std::os::fd::{AsRawFd, FromRawFd, RawFd};
use std::sync::atomic::{AtomicBool, AtomicI32, Ordering};
use std::sync::{Arc, Mutex, OnceLock};

use serde::Serialize;
use serde::de::DeserializeOwned;

use crate::jsonl::JsonlBuffer;
use crate::readiness::wait_for_io;

const READ_FD_ENV: &str = "MCP_CONSOLE_SIDEBAND_READ_FD";
const WRITE_FD_ENV: &str = "MCP_CONSOLE_SIDEBAND_WRITE_FD";
const READ_CHUNK_SIZE: usize = 8 * 1024;

static SIDEBAND_ALLOWED: AtomicBool = AtomicBool::new(true);
static FORK_READ_FD: AtomicI32 = AtomicI32::new(-1);
static FORK_WRITE_FD: AtomicI32 = AtomicI32::new(-1);
static ATFORK_RESULT: OnceLock<libc::c_int> = OnceLock::new();

pub(crate) struct Reader {
    endpoint: PipeReader,
    buffer: JsonlBuffer,
}

#[derive(Clone)]
pub(crate) struct Writer(Arc<Mutex<PipeWriter>>);

pub(crate) struct ChildEndpoints {
    reader: PipeReader,
    writer: PipeWriter,
}

/// Creates the two anonymous pipes used for one worker sideband.
pub(crate) fn bind() -> io::Result<(Reader, Writer, ChildEndpoints)> {
    let (worker_reader, relay_writer) = io::pipe()?;
    let (relay_reader, worker_writer) = io::pipe()?;
    make_inheritable(worker_reader.as_raw_fd())?;
    make_inheritable(worker_writer.as_raw_fd())?;
    let (reader, writer) = split(relay_reader, relay_writer)?;
    Ok((
        reader,
        writer,
        ChildEndpoints {
            reader: worker_reader,
            writer: worker_writer,
        },
    ))
}

/// Takes ownership of the two sideband endpoints inherited by a worker.
pub(crate) fn connect_from_env() -> io::Result<(Reader, Writer)> {
    let read_fd = inherited_fd(READ_FD_ENV)?;
    let write_fd = inherited_fd(WRITE_FD_ENV)?;
    if read_fd <= 2 || write_fd <= 2 || read_fd == write_fd {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "worker sideband requires two distinct descriptors above stderr",
        ));
    }
    // SAFETY: bootstrap supplies two live, distinct descriptors. This process
    // adopts each once; errors below drop both endpoints.
    let reader = unsafe { PipeReader::from_raw_fd(read_fd) };
    let writer = unsafe { PipeWriter::from_raw_fd(write_fd) };
    set_close_on_exec(read_fd, true)?;
    set_close_on_exec(write_fd, true)?;

    // The descriptor numbers are bootstrap data, not part of the R session.
    unsafe {
        std::env::remove_var(READ_FD_ENV);
        std::env::remove_var(WRITE_FD_ENV);
    }

    let sideband = split(reader, writer)?;
    register_fork_cleanup(read_fd, write_fd)?;
    Ok(sideband)
}

fn split(reader: PipeReader, writer: PipeWriter) -> io::Result<(Reader, Writer)> {
    set_nonblocking(reader.as_raw_fd())?;
    set_nonblocking(writer.as_raw_fd())?;
    #[cfg(target_os = "macos")]
    {
        // Darwin can deliver a pipe's SIGPIPE to another native thread. A
        // writer-local mask cannot protect R's handler once DuckDB has threads.
        // sys/fcntl.h supplies this option; libc does not expose it on macOS.
        const F_SETNOSIGPIPE: libc::c_int = 73;
        if unsafe { libc::fcntl(writer.as_raw_fd(), F_SETNOSIGPIPE, 1) } < 0 {
            return Err(io::Error::last_os_error());
        }
    }
    Ok((Reader::new(reader), Writer::new(writer)))
}

impl Reader {
    fn new(endpoint: PipeReader) -> Self {
        Self {
            endpoint,
            buffer: JsonlBuffer::default(),
        }
    }

    pub(crate) fn has_buffered_data(&self) -> bool {
        self.buffer.has_buffered_data()
    }

    /// Receives one newline-delimited JSON message from the worker.
    pub(crate) fn receive<T: DeserializeOwned>(&mut self) -> io::Result<T> {
        loop {
            if let Some(message) = self.receive_buffered()? {
                return Ok(message);
            }
            match self.read_chunk() {
                Ok(()) => {}
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                    wait_for_io(self.as_raw_fd(), libc::POLLIN, None)?;
                }
                Err(error) => return Err(error),
            }
        }
    }

    /// Returns one complete frame already assembled from prior reads.
    pub(crate) fn receive_buffered<T: DeserializeOwned>(&mut self) -> io::Result<Option<T>> {
        self.buffer
            .next()
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))
    }

    /// Reads one chunk after the caller observes descriptor readiness.
    pub(crate) fn read_chunk(&mut self) -> io::Result<()> {
        let mut buffer = [0; READ_CHUNK_SIZE];
        match self.endpoint.read(&mut buffer)? {
            0 if !self.buffer.has_buffered_data() => Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "worker sideband closed",
            )),
            0 => Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "worker sideband closed midway through a frame",
            )),
            length => {
                self.buffer.append(&buffer[..length]);
                Ok(())
            }
        }
    }
}

impl AsRawFd for Reader {
    fn as_raw_fd(&self) -> RawFd {
        self.endpoint.as_raw_fd()
    }
}

impl Writer {
    fn new(endpoint: PipeWriter) -> Self {
        Self(Arc::new(Mutex::new(endpoint)))
    }

    /// Sends and flushes one newline-delimited JSON message to the worker.
    pub(crate) fn send<T: Serialize>(&self, message: &T) -> io::Result<()> {
        self.send_cancellable(message, None)
    }

    pub(crate) fn send_cancellable<T: Serialize>(
        &self,
        message: &T,
        cancelled: Option<&PipeReader>,
    ) -> io::Result<()> {
        let endpoint = self
            .0
            .lock()
            .map_err(|_| io::Error::other("worker sideband writer lock poisoned"))?;
        let mut frame = serde_json::to_vec(message)?;
        frame.push(b'\n');
        let mut remaining = frame.as_slice();
        while !remaining.is_empty() {
            if wait_for_io(endpoint.as_raw_fd(), libc::POLLOUT, cancelled)?.cancelled {
                return Err(io::Error::new(
                    io::ErrorKind::BrokenPipe,
                    "worker sideband writer cancelled",
                ));
            }
            match write_without_sigpipe(&endpoint, remaining) {
                Ok(0) => return Err(io::ErrorKind::WriteZero.into()),
                Ok(length) => remaining = &remaining[length..],
                Err(error)
                    if matches!(
                        error.kind(),
                        io::ErrorKind::Interrupted | io::ErrorKind::WouldBlock
                    ) => {}
                Err(error) => return Err(error),
            }
        }
        Ok(())
    }
}

impl ChildEndpoints {
    /// Passes only the two inheritable worker endpoints to the child process.
    pub(crate) fn configure_process(&self, command: &mut std::process::Command) {
        let read_fd = self.reader.as_raw_fd();
        let write_fd = self.writer.as_raw_fd();
        command
            .env(READ_FD_ENV, read_fd.to_string())
            .env(WRITE_FD_ENV, write_fd.to_string());
    }
}

/// Preserves both endpoints across the existing Linux R-library-path exec.
#[cfg(target_os = "linux")]
pub(crate) fn configure_exec(
    reader: &Reader,
    writer: &Writer,
    command: &mut std::process::Command,
) -> io::Result<()> {
    let writer = writer
        .0
        .lock()
        .map_err(|_| io::Error::other("worker sideband writer lock poisoned"))?;
    make_inheritable(reader.as_raw_fd())?;
    make_inheritable(writer.as_raw_fd())?;
    command
        .env(READ_FD_ENV, reader.as_raw_fd().to_string())
        .env(WRITE_FD_ENV, writer.as_raw_fd().to_string());
    Ok(())
}

fn set_nonblocking(fd: RawFd) -> io::Result<()> {
    // SAFETY: fd is an owned pipe; preserve existing status flags.
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn write_without_sigpipe(mut pipe: &PipeWriter, bytes: &[u8]) -> io::Result<usize> {
    // split() disabled SIGPIPE on this descriptor before native startup.
    pipe.write(bytes)
}

#[cfg(not(target_os = "macos"))]
fn write_without_sigpipe(mut pipe: &PipeWriter, bytes: &[u8]) -> io::Result<usize> {
    // Linux directs SIGPIPE to the writing thread. R may have installed its
    // own process-wide handler, so suppress this write's signal locally.
    // Consume only a newly generated signal on EPIPE, preserving an already
    // pending signal and restoring the caller's mask before returning.
    unsafe {
        let mut signal = std::mem::zeroed();
        libc::sigemptyset(&mut signal);
        libc::sigaddset(&mut signal, libc::SIGPIPE);
        let mut previous = std::mem::zeroed();
        let error = libc::pthread_sigmask(libc::SIG_BLOCK, &signal, &mut previous);
        if error != 0 {
            return Err(io::Error::from_raw_os_error(error));
        }
        let mut pending = std::mem::zeroed();
        libc::sigpending(&mut pending);
        let was_pending = libc::sigismember(&pending, libc::SIGPIPE) == 1;
        let result = pipe.write(bytes);
        if result
            .as_ref()
            .is_err_and(|error| error.kind() == io::ErrorKind::BrokenPipe)
            && !was_pending
        {
            libc::sigpending(&mut pending);
            if libc::sigismember(&pending, libc::SIGPIPE) == 1 {
                let mut received = 0;
                libc::sigwait(&signal, &mut received);
            }
        }
        let error = libc::pthread_sigmask(libc::SIG_SETMASK, &previous, std::ptr::null_mut());
        if error != 0 {
            return Err(io::Error::from_raw_os_error(error));
        }
        result
    }
}

fn make_inheritable(fd: RawFd) -> io::Result<()> {
    set_close_on_exec(fd, false)
}

fn inherited_fd(name: &str) -> io::Result<RawFd> {
    std::env::var(name)
        .map_err(|_| io::Error::new(io::ErrorKind::NotFound, format!("{name} is missing")))?
        .parse()
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, format!("{name} is invalid")))
}

fn set_close_on_exec(fd: RawFd, enabled: bool) -> io::Result<()> {
    // SAFETY: the caller owns the live descriptor, and F_GETFD does not modify memory.
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
    if flags < 0 {
        return Err(io::Error::last_os_error());
    }
    let flags = if enabled {
        flags | libc::FD_CLOEXEC
    } else {
        flags & !libc::FD_CLOEXEC
    };
    // SAFETY: `fd` remains live, and F_SETFD receives the flags read above.
    if unsafe { libc::fcntl(fd, libc::F_SETFD, flags) } < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

pub(crate) fn available_in_process() -> bool {
    SIDEBAND_ALLOWED.load(Ordering::SeqCst)
}

extern "C" fn close_sideband_in_fork_child() {
    SIDEBAND_ALLOWED.store(false, Ordering::SeqCst);
    for descriptor in [&FORK_READ_FD, &FORK_WRITE_FD] {
        let descriptor = descriptor.swap(-1, Ordering::SeqCst);
        // Closing the child's descriptors leaves the parent's pipes usable.
        unsafe {
            if descriptor >= 0 {
                libc::close(descriptor);
            }
        }
    }
}

fn register_fork_cleanup(read_fd: RawFd, write_fd: RawFd) -> io::Result<()> {
    // CLOEXEC does not close these descriptors in fork-only R descendants.
    let result = *ATFORK_RESULT.get_or_init(|| unsafe {
        libc::pthread_atfork(None, None, Some(close_sideband_in_fork_child))
    });
    if result != 0 {
        return Err(io::Error::from_raw_os_error(result));
    }
    FORK_READ_FD.store(read_fd, Ordering::SeqCst);
    FORK_WRITE_FD.store(write_fd, Ordering::SeqCst);
    Ok(())
}
