//! Cancellable descriptor I/O for child processes.

use std::fs::File;
use std::io::{self, Read, Write};
use std::os::fd::{AsRawFd, FromRawFd, RawFd};

pub(crate) fn duplicate(descriptor: RawFd) -> Result<File, String> {
    let fd = unsafe { libc::fcntl(descriptor, libc::F_DUPFD_CLOEXEC, 3) };
    if fd < 0 {
        return Err(io::Error::last_os_error().to_string());
    }
    Ok(unsafe { File::from_raw_fd(fd) })
}

fn poll(descriptors: &[(RawFd, libc::c_short)]) -> Result<Vec<libc::c_short>, String> {
    let mut descriptors = descriptors
        .iter()
        .map(|&(fd, events)| libc::pollfd {
            fd,
            events,
            revents: 0,
        })
        .collect::<Vec<_>>();
    loop {
        let result = unsafe { libc::poll(descriptors.as_mut_ptr(), descriptors.len() as _, -1) };
        if result > 0 {
            return Ok(descriptors.iter().map(|event| event.revents).collect());
        }
        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::Interrupted {
            return Err(error.to_string());
        }
    }
}

pub(crate) struct Io<T> {
    inner: T,
    cancelled: Option<io::PipeReader>,
}

impl<T: AsRawFd> Io<T> {
    pub(crate) fn new(inner: T, cancelled: Option<io::PipeReader>) -> Result<Self, String> {
        let flags = unsafe { libc::fcntl(inner.as_raw_fd(), libc::F_GETFL) };
        if flags < 0
            || unsafe { libc::fcntl(inner.as_raw_fd(), libc::F_SETFL, flags | libc::O_NONBLOCK) }
                < 0
        {
            return Err(io::Error::last_os_error().to_string());
        }
        Ok(Self { inner, cancelled })
    }

    fn wait(&self, events: libc::c_short) -> io::Result<()> {
        let ready = poll(&[
            (self.inner.as_raw_fd(), events),
            (
                self.cancelled.as_ref().map_or(-1, AsRawFd::as_raw_fd),
                libc::POLLIN,
            ),
        ])
        .map_err(io::Error::other)?;
        if ready[1] != 0 {
            return Err(io::Error::other("process transfer cancelled"));
        }
        Ok(())
    }
}

impl<T: AsRawFd + Read> Read for Io<T> {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        loop {
            self.wait(libc::POLLIN)?;
            match self.inner.read(buffer) {
                Err(e)
                    if matches!(
                        e.kind(),
                        io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                    ) => {}
                result => return result,
            }
        }
    }
}

impl<T: AsRawFd + Write> Write for Io<T> {
    fn write(&mut self, buffer: &[u8]) -> io::Result<usize> {
        loop {
            // Check cancellation between partial writes without waiting for
            // output readiness. A full pipe still takes the write/EAGAIN path.
            if let Some(cancelled) = &self.cancelled {
                let mut event = libc::pollfd {
                    fd: cancelled.as_raw_fd(),
                    events: libc::POLLIN,
                    revents: 0,
                };
                if unsafe { libc::poll(&mut event, 1, 0) } < 0 {
                    let error = io::Error::last_os_error();
                    if error.kind() == io::ErrorKind::Interrupted {
                        continue;
                    }
                    return Err(error);
                }
                if event.revents != 0 {
                    return Err(io::Error::other("process transfer cancelled"));
                }
            }
            match self.inner.write(buffer) {
                Err(e)
                    if matches!(
                        e.kind(),
                        io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                    ) =>
                {
                    let ready = poll(&[
                        (self.inner.as_raw_fd(), libc::POLLOUT),
                        (
                            self.cancelled.as_ref().map_or(-1, AsRawFd::as_raw_fd),
                            libc::POLLIN,
                        ),
                    ])
                    .map_err(io::Error::other)?;
                    if ready[1] != 0 {
                        return Err(io::Error::other("process transfer cancelled"));
                    }
                }
                result => return result,
            }
        }
    }
    fn flush(&mut self) -> io::Result<()> {
        self.inner.flush()
    }
}
