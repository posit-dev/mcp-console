use std::io;
use std::os::fd::{AsRawFd, RawFd};

use super::super::core;
use super::super::r_integration::Integration;
use crate::worker_protocol::ServerMessage;

pub(in crate::worker) enum CommandReadiness {
    Ready(ServerMessage),
    Waiting(RawFd),
}

pub(in crate::worker) fn next_command() -> Result<CommandReadiness, String> {
    if let Some(message) = core::queued_command()? {
        return Ok(CommandReadiness::Ready(message));
    }
    let (buffered, descriptor) = {
        let reader = core::worker_reader()?;
        (reader.has_buffered_data(), reader.as_raw_fd())
    };
    if buffered {
        core::receive_server_message().map(CommandReadiness::Ready)
    } else {
        Ok(CommandReadiness::Waiting(descriptor))
    }
}

pub(in crate::worker) fn wait_for_message(r: &Integration) -> Result<ServerMessage, String> {
    loop {
        let sideband_fd = match next_command()? {
            CommandReadiness::Ready(message) => return Ok(message),
            CommandReadiness::Waiting(descriptor) => descriptor,
        };
        // R activity must service callbacks before the next wait. The
        // native wait also wakes for interrupts and input shutdown.
        if r.wait_for_activity(sideband_fd)? {
            return core::receive_server_message();
        }

        r.idle()?;
        if let Some(message) = core::take_worker_failure() {
            return Err(message);
        }
    }
}

pub(crate) fn observe_stdin_shutdown() -> Result<(), String> {
    let mut event = libc::pollfd {
        fd: libc::STDIN_FILENO,
        events: libc::POLLIN,
        revents: 0,
    };
    loop {
        let result = unsafe { libc::poll(&mut event, 1, 0) };
        if result >= 0 {
            if event.revents & libc::POLLHUP != 0 {
                core::mark_shutting_down();
            }
            return Ok(());
        }
        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::Interrupted {
            return Err(format!("worker stdin readiness check failed: {error}"));
        }
    }
}
