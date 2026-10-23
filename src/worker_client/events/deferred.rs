//! Preserve suspended sideband order without retaining the stream in memory.

use std::fs::File;
use std::io::{self, BufRead as _, BufReader, Seek as _, SeekFrom, Write as _};

use crate::relay_protocol::RelayEvent;

const MAX_SPOOL_BYTES: u64 = 16 * 1024 * 1024;

#[derive(Default)]
pub(super) struct DeferredEvents {
    spool: Option<Spool>,
}

struct Spool {
    file: File,
    read: u64,
    written: u64,
}

impl DeferredEvents {
    pub(super) fn is_empty(&self) -> bool {
        self.spool.is_none()
    }

    pub(super) fn push(&mut self, event: &RelayEvent) -> Result<(), String> {
        if self.spool.is_none() {
            self.spool = Some(Spool::create().map_err(spool_error)?);
        }
        let spool = self.spool.as_mut().expect("deferred event spool");
        spool
            .file
            .seek(SeekFrom::Start(spool.written))
            .map_err(spool_error)?;
        serde_json::to_writer(&mut *spool, event).map_err(spool_error)?;
        spool.write_all(b"\n").map_err(spool_error)?;
        Ok(())
    }

    pub(super) fn pop(&mut self) -> Result<Option<RelayEvent>, String> {
        let Some(spool) = self.spool.as_mut() else {
            return Ok(None);
        };
        spool
            .file
            .seek(SeekFrom::Start(spool.read))
            .map_err(spool_error)?;
        let mut line = String::new();
        let bytes = BufReader::new(&mut spool.file)
            .read_line(&mut line)
            .map_err(spool_error)?;
        if bytes == 0 {
            return Err("deferred bootstrap spool ended before its final event".into());
        }
        let event = serde_json::from_str(&line).map_err(spool_error)?;
        spool.read += bytes as u64;
        if spool.read == spool.written {
            self.clear();
        }
        Ok(Some(event))
    }

    pub(super) fn clear(&mut self) {
        self.spool = None;
    }
}

impl Spool {
    fn create() -> std::io::Result<Self> {
        Ok(Self {
            // The OS removes this private spool when its last handle closes,
            // including controller exit without running Rust destructors.
            file: tempfile::tempfile()?,
            read: 0,
            written: 0,
        })
    }
}

impl io::Write for Spool {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if bytes.len() as u64 > MAX_SPOOL_BYTES - self.written {
            return Err(io::Error::other(
                "deferred bootstrap retention exceeds 16 MiB",
            ));
        }
        let written = self.file.write(bytes)?;
        self.written += written as u64;
        Ok(written)
    }

    fn flush(&mut self) -> io::Result<()> {
        self.file.flush()
    }
}

fn spool_error(error: impl std::fmt::Display) -> String {
    format!("deferred bootstrap spool failed: {error}")
}
