use super::{core, embedded_r, interrupt};

use std::cell::{Cell, RefCell};
use std::path::{Path, PathBuf};
use std::rc::Rc;
use std::sync::OnceLock;
use std::sync::atomic::{AtomicBool, Ordering};

use crate::local_runtime::RInstallation;
use crate::r_environment::is_library_pattern;
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
    static PREPARED_LIBRARY: RefCell<Option<PreparedLibrary>> = const { RefCell::new(None) };
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
    ShuttingDown,
}

const TEMPDIR_WHITESPACE: &str = "R requires a temporary directory without whitespace. \
    Set sandbox.tempdir.managed.parent to a suitable directory and relaunch Console, \
    or set TMPDIR before starting R.";

impl RInitialization {
    pub(crate) fn ready(self) -> Result<bool, String> {
        match self {
            Self::Ready => Ok(true),
            Self::TempdirWhitespace => Err(TEMPDIR_WHITESPACE.into()),
            Self::ShuttingDown => Ok(false),
        }
    }

    pub(crate) fn require(self) -> Result<(), String> {
        self.ready()?
            .then_some(())
            .ok_or_else(|| "worker is shutting down".into())
    }

    /// Optional eager initialization can defer the diagnostic until a cell needs R.
    pub(crate) fn ready_or_report(self) -> bool {
        self.ready().unwrap_or_else(|error| {
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
            false
        })
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
        let state = match outcome {
            RInitialization::Ready => RInitializationState::Complete,
            RInitialization::TempdirWhitespace => RInitializationState::Paused,
            RInitialization::ShuttingDown => return Ok(outcome),
        };
        if INTERRUPTED.load(Ordering::SeqCst) {
            return Err("R initialization interrupted; explicit restart required".into());
        }
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
    if let Err(error) = StartupLibraries::read().settle(installation) {
        if core::is_shutting_down() && error.is::<crate::resolver::process::ResolverCancelled>() {
            return Ok(RInitialization::ShuttingDown);
        }
        return Err(error.to_string());
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
        let mut libraries = StartupLibraries::read();
        let prepared = PREPARED_LIBRARY
            .with_borrow(|previous| libraries.prepare(Path::new(library), previous.as_ref()));
        Ok(match prepared {
            Ok(entry) => {
                PREPARED_LIBRARY.set(Some(PreparedLibrary {
                    entry,
                    resolved: library.into(),
                    retained: libraries.retained,
                    relative_entries: libraries
                        .paths
                        .into_iter()
                        .filter(|entry| entry.is_relative())
                        .collect(),
                }));
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

// Keep the resolver's absolute path and the entry that supplied it before R started.
struct PreparedLibrary {
    entry: PathBuf,
    resolved: PathBuf,
    // These project spellings can start supplying the candidate after a chdir.
    relative_entries: Vec<PathBuf>,
    retained: Vec<RetainedLibrary>,
}

#[derive(Clone)]
struct RetainedLibrary {
    path: PathBuf,
    // Later project entries that supplied this slot or may expand to it.
    aliases: Vec<PathBuf>,
}

impl RetainedLibrary {
    fn slot(&self, paths: &[PathBuf]) -> Option<usize> {
        let index = paths.iter().position(|entry| *entry == self.path)?;
        // .Renviron can replace the later aliases with an explicit absolute
        // entry. Only a slot still accompanied by a recorded alias is conditional.
        paths[index + 1..]
            .iter()
            .any(|entry| self.aliases.contains(entry))
            .then_some(index)
    }
}

const MANAGED_LIBRARY: &str = "MCP_CONSOLE_R_LIBRARY";

struct StartupLibraries {
    paths: Vec<PathBuf>,
    owned: Option<usize>,
    retained: Vec<RetainedLibrary>,
}

impl StartupLibraries {
    fn read() -> Self {
        let paths: Vec<_> = std::env::var_os("R_LIBS")
            .iter()
            .flat_map(std::env::split_paths)
            .filter(|path| !path.as_os_str().is_empty())
            .collect();
        let owned = std::env::var_os(MANAGED_LIBRARY)
            .and_then(|managed| paths.iter().position(|path| path.as_os_str() == managed));
        Self {
            paths,
            owned,
            retained: Vec::new(),
        }
    }

    fn settle(mut self, installation: &RInstallation) -> Result<(), Box<dyn std::error::Error>> {
        // Recheck the accepted library in R's final startup directory unless
        // .Renviron deliberately removed its supplying entry.
        let previous = PREPARED_LIBRARY.take();
        let pending = previous
            .as_ref()
            .filter(|prepared| self.paths.contains(&prepared.entry))
            .filter(|prepared| prepared.resolved.is_dir());
        let mut retained: Vec<_> = previous
            .iter()
            .flat_map(|prepared| &prepared.retained)
            .filter_map(|library| library.slot(&self.paths))
            .filter(|index| Some(*index) != self.owned)
            .collect();
        let active = pending
            .map(|prepared| prepared.resolved.clone())
            .or_else(|| {
                std::env::var_os(MANAGED_LIBRARY)
                    .filter(|path| !path.is_empty())
                    .map(PathBuf::from)
            });
        let candidates: Vec<_> = retained
            .iter()
            .map(|&index| self.paths[index].clone())
            .chain(active.iter().cloned())
            .collect();
        let supplied = self.project_supplied_libraries(installation, &retained, &candidates)?;
        retained.sort_unstable();
        let count = self.paths.len();
        for index in retained.into_iter().rev() {
            if supplied.contains(&self.paths[index]) {
                continue;
            }
            self.paths.remove(index);
            if let Some(owned) = &mut self.owned
                && *owned > index
            {
                *owned -= 1;
            }
        }
        if self.paths.len() != count {
            embedded_r::set_environment(&[("R_LIBS", &std::env::join_paths(&self.paths)?)])?;
        }
        if let Some(prepared) = pending {
            self.prepare(&prepared.resolved, Some(prepared))?;
        }
        if active.is_some_and(|path| supplied.contains(&path)) {
            embedded_r::set_environment(&[(MANAGED_LIBRARY, "".as_ref())])?;
        }
        Ok(())
    }

    fn project_supplied_libraries(
        &self,
        installation: &RInstallation,
        retained: &[usize],
        candidates: &[PathBuf],
    ) -> Result<Vec<PathBuf>, Box<dyn std::error::Error>> {
        if candidates.is_empty() {
            return Ok(Vec::new());
        }
        // A provisional slot cannot prove ownership of another library. Check
        // the project's entries together before native profiles can change them.
        let project: Vec<_> = self
            .paths
            .iter()
            .enumerate()
            .filter(|(index, _)| Some(*index) != self.owned && !retained.contains(index))
            .map(|(_, path)| path.clone())
            .collect();
        if has_library_patterns(&project) {
            return project_glob_libraries(installation, &project, candidates);
        }
        Ok(candidates
            .iter()
            .filter(|candidate| {
                candidate.canonicalize().is_ok_and(|target| {
                    project.iter().any(|path| is_literal_library(path, &target))
                })
            })
            .cloned()
            .collect())
    }

    fn prepare(
        &mut self,
        library: &Path,
        prepared: Option<&PreparedLibrary>,
    ) -> Result<PathBuf, Box<dyn std::error::Error>> {
        let target = library.canonicalize()?;
        if let Some(prepared) = prepared {
            self.retained.clone_from(&prepared.retained);
        }
        let previous = self.owned;
        if let Some(index) = previous
            && let Ok(current) = self.paths[index].canonicalize()
        {
            let aliases: Vec<_> = self.paths[index + 1..]
                .iter()
                .filter(|path| {
                    // Defer wildcard ownership until R's final startup directory.
                    if literal_library_path(path).is_none() {
                        return self.paths[index] != library;
                    }
                    // A relative entry that starts matching after a chdir keeps
                    // its project slot when we remove the earlier fallback.
                    !prepared.is_some_and(|p| {
                        p.resolved == self.paths[index] && p.relative_entries.contains(path)
                    }) && is_literal_library(path, &current)
                })
                .cloned()
                .collect();
            if !aliases.is_empty() {
                // Native R keeps the first occurrence. Remember which later
                // project spellings supplied it so startup can recheck them.
                self.retained
                    .retain(|library| library.path != self.paths[index]);
                self.retained.push(RetainedLibrary {
                    path: self.paths[index].clone(),
                    aliases,
                });
                self.owned = None;
            }
        }
        let project = self
            .paths
            .iter()
            .enumerate()
            .find(|(index, path)| self.owned != Some(*index) && is_literal_library(path, &target))
            .map(|(_, entry)| entry.clone());
        // Preserve the native R_LIBS spelling (including empty entries) when
        // the same entry still supplies the candidate after .Renviron.
        if project.is_none() && self.owned.is_some_and(|index| self.paths[index] == library) {
            return Ok(library.to_owned());
        }
        if let Some(entry) = &project
            && self.owned.is_none()
        {
            embedded_r::set_environment(&[(MANAGED_LIBRARY, "".as_ref())])?;
            return Ok(entry.clone());
        }
        if let Some(index) = self.owned.take() {
            self.paths.remove(index);
        }
        let marker = if project.is_some() {
            Path::new("")
        } else {
            let position = if has_library_patterns(&self.paths) {
                self.paths.len()
            } else {
                previous.unwrap_or(0)
            };
            self.paths.insert(position, library.to_owned());
            self.owned = Some(position);
            library
        };
        let paths = std::env::join_paths(&self.paths)?;
        embedded_r::set_environment(&[("R_LIBS", &paths), (MANAGED_LIBRARY, marker.as_os_str())])?;
        Ok(project.unwrap_or_else(|| library.to_owned()))
    }
}

fn has_library_patterns(paths: &[PathBuf]) -> bool {
    paths
        .iter()
        .any(|path| literal_library_path(path).is_none())
}

fn literal_library_path(path: &Path) -> Option<std::borrow::Cow<'_, Path>> {
    // .libPaths() expands the home before Sys.glob interprets the resulting path.
    let expanded = embedded_r::expand_library_home(path);
    (!is_library_pattern(&expanded)).then_some(expanded)
}

fn is_literal_library(path: &Path, target: &Path) -> bool {
    literal_library_path(path)
        .and_then(|path| path.canonicalize().ok())
        .as_deref()
        == Some(target)
}

fn project_glob_libraries(
    installation: &RInstallation,
    paths: &[PathBuf],
    candidates: &[PathBuf],
) -> Result<Vec<PathBuf>, Box<dyn std::error::Error>> {
    use crate::resolver::process::{ResolverProcess, resolver_command};
    use crate::resolver::result_file::ResultFile;

    // Let the selected R expand just the project's R_LIBS in the worker's
    // startup directory. R's base library is always on the search path.
    let temporary = match crate::worker::managed_tempdir() {
        Some(path) => PathBuf::from(path),
        None => embedded_r::tempdir_parent()?,
    };
    let result = ResultFile::create(&temporary)?;
    let executable = crate::local_runtime::rscript_in(&installation.home);
    let mut command = resolver_command(&executable);
    installation.configure(&mut command)?;
    command
        .args([
            "--vanilla",
            "--default-packages=NULL",
            "-e",
            concat!(
                "args <- commandArgs(TRUE); ",
                "libraries <- normalizePath(args[-1L], winslash='/', mustWork=FALSE); ",
                "writeBin(as.raw(libraries %in% .libPaths()), args[[1L]])"
            ),
        ])
        .arg(result.path())
        .args(candidates)
        // R's base profile sources R_TESTS even with --vanilla. The hook
        // belongs to the embedded session and must run there only.
        .env_remove("R_TESTS")
        .env("R_LIBS", std::env::join_paths(paths)?)
        .env("R_LIBS_USER", "NULL")
        .env("R_LIBS_SITE", "NULL");
    let output = interrupt::with_inspection_interrupt("R library inspection", |started| {
        let resolver = ResolverProcess::new();
        let invocation = resolver
            .spawn(&mut command, None)
            .map_err(|error| format!("cannot start Rscript library inspection: {error}"))?;
        resolver.collect_or_cancel(invocation, &executable, "R library inspection", started)
    })?;
    let output = match output {
        std::ops::ControlFlow::Continue(output) => output,
        std::ops::ControlFlow::Break(cancelled) => return Err(cancelled.into()),
    };
    if !output.status.success() {
        return Err(output
            .failure(format!(
                "cannot inspect project R library patterns with Rscript ({}): {}",
                output.status,
                String::from_utf8_lossy(&output.stderr).trim()
            ))
            .into());
    }
    // R_BATCH and native shutdown hooks can write to stdout on success.
    let matches = result.read(candidates.len() as u64)?;
    if matches.len() != candidates.len() || matches.iter().any(|&found| found > 1) {
        return Err("Rscript returned an invalid project library result".into());
    }
    Ok(candidates
        .iter()
        .zip(matches)
        .filter(|(_, found)| *found == 1)
        .map(|(candidate, _)| candidate.clone())
        .collect())
}
