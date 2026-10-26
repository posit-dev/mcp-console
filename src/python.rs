mod environment;
mod inspection;
mod probe;
mod requirements;
mod reticulate;
mod startup;

pub(crate) use inspection::{NativePython, explicit_executable, inspect_native};
pub(crate) use requirements::{ActivationFailure, ensure_libpython_compatible};
#[cfg(all(test, unix))]
pub(crate) use requirements::{ActivationInput, activate_managed_environment};
pub(crate) use startup::{finish_initialization, initialize_selected, setup_runtime};

const RUNTIME_SOURCE: &str = include_str!("python/runtime.py");

/// Import policy installed before shared runtime setup is complete.
pub(crate) enum ImportResolution {
    Implicit,
    Native,
}

#[derive(serde::Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum PreparationOutcome {
    #[serde(deserialize_with = "crate::worker_protocol::deserialize_payload_free")]
    Prepared,
    Failed {
        message: String,
    },
    Rejected {
        message: String,
    },
}

/// Rust-owned Python runtime boundary.
///
/// Every cell enters the same private evaluator through the retained CPython
/// library. The optional reticulate adapter retains R-side discovery and
/// attachment policy; it is absent until R is initialized.
pub(crate) struct Runtime {
    next_evaluation_id: u64,
}

pub(crate) enum SqlProvider {
    R,
    Managed,
    Handled,
}

pub(crate) use platform::configure_worker_environment as configure_native_worker_environment;

pub(crate) use reticulate::with_r_startup_environment;

thread_local! {
    static ADAPTER: std::cell::RefCell<Option<std::rc::Rc<reticulate::Adapter>>> = const { std::cell::RefCell::new(None) };
}
static SELECTION: std::sync::OnceLock<crate::local_runtime::WorkerSelection> =
    std::sync::OnceLock::new();

pub(crate) fn attach_r_adapter() -> Result<(), String> {
    if let Some(manifest) = requirements::retained_manifest() {
        unsafe {
            std::env::set_var(
                "MCP_CONSOLE_MANAGED_PYTHON",
                serde_json::to_string(&manifest).map_err(|error| error.to_string())?,
            )
        };
    }
    let adapter = std::rc::Rc::new(reticulate::Adapter::initialize()?);
    ADAPTER.with(|slot| *slot.borrow_mut() = Some(adapter));
    Ok(())
}

fn adapter() -> Option<std::rc::Rc<reticulate::Adapter>> {
    ADAPTER.with(|slot| slot.borrow().clone())
}

fn pending_executable() -> Option<Result<&'static std::path::Path, String>> {
    Some(match SELECTION.get()?.pending_python.as_ref()? {
        crate::local_runtime::PendingPython::Executable(path) => Ok(path),
        crate::local_runtime::PendingPython::Unavailable(error) => Err(error.clone()),
    })
}

pub(crate) fn attach_bridge() -> Result<bool, String> {
    adapter()
        .ok_or("R bridge is unavailable")?
        .ensure_initialized()
}

pub(crate) fn reinstall_services() -> Result<(), String> {
    #[cfg(windows)]
    crate::windows::restore_worker_stdio().map_err(|error| error.to_string())?;
    if library::initialized_selection()?.is_some() && library::services_installed()? {
        library::install_services()?;
    }
    Ok(())
}

pub(crate) fn ensure_initialized() -> Result<bool, String> {
    if library::runtime_configured()? {
        return Ok(true);
    }
    if let Some(candidate) = requirements::materialized() {
        return startup::initialize_native(&candidate.selected, true);
    }
    let selection = SELECTION
        .get()
        .ok_or("Python capability is not configured")?;
    if let Some(python) = &selection.python {
        return startup::initialize_native(&python.selected, python.managed);
    }
    // Capture the executable before native R startup, but run mutable Python
    // inspection only when Python is needed and under its ordinary policy.
    if let Some(executable) = pending_executable() {
        let selected = match executable.and_then(crate::worker::inspect_python) {
            Ok(selected) => selected,
            Err(error) => {
                crate::worker::emit_output(
                    crate::worker_protocol::ConsoleChannel::Diagnostic,
                    format!("Error: {error}\n").as_bytes(),
                );
                return Ok(false);
            }
        };
        return startup::initialize_native(&selected, false);
    }
    if !crate::worker::r_initialized()
        && let Some(explicit) = std::env::var_os("RETICULATE_PYTHON")
            .filter(|value| !value.is_empty() && value != "managed")
    {
        let selected = match explicit_executable(&explicit)
            .and_then(|path| crate::worker::inspect_python(&path))
        {
            Ok(selected) => selected,
            Err(error) => {
                crate::worker::emit_output(
                    crate::worker_protocol::ConsoleChannel::Diagnostic,
                    format!("Error: {error}\n").as_bytes(),
                );
                return Ok(false);
            }
        };
        return startup::initialize_native(&selected, false);
    }
    // R declarations and selection callbacks genuinely require R. Only an
    // unresolved compatibility selection enters this path.
    crate::worker::ensure_r()?;
    if crate::worker::bootstrapping() {
        // A bare R library can genuinely lack the Python selection adapter.
        // Its advertised Python field is not an installed runtime capability;
        // eager startup must leave ordinary R cells usable in that session.
        let available = harp::parse_eval_base(r#"requireNamespace("reticulate", quietly = TRUE)"#)
            .and_then(bool::try_from)
            .map_err(|error| error.to_string())?;
        if !available {
            return Ok(true);
        }
    }
    let adapter = adapter().ok_or("R selection adapter is unavailable")?;
    let selected = match adapter.select(crate::worker::bootstrapping())? {
        reticulate::Selection::Selected(selected) => selected,
        // Discovery ran its ordinary callbacks and found no interpreter. This
        // completes optional bootstrap; an actual Python cell still reports
        // the selection error through the ordinary, required path.
        reticulate::Selection::Unavailable => return Ok(true),
        reticulate::Selection::Incomplete => return Ok(false),
    };
    startup::initialize_native(&selected, adapter.managed)
}

impl Runtime {
    pub(crate) fn new(selection: crate::local_runtime::WorkerSelection) -> Result<Self, String> {
        requirements::configure()?;
        SELECTION
            .set(selection)
            .map_err(|_| "runtime capabilities already configured")?;
        Ok(Self {
            next_evaluation_id: 1,
        })
    }

    pub(crate) fn evaluate(&mut self, source: &str) -> Result<(), String> {
        let filename = format!("<mcp-console:python:e{}>", self.next_evaluation_id);
        self.next_evaluation_id += 1;
        if !ensure_initialized()? {
            return Ok(());
        }
        evaluate_embedded(source, &filename)
    }

    pub(crate) fn prepare(&self, packages: Vec<String>) -> Result<PreparationOutcome, String> {
        requirements::prepare(packages)
    }
}

pub(crate) fn resolve_managed_import(
    resolution: crate::worker_protocol::PythonImportResolution,
) -> Result<String, String> {
    requirements::resolve_import(resolution)
}

pub(crate) fn evaluate_embedded(source: &str, filename: &str) -> Result<(), String> {
    #[cfg(windows)]
    crate::windows::restore_worker_stdio().map_err(|error| error.to_string())?;
    library::evaluate(source, filename)
}

pub(crate) fn install_sql_runtime(source: &str) -> Result<bool, String> {
    library::install_sql_runtime(source)
}

pub(crate) fn dispatch_sql(source: &str) -> Result<SqlProvider, String> {
    if !crate::worker::r_available() && !library::runtime_configured()? && !ensure_initialized()? {
        return Ok(SqlProvider::Handled);
    }
    library::dispatch_sql(source)
}

pub(crate) fn use_r_sql() -> Result<(), String> {
    library::use_r_sql()
}

pub(crate) fn r_sql_connection_selected() -> Result<bool, String> {
    library::r_sql_connection_selected()
}

pub(crate) fn initialize_sql_source(source: &str) -> Result<bool, String> {
    if !ensure_initialized()? {
        return Err("Python initialization is incomplete".into());
    }
    library::initialize_sql_source(source)
}

pub(crate) fn take_sql_restore_request() -> Result<bool, String> {
    library::take_sql_restore_request()
}

pub(crate) fn has_selected_sql_connection() -> Result<bool, String> {
    library::has_selected_sql_connection()
}

pub(crate) fn prepare_process_exit() -> Result<(), String> {
    library::prepare_process_exit()
}

#[cfg(test)]
mod tests {
    use super::PreparationOutcome;

    #[test]
    fn python_preparation_outcome_rejects_unknown_fields() {
        assert!(serde_json::from_str::<PreparationOutcome>(r#"{"kind":"prepared"}"#).is_ok());
        assert!(
            serde_json::from_str::<PreparationOutcome>(
                r#"{"kind":"prepared","checkpoint":{"packages":[]}}"#
            )
            .is_err()
        );
    }
}

mod library;

mod platform {
    use std::ffi::{CStr, CString};
    use std::fs;
    use std::io;
    #[cfg(unix)]
    use std::os::unix::fs::symlink;
    use std::path::{Path, PathBuf};
    use std::sync::OnceLock;

    static MATPLOTLIB_DIRECTORY: OnceLock<PathBuf> = OnceLock::new();
    static INHERITED_MATPLOTLIB_DIRECTORY: OnceLock<PathBuf> = OnceLock::new();

    pub(crate) fn configure_worker_environment(
        temporary_directory: Option<&Path>,
    ) -> io::Result<()> {
        for (name, value, overwrite) in [
            (c"COLUMNS", c"200", true),
            (c"UV_OFFLINE", c"1", true),
            (c"MPLBACKEND", c"agg", false),
        ] {
            set_environment(name, value, overwrite)?;
        }
        // Unmanaged modes retain the libraries' normal cache selection; no
        // fixed shared subdirectory may masquerade as Console-owned scratch.
        let Some(temporary_directory) = temporary_directory else {
            return Ok(());
        };
        let matplotlib_cache_directory = std::env::var_os("MCP_CONSOLE_MATPLOTLIB_CACHE")
            .map(std::path::absolute)
            .transpose()?
            .or_else(matplotlib_cache_directory);
        let matplotlib_config_directory =
            inherited_matplotlib_directory("XDG_CONFIG_HOME", ".config");
        // Preserve the selected host configuration before redirecting all
        // Matplotlib writes to the worker's private directory.
        if let Some(config) = inherited_matplotlibrc(matplotlib_config_directory.as_deref()) {
            let config = path_cstring(&config)
                .expect("Matplotlib configuration path should not contain NUL");
            set_environment(c"MATPLOTLIBRC", &config, true)?;
        }
        let matplotlib_directory = temporary_directory.join("matplotlib");
        MATPLOTLIB_DIRECTORY
            .set(matplotlib_directory.clone())
            .map_err(|_| io::Error::other("Matplotlib directory is already configured"))?;
        if let Some(cache) = matplotlib_cache_directory {
            let _ = INHERITED_MATPLOTLIB_DIRECTORY.set(cache);
        }
        link_matplotlib_caches();

        for (name, directory) in [
            (c"MPLCONFIGDIR", matplotlib_directory),
            (c"XDG_CACHE_HOME", temporary_directory.join("cache")),
        ] {
            let directory =
                path_cstring(&directory).expect("temporary directory should not contain NUL");
            set_environment(name, &directory, true)?;
        }
        Ok(())
    }

    pub(crate) fn link_matplotlib_caches() {
        let (Some(cache_directory), Some(directory)) = (
            INHERITED_MATPLOTLIB_DIRECTORY.get(),
            MATPLOTLIB_DIRECTORY.get(),
        ) else {
            return;
        };
        let Ok(caches) = fs::read_dir(cache_directory) else {
            return;
        };
        if fs::create_dir_all(directory).is_err() {
            return;
        }
        for cache in caches.flatten() {
            let name = cache.file_name();
            let Some(name) = name.to_str() else {
                continue;
            };
            if !name.starts_with("fontlist-v")
                || !name.ends_with(".json")
                || !cache.file_type().is_ok_and(|file_type| file_type.is_file())
            {
                continue;
            }
            let link = directory.join(name);
            if fs::symlink_metadata(&link).is_err() {
                #[cfg(unix)]
                let _ = symlink(cache.path(), link);
                #[cfg(windows)]
                let _ = fs::copy(cache.path(), link);
            }
        }
    }

    fn inherited_matplotlibrc(config_directory: Option<&Path>) -> Option<PathBuf> {
        if let Some(config) = std::env::var_os("MATPLOTLIBRC").filter(|path| !path.is_empty()) {
            let config = PathBuf::from(config);
            if let Some(config) =
                regular_file(&config).or_else(|| regular_file(&config.join("matplotlibrc")))
            {
                return Some(config);
            }
        }

        regular_file(&config_directory?.join("matplotlibrc"))
    }

    pub(crate) fn matplotlib_cache_directory() -> Option<PathBuf> {
        inherited_matplotlib_directory("XDG_CACHE_HOME", ".cache")
    }

    fn inherited_matplotlib_directory(xdg_variable: &str, xdg_default: &str) -> Option<PathBuf> {
        let directory = match std::env::var_os("MPLCONFIGDIR") {
            Some(directory) if !directory.is_empty() => PathBuf::from(directory),
            Some(_) | None if cfg!(target_os = "linux") => {
                let root = std::env::var_os(xdg_variable)
                    .filter(|path| !path.is_empty())
                    .map(PathBuf::from)
                    .or_else(|| {
                        std::env::var_os("HOME")
                            .filter(|home| !home.is_empty())
                            .map(|home| PathBuf::from(home).join(xdg_default))
                    })?;
                root.join("matplotlib")
            }
            Some(_) | None => {
                PathBuf::from(std::env::var_os("HOME").filter(|home| !home.is_empty())?)
                    .join(".matplotlib")
            }
        };
        if directory.is_absolute() {
            Some(directory)
        } else {
            Some(std::env::current_dir().ok()?.join(directory))
        }
    }

    fn regular_file(path: &Path) -> Option<PathBuf> {
        let path = path.canonicalize().ok()?;
        path.is_file().then_some(path)
    }

    fn path_cstring(path: &Path) -> Result<CString, std::ffi::NulError> {
        #[cfg(unix)]
        {
            use std::os::unix::ffi::OsStrExt;
            CString::new(path.as_os_str().as_bytes())
        }
        #[cfg(windows)]
        {
            CString::new(path.to_string_lossy().as_bytes())
        }
    }

    #[cfg(unix)]
    pub(super) fn set_environment(name: &CStr, value: &CStr, overwrite: bool) -> io::Result<()> {
        if unsafe { libc::setenv(name.as_ptr(), value.as_ptr(), overwrite.into()) } != 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }
    #[cfg(windows)]
    pub(super) fn set_environment(name: &CStr, value: &CStr, overwrite: bool) -> io::Result<()> {
        let name = name.to_str().map_err(io::Error::other)?;
        if overwrite || std::env::var_os(name).is_none() {
            let value = value.to_str().map_err(io::Error::other)?;
            synchronize_crt_environment(name, std::ffi::OsStr::new(value))?;
        }
        Ok(())
    }

    #[cfg(windows)]
    pub(super) fn synchronize_crt_environment(
        name: &str,
        value: &std::ffi::OsStr,
    ) -> io::Result<()> {
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::System::LibraryLoader::{GetModuleHandleW, GetProcAddress};

        let name_wide = std::ffi::OsStr::new(name)
            .encode_wide()
            .chain([0])
            .collect::<Vec<_>>();
        let value_wide = value.encode_wide().chain([0]).collect::<Vec<_>>();
        // Both setters update the Win32 environment too. Console links its
        // CRT statically; R and CPython can retain another loaded CRT's copy.
        // Update that copy even after native R startup, when restoring hints
        // such as RETICULATE_PYTHON before attaching the managed bridge.
        let status = unsafe { libc::wputenv_s(name_wide.as_ptr(), value_wide.as_ptr()) };
        if status != 0 {
            return Err(io::Error::from_raw_os_error(status));
        }
        let module = unsafe { GetModuleHandleW(windows_sys::w!("ucrtbase.dll")) };
        if module.is_null() {
            return Ok(()); // No loaded shared CRT has an environment copy to update.
        }
        let address = unsafe { GetProcAddress(module, c"_wputenv_s".as_ptr().cast()) }
            .ok_or_else(|| io::Error::other("loaded Windows CRT does not export _wputenv_s"))?;
        let putenv: unsafe extern "C" fn(*const u16, *const u16) -> libc::c_int =
            unsafe { std::mem::transmute(address) };
        let status = unsafe { putenv(name_wide.as_ptr(), value_wide.as_ptr()) };
        if status != 0 {
            return Err(io::Error::from_raw_os_error(status));
        }
        Ok(())
    }
}

pub(crate) use platform::{link_matplotlib_caches, matplotlib_cache_directory};
