use std::io;
use std::os::fd::AsRawFd;
#[cfg(target_os = "macos")]
use std::os::fd::{FromRawFd, OwnedFd};

pub(super) type Cancel = io::PipeWriter;
pub(super) fn cancellation() -> Result<(io::PipeReader, Cancel), String> {
    io::pipe().map_err(|error| error.to_string())
}

const CHILD_EXITED: libc::c_int = 1;
const CHILD_KILLED: libc::c_int = 2;
const CHILD_DUMPED: libc::c_int = 3;
const CHILD_STOPPED: libc::c_int = 5;
const CHILD_CONTINUED: libc::c_int = 6;

pub(super) struct Observer(libc::pid_t);

impl Observer {
    pub(super) fn new(process_id: u32) -> Result<Self, String> {
        valid_process_id(process_id)
            .map(Self)
            .map_err(|_| "child process ID is invalid".to_string())
    }

    pub(super) fn wait(self) -> Result<(), String> {
        super::wait_for_direct_child_exit(self.0)
    }

    pub(super) fn wait_cancellable(self, cancelled: io::PipeReader) -> Result<bool, String> {
        #[cfg(target_os = "linux")]
        let result = wait_for_signal_exit(self.0, cancelled);
        #[cfg(target_os = "macos")]
        let result = (|| {
            if observe_direct_child(self.0, false)? {
                return Ok(true);
            }
            let Some(descriptor) = exit_notification(self.0)? else {
                return Ok(true);
            };
            let ready = crate::readiness::wait_for_io(
                descriptor.as_raw_fd(),
                libc::POLLIN,
                Some(&cancelled),
            )?;
            if ready.cancelled {
                return Ok(false);
            }
            // macOS NOTE_EXIT can precede a waitable terminal status. After
            // exit readiness, wait for that status with WNOWAIT so the owner
            // retains the child's identity and remains the sole reaper.
            observe_direct_child(self.0, true)
        })();
        result.map_err(|error| format!("failed to observe child process {} exit: {error}", self.0))
    }
}

#[cfg(target_os = "linux")]
struct ChildSignal(signal_hook::SigId);

#[cfg(target_os = "linux")]
impl Drop for ChildSignal {
    fn drop(&mut self) {
        // Unregister settles in-flight callbacks before their endpoints close.
        signal_hook::low_level::unregister(self.0);
    }
}

#[cfg(target_os = "linux")]
fn wait_for_signal_exit(pid: libc::pid_t, cancelled: io::PipeReader) -> io::Result<bool> {
    use std::io::Read;
    use std::os::unix::net::UnixStream;

    let (mut notifications, notify) = UnixStream::pair()?;
    notifications.set_nonblocking(true)?;
    // Install before probing status: an earlier exit is waitable, and a later
    // exit queues a wake. SIGCHLD is only a hint; waitid is the exit evidence.
    // Each registration receives the wake, preserving concurrent observers.
    let _signal = ChildSignal(signal_hook::low_level::pipe::register(
        libc::SIGCHLD,
        notify,
    )?);
    // The observer alone must accept SIGCHLD even if the host inherited a
    // blocked mask. Other masks stay unchanged; signal-hook chains handlers.
    let mut signals = unsafe { std::mem::zeroed() };
    unsafe {
        libc::sigemptyset(&mut signals);
        libc::sigaddset(&mut signals, libc::SIGCHLD);
    }
    let error = unsafe { libc::pthread_sigmask(libc::SIG_UNBLOCK, &signals, std::ptr::null_mut()) };
    if error != 0 {
        return Err(io::Error::from_raw_os_error(error));
    }
    loop {
        if observe_direct_child(pid, false)? {
            return Ok(true);
        }
        let ready = crate::readiness::wait_for_io(
            notifications.as_raw_fd(),
            libc::POLLIN,
            Some(&cancelled),
        )?;
        if ready.cancelled {
            return Ok(false);
        }
        // Clear a finite queued wake before checking status again. Clearing
        // after the check could lose an exit coalesced with another SIGCHLD.
        match notifications.read(&mut [0; 64]) {
            Ok(_) => {}
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::Interrupted | io::ErrorKind::WouldBlock
                ) => {}
            Err(error) => return Err(error),
        }
    }
}

#[cfg(target_os = "macos")]
fn exit_notification(pid: libc::pid_t) -> io::Result<Option<OwnedFd>> {
    let fd = unsafe { libc::kqueue() };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    let queue = unsafe { OwnedFd::from_raw_fd(fd) };
    if unsafe { libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC) } < 0 {
        return Err(io::Error::last_os_error());
    }
    let change = libc::kevent {
        ident: pid as _,
        filter: libc::EVFILT_PROC,
        flags: libc::EV_ADD | libc::EV_ONESHOT,
        fflags: libc::NOTE_EXIT,
        data: 0,
        udata: std::ptr::null_mut(),
    };
    // XNU rejects NOTE_EXIT registration after exit, even for an unreaped
    // child whose terminal status is not yet waitable. As after NOTE_EXIT,
    // wait for that status while WNOWAIT keeps its identity pinned.
    if unsafe { libc::kevent(fd, &change, 1, std::ptr::null_mut(), 0, std::ptr::null()) } < 0 {
        let error = io::Error::last_os_error();
        if error.raw_os_error() == Some(libc::ESRCH) && observe_direct_child(pid, true)? {
            return Ok(None);
        }
        return Err(error);
    }
    Ok(Some(queue))
}

pub(crate) fn direct_child_has_exited(process_id: u32) -> io::Result<bool> {
    observe_direct_child(valid_process_id(process_id)?, false)
}

pub(crate) fn wait_for_direct_child_exit(process_id: libc::pid_t) -> Result<(), String> {
    loop {
        match observe_direct_child(process_id, true) {
            Ok(true) => return Ok(()),
            Ok(false) => {}
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => {
                return Err(format!(
                    "failed to observe child process {process_id} exit: {error}"
                ));
            }
        }
    }
}

fn observe_direct_child(process_id: libc::pid_t, blocking: bool) -> io::Result<bool> {
    let wait_id = process_id as libc::id_t;
    let options = libc::WEXITED | libc::WNOWAIT | if blocking { 0 } else { libc::WNOHANG };

    loop {
        let mut information = std::mem::MaybeUninit::<libc::siginfo_t>::zeroed();
        // SAFETY: `information` points to writable storage and `process_id`
        // identifies the direct child. WNOWAIT preserves its exit status for
        // the child owner, which remains the sole reaper.
        let result =
            unsafe { libc::waitid(libc::P_PID, wait_id, information.as_mut_ptr(), options) };
        if result < 0 {
            let error = io::Error::last_os_error();
            if error.kind() == io::ErrorKind::Interrupted {
                continue;
            }
            return Err(error);
        }

        // SAFETY: successful `waitid` initialized the zeroed structure.
        let information = unsafe { information.assume_init() };
        // SAFETY: waitid populated the child-status variant of siginfo_t.
        let observed_pid = unsafe { information.si_pid() };
        if observed_pid == 0 {
            return Ok(false);
        }
        if observed_pid != process_id {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "waitid returned process {} while waiting for child process {process_id}",
                    observed_pid
                ),
            ));
        }
        match information.si_code {
            CHILD_EXITED | CHILD_KILLED | CHILD_DUMPED => return Ok(true),
            CHILD_STOPPED | CHILD_CONTINUED => {
                // A nonblocking exit probe must leave stop/continue status
                // with its owner. Resolver interruption waits for the stop
                // before signaling; consuming it here would strand that wait.
                if !blocking {
                    return Ok(false);
                }
                // Darwin can report these even for WEXITED. Consume only the
                // nonterminal notification and retry interrupted consumption.
                match consume_non_exit_notification(wait_id, process_id) {
                    Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                    result => result?,
                }
            }
            code => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    format!(
                        "waitid returned unexpected status code {code} for child process {process_id}"
                    ),
                ));
            }
        }
    }
}

fn consume_non_exit_notification(wait_id: libc::id_t, process_id: libc::pid_t) -> io::Result<()> {
    let mut information = std::mem::MaybeUninit::<libc::siginfo_t>::zeroed();
    // SAFETY: `information` points to writable storage. Omitting WEXITED and
    // WNOWAIT consumes only a pending stop or continue notification.
    let result = unsafe {
        libc::waitid(
            libc::P_PID,
            wait_id,
            information.as_mut_ptr(),
            libc::WSTOPPED | libc::WCONTINUED | libc::WNOHANG,
        )
    };
    if result < 0 {
        return Err(io::Error::last_os_error());
    }

    // SAFETY: successful `waitid` initialized the zeroed structure.
    let information = unsafe { information.assume_init() };
    // SAFETY: waitid populated the child-status variant of siginfo_t.
    let observed_pid = unsafe { information.si_pid() };
    if observed_pid == 0 {
        return Ok(());
    }
    if observed_pid != process_id {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!(
                "waitid returned process {} while consuming a notification for child process {process_id}",
                observed_pid
            ),
        ));
    }
    if !matches!(information.si_code, CHILD_STOPPED | CHILD_CONTINUED) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!(
                "waitid consumed unexpected status code {} for child process {process_id}",
                information.si_code
            ),
        ));
    }
    Ok(())
}

fn valid_process_id(process_id: u32) -> io::Result<libc::pid_t> {
    libc::pid_t::try_from(process_id)
        .ok()
        .filter(|process_id| *process_id > 0)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "invalid process ID"))
}
