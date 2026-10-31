use std::io::Write;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use super::{CaptureFile, Owner, Transcript, queue::QueuedEvent};

pub(super) const MAX_CELL_OUTPUT_BYTES: u64 = 1024 * 1024 * 1024;

/// One private append-only file containing cell or session output.
///
/// The pending-output tape remains independently bounded for MCP projection.
/// This file retains console text and direct stdout/stderr bytes before that
/// projection discards overflow. Images remain separate transcript artifacts.
pub(crate) struct CellOutput {
    file: Arc<Mutex<CaptureFile>>,
    path: String,
    public_directory: Arc<Mutex<std::path::PathBuf>>,
    owner: Owner,
    transcript: Transcript,
    failed: bool,
    retained_bytes: u64,
    record: Arc<Mutex<OutputRecordState>>,
    discarded_bytes: u64,
    retention_limit_reported: bool,
}

#[derive(Clone, Copy, Default, PartialEq, serde::Serialize)]
pub(super) struct CellOutputSummary {
    pub(super) retained_bytes: u64,
    pub(super) inline_omitted_bytes: u64,
    pub(super) discarded_bytes: u64,
}

/// One response interval's accounting receipt, shared with delivery recovery.
#[derive(Clone)]
pub(crate) struct OutputRecord {
    state: Arc<Mutex<OutputRecordState>>,
    pub(crate) public_directory: Arc<Mutex<std::path::PathBuf>>,
    pub(crate) path: Arc<str>,
    accounted: Arc<AtomicU64>,
    inspection_only: bool,
}

struct OutputRecordState {
    transcript: Transcript,
    call_id: Option<u64>,
    owner: Owner,
    path: String,
    summary: CellOutputSummary,
    finished: bool,
    published: Option<CellOutputSummary>,
}

impl OutputRecordState {
    fn publish(&mut self) {
        if self.finished && self.published != Some(self.summary) {
            self.transcript
                .record_cell_output(self.call_id, self.owner, &self.path, self.summary);
            self.published = Some(self.summary);
        }
    }
}

impl Drop for OutputRecordState {
    fn drop(&mut self) {
        self.publish();
    }
}

impl OutputRecord {
    /// Keep paths and byte counts available to an inspector without letting
    /// preview compaction publish delivery accounting through the shared receipt.
    pub(crate) fn inspection_copy(&self) -> Self {
        Self {
            inspection_only: true,
            ..self.clone()
        }
    }

    pub(crate) fn note_inline_omission(&self, bytes: u64) {
        if self.inspection_only {
            return;
        }
        let previous = self.accounted.fetch_max(bytes, Ordering::Relaxed);
        if bytes > previous {
            self.state
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .summary
                .inline_omitted_bytes += bytes - previous;
        }
    }

    pub(crate) fn publish(&self) {
        if self.inspection_only {
            return;
        }
        self.state
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .publish();
    }
}

impl Transcript {
    pub(crate) fn create_session_output(&self) -> Result<Option<CellOutput>, String> {
        self.create_output(None)
    }
    pub(crate) fn create_cell_output(
        &self,
        call_id: Option<u64>,
        omit_from_projections: bool,
    ) -> Result<Option<CellOutput>, String> {
        if self.start_cell(call_id, omit_from_projections).is_none() {
            return Ok(None);
        }
        self.create_output(call_id)
    }
    fn create_output(&self, call_id: Option<u64>) -> Result<Option<CellOutput>, String> {
        let Some(owner) = self.owner() else {
            return Ok(None);
        };
        let path = owner.path(".txt");
        let file = self.3.create_file(&path)?;
        Ok(Some(CellOutput {
            file,
            path: path.clone(),
            public_directory: self.3.public_directory.clone(),
            owner,
            transcript: self.clone(),
            failed: false,
            record: Arc::new(Mutex::new(OutputRecordState {
                transcript: self.clone(),
                call_id,
                owner,
                path,
                summary: CellOutputSummary::default(),
                finished: false,
                published: None,
            })),
            retained_bytes: 0,
            discarded_bytes: 0,
            retention_limit_reported: false,
        }))
    }
    fn record_cell_output(
        &self,
        call_id: Option<u64>,
        owner: Owner,
        path: &str,
        summary: CellOutputSummary,
    ) {
        self.queue_event(QueuedEvent::OutputSummary {
            call_id,
            owner,
            path: path.to_owned(),
            summary,
        });
    }
}

impl CellOutput {
    pub(crate) fn record(&self) -> OutputRecord {
        OutputRecord {
            state: self.record.clone(),
            public_directory: self.public_directory.clone(),
            path: Arc::from(self.path.as_str()),
            accounted: Arc::new(AtomicU64::new(0)),
            inspection_only: false,
        }
    }

    pub(crate) fn retained_bytes(&self) -> u64 {
        self.retained_bytes
    }
    pub(crate) fn discarded_bytes(&self) -> u64 {
        self.discarded_bytes
    }
    /// Appends bytes while the retention limit permits it.
    ///
    /// Returns the retained prefix length and a server-owned notice to publish after
    /// the corresponding ordinary output event. Further writes are still
    /// drained from the worker even after persistence stops.
    pub(crate) fn append(&mut self, bytes: &[u8]) -> (usize, Option<String>) {
        if bytes.is_empty() {
            return (0, None);
        }
        if self.failed {
            self.discarded_bytes = self.discarded_bytes.saturating_add(bytes.len() as u64);
            return (0, None);
        }
        let remaining = MAX_CELL_OUTPUT_BYTES.saturating_sub(self.retained_bytes);
        let retained = bytes
            .len()
            .min(usize::try_from(remaining).unwrap_or(usize::MAX));
        let offset = self.retained_bytes;
        let result = {
            let mut file = self.file.lock().expect("capture file lock");
            file.writer
                .as_mut()
                .expect("open output interval")
                .write_all(&bytes[..retained])
        };
        if let Err(error) = result {
            let observed = std::fs::metadata(&self.file.lock().expect("capture file lock").path)
                .map(|m| m.len())
                .unwrap_or(offset);
            let appended = observed.saturating_sub(offset) as usize;
            self.retained_bytes = observed;
            self.discarded_bytes += (bytes.len() - appended) as u64;
            self.failed = true;
            self.text_event(offset, appended, bytes.len() - appended);
            return (
                appended,
                Some(format!(
                    "cell output file {} (relative to recording) stopped after {} retained bytes: {error}; later text is not retained in this file",
                    self.path, observed
                )),
            );
        }
        self.text_event(offset, retained, bytes.len() - retained);
        self.retained_bytes = self.retained_bytes.saturating_add(retained as u64);

        let discarded = bytes.len() - retained;
        if discarded == 0 {
            return (retained, None);
        }
        self.discarded_bytes = self.discarded_bytes.saturating_add(discarded as u64);
        if self.retention_limit_reported {
            return (retained, None);
        }
        self.retention_limit_reported = true;
        (
            retained,
            Some(format!(
                "cell output retention limit reached at {MAX_CELL_OUTPUT_BYTES} bytes for {} (relative to recording); later text is not retained in this file",
                self.path
            )),
        )
    }

    fn text_event(&self, offset: u64, bytes: usize, discarded_bytes: usize) {
        self.transcript.queue_event(QueuedEvent::OutputText {
            owner: self.owner,
            path: self.path.clone(),
            offset,
            bytes,
            discarded_bytes,
        });
    }
    pub(crate) fn flush(&mut self) -> Option<String> {
        if self.failed {
            return None;
        }
        let result = self
            .file
            .lock()
            .expect("capture file lock")
            .writer
            .as_mut()
            .expect("open output interval")
            .flush();
        if let Err(error) = result {
            self.failed = true;
            return Some(format!(
                "cell output file {} (relative to recording) stopped after {} retained bytes: {error}; later text is not retained in this file",
                self.path, self.retained_bytes
            ));
        }
        None
    }
    pub(crate) fn finish(mut self) -> Option<String> {
        let notice = self.flush();
        let permission_error = self.file.lock().expect("capture file lock").finish().err();
        let mut record = self
            .record
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        record.summary.retained_bytes = self.retained_bytes;
        record.summary.discarded_bytes = self.discarded_bytes;
        record.finished = true;
        notice.or(permission_error)
    }
}
