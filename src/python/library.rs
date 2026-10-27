use std::ffi::{CStr, CString};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

mod services;

// A loaded handle is retained for the process lifetime. Runtime calls copy its
// immutable function table under this lock, then release the guard before
// invoking Python so Python-to-R callbacks can re-enter library access.
static PYTHON_LIBRARY: Mutex<Option<LoadedLibrary>> = Mutex::new(None);

thread_local! {
    // R can select a native connection before CPython exists. Retain that
    // choice on the interpreter thread until the SQL adapter can own it.
    static PENDING_R_SQL: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}

type PyIsInitialized = unsafe extern "C" fn() -> libc::c_int;
type PySetProgramName = unsafe extern "C" fn(*const libc::wchar_t);
type PyInitializeEx = unsafe extern "C" fn(libc::c_int);
type PySysSetArgvEx = unsafe extern "C" fn(libc::c_int, *mut *mut libc::wchar_t, libc::c_int);
#[cfg(unix)]
type PyOsSetSignal = unsafe extern "C" fn(libc::c_int, libc::sighandler_t) -> libc::sighandler_t;
type PyEvalSaveThread = unsafe extern "C" fn() -> *mut libc::c_void;
type PyEvalRestoreThread = unsafe extern "C" fn(*mut libc::c_void);
type PyObject = libc::c_void;
type PyGilState = libc::c_int;
type PyGilStateEnsure = unsafe extern "C" fn() -> PyGilState;
type PyGilStateRelease = unsafe extern "C" fn(PyGilState);
type PyImportAddModule = unsafe extern "C" fn(*const libc::c_char) -> *mut PyObject;
type PyModuleGetDict = unsafe extern "C" fn(*mut PyObject) -> *mut PyObject;
type PyDictNew = unsafe extern "C" fn() -> *mut PyObject;
type PyDictGetItemString =
    unsafe extern "C" fn(*mut PyObject, *const libc::c_char) -> *mut PyObject;
type PyDictSetItemString =
    unsafe extern "C" fn(*mut PyObject, *const libc::c_char, *mut PyObject) -> libc::c_int;
type PyRunStringFlags = unsafe extern "C" fn(
    *const libc::c_char,
    libc::c_int,
    *mut PyObject,
    *mut PyObject,
    *mut libc::c_void,
) -> *mut PyObject;
type PyObjectCallNoArgs = unsafe extern "C" fn(*mut PyObject) -> *mut PyObject;
type PyObjectCallFunctionObjArgs = unsafe extern "C" fn(*mut PyObject, ...) -> *mut PyObject;
type PyUnicodeFromStringAndSize = unsafe extern "C" fn(*const libc::c_char, isize) -> *mut PyObject;
type PyLongAsLong = unsafe extern "C" fn(*mut PyObject) -> libc::c_long;
type PyDecRef = unsafe extern "C" fn(*mut PyObject);
type PyErrFetch = unsafe extern "C" fn(*mut *mut PyObject, *mut *mut PyObject, *mut *mut PyObject);
type PyErrNormalizeException =
    unsafe extern "C" fn(*mut *mut PyObject, *mut *mut PyObject, *mut *mut PyObject);
type PyErrDisplay = unsafe extern "C" fn(*mut PyObject, *mut PyObject, *mut PyObject);
type PyErrClear = unsafe extern "C" fn();
type PyErrPrint = unsafe extern "C" fn();
type PyErrExceptionMatches = unsafe extern "C" fn(*mut PyObject) -> libc::c_int;
type PyExceptionSetTraceback = unsafe extern "C" fn(*mut PyObject, *mut PyObject) -> libc::c_int;

const PY_FILE_INPUT: libc::c_int = 257;
// The seventh field of sys.flags on the supported CPython versions.
const NO_SITE_FLAG_INDEX: isize = 6;
const SQL_PROVIDER_R: libc::c_long = 0;
const SQL_PROVIDER_MANAGED: libc::c_long = 1;
const SQL_PROVIDER_HANDLED: libc::c_long = 2;

struct LoadedLibrary {
    path: PathBuf,
    _library: libloading::Library,
    api: PythonApi,
    interpreter: Interpreter,
    configuration: Option<Configuration>,
    setup: SetupCompletion,
}

#[derive(Default)]
struct SetupCompletion {
    services: bool,
    evaluator: bool,
    site: bool,
    sql: bool,
    configured: bool,
    environment: bool,
}

impl SetupCompletion {
    fn mark_configured(&mut self) -> Result<(), String> {
        if !self.services || !self.evaluator || !self.site || !self.sql || !self.environment {
            return Err("Python runtime configuration preceded installation".to_string());
        }
        self.configured = true;
        Ok(())
    }
}

#[derive(Clone, Copy)]
struct PythonApi {
    is_initialized: PyIsInitialized,
    set_program_name: PySetProgramName,
    initialize_ex: PyInitializeEx,
    no_site_flag: usize,
    sys_get_object: unsafe extern "C" fn(*const libc::c_char) -> *mut PyObject,
    struct_sequence_get_item: unsafe extern "C" fn(*mut PyObject, isize) -> *mut PyObject,
    struct_sequence_set_item: unsafe extern "C" fn(*mut PyObject, isize, *mut PyObject),
    long_from_long: unsafe extern "C" fn(libc::c_long) -> *mut PyObject,
    set_argv_ex: PySysSetArgvEx,
    #[cfg(unix)]
    set_signal: PyOsSetSignal,
    save_thread: PyEvalSaveThread,
    restore_thread: PyEvalRestoreThread,
    gil_state_ensure: PyGilStateEnsure,
    gil_state_release: PyGilStateRelease,
    import_add_module: PyImportAddModule,
    module_get_dict: PyModuleGetDict,
    dict_new: PyDictNew,
    dict_get_item_string: PyDictGetItemString,
    dict_set_item_string: PyDictSetItemString,
    run_string_flags: PyRunStringFlags,
    call_no_args: PyObjectCallNoArgs,
    call_function_obj_args: PyObjectCallFunctionObjArgs,
    unicode_from_string_and_size: PyUnicodeFromStringAndSize,
    long_as_long: PyLongAsLong,
    dec_ref: PyDecRef,
    err_fetch: PyErrFetch,
    err_normalize_exception: PyErrNormalizeException,
    err_display: PyErrDisplay,
    err_clear: PyErrClear,
    err_print: PyErrPrint,
    err_exception_matches: PyErrExceptionMatches,
    system_exit: usize,
    keyboard_interrupt: usize,
    exception_set_traceback: PyExceptionSetTraceback,
}

#[derive(Clone, Copy, Eq, PartialEq)]
enum Interpreter {
    Uninitialized,
    Initializing,
    External,
    // PyEval_SaveThread's main-thread state, retained until process exit.
    RustOwned { saved_thread: Option<usize> },
}

struct Configuration {
    selected: super::NativePython,
    // Preserve the selection hint before startup hooks can change cwd or PATH.
    reticulate_python: Option<std::ffi::OsString>,
    program_name_wide: Vec<libc::wchar_t>,
}

pub(super) fn prepare_process_exit() -> Result<(), String> {
    let restore = {
        let mut slot = PYTHON_LIBRARY
            .lock()
            .map_err(|_| "Python shared library state is unavailable")?;
        let Some(library) = slot.as_mut() else {
            return Ok(());
        };
        let Interpreter::RustOwned {
            saved_thread: Some(state),
        } = library.interpreter
        else {
            return Ok(());
        };
        // The worker returns on the same thread that initialized CPython. Do
        // not run extension-library exit destructors with a detached state.
        // User-initiated finalization can already have invalidated that state.
        if unsafe { (library.api.is_initialized)() } == 0 {
            return Ok(());
        }
        library.interpreter = Interpreter::RustOwned { saved_thread: None };
        (library.api.restore_thread, state)
    };
    // SAFETY: this pairs the one main-thread SaveThread call below. Python
    // stays attached until process exit; this is not interpreter finalization.
    unsafe { (restore.0)(restore.1 as *mut libc::c_void) };
    Ok(())
}

pub(super) fn load(path: &Path) -> Result<bool, String> {
    with_library(path, LoadedLibrary::attach)
}

pub(super) fn initialized_selection() -> Result<Option<super::NativePython>, String> {
    let slot = PYTHON_LIBRARY
        .lock()
        .map_err(|_| "Python shared library state is unavailable")?;
    Ok(slot.as_ref().and_then(|library| {
        matches!(
            library.interpreter,
            Interpreter::RustOwned { .. } | Interpreter::External
        )
        .then(|| {
            library
                .configuration
                .as_ref()
                .map(|config| config.selected.clone())
        })
        .flatten()
    }))
}

pub(super) fn conversion_metadata() -> Result<super::inspection::ConversionMetadata, String> {
    api()?.with_gil(|api| unsafe {
        let function = api.function(c"_mcp_console", c"conversion_metadata")?;
        let result = (api.call_no_args)(function);
        if result.is_null() {
            api.display_pending_exception();
            return Err(python_function_error(
                c"_mcp_console",
                c"conversion_metadata",
            ));
        }
        let response = services::response_text(result);
        (api.dec_ref)(result);
        serde_json::from_str(&response?)
            .map_err(|error| format!("invalid running Python conversion metadata: {error}"))
    })
}

pub(super) fn environment_selection_unchanged() -> Result<bool, String> {
    let slot = PYTHON_LIBRARY
        .lock()
        .map_err(|_| "Python shared library state is unavailable")?;
    let configuration = slot
        .as_ref()
        .and_then(|library| library.configuration.as_ref())
        .ok_or("Python interpreter selection is unavailable")?;
    Ok(std::env::var_os("RETICULATE_PYTHON") == configuration.reticulate_python)
}

pub(super) fn initialize(selected: &super::NativePython) -> Result<bool, String> {
    let path = Path::new(&selected.embedding.libpython);
    let path = path.canonicalize().map_err(|error| {
        format!(
            "failed to resolve Python shared library `{}`: {error}",
            path.display()
        )
    })?;
    ensure_loaded(&path)?;
    let (api, program_name_wide) = {
        let mut slot = PYTHON_LIBRARY
            .lock()
            .map_err(|_| "Python shared library state is unavailable".to_string())?;
        let library = slot.as_mut().unwrap();
        library.ensure_path(&path)?;
        if library.interpreter == Interpreter::Initializing {
            return Err("Python interpreter initialization is already in progress".to_string());
        }
        // SAFETY: The resolved function has no preconditions.
        if unsafe { (library.api.is_initialized)() } != 0 {
            if library.interpreter == Interpreter::Uninitialized {
                library.interpreter = Interpreter::External;
            }
            library.ensure_configuration(selected)?;
            return Ok(matches!(library.interpreter, Interpreter::RustOwned { .. }));
        }
        match library.interpreter {
            Interpreter::Uninitialized => {}
            Interpreter::External => {
                return Err("externally owned Python interpreter was finalized".to_string());
            }
            Interpreter::RustOwned { .. } => {
                return Err("Rust-owned Python interpreter was finalized".to_string());
            }
            Interpreter::Initializing => unreachable!(),
        }
        let configuration = Configuration::new(selected)?;
        let program_name_wide = configuration.program_name_wide.as_ptr();
        super::startup::configure_process_environment(selected)?;
        library.configuration = Some(configuration);
        library.interpreter = Interpreter::Initializing;
        (library.api, program_name_wide)
    };

    // The selected configuration remains owned by the process-lifetime library
    // state. Release its lock before CPython runs site hooks or callbacks.
    unsafe {
        (api.set_program_name)(program_name_wide);
        // Defer executable .pth files and sitecustomize until Console's input
        // and interrupt services are connected by shared runtime setup.
        *(api.no_site_flag as *mut libc::c_int) = 1;
        (api.initialize_ex)(0);
    }
    // SAFETY: The resolved function has no preconditions.
    if unsafe { (api.is_initialized)() } == 0 {
        return Err("CPython initialization did not complete".to_string());
    }
    // CPython's Windows path bootstrap requires native separators; retain the
    // caller's executable spelling in the public argument identity.
    let mut argument_name = wide_string(&selected.embedding.python, "program name")?;
    let mut argv = [argument_name.as_mut_ptr()];
    unsafe {
        // Workspace lookup is installed by common setup, never the bin directory.
        (api.set_argv_ex)(1, argv.as_mut_ptr(), 0);
        let sys = (api.import_add_module)(c"sys".as_ptr());
        if sys.is_null() {
            return Err("cannot access Python interpreter arguments".into());
        }
        let original = (api.sys_get_object)(c"argv".as_ptr());
        if (api.dict_set_item_string)((api.module_get_dict)(sys), c"orig_argv".as_ptr(), original)
            != 0
        {
            return Err("cannot retain Python interpreter arguments".into());
        }
        // Interactive argv names no script; orig_argv retains the interpreter
        // executable. Initialize both before site hooks, and only once.
        (api.set_argv_ex)(0, std::ptr::null_mut(), 0);
    }
    let mut slot = PYTHON_LIBRARY
        .lock()
        .map_err(|_| "Python shared library state is unavailable".to_string())?;
    slot.as_mut().unwrap().interpreter = Interpreter::RustOwned { saved_thread: None };
    Ok(true)
}

pub(super) fn initialize_site() -> Result<bool, String> {
    let api = {
        let mut slot = PYTHON_LIBRARY
            .lock()
            .map_err(|_| "Python shared library state is unavailable")?;
        let library = slot.as_mut().ok_or("Python shared library is not loaded")?;
        // An externally initialized interpreter has already run its site hooks.
        if library.interpreter == Interpreter::External {
            library.setup.site = true;
        }
        if library.setup.site {
            return Ok(true);
        }
        library.api
    };
    // Hooks can call Console services. Release the library lock before Python.
    let initialized = api.with_gil(|api| unsafe {
        // Restore normal flags before multiprocessing forwards no_site as -S
        // to children that need the selected environment's installed packages.
        let flags = (api.sys_get_object)(c"flags".as_ptr());
        let enabled = (api.long_from_long)(0);
        if enabled.is_null() {
            return Err("cannot restore Python site flags".into());
        }
        let previous = (api.struct_sequence_get_item)(flags, NO_SITE_FLAG_INDEX);
        (api.struct_sequence_set_item)(flags, NO_SITE_FLAG_INDEX, enabled);
        // SetItem steals the new reference without releasing the previous one.
        (api.dec_ref)(previous);
        *(api.no_site_flag as *mut libc::c_int) = 0;
        let function = api.function(c"_mcp_console_services", c"initialize_site")?;
        api.finish_setup((api.call_no_args)(function))
    })?;
    if initialized {
        PYTHON_LIBRARY.lock().unwrap().as_mut().unwrap().setup.site = true;
    }
    Ok(initialized)
}

pub(super) fn configure_environment() -> Result<bool, String> {
    let selected = {
        let slot = PYTHON_LIBRARY
            .lock()
            .map_err(|_| "Python shared library state is unavailable")?;
        let library = slot.as_ref().ok_or("Python shared library is not loaded")?;
        if library.setup.environment {
            return Ok(true);
        }
        library
            .configuration
            .as_ref()
            .ok_or("Python has no inspected selection")?
            .selected
            .clone()
    };
    if !configure_inspected_environment(&selected)? {
        return Ok(false);
    }
    PYTHON_LIBRARY
        .lock()
        .unwrap()
        .as_mut()
        .unwrap()
        .setup
        .environment = true;
    Ok(true)
}

pub(super) fn accept_configuration(selected: &super::NativePython) -> Result<(), String> {
    let mut slot = PYTHON_LIBRARY
        .lock()
        .map_err(|_| "Python shared library state is unavailable")?;
    let configuration = slot
        .as_mut()
        .and_then(|library| library.configuration.as_mut())
        .ok_or("Python has no inspected selection")?;
    // Keep the original program-name allocation alive for CPython. Only its
    // current environment identity changes after successful live activation.
    configuration.selected = selected.clone();
    Ok(())
}

pub(super) fn selected_configuration() -> Result<super::NativePython, String> {
    let slot = PYTHON_LIBRARY
        .lock()
        .map_err(|_| "Python shared library state is unavailable")?;
    slot.as_ref()
        .and_then(|library| library.configuration.as_ref())
        .map(|configuration| configuration.selected.clone())
        .ok_or_else(|| "Python has no inspected selection".into())
}

pub(super) fn add_bridge_path(path: &str) -> Result<(), String> {
    let path = serde_json::to_string(path).map_err(|error| error.to_string())?;
    let source = CString::new(format!(
        "import sys\nif {path} not in sys.path: sys.path.append({path})"
    ))
    .map_err(|error| error.to_string())?;
    api()?.with_gil(|api| unsafe { api.run_module(c"_mcp_console_bridge", &source) })
}

pub(super) fn install_runtime(source: &str) -> Result<(), String> {
    let api = {
        let slot = PYTHON_LIBRARY.lock().unwrap();
        let library = slot.as_ref().ok_or("Python shared library is not loaded")?;
        if library.setup.evaluator {
            return Ok(());
        }
        library.api
    };
    let source = CString::new(source)
        .map_err(|_| "embedded Python runtime source contains NUL".to_string())?;
    api.with_gil(|api| unsafe { api.run_runtime(&source) })?;
    PYTHON_LIBRARY
        .lock()
        .unwrap()
        .as_mut()
        .unwrap()
        .setup
        .evaluator = true;
    Ok(())
}

pub(super) fn install_sql_runtime(source: &str) -> Result<bool, String> {
    let api = {
        let slot = PYTHON_LIBRARY.lock().unwrap();
        let library = slot.as_ref().ok_or("Python shared library is not loaded")?;
        if library.setup.sql {
            return Ok(true);
        }
        library.api
    };
    let source = CString::new(source)
        .map_err(|_| "embedded Python SQL runtime source contains NUL".to_string())?;
    let installed = api.with_gil(|api| unsafe {
        let result = api.run_module_result(c"_mcp_console_sql", &source)?;
        api.finish_setup(result)
    })?;
    if !installed {
        return Ok(false);
    }
    if PENDING_R_SQL.with(|selected| selected.get()) {
        // Replay is part of setup: retain its exception and the pending
        // selection on interruption, so retry preserves live worker state.
        let selected = api.with_gil(|api| unsafe {
            let function = api.function(c"_mcp_console_sql", c"use_r")?;
            api.finish_setup((api.call_no_args)(function))
        })?;
        if !selected {
            return Ok(false);
        }
        PENDING_R_SQL.with(|selected| selected.set(false));
    }
    PYTHON_LIBRARY.lock().unwrap().as_mut().unwrap().setup.sql = true;
    Ok(true)
}

fn api() -> Result<PythonApi, String> {
    PYTHON_LIBRARY
        .lock()
        .map_err(|_| "Python shared library state is unavailable")?
        .as_ref()
        .map(|library| library.api)
        .ok_or_else(|| "Python shared library is not loaded".to_string())
}

pub(super) fn services_installed() -> Result<bool, String> {
    let slot = PYTHON_LIBRARY
        .lock()
        .map_err(|_| "Python shared library state is unavailable")?;
    Ok(slot
        .as_ref()
        .ok_or("Python shared library is not loaded")?
        .setup
        .services)
}

pub(super) fn install_services() -> Result<(), String> {
    let (api, installed) = {
        let slot = PYTHON_LIBRARY
            .lock()
            .map_err(|_| "Python shared library state is unavailable")?;
        let library = slot.as_ref().ok_or("Python shared library is not loaded")?;
        (library.api, library.setup.services)
    };
    api.with_gil(|api| services::install(api, installed))?;
    if !installed {
        PYTHON_LIBRARY
            .lock()
            .map_err(|_| "Python shared library state is unavailable")?
            .as_mut()
            .unwrap()
            .setup
            .services = true;
    }
    Ok(())
}

pub(super) fn configure_import_resolution(
    resolution: super::ImportResolution,
) -> Result<bool, String> {
    api()?.with_gil(|api| unsafe {
        let function = api.function(c"_mcp_console", c"configure_import_resolution")?;
        let builtins = (api.import_add_module)(c"builtins".as_ptr());
        if builtins.is_null() {
            return api.finish_setup(std::ptr::null_mut());
        }
        let namespace = (api.module_get_dict)(builtins);
        if namespace.is_null() {
            return api.finish_setup(std::ptr::null_mut());
        }
        let none = (api.dict_get_item_string)(namespace, c"None".as_ptr());
        if none.is_null() {
            return api.finish_setup(std::ptr::null_mut());
        }
        let callback = match resolution {
            super::ImportResolution::Implicit => {
                api.function(c"_mcp_console_services", c"resolve_import")?
            }
            super::ImportResolution::Native => none,
        };
        let result =
            (api.call_function_obj_args)(function, callback, std::ptr::null_mut::<PyObject>());
        api.finish_setup(result)
    })
}

pub(super) fn runtime_configured() -> Result<bool, String> {
    let slot = PYTHON_LIBRARY
        .lock()
        .map_err(|_| "Python shared library state is unavailable")?;
    Ok(slot
        .as_ref()
        .is_some_and(|library| library.setup.configured))
}

pub(super) fn mark_runtime_configured() -> Result<(), String> {
    let mut slot = PYTHON_LIBRARY
        .lock()
        .map_err(|_| "Python shared library state is unavailable")?;
    let library = slot.as_mut().ok_or("Python shared library is not loaded")?;
    library.setup.mark_configured()
}

fn configure_inspected_environment(configuration: &super::NativePython) -> Result<bool, String> {
    let executable = serde_json::to_string(configuration)
        .map_err(|error| format!("cannot encode native Python environment: {error}"))?;
    api()?.with_gil(|api| unsafe {
        let function = api.function(c"_mcp_console", c"configure_environment")?;
        let executable = (api.unicode_from_string_and_size)(
            executable.as_ptr().cast(),
            executable.len() as isize,
        );
        if executable.is_null() {
            return Err("cannot encode selected Python executable".into());
        }
        let result =
            (api.call_function_obj_args)(function, executable, std::ptr::null_mut::<PyObject>());
        (api.dec_ref)(executable);
        api.finish_setup(result)
    })
}

pub(super) fn configure_native_child_environment(
    configuration: &super::NativePython,
) -> Result<bool, String> {
    let encoded = serde_json::to_string(configuration)
        .map_err(|error| format!("cannot encode native Python environment: {error}"))?;
    api()?.with_gil(|api| unsafe {
        let function = api.function(c"_mcp_console", c"configure_native_child_environment")?;
        let argument =
            (api.unicode_from_string_and_size)(encoded.as_ptr().cast(), encoded.len() as isize);
        if argument.is_null() {
            return api.finish_setup(std::ptr::null_mut());
        }
        let result =
            (api.call_function_obj_args)(function, argument, std::ptr::null_mut::<PyObject>());
        (api.dec_ref)(argument);
        api.finish_setup(result)
    })
}

pub(super) fn display_setup_exception() -> Result<(), String> {
    let api = {
        let slot = PYTHON_LIBRARY
            .lock()
            .map_err(|_| "Python shared library state is unavailable")?;
        let library = slot.as_ref().ok_or("Python shared library is not loaded")?;
        // Retained setup exceptions arise after the private runtime is installed.
        if !library.setup.evaluator {
            return Ok(());
        }
        library.api
    };
    api.with_gil(|api| api.call_unit(c"_mcp_console", c"display_activation_exception"))
}

pub(super) fn display_activation_exception() -> Result<(), String> {
    api()?.with_gil(|api| api.call_unit(c"_mcp_console", c"display_activation_exception"))
}

pub(super) fn activate_environment(script: &str, executable: &str) -> Result<bool, String> {
    api()?.with_gil(|api| unsafe {
        let function = api.function(c"_mcp_console", c"activate_environment")?;
        let script =
            (api.unicode_from_string_and_size)(script.as_ptr().cast(), script.len() as isize);
        let executable = (api.unicode_from_string_and_size)(
            executable.as_ptr().cast(),
            executable.len() as isize,
        );
        if script.is_null() || executable.is_null() {
            for object in [script, executable] {
                if !object.is_null() {
                    (api.dec_ref)(object);
                }
            }
            return api.finish_setup(std::ptr::null_mut());
        }
        let result = (api.call_function_obj_args)(
            function,
            script,
            executable,
            std::ptr::null_mut::<PyObject>(),
        );
        (api.dec_ref)(script);
        (api.dec_ref)(executable);
        api.finish_setup(result)
    })
}

pub(super) fn install_environment() -> Result<(), String> {
    let source = CString::new(include_str!("environment.py")).unwrap();
    api()?.with_gil(|api| unsafe { api.run_module(c"_mcp_console_environment", &source) })
}

pub(super) fn environment_call(name: &CStr, request: &str) -> Result<Option<String>, String> {
    api()?.with_gil(|api| unsafe {
        let function = api.function(c"_mcp_console_environment", name)?;
        let argument =
            (api.unicode_from_string_and_size)(request.as_ptr().cast(), request.len() as isize);
        if argument.is_null() {
            api.display_pending_exception();
            return Err("failed to create Python environment request".to_string());
        }
        let result =
            (api.call_function_obj_args)(function, argument, std::ptr::null_mut::<PyObject>());
        (api.dec_ref)(argument);
        if result.is_null() {
            if crate::worker::is_shutting_down() {
                // Retirement cancels the host inspection callback. The old
                // interpreter cannot complete setup and has no user error to
                // display; let its coordinator finish the shutdown boundary.
                (api.err_clear)();
                return Ok(None);
            }
            if (api.err_exception_matches)(api.keyboard_interrupt as *mut PyObject) != 0 {
                if name == c"initialize" {
                    // Startup's R adapter rethrows the retained exception;
                    // preparation carries a separate interrupted outcome.
                    api.finish_setup(result)?;
                } else {
                    (api.err_clear)();
                }
                return Ok(None);
            }
            api.display_pending_exception();
            return Err(python_function_error(c"_mcp_console_environment", name));
        }
        let response = services::response_text(result).map(Some);
        if response.is_err() {
            api.display_pending_exception();
        }
        (api.dec_ref)(result);
        response
    })
}

pub(super) fn configure_module_defaults() -> Result<bool, String> {
    api()?.with_gil(|api| unsafe {
        let function = api.function(c"_mcp_console", c"configure_module_defaults")?;
        api.finish_setup((api.call_no_args)(function))
    })
}

pub(super) fn evaluate(source: &str, filename: &str) -> Result<(), String> {
    api()?.with_gil(|api| unsafe {
        let function = api.function(c"_mcp_console", c"eval_cell")?;
        let source =
            (api.unicode_from_string_and_size)(source.as_ptr().cast(), source.len() as isize);
        let filename =
            (api.unicode_from_string_and_size)(filename.as_ptr().cast(), filename.len() as isize);
        if source.is_null() || filename.is_null() {
            for object in [source, filename] {
                if !object.is_null() {
                    (api.dec_ref)(object);
                }
            }
            api.display_pending_exception();
            return Err("failed to create Python cell arguments".to_string());
        }
        let result = (api.call_function_obj_args)(
            function,
            source,
            filename,
            std::ptr::null_mut::<PyObject>(),
        );
        (api.dec_ref)(source);
        (api.dec_ref)(filename);
        if result.is_null() {
            // Only an uncaught cell exit reaches CPython's REPL exit handler.
            // Adapter and service calls retain their handled-exception boundary.
            if (api.err_exception_matches)(api.system_exit as *mut PyObject) != 0 {
                (api.err_print)();
            } else {
                crate::worker::record_execution_error(crate::worker_protocol::ExecutionError::new(
                    "PythonError",
                    "Python evaluation failed",
                ));
                api.display_pending_exception();
            }
        } else {
            (api.dec_ref)(result);
        }
        Ok(())
    })
}

pub(super) fn dispatch_sql(source: &str) -> Result<super::SqlProvider, String> {
    let Some(api) = installed_sql_api()? else {
        return Ok(super::SqlProvider::R);
    };
    api.with_gil(|api| api.call_sql_dispatch(source))
}

pub(super) fn initialize_sql_source(source: &str) -> Result<bool, String> {
    api()?.with_gil(|api| unsafe {
        let function = api.function(c"_mcp_console_sql", c"initialize_connection")?;
        let argument =
            (api.unicode_from_string_and_size)(source.as_ptr().cast(), source.len() as isize);
        if argument.is_null() {
            return Err("cannot encode startup source".into());
        }
        let result =
            (api.call_function_obj_args)(function, argument, std::ptr::null_mut::<PyObject>());
        (api.dec_ref)(argument);
        if result.is_null() {
            api.display_pending_exception();
            return Err("Python startup did not complete".into());
        }
        let complete = (api.long_as_long)(result);
        (api.dec_ref)(result);
        if complete == -1 {
            crate::worker::record_bootstrap_interrupt();
        }
        Ok(complete == 1)
    })
}

pub(super) fn use_r_sql() -> Result<(), String> {
    let Some(api) = installed_sql_api()? else {
        PENDING_R_SQL.with(|selected| selected.set(true));
        return Ok(());
    };
    api.with_gil(|api| api.call_unit(c"_mcp_console_sql", c"use_r"))
}

pub(super) fn r_sql_connection_selected() -> Result<bool, String> {
    let Some(api) = installed_sql_api()? else {
        // SQL dispatch also defaults to R until the Python adapter is installed.
        // Inspecting selection must not initialize Python or consume a reset.
        return Ok(true);
    };
    api.with_gil(|api| unsafe {
        let function = api.function(c"_mcp_console_sql", c"r_connection_selected")?;
        let result = (api.call_no_args)(function);
        if result.is_null() {
            api.display_pending_exception();
            return Err(python_function_error(
                c"_mcp_console_sql",
                c"r_connection_selected",
            ));
        }
        let selected = (api.long_as_long)(result);
        (api.dec_ref)(result);
        match selected {
            0 => Ok(false),
            1 => Ok(true),
            _ => Err("Python SQL selection returned an invalid value".into()),
        }
    })
}

pub(super) fn initialize_managed_sql() -> Result<(), String> {
    api()?.with_gil(|api| {
        // The GIL covers the call, exception inspection, and reference release.
        unsafe {
            let function = api.function(c"_mcp_console_sql", c"initialize_managed_connection")?;
            let result = (api.call_no_args)(function);
            if result.is_null() {
                let interrupted =
                    (api.err_exception_matches)(api.keyboard_interrupt as *mut PyObject) != 0;
                api.display_pending_exception();
                if interrupted {
                    crate::worker::record_bootstrap_interrupt();
                    return Ok(());
                }
                return Err(python_function_error(
                    c"_mcp_console_sql",
                    c"initialize_managed_connection",
                ));
            }
            (api.dec_ref)(result);
            Ok(())
        }
    })
}

pub(super) fn configure_native_sql() -> Result<(), String> {
    api()?.with_gil(|api| api.call_unit(c"_mcp_console_sql", c"enable_native"))
}

pub(super) fn take_sql_restore_request() -> Result<bool, String> {
    let Some(api) = installed_sql_api()? else {
        return Ok(false);
    };
    api.with_gil(|api| api.call_sql_bool(c"take_managed_restore_request"))
}

pub(super) fn has_selected_sql_connection() -> Result<bool, String> {
    let Some(api) = installed_sql_api()? else {
        return Ok(false);
    };
    api.with_gil(|api| api.call_sql_bool(c"has_selected_connection"))
}

fn installed_sql_api() -> Result<Option<PythonApi>, String> {
    let library_slot = PYTHON_LIBRARY
        .lock()
        .map_err(|_| "Python shared library state is unavailable".to_string())?;
    let Some(library) = library_slot.as_ref() else {
        return Ok(None);
    };
    Ok(library.setup.sql.then_some(library.api))
}

pub(super) fn finish_initialization() -> Result<(), String> {
    let save_thread = {
        let slot = PYTHON_LIBRARY
            .lock()
            .map_err(|_| "Python shared library state is unavailable".to_string())?;
        let library = slot
            .as_ref()
            .ok_or_else(|| "Python shared library is not loaded".to_string())?;
        match library.interpreter {
            Interpreter::Uninitialized => {
                return Err("Python interpreter is not initialized".to_string());
            }
            Interpreter::Initializing => {
                return Err("Python interpreter initialization is incomplete".to_string());
            }
            Interpreter::External
            | Interpreter::RustOwned {
                saved_thread: Some(_),
            } => return Ok(()),
            Interpreter::RustOwned { saved_thread: None } => {}
        }
        if unsafe { (library.api.is_initialized)() } == 0 {
            return Err("Rust-owned Python interpreter was finalized".to_string());
        }
        library.api.save_thread
    };
    // Reticulate has returned from its C adapter. Release the library lock
    // before detaching the initial main-thread state.
    let thread_state = unsafe { save_thread() };
    if thread_state.is_null() {
        return Err("CPython did not return its initial thread state".to_string());
    }
    let mut slot = PYTHON_LIBRARY
        .lock()
        .map_err(|_| "Python shared library state is unavailable".to_string())?;
    slot.as_mut().unwrap().interpreter = Interpreter::RustOwned {
        saved_thread: Some(thread_state as usize),
    };
    Ok(())
}

fn with_library<T>(
    path: &Path,
    operation: impl FnOnce(&mut LoadedLibrary) -> Result<T, String>,
) -> Result<T, String> {
    let path = path.canonicalize().map_err(|error| {
        format!(
            "failed to resolve Python shared library `{}`: {error}",
            path.display()
        )
    })?;
    ensure_loaded(&path)?;
    let mut library_slot = PYTHON_LIBRARY
        .lock()
        .map_err(|_| "Python shared library state is unavailable".to_string())?;
    let library = library_slot
        .as_mut()
        .expect("Python shared library should have been loaded");
    library.ensure_path(&path)?;
    operation(library)
}

fn ensure_loaded(path: &Path) -> Result<(), String> {
    let missing = PYTHON_LIBRARY
        .lock()
        .map_err(|_| "Python shared library state is unavailable".to_string())?
        .is_none();
    if missing {
        // Loading a shared object may run its constructors. Keep those outside
        // the library-state lock just as we do for interpreter execution.
        let loaded = LoadedLibrary::open(path.to_path_buf())?;
        let mut slot = PYTHON_LIBRARY
            .lock()
            .map_err(|_| "Python shared library state is unavailable".to_string())?;
        if slot.is_none() {
            *slot = Some(loaded);
        }
    }
    Ok(())
}

impl LoadedLibrary {
    fn open(path: PathBuf) -> Result<Self, String> {
        // SAFETY: The selected path comes from reticulate's interpreter
        // discovery. Global, eager loading exposes the CPython API before
        // either runtime initializes the interpreter.
        #[cfg(unix)]
        let opened = unsafe {
            libloading::os::unix::Library::open(
                Some(path.as_os_str()),
                libc::RTLD_NOW | libc::RTLD_GLOBAL,
            )
        }
        .map(libloading::Library::from);
        #[cfg(windows)]
        let opened = unsafe {
            // Python supplies adjacent dependencies such as vcruntime140.dll.
            // Console's static CRT no longer preloads a system copy. Search
            // beside the selected, absolute DLL before the normal fallbacks.
            libloading::os::windows::Library::load_with_flags(
                &path,
                libloading::os::windows::LOAD_WITH_ALTERED_SEARCH_PATH,
            )
        }
        .map(libloading::Library::from);
        let library = opened.map_err(|error| {
            format!(
                "failed to load Python shared library `{}`: {error}",
                path.display()
            )
        })?;
        // SAFETY: Each requested symbol is a process-lifetime CPython API
        // function. The owning library handle is retained beside the copied
        // function pointers.
        let api = unsafe { PythonApi::load(&library, &path)? };
        // SAFETY: The resolved function has no preconditions.
        let interpreter = if unsafe { (api.is_initialized)() } == 0 {
            Interpreter::Uninitialized
        } else {
            Interpreter::External
        };
        Ok(Self {
            path,
            _library: library,
            api,
            interpreter,
            configuration: None,
            setup: SetupCompletion::default(),
        })
    }

    fn ensure_path(&self, requested: &Path) -> Result<(), String> {
        if self.path == requested {
            return Ok(());
        }
        Err(format!(
            "Python shared library is already loaded from `{}` and cannot switch to `{}`",
            self.path.display(),
            requested.display()
        ))
    }

    fn attach(&mut self) -> Result<bool, String> {
        // SAFETY: The resolved function has no preconditions.
        if unsafe { (self.api.is_initialized)() } == 0 {
            return Err("cannot attach to Python before it is initialized".to_string());
        }
        if self.interpreter == Interpreter::Uninitialized {
            self.interpreter = Interpreter::External;
        }
        Ok(matches!(self.interpreter, Interpreter::RustOwned { .. }))
    }

    fn ensure_configuration(&mut self, selected: &super::NativePython) -> Result<(), String> {
        let Some(configuration) = self.configuration.as_ref() else {
            // Retain the observed identity when attaching to an interpreter
            // initialized before Console installed its startup adapter. Its
            // environment and thread-state ownership are already established.
            self.configuration = Some(Configuration::new(selected)?);
            return Ok(());
        };
        if &configuration.selected == selected {
            return Ok(());
        }
        Err("Python interpreter is already initialized with different configuration".to_string())
    }
}

impl PythonApi {
    unsafe fn finish_setup(&self, result: *mut PyObject) -> Result<bool, String> {
        // Keep the original exception and traceback for the R adapter to
        // rethrow through reticulate's existing condition/interrupt boundary.
        // Do not display it here or turn a failed setup into activation success.
        unsafe {
            if !result.is_null() {
                (self.dec_ref)(result);
                return Ok(true);
            }
            if (self.err_exception_matches)(self.keyboard_interrupt as *mut PyObject) != 0 {
                crate::worker::record_bootstrap_interrupt();
            }
            let mut exception_type = std::ptr::null_mut();
            let mut exception_value = std::ptr::null_mut();
            let mut traceback = std::ptr::null_mut();
            (self.err_fetch)(&mut exception_type, &mut exception_value, &mut traceback);
            (self.err_normalize_exception)(
                &mut exception_type,
                &mut exception_value,
                &mut traceback,
            );
            // Site hooks can fail before evaluator installation. Its later
            // installation reuses this module and the retained exception.
            let runtime = (self.import_add_module)(c"_mcp_console".as_ptr());
            let namespace = if runtime.is_null() {
                std::ptr::null_mut()
            } else {
                (self.module_get_dict)(runtime)
            };
            let retained = !exception_value.is_null()
                && !namespace.is_null()
                && (traceback.is_null()
                    || (self.exception_set_traceback)(exception_value, traceback) == 0)
                && (self.dict_set_item_string)(namespace, c"setup_error".as_ptr(), exception_value)
                    == 0;
            for object in [exception_type, exception_value, traceback] {
                if !object.is_null() {
                    (self.dec_ref)(object);
                }
            }
            if retained {
                Ok(false)
            } else {
                (self.err_clear)();
                Err("failed to retain Python setup exception".to_string())
            }
        }
    }

    fn with_gil<T>(
        &self,
        operation: impl FnOnce(&PythonApi) -> Result<T, String>,
    ) -> Result<T, String> {
        // SAFETY: The resolved function has no preconditions.
        if unsafe { (self.is_initialized)() } == 0 {
            return Err("Python interpreter is not initialized".to_string());
        }
        // SAFETY: CPython is initialized. PyGILState_Ensure permits this call
        // both while reticulate holds the GIL and after it has released it.
        let gil_state = unsafe { (self.gil_state_ensure)() };
        let result = operation(self);
        // SAFETY: This state was returned by the matching ensure call above.
        unsafe { (self.gil_state_release)(gil_state) };
        result
    }

    unsafe fn run_runtime(&self, source: &CStr) -> Result<(), String> {
        // Match reticulate::py_run_string(local = TRUE): definitions are
        // isolated in a fresh locals dictionary while functions retain the
        // persistent __main__ globals used by Python cells.
        let main = unsafe { (self.import_add_module)(c"__main__".as_ptr()) };
        if main.is_null() {
            unsafe { (self.err_print)() };
            return Err("failed to access Python's main module".to_string());
        }
        let globals = unsafe { (self.module_get_dict)(main) };
        if globals.is_null() {
            unsafe { (self.err_print)() };
            return Err("failed to access Python's main namespace".to_string());
        }
        let locals = unsafe { (self.dict_new)() };
        if locals.is_null() {
            unsafe { (self.err_print)() };
            return Err("failed to create the private Python runtime namespace".to_string());
        }
        let result = unsafe {
            (self.run_string_flags)(
                source.as_ptr(),
                PY_FILE_INPUT,
                globals,
                locals,
                std::ptr::null_mut(),
            )
        };
        if result.is_null() {
            unsafe {
                (self.err_print)();
                (self.dec_ref)(locals);
            }
            return Err("failed to install MCP Console's private Python runtime".to_string());
        }
        unsafe {
            (self.dec_ref)(result);
            (self.dec_ref)(locals);
        }
        Ok(())
    }

    unsafe fn run_module(&self, name: &CStr, source: &CStr) -> Result<(), String> {
        let result = unsafe { self.run_module_result(name, source)? };
        if result.is_null() {
            unsafe { (self.err_print)() };
            return Err(format!(
                "failed to install Python module `{}`",
                name.to_string_lossy()
            ));
        }
        unsafe { (self.dec_ref)(result) };
        Ok(())
    }

    unsafe fn run_module_result(
        &self,
        name: &CStr,
        source: &CStr,
    ) -> Result<*mut PyObject, String> {
        let module = unsafe { (self.import_add_module)(name.as_ptr()) };
        if module.is_null() {
            unsafe { (self.err_print)() };
            return Err(format!(
                "failed to create Python module `{}`",
                name.to_string_lossy()
            ));
        }
        let namespace = unsafe { (self.module_get_dict)(module) };
        if namespace.is_null() {
            unsafe { (self.err_print)() };
            return Err(format!(
                "failed to access Python module `{}`",
                name.to_string_lossy()
            ));
        }
        Ok(unsafe {
            (self.run_string_flags)(
                source.as_ptr(),
                PY_FILE_INPUT,
                namespace,
                namespace,
                std::ptr::null_mut(),
            )
        })
    }

    fn call_unit(&self, module: &CStr, name: &CStr) -> Result<(), String> {
        // SAFETY: The GIL is held for the complete call and reference release.
        unsafe {
            let function = self.function(module, name)?;
            let result = (self.call_no_args)(function);
            if result.is_null() {
                self.display_pending_exception();
                return Err(python_function_error(module, name));
            }
            (self.dec_ref)(result);
            Ok(())
        }
    }

    fn call_sql_dispatch(&self, source: &str) -> Result<super::SqlProvider, String> {
        // SAFETY: The GIL is held and PyUnicode_FromStringAndSize copies the
        // UTF-8 source before the Rust buffer can be released.
        unsafe {
            let function = self.function(c"_mcp_console_sql", c"dispatch")?;
            let argument =
                (self.unicode_from_string_and_size)(source.as_ptr().cast(), source.len() as isize);
            if argument.is_null() {
                (self.err_print)();
                return Err("failed to create Python SQL source string".to_string());
            }
            let result =
                (self.call_function_obj_args)(function, argument, std::ptr::null_mut::<PyObject>());
            (self.dec_ref)(argument);
            if result.is_null() {
                // Database errors are normally caught by the Python adapter.
                // Escaping exceptions such as KeyboardInterrupt remain ordinary
                // console output and must not fall through to an R provider.
                crate::worker::record_execution_error(crate::worker_protocol::ExecutionError::new(
                    "SQLError",
                    "SQL evaluation failed",
                ));
                self.display_pending_exception();
                return Ok(super::SqlProvider::Handled);
            }
            let provider = (self.long_as_long)(result);
            (self.dec_ref)(result);
            match provider {
                SQL_PROVIDER_R => Ok(super::SqlProvider::R),
                SQL_PROVIDER_MANAGED => Ok(super::SqlProvider::Managed),
                SQL_PROVIDER_HANDLED => Ok(super::SqlProvider::Handled),
                _ => {
                    if provider == -1 {
                        (self.err_print)();
                    }
                    Err("Python SQL dispatch returned an invalid provider".to_string())
                }
            }
        }
    }

    fn call_sql_bool(&self, name: &CStr) -> Result<bool, String> {
        // SAFETY: The GIL is held for the private Python call and reference release.
        unsafe {
            let function = self.function(c"_mcp_console_sql", name)?;
            let result = (self.call_no_args)(function);
            if result.is_null() {
                self.display_pending_exception();
                return Err(python_function_error(c"_mcp_console_sql", name));
            }
            let requested = (self.long_as_long)(result);
            (self.dec_ref)(result);
            match requested {
                0 => Ok(false),
                1 => Ok(true),
                _ => Err(format!(
                    "Python SQL `{}` returned an invalid boolean",
                    name.to_string_lossy()
                )),
            }
        }
    }

    fn display_pending_exception(&self) {
        // Adapter and service failures are handled here, including SystemExit.
        // Fetch and display them without invoking CPython's process exit handler.
        unsafe {
            let mut exception_type = std::ptr::null_mut();
            let mut exception_value = std::ptr::null_mut();
            let mut traceback = std::ptr::null_mut();
            (self.err_fetch)(&mut exception_type, &mut exception_value, &mut traceback);
            if !exception_type.is_null() {
                (self.err_normalize_exception)(
                    &mut exception_type,
                    &mut exception_value,
                    &mut traceback,
                );
                if !exception_type.is_null() && !exception_value.is_null() {
                    (self.err_display)(exception_type, exception_value, traceback);
                }
            }
            for object in [exception_type, exception_value, traceback] {
                if !object.is_null() {
                    (self.dec_ref)(object);
                }
            }
            (self.err_clear)();
        }
    }

    unsafe fn function(&self, module: &CStr, name: &CStr) -> Result<*mut PyObject, String> {
        let module_object = unsafe { (self.import_add_module)(module.as_ptr()) };
        if module_object.is_null() {
            unsafe { (self.err_print)() };
            return Err(format!(
                "failed to access Python module `{}`",
                module.to_string_lossy()
            ));
        }
        let namespace = unsafe { (self.module_get_dict)(module_object) };
        if namespace.is_null() {
            unsafe { (self.err_print)() };
            return Err(format!(
                "failed to access Python module `{}`",
                module.to_string_lossy()
            ));
        }
        let function = unsafe { (self.dict_get_item_string)(namespace, name.as_ptr()) };
        if function.is_null() {
            unsafe { (self.err_print)() };
            return Err(format!(
                "Python module `{}` is missing `{}`",
                module.to_string_lossy(),
                name.to_string_lossy()
            ));
        }
        Ok(function)
    }

    unsafe fn load(library: &libloading::Library, path: &Path) -> Result<Self, String> {
        Ok(Self {
            // SAFETY: Symbol types match the documented CPython C API.
            is_initialized: unsafe { load_symbol(library, path, b"Py_IsInitialized\0")? },
            set_program_name: unsafe { load_symbol(library, path, b"Py_SetProgramName\0")? },
            initialize_ex: unsafe { load_symbol(library, path, b"Py_InitializeEx\0")? },
            no_site_flag: unsafe {
                load_symbol::<*mut libc::c_int>(library, path, b"Py_NoSiteFlag\0")? as usize
            },
            sys_get_object: unsafe { load_symbol(library, path, b"PySys_GetObject\0")? },
            struct_sequence_get_item: unsafe {
                load_symbol(library, path, b"PyStructSequence_GetItem\0")?
            },
            struct_sequence_set_item: unsafe {
                load_symbol(library, path, b"PyStructSequence_SetItem\0")?
            },
            long_from_long: unsafe { load_symbol(library, path, b"PyLong_FromLong\0")? },
            set_argv_ex: unsafe { load_symbol(library, path, b"PySys_SetArgvEx\0")? },
            #[cfg(unix)]
            set_signal: unsafe { load_symbol(library, path, b"PyOS_setsig\0")? },
            save_thread: unsafe { load_symbol(library, path, b"PyEval_SaveThread\0")? },
            restore_thread: unsafe { load_symbol(library, path, b"PyEval_RestoreThread\0")? },
            gil_state_ensure: unsafe { load_symbol(library, path, b"PyGILState_Ensure\0")? },
            gil_state_release: unsafe { load_symbol(library, path, b"PyGILState_Release\0")? },
            import_add_module: unsafe { load_symbol(library, path, b"PyImport_AddModule\0")? },
            module_get_dict: unsafe { load_symbol(library, path, b"PyModule_GetDict\0")? },
            dict_new: unsafe { load_symbol(library, path, b"PyDict_New\0")? },
            dict_get_item_string: unsafe { load_symbol(library, path, b"PyDict_GetItemString\0")? },
            dict_set_item_string: unsafe { load_symbol(library, path, b"PyDict_SetItemString\0")? },
            run_string_flags: unsafe { load_symbol(library, path, b"PyRun_StringFlags\0")? },
            call_no_args: unsafe { load_symbol(library, path, b"PyObject_CallNoArgs\0")? },
            call_function_obj_args: unsafe {
                load_symbol(library, path, b"PyObject_CallFunctionObjArgs\0")?
            },
            unicode_from_string_and_size: unsafe {
                load_symbol(library, path, b"PyUnicode_FromStringAndSize\0")?
            },
            long_as_long: unsafe { load_symbol(library, path, b"PyLong_AsLong\0")? },
            dec_ref: unsafe { load_symbol(library, path, b"Py_DecRef\0")? },
            err_fetch: unsafe { load_symbol(library, path, b"PyErr_Fetch\0")? },
            err_normalize_exception: unsafe {
                load_symbol(library, path, b"PyErr_NormalizeException\0")?
            },
            err_display: unsafe { load_symbol(library, path, b"PyErr_Display\0")? },
            err_clear: unsafe { load_symbol(library, path, b"PyErr_Clear\0")? },
            err_print: unsafe { load_symbol(library, path, b"PyErr_Print\0")? },
            err_exception_matches: unsafe {
                load_symbol(library, path, b"PyErr_ExceptionMatches\0")?
            },
            system_exit: unsafe {
                *load_symbol::<*const *mut PyObject>(library, path, b"PyExc_SystemExit\0")?
            } as usize,
            keyboard_interrupt: unsafe {
                *load_symbol::<*const *mut PyObject>(library, path, b"PyExc_KeyboardInterrupt\0")?
            } as usize,
            exception_set_traceback: unsafe {
                load_symbol(library, path, b"PyException_SetTraceback\0")?
            },
        })
    }
}

fn python_function_error(module: &CStr, name: &CStr) -> String {
    format!(
        "Python function `{}.{}` failed",
        module.to_string_lossy(),
        name.to_string_lossy()
    )
}

unsafe fn load_symbol<T: Copy>(
    library: &libloading::Library,
    path: &Path,
    name: &'static [u8],
) -> Result<T, String> {
    let display_name =
        std::str::from_utf8(&name[..name.len() - 1]).expect("CPython symbol names should be UTF-8");
    // SAFETY: The caller supplies the documented function-pointer type for
    // the named CPython C API symbol, and the library handle outlives it.
    unsafe { library.get::<T>(name) }
        .map(|symbol| *symbol)
        .map_err(|error| {
            format!(
                "Python shared library `{}` does not export {display_name}: {error}",
                path.display()
            )
        })
}

impl Configuration {
    fn new(selected: &super::NativePython) -> Result<Self, String> {
        #[cfg(windows)]
        // getpath's virtualenv search requires native Windows separators.
        let program_name = selected.embedding.python.replace('/', "\\");
        #[cfg(not(windows))]
        let program_name = &selected.embedding.python;
        let program_name_wide = wide_string(program_name.as_str(), "program name")?;
        #[cfg(windows)]
        let program_name_wide = {
            // CPython mistakes an 8.3 alias for an executable symlink when
            // finding the base interpreter. Expand the startup spelling while
            // retaining the caller's selection in sys.executable and orig_argv.
            let mut expanded = vec![0; 32768];
            let length = unsafe {
                windows_sys::Win32::Storage::FileSystem::GetLongPathNameW(
                    program_name_wide.as_ptr(),
                    expanded.as_mut_ptr(),
                    expanded.len() as u32,
                )
            } as usize;
            if length == 0 || length >= expanded.len() {
                return Err(format!(
                    "cannot expand selected Python program name: {}",
                    std::io::Error::last_os_error()
                ));
            }
            expanded.truncate(length + 1);
            expanded
        };
        Ok(Self {
            selected: selected.clone(),
            reticulate_python: std::env::var_os("RETICULATE_PYTHON"),
            program_name_wide,
        })
    }
}

fn wide_string(value: &str, label: &str) -> Result<Vec<libc::wchar_t>, String> {
    if value.contains('\0') {
        return Err(format!("Python {label} contains NUL"));
    }
    #[cfg(windows)]
    let mut wide = value.encode_utf16().collect::<Vec<_>>();
    #[cfg(unix)]
    let mut wide = value
        .chars()
        .map(|character| character as libc::wchar_t)
        .collect::<Vec<_>>();
    wide.push(0);
    Ok(wide)
}
