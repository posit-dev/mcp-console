use crate::windows::{Event, Pipe};
use std::io::{self, Read};

pub(crate) struct RelayOutput {
    stdout: Pipe,
    exited: Event,
    remaining: Option<usize>,
}
impl RelayOutput {
    pub(crate) fn new(stdout: Pipe, exited: Event) -> Self {
        Self {
            stdout: stdout.with_cancel(exited.clone()),
            exited,
            remaining: None,
        }
    }
}
impl Read for RelayOutput {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        if buffer.is_empty() {
            return Ok(0);
        }
        if self.remaining.is_none() && self.exited.wait(Some(std::time::Duration::ZERO))? {
            self.remaining = Some(self.stdout.available().unwrap_or(0));
            self.stdout.clear_cancel();
        }
        let limit = self.remaining.map_or(buffer.len(), |n| n.min(buffer.len()));
        if limit == 0 {
            return Ok(0);
        }
        match self.stdout.read(&mut buffer[..limit]) {
            Ok(count) => {
                if let Some(remaining) = &mut self.remaining {
                    *remaining -= count;
                }
                Ok(count)
            }
            Err(error) if error.kind() == io::ErrorKind::ConnectionAborted => {
                self.remaining = Some(self.stdout.available().unwrap_or(0));
                self.stdout.clear_cancel();
                self.read(buffer)
            }
            result => result,
        }
    }
}
