use std::io::{self, PipeReader};
use std::os::fd::{AsRawFd, RawFd};

pub(crate) struct ReadyIo {
    pub(crate) stream: bool,
    pub(crate) cancelled: bool,
}

/// Blocks for stream or cancellation activity, retrying interrupted waits.
pub(crate) fn wait_for_io(
    descriptor: RawFd,
    events: libc::c_short,
    cancelled: Option<&PipeReader>,
) -> io::Result<ReadyIo> {
    loop {
        let mut descriptors = [
            libc::pollfd {
                fd: descriptor,
                events,
                revents: 0,
            },
            libc::pollfd {
                fd: cancelled.map_or(-1, AsRawFd::as_raw_fd),
                events: libc::POLLIN,
                revents: 0,
            },
        ];
        // SAFETY: all nonnegative descriptors stay open through this wait, and
        // the array pointer and length describe initialized storage exactly.
        if unsafe { libc::poll(descriptors.as_mut_ptr(), descriptors.len() as _, -1) } >= 0 {
            return Ok(ReadyIo {
                stream: descriptors[0].revents != 0,
                cancelled: descriptors[1].revents != 0,
            });
        }
        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::Interrupted {
            return Err(error);
        }
    }
}
