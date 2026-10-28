use libr::SEXP;

use super::NativePython;

const PYTHON_BRIDGE_SOURCE: &str = include_str!("bridge.R");
const PYTHON_INITIALIZER_SOURCE: &str = include_str!("initialize.R");

/// Optional R-side selection and attachment compatibility adapter.
///
/// This still preserves reticulate discovery and initialization hooks. CPython
/// loading, lifetime, and cell evaluation remain owned by Console.
pub(super) struct Adapter {
    bridge: crate::r_bridge::Bridge,
    completed: std::cell::Cell<bool>,
    pub(super) managed: bool,
}

pub(super) enum Selection {
    Selected(Box<NativePython>),
    Unavailable,
    Incomplete,
}

pub(crate) fn with_r_startup_environment(
    initialize: impl FnOnce() -> Result<(), String>,
) -> Result<(), String> {
    super::platform::set_environment(c"RETICULATE_REMAP_OUTPUT_STREAMS", c"0", true)
        .map_err(|error| error.to_string())?;
    // Mark the embedding before native startup, including Python hooks that
    // import rpy2. The marker does not depend on Python selection.
    let marker = std::ffi::CString::new(format!("PID={}:NAME=\"reticulate\"", std::process::id()))
        .map_err(|error| error.to_string())?;
    super::platform::set_environment(c"R_SESSION_INITIALIZED", &marker, true)
        .map_err(|error| error.to_string())?;
    // Before Console's adapter exists, stock reticulate would resolve the
    // literal "managed" hint independently. Startup packages must use the
    // environment already prepared for this generation instead.
    let selected = if std::env::var_os("RETICULATE_PYTHON").is_some_and(|value| value == "managed")
        && super::library::initialized_selection()?.is_none()
    {
        super::requirements::materialized()
            .map(|candidate| candidate.selected.embedding.python)
            .or_else(|| {
                super::SELECTION
                    .get()?
                    .python
                    .as_ref()
                    .filter(|python| python.managed)
                    .map(|python| python.selected.embedding.python.clone())
            })
    } else {
        None
    };
    if let Some(selected) = &selected {
        let executable =
            std::ffi::CString::new(selected.as_str()).map_err(|error| error.to_string())?;
        super::platform::set_environment(c"RETICULATE_PYTHON", &executable, true)
            .map_err(|error| error.to_string())?;
    }
    let result = initialize();
    // Restore before adapter attachment, even when native startup failed.
    // Preserve profile edits; adoption validates their selected interpreter.
    if let Some(selected) = selected
        && std::env::var_os("RETICULATE_PYTHON")
            .is_some_and(|value| value == std::ffi::OsStr::new(&selected))
    {
        super::platform::set_environment(c"RETICULATE_PYTHON", c"managed", true)
            .map_err(|error| error.to_string())?;
    }
    result
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_python_retained_manifest() -> harp::Result<SEXP> {
    let manifest = super::requirements::retained_manifest()
        .ok_or_else(|| harp::anyhow!("managed Python declaration is unavailable"))?;
    Ok(harp::object::RObject::from(
        serde_json::to_string(&manifest).map_err(|error| harp::anyhow!("{error}"))?,
    )
    .sexp)
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_running_python() -> harp::Result<SEXP> {
    let selected =
        super::library::initialized_selection().map_err(|error| harp::anyhow!("{error}"))?;
    match selected {
        Some(selected) => Ok(harp::object::RObject::from(
            serde_json::to_string(&selected).map_err(|error| harp::anyhow!("{error}"))?,
        )
        .sexp),
        None => unsafe { Ok(libr::R_NilValue) },
    }
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_python_environment_selection_unchanged() -> harp::Result<SEXP>
{
    let unchanged = super::library::environment_selection_unchanged()
        .map_err(|error| harp::anyhow!("{error}"))?;
    Ok(harp::object::RObject::from(unchanged).sexp)
}

impl Adapter {
    pub(super) fn initialize() -> Result<Self, String> {
        let managed = std::env::var_os("MCP_CONSOLE_MANAGED_PYTHON").is_some();
        let source = format!(
            "base::local(
  {{
    state <- ({PYTHON_BRIDGE_SOURCE})
{PYTHON_INITIALIZER_SOURCE}
    state
  }},
  envir = base::new.env(parent = base::baseenv())
)"
        );
        Ok(Self {
            bridge: crate::r_bridge::Bridge::initialize(&source, "Python")?,
            completed: std::cell::Cell::new(false),
            managed,
        })
    }

    pub(super) fn ensure_initialized(&self) -> Result<bool, String> {
        if !self.completed.get() {
            let Selection::Selected(selected) = self.select(false)? else {
                return Ok(false);
            };
            if let Err(error) = super::initialize_selected(&selected) {
                self.cancel_selection()?;
                return Err(error);
            }
            if super::startup::setup_in_progress() {
                // A site hook needs conversion while the outer setup still
                // owns Python's active callback and initial thread state.
                // Attachment is usable now, but runtime setup is not complete.
                return self.attach();
            }
            // Fresh R-first initialization completes native setup before
            // conversion attachment. The explicit setup call also covers
            // interpreters initialized by native R startup before this adapter.
            let result = self
                .attach()
                .and_then(|attached| if attached { self.setup() } else { Ok(false) });
            let finished = super::finish_initialization();
            let completed = result?;
            finished?;
            self.completed.set(completed);
        }
        Ok(self.completed.get())
    }

    pub(super) fn select(&self, optional: bool) -> Result<Selection, String> {
        // Discovery and serialization share the existing R interrupt boundary.
        let source = if optional {
            "select_optional"
        } else {
            "select"
        };
        let Some(selected) = self.bridge.evaluate_completed_string(source)? else {
            return Ok(Selection::Incomplete);
        };
        if selected.is_empty() {
            return Ok(Selection::Incomplete);
        }
        serde_json::from_str::<Option<NativePython>>(&selected)
            .map(|selected| match selected {
                Some(selected) => Selection::Selected(Box::new(selected)),
                None => Selection::Unavailable,
            })
            .map_err(|error| format!("invalid selected Python configuration: {error}"))
    }

    fn cancel_selection(&self) -> Result<(), String> {
        self.bridge.call0_integer(c"cancel_python_selection")?;
        Ok(())
    }

    fn attach(&self) -> Result<bool, String> {
        self.bridge.evaluate_completed("attach")
    }

    fn setup(&self) -> Result<bool, String> {
        Ok(self.bridge.evaluate_completed("setup")? && super::library::runtime_configured()?)
    }
}

// A captured existing interpreter owns its metadata as well as its executable.
// Resolve it before reticulate can discover or validate a mutable profile hint.
#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_captured_python() -> harp::Result<SEXP> {
    let selected = if let Some(pending) = super::pending_executable() {
        let executable = pending.map_err(|error| harp::anyhow!("{error}"))?;
        crate::worker::check_python_selection_interrupt()
            .map_err(|error| harp::anyhow!("{error}"))?;
        Some(crate::worker::inspect_python(executable).map_err(|error| harp::anyhow!("{error}"))?)
    } else {
        super::SELECTION
            .get()
            .and_then(|selection| selection.python.as_ref())
            .filter(|python| !python.managed)
            .map(|python| (*python.selected).clone())
    };
    let Some(selected) = selected else {
        return unsafe { Ok(libr::R_NilValue) };
    };
    let result = serde_json::to_string(&selected).map_err(|error| harp::anyhow!("{error}"))?;
    Ok(harp::object::RObject::from(result).sexp)
}

// Called once for a fresh selection, before the adapter changes the worker
// environment. The adapter retains these embedding fields for attachment.
#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_inspect_python(python: SEXP) -> harp::Result<SEXP> {
    let python = String::try_from(harp::object::RObject::view(python))?;
    let executable = match std::env::var_os("RETICULATE_PYTHON")
        .filter(|value| !value.is_empty() && value != "managed")
    {
        Some(explicit) => {
            super::explicit_executable(&explicit).map_err(|error| harp::anyhow!("{error}"))?
        }
        None => python.into(),
    };
    crate::worker::check_python_selection_interrupt().map_err(|error| harp::anyhow!("{error}"))?;
    let captured = crate::local_runtime::Selection::from_environment()
        .map_err(|error| harp::anyhow!("{error}"))?
        .and_then(|runtime| runtime.python);
    let resolved = super::requirements::resolved_selection();
    let selected = if let Some(resolved) = resolved
        && std::path::Path::new(&resolved.embedding.python) == executable
    {
        resolved
    } else if let Some(captured) = captured
        && std::path::Path::new(&captured.selected.embedding.python) == executable
    {
        *captured.selected
    } else {
        crate::worker::inspect_python(&executable).map_err(|error| harp::anyhow!("{error}"))?
    };
    let result = serde_json::to_string(&selected).map_err(|error| harp::anyhow!("{error}"))?;
    Ok(harp::object::RObject::from(result).sexp)
}

// Rust initializes the exact interpreter selected by reticulate. Reticulate
// then observes the running interpreter and attaches its conversion runtime.
#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_initialize_python(
    selection: SEXP,
    bridge_path: SEXP,
) -> harp::Result<SEXP> {
    let selection = String::try_from(harp::object::RObject::view(selection))?;
    let selected: NativePython =
        serde_json::from_str(&selection).map_err(|error| harp::anyhow!("{error}"))?;
    let bridge_path = String::try_from(harp::object::RObject::view(bridge_path))?;
    let rust_owned =
        super::initialize_selected(&selected).map_err(|error| harp::anyhow!("{error}"))?;
    super::library::add_bridge_path(&bridge_path).map_err(|error| harp::anyhow!("{error}"))?;
    Ok(harp::object::RObject::from(rust_owned).sexp)
}

// Reticulate calls this after installing its own stream and input hooks.
#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_install_python_services(
    libpython: SEXP,
) -> harp::Result<SEXP> {
    let libpython = String::try_from(harp::object::RObject::view(libpython))?;
    super::startup::install_services(std::path::Path::new(&libpython))
        .map_err(|error| harp::anyhow!("{error}"))?;
    unsafe { Ok(libr::R_NilValue) }
}

// Setup can call user Python module hooks. Keep the caller's interrupt
// context while it runs, as for managed activation; protect only R conversion.
#[ctor::ctor(unsafe)]
fn register_python_setup() {
    type CallMethod = unsafe extern "C-unwind" fn() -> *mut libc::c_void;
    // SAFETY: R calls this on its owning thread with three protected arguments.
    unsafe {
        harp::routines::add(libr::R_CallMethodDef {
            name: c"mcp_console_setup_python_runtime".as_ptr(),
            fun: Some(std::mem::transmute::<*const (), CallMethod>(
                setup_python_runtime as *const (),
            )),
            numArgs: 2,
        });
    }
}

#[allow(clippy::result_large_err)]
extern "C-unwind" fn setup_python_runtime(libpython: SEXP, managed: SEXP) -> SEXP {
    harp::exec::r_unwrap(|| -> harp::Result<SEXP> {
        let (libpython, managed) = harp::exec::r_sandbox(|| -> harp::Result<_> {
            let libpython = Option::<String>::try_from(harp::object::RObject::view(libpython))?
                .ok_or_else(|| harp::anyhow!("Python-hosted R is not supported"))?;
            let managed = bool::try_from(harp::object::RObject::view(managed))?;
            Ok((libpython, managed))
        })??;
        let completed = super::setup_runtime(std::path::Path::new(&libpython), managed)
            .map_err(|error| harp::anyhow!("{error}"))?;
        if !completed && crate::worker::is_shutting_down() {
            // Retirement deliberately retains no Python exception. Distinguish
            // cancellation from FALSE, whose exception the R adapter rethrows.
            return unsafe { Ok(libr::R_NilValue) };
        }
        harp::exec::r_sandbox(|| harp::object::RObject::from(completed).sexp)
    })
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_python_runtime_is_configured() -> harp::Result<SEXP> {
    let configured =
        super::library::runtime_configured().map_err(|error| harp::anyhow!("{error}"))?;
    Ok(harp::object::RObject::from(configured).sexp)
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_python_setup_in_progress() -> harp::Result<SEXP> {
    Ok(harp::object::RObject::from(super::startup::setup_in_progress()).sexp)
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_python_conversion_metadata() -> harp::Result<SEXP> {
    let metadata =
        super::library::conversion_metadata().map_err(|error| harp::anyhow!("{error}"))?;
    Ok(harp::object::RObject::from(
        serde_json::to_string(&metadata).map_err(|error| harp::anyhow!("{error}"))?,
    )
    .sexp)
}

// Release the initial GIL when control leaves reticulate's C initializer,
// including its error paths. Later reticulate calls acquire the GIL normally.
#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_finish_python_initialization() -> harp::Result<SEXP> {
    super::finish_initialization().map_err(|error| harp::anyhow!("{error}"))?;
    unsafe { Ok(libr::R_NilValue) }
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_resolve_python(request: SEXP) -> harp::Result<SEXP> {
    let request = String::try_from(harp::object::RObject::view(request))?;
    let request = serde_json::from_str(&request).map_err(|error| harp::anyhow!("{error}"))?;
    let candidate =
        crate::worker::resolve_python(request).map_err(|error| harp::anyhow!("{error}"))?;
    let python = candidate.selected.embedding.python.clone();
    super::requirements::resolved(candidate);
    Ok(harp::object::RObject::from(python).sexp)
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_resolve_python_version(request: SEXP) -> harp::Result<SEXP> {
    let request = String::try_from(harp::object::RObject::view(request))?;
    let request = serde_json::from_str(&request).map_err(|error| harp::anyhow!("{error}"))?;
    let version =
        crate::worker::resolve_python_version(request).map_err(|error| harp::anyhow!("{error}"))?;
    Ok(harp::object::RObject::from(version).sexp)
}
