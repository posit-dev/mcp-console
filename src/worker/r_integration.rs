use super::{core, embedded_r, interrupt};

use std::cell::{Cell, RefCell};
use std::path::{Path, PathBuf};
use std::rc::Rc;
use std::sync::OnceLock;
use std::sync::atomic::{AtomicBool, Ordering};

use crate::local_runtime::RInstallation;
use crate::worker_protocol::{RInitializationState, WorkerMessage};

static R_INSTALLATION: OnceLock<Option<RInstallation>> = OnceLock::new();
static R_SETTINGS: OnceLock<crate::settings::R> = OnceLock::new();
static R_LIBRARIES: OnceLock<Result<harp::library::RLibraries, String>> = OnceLock::new();
static INITIALIZING: AtomicBool = AtomicBool::new(false);
static INTERRUPTED: AtomicBool = AtomicBool::new(false);

pub(super) fn record_initialization_interrupt() {
    if INITIALIZING.load(Ordering::SeqCst) {
        INTERRUPTED.store(true, Ordering::SeqCst);
    }
}
thread_local! {
    static RUNTIME: RefCell<Option<Rc<embedded_r::Runtime>>> = const { RefCell::new(None) };
    static PROJECT_LIBRARY: Cell<Option<ProjectLibrary>> = const { Cell::new(None) };
    static FAILED: Cell<bool> = const { Cell::new(false) };
}

// Clone the process-lifetime handle before interpreter calls: synchronous
// R -> Python -> R callbacks must never reenter a borrowed runtime slot.
fn runtime() -> Option<Rc<embedded_r::Runtime>> {
    RUNTIME.with(|runtime| runtime.borrow().clone())
}

pub(crate) fn initialized() -> bool {
    runtime().is_some()
}

pub(super) fn evaluate_priority(source: &str) -> Result<(), String> {
    let runtime =
        runtime().ok_or("initialize R with a serial evaluation before immediate execution")?;
    runtime.evaluate_priority(source)
}

pub(super) fn shutdown() -> Result<(), String> {
    if let Some(runtime) = runtime() {
        runtime.shutdown().map_err(|error| error.to_string())?;
    }
    Ok(())
}

pub(crate) fn available() -> bool {
    R_INSTALLATION.get().is_some_and(Option::is_some)
}

pub(crate) enum RInitialization {
    Ready,
    TempdirWhitespace,
}

const TEMPDIR_WHITESPACE: &str = "R requires a temporary directory without whitespace. \
    Set sandbox.tempdir.managed.parent to a suitable directory and relaunch Console, \
    or set TMPDIR before starting R.";

impl RInitialization {
    pub(crate) fn require(self) -> Result<(), String> {
        match self {
            Self::Ready => Ok(()),
            Self::TempdirWhitespace => Err(TEMPDIR_WHITESPACE.into()),
        }
    }

    /// Optional eager initialization can defer the diagnostic until a cell needs R.
    pub(crate) fn ready_or_report(self) -> bool {
        if let Err(error) = self.require() {
            if !core::bootstrapping() {
                core::emit_output(
                    crate::worker_protocol::ConsoleChannel::Diagnostic,
                    format!("Error: {error}\n").as_bytes(),
                );
                core::record_execution_error(crate::worker_protocol::ExecutionError::new(
                    "RInitializationError",
                    error,
                ));
            }
            return false;
        }
        true
    }
}

pub(crate) fn ensure_initialized() -> Result<RInitialization, String> {
    if FAILED.with(Cell::get) {
        return Err("R initialization is incomplete; restart required".into());
    }
    if initialized() {
        return Ok(RInitialization::Ready);
    }
    let installation = R_INSTALLATION
        .get()
        .and_then(Option::as_ref)
        .ok_or("R is unavailable in this session")?;
    if INITIALIZING.swap(true, Ordering::SeqCst) {
        return Err("R initialization is incomplete; restart required".into());
    }
    let result = initialize(installation);
    INITIALIZING.store(false, Ordering::SeqCst);
    let result = result.and_then(|outcome| {
        if INTERRUPTED.load(Ordering::SeqCst) {
            return Err("R initialization interrupted; explicit restart required".into());
        }
        let state = match outcome {
            RInitialization::Ready => RInitializationState::Complete,
            RInitialization::TempdirWhitespace => RInitializationState::Paused,
        };
        core::send_worker_message(&WorkerMessage::RInitialization { state })?;
        Ok(outcome)
    });
    FAILED.with(|failed| failed.set(result.is_err()));
    result
}

fn initialize(installation: &RInstallation) -> Result<RInitialization, String> {
    core::send_worker_message(&WorkerMessage::RInitialization {
        state: RInitializationState::Entered,
    })?;
    // R's argument processing reads .Renviron. Retain that setup so a refused
    // temp directory can be corrected without replaying native startup.
    let resuming = R_LIBRARIES.get().is_some();
    let libraries = R_LIBRARIES
        .get_or_init(|| {
            // R can mutate the environment after Python has started native threads.
            // Owning-thread affinity and the GIL do not serialize foreign environment
            // access; see RT-1 in docs/TODO.md.
            crate::python::configure_r_environment().map_err(|error| error.to_string())?;
            embedded_r::read_startup_environment(installation, R_SETTINGS.get().unwrap().vanilla)
                .map_err(|error| error.to_string())
        })
        .as_ref()
        .map_err(Clone::clone)?;
    if embedded_r::rejects_tempdir(&installation.home).map_err(|error| error.to_string())? {
        return Ok(RInitialization::TempdirWhitespace);
    }
    if resuming {
        // Python could change the installation paths while correcting TMPDIR.
        embedded_r::configure_installation(installation).map_err(|error| error.to_string())?;
    }
    // R resolves relative R_LIBS entries in the directory where its main loop
    // starts. Python may have changed directories since the last activation.
    if let Some(project) = PROJECT_LIBRARY.take() {
        let paths = startup_library_paths();
        // Native environment files can also replace R_LIBS deliberately.
        if paths.contains(&project.entry) {
            prepare_startup_library(&project.resolved, paths).map_err(|error| error.to_string())?;
        }
    }
    embedded_r::start_mainloop(libraries).map_err(|error| error.to_string())?;
    publish_r_session()?;
    let runtime = Rc::new(embedded_r::Runtime::initialize().map_err(|error| error.to_string())?);
    RUNTIME.with(|slot| *slot.borrow_mut() = Some(runtime.clone()));
    if core::bootstrapping()
        || core::cell_language()
            .is_some_and(|language| !matches!(language, crate::cell::Language::Sql))
    {
        runtime.begin_graphics()?;
    }
    crate::python::attach_r_adapter()?;
    crate::sql::attach_r()?;
    interrupt::reinstall().map_err(|error| error.to_string())?;
    crate::python::reinstall_services()?;
    Ok(RInitialization::Ready)
}

fn publish_r_session() -> Result<(), String> {
    // rpy2 trusts this PID before accessing R objects, including via an
    // already-live Python's cached os.environ.
    let marker = format!("PID={}:NAME=\"reticulate\"", std::process::id());
    embedded_r::set_environment(&[("R_SESSION_INITIALIZED", marker.as_ref())])
        .map_err(|error| error.to_string())?;
    crate::python::publish_r_session()
}

pub(crate) fn ensure_r_for_rpy2() -> Result<(), String> {
    // Reticulate can import rpy2 from inside R's native startup profiles.
    // Attach to that live R evaluation without entering startup recursively.
    if INITIALIZING.load(Ordering::SeqCst)
        && R_LIBRARIES.get().is_some()
        && embedded_r::has_global_environment().map_err(|error| error.to_string())?
    {
        return publish_r_session();
    }
    ensure_initialized()?.require()
}

pub(crate) fn ensure_bridge() -> Result<(), String> {
    ensure_initialized()?.require()?;
    if crate::python::attach_bridge()? {
        Ok(())
    } else {
        Err("R/Python attachment did not complete; retry the operation".into())
    }
}

pub(super) struct Integration;

impl Integration {
    pub(super) fn new(
        installation: Option<RInstallation>,
        settings: crate::settings::R,
    ) -> std::io::Result<Self> {
        R_SETTINGS
            .set(settings)
            .map_err(|_| std::io::Error::other("R settings already configured"))?;
        R_INSTALLATION
            .set(installation)
            .map_err(|_| std::io::Error::other("R capability already configured"))?;
        interrupt::initialize_native()?;
        Ok(Self)
    }

    #[cfg(unix)]
    pub(super) fn wait_for_activity(&self, sideband_fd: libc::c_int) -> Result<bool, String> {
        if initialized() {
            embedded_r::wait_for_activity(sideband_fd)
        } else {
            interrupt::wait_for_activity(sideband_fd)
        }
    }

    pub(super) fn idle(&self) -> Result<(), String> {
        if let Some(r) = runtime() {
            r.idle()
        } else {
            // Consume idle SIGINT before admitting another cell. CPython's
            // pending hook then sees acknowledged state and returns normally.
            interrupt::acknowledge_python_interrupt();
            core::observe_stdin_shutdown()
        }
    }

    pub(super) fn check_interrupts(&self) {
        if initialized() {
            embedded_r::check_interrupts();
        }
    }

    pub(super) fn begin_graphics(&self) -> Result<(), String> {
        if let Some(r) = runtime() {
            r.begin_graphics()
        } else {
            Ok(())
        }
    }

    pub(super) fn finish_graphics(&self) -> Result<(), String> {
        if let Some(r) = runtime() {
            r.finish_graphics()
        } else {
            Ok(())
        }
    }

    pub(super) fn evaluate_r(&self, source: String) -> Result<(), String> {
        if ensure_initialized()?.ready_or_report() {
            runtime().unwrap().evaluate(source)?;
        }
        Ok(())
    }

    pub(super) fn prepare_r(
        &self,
        library: &str,
    ) -> Result<crate::r_environment::PreparationOutcome, String> {
        if let Some(runtime) = runtime() {
            return runtime.prepare(library);
        }
        // R may already have read .Renviron. Update its live search path
        // without running profiles or initializing an unused interpreter.
        let prepared = prepare_startup_library(Path::new(library), startup_library_paths());
        Ok(match prepared {
            Ok(project) => {
                PROJECT_LIBRARY.set(project);
                crate::r_environment::PreparationOutcome::Prepared {
                    library: library.into(),
                }
            }
            Err(error) => crate::r_environment::PreparationOutcome::Failed {
                message: error.to_string(),
            },
        })
    }
}

// Keep the resolver's absolute path while a project spelling supplies it.
struct ProjectLibrary {
    entry: PathBuf,
    resolved: PathBuf,
}

fn startup_library_paths() -> Vec<PathBuf> {
    std::env::var_os("R_LIBS")
        .iter()
        .flat_map(std::env::split_paths)
        .filter(|path| !path.as_os_str().is_empty())
        .collect()
}

fn prepare_startup_library(
    library: &Path,
    mut paths: Vec<PathBuf>,
) -> Result<Option<ProjectLibrary>, Box<dyn std::error::Error>> {
    const MANAGED: &str = "MCP_CONSOLE_R_LIBRARY";
    // Remove only the spelling Console supplied. A project alias for the
    // previous directory is a separate entry.
    let previous = std::env::var_os(MANAGED)
        .filter(|path| !path.is_empty())
        .and_then(|previous| paths.iter().position(|path| path.as_os_str() == previous));
    if let Some(index) = previous {
        paths.remove(index);
    }
    // As in the running R bridge, a project entry already providing this
    // library stays at its position and remains project-owned.
    let target = library.canonicalize()?;
    let project = paths
        .iter()
        .find(|path| embedded_r::canonical_library_directory(path).as_ref() == Some(&target))
        .map(|entry| ProjectLibrary {
            entry: entry.clone(),
            resolved: library.to_owned(),
        });
    let owned = if project.is_some() {
        Path::new("")
    } else {
        paths.insert(previous.unwrap_or(0), library.to_owned());
        library
    };
    let r_libs = std::env::join_paths(paths)?;
    embedded_r::set_environment(&[("R_LIBS", &r_libs), (MANAGED, owned.as_os_str())])?;
    Ok(project)
}
