use std::collections::VecDeque;
use std::fs::File;
use std::io::{self, Write};
#[cfg(unix)]
use std::os::fd::{AsFd, AsRawFd};
#[cfg(unix)]
use std::os::unix::fs::FileTypeExt;
use std::sync::{Arc, Condvar, Mutex, MutexGuard, OnceLock};
use std::thread;
use std::time::{Duration, Instant};

use crate::relay_protocol::RelayEvent;

const RETIREMENT_FLUSH_TIMEOUT: Duration = Duration::from_secs(1);
const OUTPUT_BYTES: usize = 8 * 1024 * 1024;
const OUTPUT_FRAMES: usize = 512;
const SUPERVISOR_BYTES: usize = 64 * 1024;
const SUPERVISOR_FRAMES: usize = 16;
const EXPIRED: &str = "relay stdout write failed: relay stdout retirement deadline expired";

#[derive(Clone)]
pub(super) struct EventSender(Arc<EventQueue>);

struct Frame {
    bytes: Vec<u8>,
    supervisor: bool,
}

#[derive(Default)]
struct Usage {
    bytes: usize,
    frames: usize,
}

enum Status {
    Open,
    Finishing,
    Failed(String),
}

struct QueuedEvents {
    frames: VecDeque<Frame>,
    output: Usage,
    supervisor: Usage,
    status: Status,
}

impl QueuedEvents {
    fn fail(&mut self, error: String) {
        if !matches!(self.status, Status::Failed(_)) {
            self.status = Status::Failed(error);
        }
    }

    fn usage(&mut self, supervisor: bool) -> &mut Usage {
        if supervisor {
            &mut self.supervisor
        } else {
            &mut self.output
        }
    }
}

struct EventQueue {
    state: Mutex<QueuedEvents>,
    changed: Condvar,
    deadline: OnceLock<Instant>,
}

impl EventQueue {
    fn lock(&self) -> MutexGuard<'_, QueuedEvents> {
        self.state.lock().expect("relay event queue lock poisoned")
    }

    fn expire(&self, state: &mut QueuedEvents) {
        if self
            .deadline
            .get()
            .is_some_and(|deadline| Instant::now() >= *deadline)
        {
            state.fail(EXPIRED.to_string());
            self.changed.notify_all();
        }
    }

    fn wait<'a>(&self, state: MutexGuard<'a, QueuedEvents>) -> MutexGuard<'a, QueuedEvents> {
        match self.deadline.get() {
            Some(deadline) => {
                self.changed
                    .wait_timeout(state, deadline.saturating_duration_since(Instant::now()))
                    .expect("relay event queue lock poisoned")
                    .0
            }
            None => self
                .changed
                .wait(state)
                .expect("relay event queue lock poisoned"),
        }
    }

    fn next(&self) -> Option<Frame> {
        let mut state = self.lock();
        loop {
            if matches!(state.status, Status::Failed(_)) {
                return None;
            }
            if let Some(frame) = state.frames.pop_front() {
                // Popping does not release capacity: the in-flight frame is
                // still charged until the downstream write completes.
                return Some(frame);
            }
            if matches!(state.status, Status::Finishing) {
                return None;
            }
            // An empty queue has no pending bytes to time out. Producers
            // enforce their admission deadline; finish wakes this waiter.
            state = self
                .changed
                .wait(state)
                .expect("relay event queue lock poisoned");
        }
    }

    fn write(&self, mut output: EventOutput) -> Result<(), String> {
        while let Some(frame) = self.next() {
            let result = output.write_all(&frame.bytes).and_then(|()| output.flush());
            let length = frame.bytes.len();
            let supervisor = frame.supervisor;
            drop(frame);
            let mut state = self.lock();
            let usage = state.usage(supervisor);
            usage.bytes -= length;
            usage.frames -= 1;
            if let Err(error) = result {
                state.fail(format!("relay stdout write failed: {error}"));
            }
            self.changed.notify_all();
        }
        let mut state = self.lock();
        state.frames.clear();
        self.changed.notify_all();
        match &state.status {
            Status::Failed(error) => Err(error.clone()),
            Status::Finishing => Ok(()),
            Status::Open => unreachable!("event writer stops only after finish or failure"),
        }
    }
}

pub(super) struct EventWriter {
    queue: Arc<EventQueue>,
    bounded_output: bool,
    wake: Option<io::PipeWriter>,
    thread: thread::JoinHandle<Result<(), String>>,
    #[cfg(windows)]
    completion: std::sync::mpsc::Receiver<()>,
}

pub(super) fn start(
    on_error: impl FnOnce(String) + Send + 'static,
) -> Result<(EventSender, EventWriter), String> {
    let (reader, wake) =
        io::pipe().map_err(|error| format!("failed to create relay stdout wake pipe: {error}"))?;
    let queue = Arc::new(EventQueue {
        state: Mutex::new(QueuedEvents {
            frames: VecDeque::new(),
            output: Usage::default(),
            supervisor: Usage::default(),
            status: Status::Open,
        }),
        changed: Condvar::new(),
        deadline: OnceLock::new(),
    });
    let output = EventOutput::new(reader, queue.clone())
        .map_err(|error| format!("failed to configure relay stdout: {error}"))?;
    let bounded_output = output.original_flags.is_some();
    let writer_queue = queue.clone();
    #[cfg(windows)]
    let (completed, completion) = std::sync::mpsc::sync_channel(1);
    let thread = thread::spawn(move || {
        let result = writer_queue.write(output);
        if let Err(error) = &result {
            on_error(error.clone());
        }
        #[cfg(windows)]
        let _ = completed.send(());
        result
    });
    Ok((
        EventSender(queue.clone()),
        EventWriter {
            queue,
            bounded_output,
            wake: Some(wake),
            thread,
            #[cfg(windows)]
            completion,
        },
    ))
}

impl EventSender {
    // The writer owns the transport error. A false result only tells a reader
    // to stop producing, without reporting the same error through every task.
    pub(super) fn send(&self, event: RelayEvent) -> bool {
        self.enqueue(event, false)
    }

    pub(super) fn send_supervisor(&self, event: RelayEvent) -> bool {
        self.enqueue(event, true)
    }

    fn enqueue(&self, event: RelayEvent, supervisor: bool) -> bool {
        let mut bytes =
            serde_json::to_vec(&event).expect("relay event serialization should succeed");
        bytes.push(b'\n');
        // Retain only the encoded payload while waiting for capacity.
        drop(event);
        let length = bytes.len();
        let mut state = self.0.lock();
        loop {
            self.0.expire(&mut state);
            if !matches!(state.status, Status::Open) {
                return false;
            }
            let usage = state.usage(supervisor);
            let available = if supervisor {
                usage.frames < SUPERVISOR_FRAMES
                    && length <= SUPERVISOR_BYTES
                    && usage.bytes <= SUPERVISOR_BYTES - length
            } else {
                usage.frames == 0
                    || (usage.frames < OUTPUT_FRAMES
                        && length <= OUTPUT_BYTES
                        && usage.bytes <= OUTPUT_BYTES - length)
            };
            if available {
                usage.bytes += length;
                usage.frames += 1;
                state.frames.push_back(Frame { bytes, supervisor });
                self.0.changed.notify_all();
                return true;
            }
            if supervisor {
                state.fail("relay supervisor event queue capacity exceeded".to_string());
                self.0.changed.notify_all();
                return false;
            }
            state = self.0.wait(state);
        }
    }

    pub(super) fn finish(&self) {
        let mut state = self.0.lock();
        if matches!(state.status, Status::Open) {
            state.status = Status::Finishing;
        }
        self.0.changed.notify_all();
    }
}

impl EventWriter {
    pub(super) fn begin_retirement(&mut self) {
        if self.bounded_output {
            // Publish under the capacity mutex so no waiter can miss the
            // transition from an untimed wait to the retirement deadline.
            let _state = self.queue.lock();
            self.queue
                .deadline
                .set(Instant::now() + RETIREMENT_FLUSH_TIMEOUT)
                .expect("relay stdout should retire only once");
            self.queue.changed.notify_all();
        }
        drop(self.wake.take());
    }

    pub(super) fn join(self) -> Result<(), String> {
        #[cfg(windows)]
        self.completion
            .recv_timeout(RETIREMENT_FLUSH_TIMEOUT)
            .map_err(|_| EXPIRED.to_string())?;
        self.thread
            .join()
            .map_err(|_| "relay event writer task failed".to_string())?
    }
}

#[cfg(unix)]
struct EventOutput {
    file: File,
    original_flags: Option<libc::c_int>,
    wake: io::PipeReader,
    queue: Arc<EventQueue>,
}

#[cfg(unix)]
impl EventOutput {
    fn new(wake: io::PipeReader, queue: Arc<EventQueue>) -> io::Result<Self> {
        let file = File::from(io::stdout().as_fd().try_clone_to_owned()?);
        let kind = file.metadata()?.file_type();
        let original_flags = if kind.is_fifo() || kind.is_socket() {
            // SAFETY: the owned duplicate remains open throughout configuration.
            let flags = unsafe { libc::fcntl(file.as_raw_fd(), libc::F_GETFL) };
            if flags < 0 {
                return Err(io::Error::last_os_error());
            }
            Some(flags)
        } else {
            None
        };
        let output = Self {
            file,
            original_flags,
            wake,
            queue,
        };
        // Duplicates share file status flags. The relay is the sole protocol
        // writer; restore the original flags when its output task finishes.
        // SAFETY: this preserves existing flags on the live output descriptor.
        if let Some(flags) = original_flags
            && unsafe {
                libc::fcntl(
                    output.file.as_raw_fd(),
                    libc::F_SETFL,
                    flags | libc::O_NONBLOCK,
                )
            } < 0
        {
            return Err(io::Error::last_os_error());
        }
        Ok(output)
    }

    fn check_deadline(&self) -> io::Result<()> {
        if self
            .queue
            .deadline
            .get()
            .is_some_and(|deadline| Instant::now() >= *deadline)
        {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "relay stdout retirement deadline expired",
            ));
        }
        Ok(())
    }

    fn wait_writable(&self) -> io::Result<()> {
        loop {
            self.check_deadline()?;
            let deadline = self.queue.deadline.get();
            let timeout = deadline.map_or(-1, |deadline| {
                deadline
                    .saturating_duration_since(Instant::now())
                    .as_millis()
                    .max(1) as libc::c_int
            });
            let mut descriptors = [
                libc::pollfd {
                    fd: self.file.as_raw_fd(),
                    events: libc::POLLOUT,
                    revents: 0,
                },
                libc::pollfd {
                    // Once retirement starts, EOF has served its one wakeup.
                    // Polling it again would spin instead of waiting for output.
                    fd: if deadline.is_some() {
                        -1
                    } else {
                        self.wake.as_raw_fd()
                    },
                    events: libc::POLLIN,
                    revents: 0,
                },
            ];
            // SAFETY: the descriptors remain open and the array is initialized.
            if unsafe { libc::poll(descriptors.as_mut_ptr(), descriptors.len() as _, timeout) } >= 0
            {
                return Ok(());
            }
            let error = io::Error::last_os_error();
            if error.kind() != io::ErrorKind::Interrupted {
                return Err(error);
            }
        }
    }
}

#[cfg(unix)]
impl Write for EventOutput {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        loop {
            self.check_deadline()?;
            match self.file.write(bytes) {
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => self.wait_writable()?,
                Err(error) if error.kind() == io::ErrorKind::Interrupted => {}
                result => return result,
            }
        }
    }

    fn flush(&mut self) -> io::Result<()> {
        // File writes are unbuffered. Once write_all succeeds, no pending
        // bytes remain for a deadline to expire during this no-op.
        Ok(())
    }
}

#[cfg(unix)]
impl Drop for EventOutput {
    fn drop(&mut self) {
        if let Some(flags) = self.original_flags {
            // SAFETY: the owned descriptor is still open during Drop.
            let _ = unsafe { libc::fcntl(self.file.as_raw_fd(), libc::F_SETFL, flags) };
        }
    }
}

#[cfg(windows)]
struct EventOutput {
    file: File,
    original_flags: Option<i32>,
}
#[cfg(windows)]
impl EventOutput {
    fn new(_wake: io::PipeReader, _queue: Arc<EventQueue>) -> io::Result<Self> {
        use std::os::windows::io::AsHandle;
        Ok(Self {
            file: File::from(io::stdout().as_handle().try_clone_to_owned()?),
            original_flags: Some(0),
        })
    }
}
#[cfg(windows)]
impl Write for EventOutput {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.file.write(bytes)
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}
