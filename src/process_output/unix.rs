use super::Diagnostics;
use std::io::{self, PipeReader, Read};
use std::os::fd::AsRawFd;
use std::process::ChildStdout;

pub(crate) fn forward<T: Read + AsRawFd>(
    source: T,
    exited: PipeReader,
    output: Diagnostics,
) -> io::Result<()> {
    let mut output = output();
    let mut source = RelayOutput::new(source, exited);
    let result = (|| {
        let mut buffer = [0; 8192];
        loop {
            let count = source.read(&mut buffer)?;
            if count == 0 {
                return Ok(());
            }
            output(&buffer[..count]);
        }
    })();
    output(&[]);
    result
}

pub(crate) struct RelayOutput<T = ChildStdout> {
    stdout: T,
    exited: PipeReader,
    remaining: Option<usize>,
}

impl<T: Read + AsRawFd> RelayOutput<T> {
    pub(crate) fn new(stdout: T, exited: PipeReader) -> Self {
        Self {
            stdout,
            exited,
            remaining: None,
        }
    }

    fn wait(&mut self) -> io::Result<()> {
        let ready = crate::readiness::wait_for_io(
            self.stdout.as_raw_fd(),
            libc::POLLIN,
            Some(&self.exited),
        )?;
        if ready.cancelled {
            // The child is gone, but an unsupervised descendant may still own
            // stdout. Preserve everything already queued without waiting for
            // that descendant to close the stream or accepting more output.
            let mut queued: libc::c_int = 0;
            // SAFETY: stdout is an owned pipe and queued is writable storage.
            if unsafe { libc::ioctl(self.stdout.as_raw_fd(), libc::FIONREAD, &mut queued) } < 0 {
                return Err(io::Error::last_os_error());
            }
            self.remaining = Some(queued as usize);
        }
        Ok(())
    }
}

impl<T: Read + AsRawFd> Read for RelayOutput<T> {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        if buffer.is_empty() {
            return Ok(0);
        }
        if self.remaining.is_none() {
            self.wait()?;
        }
        let limit = self.remaining.map_or(buffer.len(), |n| n.min(buffer.len()));
        if limit == 0 {
            return Ok(0);
        }
        let count = self.stdout.read(&mut buffer[..limit])?;
        if let Some(remaining) = &mut self.remaining {
            *remaining -= count;
        }
        Ok(count)
    }
}
