use std::os::fd::{AsRawFd, RawFd};
use std::os::unix::net::UnixStream;

pub(crate) struct InputWatch([libc::pollfd; 2]);

impl InputWatch {
    pub(crate) fn new(completion: RawFd) -> Result<Self, String> {
        Ok(Self([
            libc::pollfd {
                fd: libc::STDIN_FILENO,
                events: libc::POLLRDHUP,
                revents: 0,
            },
            libc::pollfd {
                fd: completion,
                events: 0,
                revents: 0,
            },
        ]))
    }

    pub(crate) fn wait(mut self, completion: UnixStream) -> Result<(), String> {
        debug_assert_eq!(self.0[1].fd, completion.as_raw_fd());
        loop {
            // Request only peer closure: queued MCP input must neither wake
            // this observer repeatedly nor be consumed by startup discovery.
            let count = unsafe { libc::poll(self.0.as_mut_ptr(), self.0.len() as _, -1) };
            if count < 0 {
                let error = std::io::Error::last_os_error();
                if error.kind() == std::io::ErrorKind::Interrupted {
                    continue;
                }
                return Err(format!("failed to wait for MCP input closure: {error}"));
            }
            if self.0[0].revents != 0 {
                return Err("server startup cancelled because MCP input closed".to_string());
            }
            if self.0[1].revents != 0 {
                return Ok(());
            }
        }
    }
}
