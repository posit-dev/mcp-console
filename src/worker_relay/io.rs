use std::os::fd::AsRawFd;
use std::sync::{Arc, Mutex};

#[derive(Clone)]
pub(super) struct Cancellation(Arc<Mutex<Option<std::io::PipeWriter>>>);

impl Cancellation {
    pub(super) fn cancel(&self) {
        let mut writer = match self.0.lock() {
            Ok(writer) => writer,
            Err(poisoned) => poisoned.into_inner(),
        };
        drop(writer.take());
    }
}

pub(super) fn cancellation_pipe(
    description: &str,
) -> Result<(std::io::PipeReader, Cancellation), String> {
    let (reader, writer) = std::io::pipe()
        .map_err(|error| format!("failed to create {description} cancellation pipe: {error}"))?;
    Ok((reader, Cancellation(Arc::new(Mutex::new(Some(writer))))))
}

pub(super) fn set_nonblocking(descriptor: &impl AsRawFd) -> Result<(), String> {
    let descriptor = descriptor.as_raw_fd();
    // SAFETY: `descriptor` is an open pipe owned by the relay.
    let flags = unsafe { libc::fcntl(descriptor, libc::F_GETFL) };
    if flags < 0 {
        return Err(format!(
            "failed to read relay descriptor flags: {}",
            std::io::Error::last_os_error()
        ));
    }
    // SAFETY: this preserves existing flags and adds O_NONBLOCK.
    if unsafe { libc::fcntl(descriptor, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
        return Err(format!(
            "failed to configure relay descriptor: {}",
            std::io::Error::last_os_error()
        ));
    }
    Ok(())
}
