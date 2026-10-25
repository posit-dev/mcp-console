//! CPython bootstrap operations, independent of the R/reticulate adapter.
//! Interpreter lifetime and setup completion are retained by `library`.

use super::ImportResolution;
use std::path::Path;

thread_local! {
    static SETUP_IN_PROGRESS: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}

pub(super) fn setup_in_progress() -> bool {
    SETUP_IN_PROGRESS.get()
}

struct SetupScope;

impl Drop for SetupScope {
    fn drop(&mut self) {
        SETUP_IN_PROGRESS.set(false);
    }
}

#[derive(Clone, Debug, PartialEq, Eq, serde::Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct SelectedPython {
    pub(crate) python: String,
    pub(crate) libpython: String,
    pub(crate) python_home: String,
}

pub(crate) fn initialize_selected(configuration: &super::NativePython) -> Result<bool, String> {
    let selection = super::SELECTION
        .get()
        .ok_or("Python capability is not configured")?;
    let adopting = super::library::initialized_selection()?.is_none();
    let prepared = super::requirements::materialized();
    let captured = super::pending_executable()
        .transpose()?
        .or_else(|| {
            prepared
                .as_ref()
                .filter(|_| adopting)
                .map(|candidate| Path::new(&candidate.selected.embedding.python))
        })
        .or_else(|| {
            selection
                .python
                .as_ref()
                .filter(|python| !python.managed || adopting)
                .map(|python| Path::new(&python.selected.embedding.python))
        });
    if let Some(captured) = captured {
        let requested = Path::new(&configuration.embedding.python);
        // A profile can initialize reticulate before Console attaches its R
        // adapter. Adopt it only when it preserves the configured environment;
        // different virtualenvs can link to the same base executable. Managed
        // activation can replace that environment after its first adoption.
        if captured != requested {
            let parent = |path: &Path| {
                path.parent()
                    .ok_or("Python executable has no parent directory")?
                    .canonicalize()
                    .map_err(|error| error.to_string())
            };
            if parent(captured)? != parent(requested)?
                || captured.canonicalize().map_err(|error| error.to_string())?
                    != requested
                        .canonicalize()
                        .map_err(|error| error.to_string())?
            {
                return Err("Python interpreter differs from the captured selection".into());
            }
        }
    }
    super::library::initialize(configuration)
}

// Invoked once, after selection validation and before CPython runs any hooks.
// These process-wide changes share the serialized interpreter thread. They
// remain committed with the interpreter even when bridge attachment fails.
pub(super) fn configure_process_environment(selected: &super::NativePython) -> Result<(), String> {
    let executable = Path::new(&selected.embedding.python);
    let directory = executable
        .parent()
        .ok_or("selected Python has no parent directory")?;
    let mut path = vec![directory.to_path_buf()];
    if let Some(inherited) = std::env::var_os("PATH") {
        path.extend(std::env::split_paths(&inherited));
    }
    let path = std::env::join_paths(path).map_err(|error| error.to_string())?;
    // The current process loads the inspected library by absolute path. Linux
    // children also need the selected installation's dependent library paths.
    #[cfg(target_os = "linux")]
    let libraries = {
        let mut libraries = vec![Path::new(&selected.prefix).join("lib")];
        if selected.base_prefix != selected.prefix {
            libraries.push(Path::new(&selected.base_prefix).join("lib"));
        }
        if let Some(inherited) = std::env::var_os("LD_LIBRARY_PATH") {
            libraries.extend(std::env::split_paths(&inherited));
        }
        std::env::join_paths(libraries).map_err(|error| error.to_string())?
    };
    // CPython reads pyvenv.cfg from the preserved program name. Setting the
    // base PythonHome overrides the virtualenv and would require reactivation.
    unsafe {
        std::env::remove_var("PYTHONHOME");
        std::env::remove_var("PYTHONPLATLIBDIR");
        std::env::set_var("PATH", path);
        #[cfg(target_os = "linux")]
        std::env::set_var("LD_LIBRARY_PATH", libraries);
        if selected.prefix != selected.base_prefix {
            std::env::set_var("VIRTUAL_ENV", &selected.prefix);
        } else {
            std::env::remove_var("VIRTUAL_ENV");
        }
        if let Some(path) = std::env::var_os("RETICULATE_PYTHONPATH") {
            std::env::set_var("PYTHONPATH", path);
        }
    }
    Ok(())
}

pub(super) fn install_services(libpython: &Path) -> Result<(), String> {
    // Reticulate calls this after its own stream and input hooks. Reasserting
    // the interrupt handler is intentional when those hooks run again.
    super::library::load(libpython)?;
    super::library::install_services()
}

/// Install the shared evaluator after the selected interpreter is live.
/// Fresh R-first selection calls this before conversion attachment; an
/// externally initialized interpreter enters through reticulate's hook.
/// Both compositions install their captured import policy through this boundary.
/// Successful steps remain in the process-lifetime library state, so a later
/// call resumes incomplete setup without replacing the interpreter.
pub(crate) fn setup_runtime(libpython: &Path, managed: bool) -> Result<bool, String> {
    super::library::load(libpython)?;
    if super::library::runtime_configured()? {
        return Ok(true);
    }
    // Site hooks can request the R bridge. That request attaches conversion
    // only; the outer setup owns site processing and its initial thread state.
    if SETUP_IN_PROGRESS.replace(true) {
        return Err("Python runtime setup reentered before completion".into());
    }
    let _setup = SetupScope;
    // The hook may already have installed services. Native callers arrive
    // here directly, and perform the same installation once.
    if !super::library::services_installed()? {
        install_services(libpython)?;
    }
    let site_initialized = super::library::initialize_site()?;
    // Startup customizations precede evaluator defaults. Install the evaluator
    // even after an interrupted hook so it can report the retained exception.
    super::library::install_runtime(super::RUNTIME_SOURCE)?;
    if !site_initialized {
        return Ok(false);
    }
    if !crate::sql::install_python_runtime()? {
        return Ok(false);
    }
    if !crate::worker::r_available() {
        super::library::configure_native_sql()?;
    }
    if !super::library::configure_environment()? {
        return Err("Python environment setup failed; restart required".into());
    }
    if !super::library::configure_module_defaults()? {
        return Ok(false);
    }
    if !super::library::configure_import_resolution(import_policy(managed))? {
        return Ok(false);
    }
    if managed
        && !super::environment::initialize(
            libpython
                .to_str()
                .ok_or("Python library path is not UTF-8")?,
        )?
    {
        return Ok(false);
    }
    super::library::mark_runtime_configured()?;
    Ok(true)
}

fn import_policy(managed: bool) -> ImportResolution {
    if !managed {
        return ImportResolution::Native;
    }
    let selection = super::SELECTION
        .get()
        .expect("runtime capabilities are configured before Python setup");
    if selection.automatic_python_resolution {
        ImportResolution::Implicit
    } else {
        ImportResolution::Native
    }
}

pub(crate) fn finish_initialization() -> Result<(), String> {
    if setup_in_progress() {
        // A callback can adopt an already-running interpreter while site
        // processing still owns its active Python thread state.
        return Ok(());
    }
    super::library::finish_initialization()
}

/// Initialize a host-selected interpreter without constructing an R adapter.
/// Successful return includes environment, SQL, and import-resolution setup.
pub(super) fn initialize_native(
    configuration: &super::NativePython,
    managed: bool,
) -> Result<bool, String> {
    let selected = &configuration.embedding;
    initialize_selected(configuration)?;
    let result = setup_runtime(Path::new(&selected.libpython), managed);
    if !matches!(result, Ok(true)) {
        super::library::display_setup_exception()?;
    }
    let finished = finish_initialization();
    let configured = result?;
    finished?;
    if !configured && managed {
        super::requirements::interrupt_initialization();
    }
    if configured && managed && !super::requirements::initialized() {
        let manifest = super::requirements::declaration()?;
        super::requirements::initialize(configuration, manifest)?;
    }
    Ok(configured)
}
