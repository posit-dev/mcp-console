use std::ffi::{CStr, CString, c_char, c_int};
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::sync::OnceLock;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread::ThreadId;

use super::{PyObject, PythonApi, load_symbol};
use crate::worker;
use crate::worker_protocol::ConsoleChannel;

type Callback = unsafe extern "C" fn(*mut PyObject, *mut PyObject) -> *mut PyObject;

#[repr(C)]
struct Method {
    name: *const c_char,
    callback: Option<Callback>,
    flags: c_int,
    doc: *const c_char,
}

struct Services {
    api: PythonApi,
    thread: ThreadId,
    pid: u32,
    unicode_utf8: unsafe extern "C" fn(*mut PyObject, *mut isize) -> *const c_char,
    inc_ref: unsafe extern "C" fn(*mut PyObject),
    set_none: unsafe extern "C" fn(*mut PyObject),
    set_string: unsafe extern "C" fn(*mut PyObject, *const c_char),
    set_interrupt: unsafe extern "C" fn(),
    none: usize,
    runtime_error: usize,
    keyboard_interrupt: usize,
    eof_error: usize,
}

static SERVICES: OnceLock<Services> = OnceLock::new();
// These stages can complete before a later installation step fails. A retry
// reuses the method table and Python module instead of wrapping streams again.
static METHODS_REGISTERED: AtomicBool = AtomicBool::new(false);
static MODULE_INSTALLED: AtomicBool = AtomicBool::new(false);

pub(super) fn install(api: &PythonApi, installed: bool) -> Result<(), String> {
    // R startup replaces this handler even when Python initialized first.
    #[cfg(unix)]
    unsafe {
        (api.set_signal)(libc::SIGPIPE, libc::SIG_IGN)
    };
    if installed {
        let services = SERVICES
            .get()
            .expect("installed Python services retain callbacks");
        api.call_unit(c"_mcp_console_services", c"install_services")?;
        return worker::install_python_interrupt(services.set_interrupt);
    }
    let services = if let Some(services) = SERVICES.get() {
        services
    } else {
        // The owning Python handle is process-long; no library lock spans Python.
        let library = loaded_library()?;
        let path = std::path::Path::new("loaded Python");
        let exception = |name| -> Result<usize, String> {
            Ok(unsafe { *load_symbol::<*const *mut PyObject>(&library, path, name)? } as usize)
        };
        let services = Services {
            api: *api,
            thread: std::thread::current().id(),
            pid: std::process::id(),
            unicode_utf8: unsafe { load_symbol(&library, path, b"PyUnicode_AsUTF8AndSize\0")? },
            inc_ref: unsafe { load_symbol(&library, path, b"Py_IncRef\0")? },
            set_none: unsafe { load_symbol(&library, path, b"PyErr_SetNone\0")? },
            set_string: unsafe { load_symbol(&library, path, b"PyErr_SetString\0")? },
            set_interrupt: unsafe { load_symbol(&library, path, b"PyErr_SetInterrupt\0")? },
            none: unsafe { load_symbol::<*mut PyObject>(&library, path, b"_Py_NoneStruct\0")? }
                as usize,
            runtime_error: exception(b"PyExc_RuntimeError\0")?,
            keyboard_interrupt: exception(b"PyExc_KeyboardInterrupt\0")?,
            eof_error: exception(b"PyExc_EOFError\0")?,
        };
        SERVICES
            .set(services)
            .map_err(|_| "Python services already installed")?;
        SERVICES.get().unwrap()
    };
    if !METHODS_REGISTERED.load(Ordering::Acquire) {
        let library = loaded_library()?;
        let path = std::path::Path::new("loaded Python");
        let add_functions: unsafe extern "C" fn(*mut PyObject, *const Method) -> c_int =
            unsafe { load_symbol(&library, path, b"PyModule_AddFunctions\0")? };
        // CPython retains the method definitions for the process lifetime.
        let methods = Box::leak(Box::new([
            method(c"write", write, 8), // METH_O
            method(c"diagnostic", diagnostic, 8),
            method(c"execution_error", execution_error, 8),
            method(c"priority_active", priority_active, 4), // METH_NOARGS
            method(c"readline", readline, 8),
            method(c"publish_plot", publish_plot, 8),
            method(c"resolve_import_request", resolve_import_request, 8),
            method(c"attach_r", attach_r, 4),
            method(c"close_r_sql_result", close_r_sql_result, 4),
            method(c"interrupt", interrupt, 1), // METH_VARARGS: signal number and frame
            method(c"inspect_python", inspect_python, 8),
            method(
                c"activate_python_environment",
                activate_python_environment,
                4,
            ),
            method(c"publish_python_activation", publish_python_activation, 8),
            method(c"begin_python_commit", begin_python_commit, 4),
            method(c"finish_python_commit", finish_python_commit, 4),
            Method {
                name: std::ptr::null(),
                callback: None,
                flags: 0,
                doc: std::ptr::null(),
            },
        ]));
        let module = unsafe { (api.import_add_module)(c"_mcp_console_services".as_ptr()) };
        if module.is_null() || unsafe { add_functions(module, methods.as_ptr()) } != 0 {
            api.display_pending_exception();
            return Err("failed to install Python console callbacks".to_string());
        }
        METHODS_REGISTERED.store(true, Ordering::Release);
    }
    // Keep the library-state lock out of this interpreter execution. A failed
    // source installation leaves this stage open for a later setup attempt.
    if !MODULE_INSTALLED.load(Ordering::Acquire) {
        let source = CString::new(include_str!("../services.py")).unwrap();
        unsafe { api.run_module(c"_mcp_console_services", &source)? };
        MODULE_INSTALLED.store(true, Ordering::Release);
        // signal.signal in install_interrupt() sets CPython's handler. Install the
        // worker's native handler afterwards so R and managed input also wake.
    }
    worker::install_python_interrupt(services.set_interrupt)?;
    Ok(())
}

fn method(name: &'static CStr, callback: Callback, flags: c_int) -> Method {
    Method {
        name: name.as_ptr(),
        callback: Some(callback),
        flags,
        doc: std::ptr::null(),
    }
}

impl Services {
    fn string(&self, text: &str) -> *mut PyObject {
        unsafe {
            (self.api.unicode_from_string_and_size)(text.as_ptr().cast(), text.len() as isize)
        }
    }
    fn text(&self, object: *mut PyObject) -> Result<String, String> {
        let mut length = 0;
        let bytes = unsafe { (self.unicode_utf8)(object, &mut length) };
        if bytes.is_null() {
            return Err("console service requires UTF-8 text".to_string());
        }
        Ok(unsafe {
            std::str::from_utf8_unchecked(std::slice::from_raw_parts(bytes.cast(), length as usize))
        }
        .to_owned())
    }

    fn none(&self) -> *mut PyObject {
        unsafe { (self.inc_ref)(self.none as *mut PyObject) };
        self.none as *mut PyObject
    }

    fn without_gil<T>(&self, operation: impl FnOnce() -> T) -> T {
        struct Restore<'a>(&'a PythonApi, *mut libc::c_void);
        impl Drop for Restore<'_> {
            fn drop(&mut self) {
                unsafe { (self.0.restore_thread)(self.1) };
            }
        }
        let _restore = Restore(&self.api, unsafe { (self.api.save_thread)() });
        operation()
    }
}

fn callback(operation: impl FnOnce(&Services) -> Result<*mut PyObject, String>) -> *mut PyObject {
    dispatch_callback(CallbackThread::Main, operation)
}

enum CallbackThread {
    Main,
    OutputOwner,
}

fn dispatch_callback(
    thread: CallbackThread,
    operation: impl FnOnce(&Services) -> Result<*mut PyObject, String>,
) -> *mut PyObject {
    let services = SERVICES
        .get()
        .expect("Python services initialized before callbacks");
    let result = catch_unwind(AssertUnwindSafe(|| {
        let managed = std::thread::current().id() == services.thread
            || matches!(thread, CallbackThread::OutputOwner) && worker::priority_background();
        if std::process::id() != services.pid || !managed {
            return Err("console service requires the main worker thread".to_string());
        }
        operation(services)
    }));
    match result {
        Ok(Ok(object)) => object,
        result => {
            let error = match result {
                Ok(Err(error)) => error,
                Err(_) => "panic in Python console service".to_string(),
                Ok(Ok(_)) => unreachable!(),
            };
            let error = CString::new(error).unwrap();
            unsafe {
                (services.set_string)(services.runtime_error as *mut PyObject, error.as_ptr())
            };
            std::ptr::null_mut()
        }
    }
}

unsafe extern "C" fn priority_active(_: *mut PyObject, _: *mut PyObject) -> *mut PyObject {
    let services = SERVICES
        .get()
        .expect("Python services initialized before callbacks");
    // Native ownership spans PyGILState_Release and its thread-local destructors.
    // A Python thread-local flag can disappear before those callbacks run.
    let active = std::process::id() == services.pid && worker::priority_active();
    unsafe { (services.api.long_from_long)(active.into()) }
}

unsafe extern "C" fn write(_: *mut PyObject, text: *mut PyObject) -> *mut PyObject {
    output(ConsoleChannel::Output, text)
}

unsafe extern "C" fn diagnostic(_: *mut PyObject, text: *mut PyObject) -> *mut PyObject {
    output(ConsoleChannel::Diagnostic, text)
}

unsafe extern "C" fn execution_error(_: *mut PyObject, error: *mut PyObject) -> *mut PyObject {
    dispatch_callback(CallbackThread::OutputOwner, |services| {
        let error = serde_json::from_str(&services.text(error)?)
            .map_err(|error| format!("invalid Python execution error: {error}"))?;
        services.without_gil(|| worker::record_execution_error(error));
        Ok(services.none())
    })
}

fn output(channel: ConsoleChannel, text: *mut PyObject) -> *mut PyObject {
    dispatch_callback(CallbackThread::OutputOwner, |services| {
        let text = services.text(text)?;
        if !text.is_empty() {
            services.without_gil(|| worker::emit_output(channel, text.as_bytes()));
        }
        Ok(services.none())
    })
}

unsafe extern "C" fn publish_plot(_: *mut PyObject, image: *mut PyObject) -> *mut PyObject {
    dispatch_callback(CallbackThread::OutputOwner, |services| {
        let image = services.text(image)?;
        services.without_gil(|| worker::publish_plot(Ok(image)));
        Ok(services.none())
    })
}

unsafe extern "C" fn attach_r(_: *mut PyObject, _: *mut PyObject) -> *mut PyObject {
    callback(|services| {
        if worker::priority_background() {
            return Err("R access is unavailable from a Python priority thread".into());
        }
        services.without_gil(crate::worker::ensure_bridge)?;
        Ok(services.none())
    })
}

unsafe extern "C" fn close_r_sql_result(_: *mut PyObject, _: *mut PyObject) -> *mut PyObject {
    callback(|services| {
        services.without_gil(crate::sql::close_r_result)?;
        Ok(services.none())
    })
}

unsafe extern "C" fn resolve_import_request(
    _: *mut PyObject,
    request: *mut PyObject,
) -> *mut PyObject {
    callback(|services| {
        let request = services.text(request)?;
        let resolution = serde_json::from_str(&request)
            .map_err(|error| format!("invalid Python import request: {error}"))?;
        let response =
            services.without_gil(|| crate::python::resolve_managed_import(resolution))?;
        Ok(unsafe {
            (services.api.unicode_from_string_and_size)(
                response.as_ptr().cast(),
                response.len() as isize,
            )
        })
    })
}

unsafe extern "C" fn readline(_: *mut PyObject, prompt: *mut PyObject) -> *mut PyObject {
    callback(|services| {
        if worker::priority_active() {
            return Err("stdin is unavailable during priority execution".into());
        }
        let prompt = services.text(prompt)?;
        let line = services.without_gil(|| worker::read_python_input(&prompt))?;
        match line {
            worker::PythonInput::Line(line) => Ok(unsafe {
                (services.api.unicode_from_string_and_size)(
                    line.as_ptr().cast(),
                    line.len() as isize,
                )
            }),
            worker::PythonInput::Interrupted => {
                worker::acknowledge_python_interrupt();
                unsafe { (services.set_none)(services.keyboard_interrupt as *mut PyObject) };
                Ok(std::ptr::null_mut())
            }
            worker::PythonInput::Eof => {
                unsafe { (services.set_none)(services.eof_error as *mut PyObject) };
                Ok(std::ptr::null_mut())
            }
        }
    })
}

unsafe extern "C" fn interrupt(_: *mut PyObject, _: *mut PyObject) -> *mut PyObject {
    callback(|services| {
        if worker::acknowledge_python_interrupt() {
            unsafe { (services.set_none)(services.keyboard_interrupt as *mut PyObject) };
            Ok(std::ptr::null_mut())
        } else {
            // R consumed it, or has suspended delivery. Its existing event
            // integration will recheck later; never re-arm from this handler.
            Ok(services.none())
        }
    })
}

unsafe extern "C" fn inspect_python(_: *mut PyObject, executable: *mut PyObject) -> *mut PyObject {
    callback(|services| {
        let executable = services.text(executable)?;
        let result = services.without_gil(|| super::super::probe::inspect(&executable));
        if worker::acknowledge_python_interrupt() {
            unsafe { (services.set_none)(services.keyboard_interrupt as *mut PyObject) };
            return Ok(std::ptr::null_mut());
        }
        let result = result.map_err(|error| error.to_string())?;
        Ok(services.string(&result.to_string()))
    })
}

unsafe extern "C" fn activate_python_environment(
    _: *mut PyObject,
    _: *mut PyObject,
) -> *mut PyObject {
    callback(|services| {
        match services.without_gil(super::super::requirements::activate_pending_selection) {
            Ok(()) => Ok(services.none()),
            Err(super::super::ActivationFailure::PythonException) => unsafe {
                let function = services
                    .api
                    .function(c"_mcp_console", c"raise_setup_error")?;
                Ok((services.api.call_no_args)(function))
            },
            Err(error) => Err(error.to_string()),
        }
    })
}

unsafe extern "C" fn publish_python_activation(
    _: *mut PyObject,
    activation: *mut PyObject,
) -> *mut PyObject {
    callback(|services| {
        let activation: super::super::environment::Activation =
            serde_json::from_str(&services.text(activation)?).map_err(|error| error.to_string())?;
        services.without_gil(|| super::super::requirements::publish_activation(activation))?;
        Ok(services.none())
    })
}

unsafe extern "C" fn begin_python_commit(_: *mut PyObject, _: *mut PyObject) -> *mut PyObject {
    callback(|services| {
        worker::begin_python_commit();
        Ok(services.none())
    })
}

unsafe extern "C" fn finish_python_commit(_: *mut PyObject, _: *mut PyObject) -> *mut PyObject {
    callback(|services| {
        if worker::finish_python_commit() {
            unsafe { (services.set_none)(services.keyboard_interrupt as *mut PyObject) };
            Ok(std::ptr::null_mut())
        } else {
            Ok(services.none())
        }
    })
}

pub(super) fn response_text(value: *mut PyObject) -> Result<String, String> {
    SERVICES
        .get()
        .expect("Python services initialized")
        .text(value)
}

fn loaded_library() -> Result<libloading::Library, String> {
    #[cfg(unix)]
    {
        Ok(libloading::os::unix::Library::this().into())
    }
    #[cfg(windows)]
    {
        let library = super::PYTHON_LIBRARY
            .lock()
            .map_err(|_| "Python library lock poisoned")?;
        let path = &library.as_ref().ok_or("Python library is not loaded")?.path;
        libloading::os::windows::Library::open_already_loaded(path)
            .map(Into::into)
            .map_err(|error| error.to_string())
    }
}
