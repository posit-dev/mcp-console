//! Unix libR startup and event activity. R calls remain on the coordinator thread.

use std::error::Error;
use std::ffi::{c_char, c_int, c_void};
use std::io;
use std::path::Path;
use std::sync::OnceLock;

use super::{TopLevelExec, mcp_r_read_console, r_busy, r_show_message, r_write_console};

static R_EVENTS: OnceLock<Events> = OnceLock::new();

type CheckActivity = unsafe extern "C-unwind" fn(c_int, c_int) -> *mut c_void;
type RunHandlers = unsafe extern "C-unwind" fn(*mut c_void, *mut c_void);
type AddInputHandler = unsafe extern "C-unwind" fn(
    *mut c_void,
    c_int,
    Option<unsafe extern "C-unwind" fn(*mut c_void)>,
    c_int,
) -> *mut c_void;
type RemoveInputHandler = unsafe extern "C-unwind" fn(*mut *mut c_void, *mut c_void) -> c_int;

pub(super) struct Events {
    top_level_exec: TopLevelExec,
    check_activity: CheckActivity,
    run_handlers: RunHandlers,
    add_input_handler: AddInputHandler,
    remove_input_handler: RemoveInputHandler,
    rg_wait_usec: usize,
}

unsafe extern "C" {
    fn mcp_r_run_ready_handlers(
        top_level_exec: TopLevelExec,
        check_activity: CheckActivity,
        run_handlers: RunHandlers,
        input_handlers: *mut c_void,
    );
    fn mcp_r_wait_for_activity(
        top_level_exec: TopLevelExec,
        add_input_handler: AddInputHandler,
        remove_input_handler: RemoveInputHandler,
        check_activity: CheckActivity,
        input_handlers: *mut *mut c_void,
        sideband_fd: c_int,
        wait_usec: c_int,
    ) -> c_int;
}

pub(super) fn initialize_r(
    _r_home: &Path,
    arguments: &mut [*mut c_char],
) -> Result<(), Box<dyn Error>> {
    unsafe {
        libr::Rf_initialize_R(arguments.len() as c_int, arguments.as_mut_ptr());
        libr::set(libr::R_Interactive, libr::Rboolean_TRUE);
        libr::set(libr::R_Consolefile, std::ptr::null_mut());
        libr::set(libr::R_Outputfile, std::ptr::null_mut());
        libr::set(libr::ptr_R_WriteConsole, None);
        libr::set(libr::ptr_R_WriteConsoleEx, Some(r_write_console));
        libr::set(libr::ptr_R_ReadConsole, Some(mcp_r_read_console));
        libr::set(libr::ptr_R_ShowMessage, Some(r_show_message));
        libr::set(libr::ptr_R_Busy, Some(r_busy));
    }
    // setup_Rmainloop installs R's signal handlers before profiles run. Its
    // event callback observes pending interrupts before R consumes them and
    // restores Console's SIGINT wakeup while retaining R's fatal diagnostics.
    let previous = unsafe { libr::get(libr::R_PolledEvents) };
    unsafe {
        libr::set(libr::R_PolledEvents, Some(startup_events));
        libr::setup_Rmainloop();
        if libr::get(libr::R_PolledEvents).is_some_and(|handler| {
            std::ptr::fn_addr_eq(handler, startup_events as unsafe extern "C-unwind" fn())
        }) {
            libr::set(libr::R_PolledEvents, previous);
        }
    }
    super::super::interrupt::reinstall()?;
    Ok(())
}

unsafe extern "C-unwind" fn startup_events() {
    if super::interrupt_pending() {
        super::super::r_integration::record_initialization_interrupt();
    }
    if let Err(error) = super::super::interrupt::reinstall() {
        super::record_worker_failure(error.to_string());
    }
}

impl Events {
    pub(super) fn load(
        library: &libloading::os::unix::Library,
        top_level_exec: TopLevelExec,
    ) -> Result<Self, Box<dyn Error>> {
        let check_activity = unsafe { *library.get::<CheckActivity>(b"R_checkActivity\0")? };
        let run_handlers = unsafe { *library.get::<RunHandlers>(b"R_runHandlers\0")? };
        let add_input_handler = unsafe { *library.get::<AddInputHandler>(b"addInputHandler\0")? };
        let remove_input_handler =
            unsafe { *library.get::<RemoveInputHandler>(b"removeInputHandler\0")? };
        let rg_wait_usec = unsafe { *library.get::<*mut c_int>(b"Rg_wait_usec\0")? as usize };
        Ok(Self {
            top_level_exec,
            check_activity,
            run_handlers,
            add_input_handler,
            remove_input_handler,
            rg_wait_usec,
        })
    }

    pub(super) fn install(self) -> Result<(), Box<dyn Error>> {
        R_EVENTS
            .set(self)
            .map_err(|_| io::Error::other("R event handlers were already initialized"))?;
        Ok(())
    }
}

pub(super) fn run_ready_handlers() {
    let events = R_EVENTS
        .get()
        .expect("R event handlers should be initialized");
    unsafe {
        mcp_r_run_ready_handlers(
            events.top_level_exec,
            events.check_activity,
            events.run_handlers,
            r_input_handlers(),
        );
    }
}

pub(in crate::worker) fn wait_for_activity(sideband_fd: c_int) -> Result<bool, String> {
    let events = R_EVENTS
        .get()
        .expect("R event handlers should be initialized");
    let mut wait_usec = unsafe { libr::get(libr::R_wait_usec) };
    let graphical_wait_usec = unsafe { *(events.rg_wait_usec as *const c_int) };
    if graphical_wait_usec > 0 && (wait_usec <= 0 || graphical_wait_usec < wait_usec) {
        wait_usec = graphical_wait_usec;
    }
    let status = unsafe {
        mcp_r_wait_for_activity(
            events.top_level_exec,
            events.add_input_handler,
            events.remove_input_handler,
            events.check_activity,
            libr::R_InputHandlers.cast(),
            sideband_fd,
            wait_usec,
        )
    };
    match status {
        0 => Ok(false),
        1 => Ok(true),
        _ => Err("R event wait failed".to_string()),
    }
}

fn r_input_handlers() -> *mut c_void {
    unsafe { libr::get(libr::R_InputHandlers).cast_mut() }
}
