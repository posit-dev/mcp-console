use super::super::r_integration::{Integration, initialized};
use super::super::{core, interrupt};
use crate::worker_protocol::ServerMessage;

pub(in crate::worker) fn wait_for_message(r: &Integration) -> Result<ServerMessage, String> {
    loop {
        r.idle()?;
        let message = match core::queued_command()? {
            Some(message) => Some(message),
            None => receive_idle_command()?,
        };
        // The relay signals interrupts before forwarding the next command,
        // but their watcher may still be publishing native/Python state.
        interrupt::finish_windows_publication().map_err(|error| error.to_string())?;
        r.idle()?;
        if let Some(message) = core::take_worker_failure() {
            return Err(message);
        }
        if let Some(message) = message {
            return Ok(message);
        }
    }
}

fn receive_idle_command() -> Result<Option<ServerMessage>, String> {
    // Even buffered bytes can be an incomplete frame. The Windows idle read
    // must retain its interrupt wakeup until the whole command is available.
    core::worker_reader()?
        .receive_or_wake(interrupt::windows_wakeup(), initialized())
        .map_err(|error| format!("worker sideband read failed: {error}"))
}

pub(crate) fn observe_stdin_shutdown() -> Result<(), String> {
    if let Err(error) = crate::windows::available(unsafe { libc::get_osfhandle(0) } as _) {
        if error.raw_os_error() == Some(windows_sys::Win32::Foundation::ERROR_BROKEN_PIPE as i32) {
            core::mark_shutting_down();
        } else {
            return Err(error.to_string());
        }
    }
    Ok(())
}
