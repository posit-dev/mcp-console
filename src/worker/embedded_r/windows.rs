//! Windows R startup and message events. R calls stay on the coordinator thread.

use std::error::Error;
use std::ffi::{CStr, CString, c_char, c_int};
use std::io;
use std::sync::OnceLock;

use super::{TopLevelExec, mcp_r_read_console, r_busy, r_show_message, r_write_console};

static R_EVENTS: OnceLock<Events> = OnceLock::new();
type ProcessEvents = unsafe extern "C-unwind" fn();

pub(super) struct Events {
    top_level_exec: TopLevelExec,
    process_events: ProcessEvents,
}

unsafe extern "C" {
    fn mcp_r_run_windows_events(top_level_exec: TopLevelExec, process_events: ProcessEvents);
}

impl Events {
    pub(super) fn load(
        library: &libloading::os::windows::Library,
        top_level_exec: TopLevelExec,
    ) -> Result<Self, Box<dyn Error>> {
        Ok(Self {
            top_level_exec,
            process_events: unsafe { *library.get::<ProcessEvents>(b"R_ProcessEvents\0")? },
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
        mcp_r_run_windows_events(events.top_level_exec, events.process_events);
    }
}

pub(super) extern "C-unwind" fn process_events() {
    super::super::interrupt::deliver_windows_r_interrupt();
}

pub(super) fn initialize_r(
    r_home: &std::path::Path,
    arguments: &mut [*mut c_char],
) -> Result<(), Box<dyn Error>> {
    use std::mem::MaybeUninit;
    static STARTUP_PATHS: OnceLock<(CString, CString)> = OnceLock::new();

    let r_home = CString::new(r_home.to_string_lossy().as_bytes())?;
    let user_home = ["R_USER", "HOME"]
        .into_iter()
        .filter_map(std::env::var_os)
        .find(|value| !value.is_empty())
        .map(std::path::PathBuf::from)
        .or_else(std::env::home_dir)
        .ok_or_else(|| io::Error::other("cannot determine R user directory"))?;
    let user_home = CString::new(user_home.to_string_lossy().as_bytes())?;
    // Older Windows R versions retain startup path pointers in R_SetParams.
    // R lives until worker exit, so its backing strings must do the same.
    STARTUP_PATHS
        .set((r_home, user_home))
        .map_err(|_| io::Error::other("R startup paths were already initialized"))?;
    let (r_home, user_home) = STARTUP_PATHS.get().expect("R startup paths initialized");
    let library = libloading::os::windows::Library::open_already_loaded("R.dll")?;
    let set_arguments = unsafe {
        *library.get::<unsafe extern "C-unwind" fn(c_int, *mut *mut c_char)>(
            b"R_set_command_line_arguments\0",
        )?
    };
    unsafe {
        libr::set(libr::R_SignalHandlers, 0);
        // cmdlineoptions reads environment files before common option parsing,
        // so it must see --vanilla. Windows expresses interactive embedding in
        // Rstart rather than accepting the Unix --interactive flag.
        let mut native_arguments = arguments
            .iter()
            .copied()
            .filter(|argument| CStr::from_ptr(*argument).to_bytes() != b"--interactive")
            .collect::<Vec<_>>();
        libr::cmdlineoptions(
            native_arguments.len() as c_int,
            native_arguments.as_mut_ptr(),
        );
        // Preserve the full interactive identity before common option parsing
        // mutates the original argument array.
        set_arguments(arguments.len() as c_int, arguments.as_mut_ptr());
        let mut params = MaybeUninit::<libr::structRstart>::uninit();
        libr::R_DefParamsEx(params.as_mut_ptr(), 0);
        let mut params = params.assume_init();
        let mut count = arguments.len() as c_int;
        libr::R_common_command_line(&mut count, arguments.as_mut_ptr(), &mut params);
        // R's cmdlineoptions has already processed or suppressed environment
        // files. Installing embedding callbacks must not process them twice.
        params.set_NoRenviron(libr::Rboolean_TRUE);
        params.R_Interactive = 1;
        params.CharacterMode = libr::UImode_RGui;
        // Console transports plain UTF-8, not RGui's marked UTF-8 spans.
        params.EmitEmbeddedUTF8 = libr::Rboolean_FALSE;
        params.rhome = r_home.as_ptr().cast_mut();
        params.home = user_home.as_ptr().cast_mut();
        params.WriteConsole = None;
        params.WriteConsoleEx = Some(r_write_console);
        params.ReadConsole = Some(mcp_r_read_console);
        params.ShowMessage = Some(r_show_message);
        params.Busy = Some(r_busy);
        params.CallBack = Some(process_events);
        libr::R_SetParams(&mut params);
        libr::graphapp::GA_initapp(0, std::ptr::null_mut());
        libr::readconsolecfg();
    }
    unsafe {
        libr::setup_Rmainloop();
    }
    Ok(())
}
