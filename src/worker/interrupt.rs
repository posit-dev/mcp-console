use std::io;
#[cfg(target_os = "macos")]
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::sync::OnceLock;
use std::sync::atomic::{AtomicBool, Ordering};

// These callbacks only inspect or update interrupt state; they must not enter
// an interpreter. The signal callback must also be async-signal-safe. In the
// mixed runtime, R supplies the state so nested calls share one acknowledgment.
pub(super) struct State {
    pub signal: unsafe extern "C" fn(),
    // Recorded SIGINT, including while an interpreter defers its delivery.
    pub requested: fn() -> bool,
    pub pending: fn() -> bool,
    pub acknowledge: fn() -> bool,
}

static R_STATE: OnceLock<State> = OnceLock::new();
#[cfg(unix)]
static SIGNAL_WAKEUP: OnceLock<libc::c_int> = OnceLock::new();
static NATIVE_PENDING: AtomicBool = AtomicBool::new(false);
#[cfg(target_os = "macos")]
static NATIVE_INPUT_WATCH: OnceLock<OwnedFd> = OnceLock::new();

#[cfg(unix)]
unsafe extern "C" fn record_interrupt() {
    super::r_integration::record_initialization_interrupt();
    if let Some(state) = R_STATE.get() {
        unsafe { (state.signal)() };
    } else {
        NATIVE_PENDING.store(true, Ordering::SeqCst);
        // Attachment may have raced signal delivery on another native thread.
        if let Some(state) = R_STATE.get()
            && NATIVE_PENDING.swap(false, Ordering::SeqCst)
        {
            unsafe { (state.signal)() };
        }
    }
}

fn native_pending() -> bool {
    NATIVE_PENDING.load(Ordering::SeqCst)
}

fn acknowledge_native_interrupt() -> bool {
    #[cfg(windows)]
    let _publication = WINDOWS_REQUEST_LOCK
        .lock()
        .expect("interrupt publication lock");
    #[cfg(windows)]
    WINDOWS_WAKEUP.get().expect("interrupt initialized").reset();
    NATIVE_PENDING.swap(false, Ordering::SeqCst)
}

#[cfg(unix)]
unsafe extern "C" {
    fn mcp_worker_interrupt_configure(
        signal: unsafe extern "C" fn(),
        wakeup: libc::c_int,
    ) -> libc::c_int;
    fn mcp_worker_install_python_interrupt(set_interrupt: unsafe extern "C" fn()) -> libc::c_int;
}

#[cfg(unix)]
pub(super) fn normalize_signal() -> io::Result<()> {
    if unsafe { libc::signal(libc::SIGINT, libc::SIG_DFL) } == libc::SIG_ERR {
        return Err(io::Error::last_os_error());
    }
    let mut signals = unsafe { std::mem::zeroed() };
    if unsafe { libc::sigemptyset(&mut signals) } != 0
        || unsafe { libc::sigaddset(&mut signals, libc::SIGINT) } != 0
    {
        return Err(io::Error::last_os_error());
    }
    let result =
        unsafe { libc::pthread_sigmask(libc::SIG_UNBLOCK, &signals, std::ptr::null_mut()) };
    (result == 0)
        .then_some(())
        .ok_or_else(|| io::Error::from_raw_os_error(result))
}

pub(super) fn attach_r(state: State) -> io::Result<()> {
    R_STATE
        .set(state)
        .map_err(|_| io::Error::other("R interrupt state already attached"))?;
    #[cfg(unix)]
    if NATIVE_PENDING.swap(false, Ordering::SeqCst) {
        unsafe { (R_STATE.get().unwrap().signal)() };
    }
    #[cfg(windows)]
    deliver_windows_r_interrupt();
    reinstall()
}

#[cfg(unix)]
pub(super) fn reinstall() -> io::Result<()> {
    let wakeup = *SIGNAL_WAKEUP.get().expect("signal wakeup initialized");
    if unsafe { mcp_worker_interrupt_configure(record_interrupt, wakeup) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

#[cfg(unix)]
pub(super) fn initialize_native() -> io::Result<()> {
    #[cfg(target_os = "macos")]
    initialize_native_input_watch()?;
    SIGNAL_WAKEUP
        .set(super::input::initialize_interrupt_wakeup()?)
        .map_err(|_| io::Error::other("signal wakeup already initialized"))?;
    reinstall()
}

fn requested() -> bool {
    #[cfg(windows)]
    if native_pending() {
        return true;
    }
    R_STATE
        .get()
        .map_or_else(native_pending, |state| (state.requested)())
}

#[cfg(target_os = "macos")]
fn initialize_native_input_watch() -> io::Result<()> {
    let descriptor = unsafe { libc::kqueue() };
    if descriptor < 0 {
        return Err(io::Error::last_os_error());
    }
    let queue = unsafe { OwnedFd::from_raw_fd(descriptor) };
    if unsafe { libc::fcntl(descriptor, libc::F_SETFD, libc::FD_CLOEXEC) } != 0 {
        return Err(io::Error::last_os_error());
    }
    let change = libc::kevent {
        ident: libc::STDIN_FILENO as libc::uintptr_t,
        filter: libc::EVFILT_READ,
        flags: libc::EV_ADD | libc::EV_CLEAR,
        fflags: 0,
        data: 0,
        udata: std::ptr::null_mut(),
    };
    if unsafe {
        libc::kevent(
            descriptor,
            &change,
            1,
            std::ptr::null_mut(),
            0,
            std::ptr::null(),
        )
    } < 0
    {
        return Err(io::Error::last_os_error());
    }
    NATIVE_INPUT_WATCH
        .set(queue)
        .map_err(|_| io::Error::other("native input watch already initialized"))
}

#[cfg(unix)]
pub(super) fn wait_for_activity(sideband_fd: libc::c_int) -> Result<bool, String> {
    let wakeup_fd = super::input::interrupt_wakeup_fd();
    let mut descriptors = [
        libc::pollfd {
            fd: sideband_fd,
            events: libc::POLLIN,
            revents: 0,
        },
        libc::pollfd {
            fd: wakeup_fd,
            events: libc::POLLIN,
            revents: 0,
        },
        libc::pollfd {
            fd: native_input_watch_fd(),
            events: native_input_watch_events(),
            revents: 0,
        },
    ];
    loop {
        let ready = unsafe { libc::poll(descriptors.as_mut_ptr(), descriptors.len() as _, -1) };
        if ready < 0 {
            let error = io::Error::last_os_error();
            if error.kind() == io::ErrorKind::Interrupted {
                continue;
            }
            return Err(format!("native worker wait failed: {error}"));
        }
        if descriptors
            .iter()
            .any(|entry| entry.revents & libc::POLLNVAL != 0)
        {
            return Err("native worker wait received an invalid descriptor".to_string());
        }
        if descriptors[1].revents != 0 {
            super::input::drain_interrupt_wakeup().map_err(|error| error.to_string())?;
        }
        if descriptors[0].revents != 0 {
            return Ok(true);
        }
        if descriptors[2].revents & libc::POLLERR != 0 {
            return Err("native worker stdin wait failed".to_string());
        }
        #[cfg(target_os = "macos")]
        if descriptors[2].revents != 0 {
            observe_native_input_watch()?;
        }
        return Ok(false);
    }
}

#[cfg(target_os = "linux")]
fn native_input_watch_fd() -> libc::c_int {
    libc::STDIN_FILENO
}

#[cfg(target_os = "linux")]
fn native_input_watch_events() -> libc::c_short {
    libc::POLLRDHUP
}

#[cfg(target_os = "macos")]
fn native_input_watch_fd() -> libc::c_int {
    NATIVE_INPUT_WATCH
        .get()
        .expect("native input watch initialized")
        .as_raw_fd()
}

#[cfg(target_os = "macos")]
fn native_input_watch_events() -> libc::c_short {
    libc::POLLIN
}

#[cfg(target_os = "macos")]
fn observe_native_input_watch() -> Result<(), String> {
    let mut event = unsafe { std::mem::zeroed() };
    // A Python input call may consume the bytes after poll observed kqueue
    // readiness. Drain the current event without blocking on a future write;
    // the outer descriptor wait remains the sole blocking admission point.
    let immediate = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    let count = unsafe {
        libc::kevent(
            native_input_watch_fd(),
            std::ptr::null(),
            0,
            &mut event,
            1,
            &immediate,
        )
    };
    if count < 0 {
        return Err(format!(
            "native worker stdin watch failed: {}",
            io::Error::last_os_error()
        ));
    }
    if event.flags & libc::EV_EOF != 0 {
        super::core::mark_shutting_down();
    }
    Ok(())
}

pub(crate) fn pending() -> bool {
    #[cfg(windows)]
    deliver_windows_r_interrupt();
    if PYTHON_COMMITS.with_borrow(|stack| !stack.is_empty()) {
        return false;
    }
    R_STATE
        .get()
        .map_or_else(native_pending, |state| (state.pending)())
}

pub(crate) fn acknowledge_python_interrupt() -> bool {
    #[cfg(windows)]
    deliver_windows_r_interrupt();
    if PYTHON_COMMITS.with_borrow(|stack| !stack.is_empty()) {
        return false;
    }
    let interrupted = R_STATE
        .get()
        .map_or_else(acknowledge_native_interrupt, |state| (state.acknowledge)());
    if interrupted {
        super::core::record_bootstrap_interrupt();
    }
    interrupted
}

#[cfg(unix)]
pub(crate) fn install_python_interrupt(
    set_interrupt: unsafe extern "C" fn(),
) -> Result<(), String> {
    if unsafe { mcp_worker_install_python_interrupt(set_interrupt) } != 0 {
        return Err(format!(
            "failed to install Python interrupt handler: {}",
            io::Error::last_os_error()
        ));
    }
    Ok(())
}

pub(crate) fn check_python_selection_interrupt() -> Result<(), String> {
    if requested() {
        Err("Python inspection interrupted".into())
    } else {
        Ok(())
    }
}

/// Connect inspection cancellation to the worker's existing SIGINT wakeup.
/// ResolverProcess continues to own termination, output collection and reaping.
#[cfg(unix)]
pub(crate) fn inspect_python(
    executable: &std::path::Path,
) -> Result<crate::python::NativePython, String> {
    super::input::drain_interrupt_wakeup().map_err(|error| error.to_string())?;
    let (finished, completion) = io::pipe().map_err(|error| error.to_string())?;
    std::thread::scope(|scope| {
        let mut watcher = None;
        let result = crate::python::inspect_native(executable, |handle| {
            // Draining stale wakeups must not hide a queued request just
            // because the calling R callback currently defers interrupts.
            if requested() {
                return Err("Python inspection interrupted".to_string());
            }
            watcher = Some(scope.spawn(move || {
                match crate::readiness::wait_for_io(
                    super::input::interrupt_wakeup_fd(),
                    libc::POLLIN,
                    Some(&finished),
                ) {
                    Ok(ready) if ready.stream => handle.stop(),
                    Ok(_) => Ok(()),
                    Err(error) => {
                        let _ = handle.stop();
                        Err(error.to_string())
                    }
                }
            }));
            Ok(())
        });
        drop(completion);
        if let Some(watcher) = watcher {
            watcher
                .join()
                .map_err(|_| "Python inspection interrupt watcher panicked")??;
        }
        result
    })
}

thread_local! {
    static PYTHON_COMMITS: std::cell::RefCell<Vec<Option<libr::Rboolean>>> = const { std::cell::RefCell::new(Vec::new()) };
}

pub(crate) fn begin_python_commit() {
    let previous = if super::r_integration::initialized() {
        let previous = unsafe { libr::get(libr::R_interrupts_suspended) };
        unsafe { libr::set(libr::R_interrupts_suspended, libr::Rboolean_TRUE) };
        Some(previous)
    } else {
        None
    };
    PYTHON_COMMITS.with_borrow_mut(|stack| stack.push(previous));
}

pub(crate) fn finish_python_commit() -> bool {
    let previous =
        PYTHON_COMMITS.with_borrow_mut(|stack| stack.pop().expect("Python commit started"));
    if let Some(previous) = previous {
        unsafe { libr::set(libr::R_interrupts_suspended, previous) };
    }
    acknowledge_python_interrupt()
}

#[cfg(windows)]
static WINDOWS_WAKEUP: OnceLock<crate::windows::Event> = OnceLock::new();
#[cfg(windows)]
static WINDOWS_REQUESTS: OnceLock<crate::windows::Event> = OnceLock::new();
// Windows publication runs on an ordinary watcher thread. Serialize the flag
// and event with consumption, including transfer to R during attachment.
#[cfg(windows)]
static WINDOWS_REQUEST_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
#[cfg(windows)]
static WINDOWS_REQUEST_PUBLISHED: std::sync::Condvar = std::sync::Condvar::new();
#[cfg(windows)]
static PYTHON_INTERRUPT: OnceLock<unsafe extern "C" fn()> = OnceLock::new();

#[cfg(windows)]
pub(super) fn windows_wakeup() -> crate::windows::Event {
    WINDOWS_WAKEUP.get().expect("interrupt initialized").clone()
}

#[cfg(windows)]
pub(super) fn finish_windows_publication() -> io::Result<()> {
    let mut publication = WINDOWS_REQUEST_LOCK
        .lock()
        .expect("interrupt publication lock");
    let requests = WINDOWS_REQUESTS.get().expect("interrupt initialized");
    while requests.wait(Some(std::time::Duration::ZERO))? {
        publication = WINDOWS_REQUEST_PUBLISHED
            .wait(publication)
            .expect("interrupt publication lock");
    }
    Ok(())
}

#[cfg(windows)]
pub(super) fn deliver_windows_r_interrupt() {
    // R callbacks and Python interrupt checks run on the interpreter thread.
    // Transfer the atomic request only after R has installed its native state.
    if let Some(state) = R_STATE.get()
        && native_pending()
        && acknowledge_native_interrupt()
    {
        unsafe {
            (state.signal)();
            libr::set(libr::UserBreak, libr::Rboolean_TRUE);
        }
    }
}

#[cfg(windows)]
extern "C" fn windows_signal(_: libc::c_int) {
    // The relay event owns delivery. CRT handlers reset after invocation.
    let _ = normalize_signal();
}

#[cfg(windows)]
pub(super) fn normalize_signal() -> io::Result<()> {
    if unsafe {
        libc::signal(
            libc::SIGINT,
            windows_signal as *const () as libc::sighandler_t,
        )
    } == libc::SIG_ERR as libc::sighandler_t
    {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

#[cfg(windows)]
pub(super) fn reinstall() -> io::Result<()> {
    // CPython's handler also wakes its Windows SIGINT event (e.g. time.sleep).
    // Leave it installed once Python owns delivery; its services restore it
    // after R startup or a native library changes the CRT handler.
    if PYTHON_INTERRUPT.get().is_none() {
        normalize_signal()?;
    }
    Ok(())
}

#[cfg(windows)]
pub(super) fn initialize_native() -> io::Result<()> {
    let handle = std::env::var("MCP_CONSOLE_INTERRUPT_HANDLE")
        .map_err(io::Error::other)?
        .parse::<usize>()
        .map_err(io::Error::other)?;
    if handle == 0 || handle == usize::MAX {
        return Err(io::Error::other("invalid worker interrupt handle"));
    }
    let requests = unsafe { crate::windows::Event::from_inherited(handle as _)? };
    WINDOWS_REQUESTS
        .set(requests.clone())
        .map_err(|_| io::Error::other("interrupt requests already initialized"))?;
    let pending = crate::windows::Event::new()?;
    super::input::initialize_windows_stdin(pending.clone())
        .map_err(|error| io::Error::other(error.to_string()))?;
    WINDOWS_WAKEUP
        .set(pending.clone())
        .map_err(|_| io::Error::other("interrupt already initialized"))?;
    reinstall()?;
    // Embedded R/Python and their native libraries use the system UCRT's
    // signal table, separate from Console's /MT runtime. Dispatch through
    // that CRT so CPython's wait event and temporary native handlers wake.
    let runtime = unsafe {
        libloading::os::windows::Library::load_with_flags(
            "ucrtbase.dll",
            libloading::os::windows::LOAD_LIBRARY_SEARCH_SYSTEM32,
        )
    }
    .map_err(io::Error::other)?;
    let signal = unsafe {
        *runtime
            .get::<unsafe extern "C" fn(libc::c_int, libc::sighandler_t) -> libc::sighandler_t>(
                b"signal\0",
            )
            .map_err(io::Error::other)?
    };
    let raise = unsafe {
        *runtime
            .get::<unsafe extern "C" fn(libc::c_int) -> libc::c_int>(b"raise\0")
            .map_err(io::Error::other)?
    };
    std::thread::Builder::new()
        .name("worker-interrupt".into())
        .spawn(move || {
            // The function pointers must not outlive their loaded CRT module.
            let _runtime = runtime;
            while requests.wait(None).is_ok() {
                {
                    let _publication = WINDOWS_REQUEST_LOCK
                        .lock()
                        .expect("interrupt publication lock");
                    NATIVE_PENDING.store(true, Ordering::SeqCst);
                    super::r_integration::record_initialization_interrupt();
                    pending.set();
                }
                // CPython's signal API is safe without the GIL, including while R
                // has not been initialized. Wake managed stdin and inspection too.
                if let Some(interrupt) = PYTHON_INTERRUPT.get() {
                    unsafe { interrupt() };
                }
                // Before interpreter startup the UCRT may still use SIG_DFL.
                // Probe and restore its handler without delivering a fatal
                // default signal; the relay event still owns the request.
                unsafe {
                    let handler = signal(libc::SIGINT, libc::SIG_IGN);
                    if handler != libc::SIG_ERR as libc::sighandler_t {
                        signal(libc::SIGINT, handler);
                        if handler != libc::SIG_DFL && handler != libc::SIG_IGN {
                            raise(libc::SIGINT);
                        }
                    }
                    // Console's own handler also rearms its private CRT table.
                    libc::raise(libc::SIGINT);
                }
                // Keep the request visible until all signal APIs have returned.
                // Idle command dispatch waits here before acknowledging it, so
                // a delayed watcher cannot interrupt the following cell.
                let _publication = WINDOWS_REQUEST_LOCK
                    .lock()
                    .expect("interrupt publication lock");
                requests.reset();
                WINDOWS_REQUEST_PUBLISHED.notify_all();
            }
        })?;
    unsafe {
        std::env::remove_var("MCP_CONSOLE_INTERRUPT_HANDLE");
    }
    Ok(())
}

#[cfg(windows)]
pub(crate) fn install_python_interrupt(
    set_interrupt: unsafe extern "C" fn(),
) -> Result<(), String> {
    PYTHON_INTERRUPT.get_or_init(|| set_interrupt);
    Ok(())
}

#[cfg(windows)]
pub(crate) fn inspect_python(
    executable: &std::path::Path,
) -> Result<crate::python::NativePython, String> {
    with_python_interrupt(|started| crate::python::inspect_native(executable, started))
}

#[cfg(windows)]
pub(crate) fn with_python_interrupt<T>(
    operation: impl FnOnce(
        &dyn Fn(crate::resolver::ResolverStopHandle) -> Result<(), String>,
    ) -> Result<T, String>,
) -> Result<T, String> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Foundation::WAIT_OBJECT_0;
    use windows_sys::Win32::System::Threading::{INFINITE, WaitForMultipleObjects};
    check_python_selection_interrupt()?;
    let wakeup = WINDOWS_WAKEUP.get().expect("interrupt initialized");
    let (finished, completion) =
        crate::windows::notification().map_err(|error| error.to_string())?;
    std::thread::scope(|scope| {
        let watcher = std::sync::Mutex::new(None);
        let result = operation(&|handle| {
            check_python_selection_interrupt()?;
            let finished = finished.clone();
            *watcher.lock().unwrap() = Some(scope.spawn(move || {
                let handles = [finished.as_raw_handle(), wakeup.as_raw_handle()];
                match unsafe { WaitForMultipleObjects(2, handles.as_ptr(), 0, INFINITE) } {
                    WAIT_OBJECT_0 => Ok(()),
                    result if result == WAIT_OBJECT_0 + 1 => handle.stop(),
                    _ => {
                        let _ = handle.stop();
                        Err(io::Error::last_os_error().to_string())
                    }
                }
            }));
            Ok(())
        });
        drop(completion);
        if let Some(watcher) = watcher.into_inner().unwrap() {
            watcher
                .join()
                .map_err(|_| "Python inspection watcher panicked")??;
        }
        result
    })
}
