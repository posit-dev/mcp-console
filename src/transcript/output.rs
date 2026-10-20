use std::io::Write;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use super::{CaptureFile, Event, Transcript, create_private_file};
use chrono::Utc;
use std::path::PathBuf;

pub(super) const MAX_CELL_OUTPUT_BYTES: u64 = 1024 * 1024 * 1024;

/// One private append-only file containing cell or session output.
///
/// The pending-output tape remains independently bounded for MCP projection.
/// This file retains console text and direct stdout/stderr bytes before that
/// projection discards overflow. Images remain separate transcript artifacts.
pub(crate) struct CellOutput {
    writer: Option<Arc<Mutex<CaptureFile>>>,
    public_directory: Arc<Mutex<PathBuf>>,
    path: String,
    retained_bytes: u64,
    record: Arc<Mutex<OutputRecordState>>,
    discarded_bytes: u64,
    retention_limit_reported: bool,
}

#[derive(Clone, Copy, Default, PartialEq)]
struct CellOutputSummary {
    retained_bytes: u64,
    inline_omitted_bytes: u64,
    discarded_bytes: u64,
}

/// One response interval's accounting receipt, shared with delivery recovery.
#[derive(Clone)]
pub(crate) struct OutputRecord {
    state: Arc<Mutex<OutputRecordState>>,
    public_directory: Arc<Mutex<PathBuf>>,
    path: Arc<str>,
    accounted: Arc<AtomicU64>,
}

struct OutputRecordState {
    transcript: Transcript,
    call_id: Option<u64>,
    path: String,
    summary: CellOutputSummary,
    finished: bool,
    published: Option<CellOutputSummary>,
}

impl OutputRecordState {
    fn publish(&mut self) {
        if self.finished && self.published != Some(self.summary) {
            self.transcript
                .record_cell_output(self.call_id, &self.path, self.summary);
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
    pub(crate) fn public_path(&self) -> Option<String> {
        let directory = self.public_directory.lock().expect("recording path lock");
        (!directory.as_os_str().is_empty())
            .then(|| directory.join(self.path.as_ref()).display().to_string())
    }

    pub(crate) fn note_inline_omission(&self, bytes: u64) {
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
        self.state
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .publish();
    }
}

impl Transcript {
    pub(crate) fn create_session_output(&self) -> Result<Option<CellOutput>, String> {
        let result = self.create_output(None);
        if let Err(error) = &result {
            self.disable(error.clone());
        }
        result
    }

    pub(crate) fn create_cell_output(
        &self,
        call_id: Option<u64>,
    ) -> Result<Option<CellOutput>, String> {
        let Some(call_id) = call_id else {
            return Ok(None);
        };
        self.create_output(Some(call_id))
    }

    fn create_output(&self, call_id: Option<u64>) -> Result<Option<CellOutput>, String> {
        let (mut state, poisoned) = self.lock();
        if poisoned {
            return Err("transcript lock poisoned while creating cell output".to_string());
        }
        if state.failure.is_some() {
            return Ok(None);
        }
        let active = state.capture()?;
        let filename =
            call_id.map_or_else(|| "session.log".into(), |id| format!("call-{id:06}.log"));
        let relative_path = format!("outputs/{filename}");
        let file_path = active.directory.join(&relative_path);
        let public_directory = active.public_directory.clone();
        let public_path = public_directory
            .lock()
            .expect("recording path lock")
            .join(&relative_path)
            .display()
            .to_string();
        let writer = create_private_file(&file_path)
            .map_err(|error| format!("failed to create {public_path}: {error}"))?;
        let writer = Arc::new(Mutex::new(CaptureFile {
            writer,
            path: file_path,
        }));
        if active.temporary.is_some() {
            active.files.insert(relative_path.clone(), writer.clone());
        }
        Ok(Some(CellOutput {
            writer: Some(writer),
            record: Arc::new(Mutex::new(OutputRecordState {
                transcript: self.clone(),
                call_id,
                path: relative_path.clone(),
                summary: CellOutputSummary::default(),
                finished: false,
                published: None,
            })),
            public_directory,
            path: relative_path,
            retained_bytes: 0,
            discarded_bytes: 0,
            retention_limit_reported: false,
        }))
    }

    fn record_cell_output(&self, call_id: Option<u64>, path: &str, summary: CellOutputSummary) {
        self.update(|state| {
            let event = match call_id {
                Some(call_id) => Event::CellOutput {
                    call_id,
                    path,
                    retained_bytes: summary.retained_bytes,
                    inline_omitted_bytes: summary.inline_omitted_bytes,
                    discarded_bytes: summary.discarded_bytes,
                    retention_limit_bytes: MAX_CELL_OUTPUT_BYTES,
                },
                None => Event::SessionOutput {
                    path,
                    retained_bytes: summary.retained_bytes,
                    inline_omitted_bytes: summary.inline_omitted_bytes,
                    discarded_bytes: summary.discarded_bytes,
                    retention_limit_bytes: MAX_CELL_OUTPUT_BYTES,
                },
            };
            state.active()?.append(event, Utc::now())
        });
    }
}

impl CellOutput {
    fn public_path(&self) -> String {
        self.public_directory
            .lock()
            .expect("recording path lock")
            .join(&self.path)
            .display()
            .to_string()
    }

    pub(crate) fn record(&self) -> OutputRecord {
        OutputRecord {
            state: self.record.clone(),
            public_directory: self.public_directory.clone(),
            path: Arc::from(self.path.as_str()),
            accounted: Arc::new(AtomicU64::new(0)),
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
        if self.writer.is_none() {
            self.discarded_bytes = self.discarded_bytes.saturating_add(bytes.len() as u64);
            return (0, None);
        }

        let remaining = MAX_CELL_OUTPUT_BYTES.saturating_sub(self.retained_bytes);
        let retained = bytes
            .len()
            .min(usize::try_from(remaining).unwrap_or(usize::MAX));
        let write_result = (retained > 0).then(|| {
            self.writer
                .as_mut()
                .expect("cell output writer presence was checked")
                .lock()
                .expect("capture file lock")
                .writer
                .write_all(&bytes[..retained])
        });
        if let Some(Err(error)) = write_result {
            let observed = self
                .writer
                .as_ref()
                .expect("failed cell output writer must remain available for inspection")
                .lock()
                .expect("capture file lock")
                .writer
                .metadata()
                .map(|metadata| metadata.len())
                .unwrap_or(self.retained_bytes);
            let seen = self
                .retained_bytes
                .saturating_add(self.discarded_bytes)
                .saturating_add(bytes.len() as u64);
            let appended = (observed - self.retained_bytes) as usize;
            self.retained_bytes = observed;
            self.discarded_bytes = seen.saturating_sub(observed);
            self.writer = None;
            return (
                appended,
                Some(format!(
                    "cell output file {} stopped after {} retained bytes: {error}; later text is not retained in this file",
                    self.public_path(),
                    self.retained_bytes
                )),
            );
        }
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
                "cell output retention limit reached at {MAX_CELL_OUTPUT_BYTES} bytes for {}; later text is not retained in this file",
                self.public_path()
            )),
        )
    }

    pub(crate) fn flush(&mut self) -> Option<String> {
        let writer = self.writer.as_mut()?;
        let result = writer.lock().expect("capture file lock").writer.flush();
        if let Err(error) = result {
            self.writer = None;
            return Some(format!(
                "cell output file {} stopped after {} retained bytes: {error}; later text is not retained in this file",
                self.public_path(),
                self.retained_bytes
            ));
        }
        None
    }

    pub(crate) fn finish(mut self) -> Option<String> {
        let notice = self.flush();
        self.writer = None;
        let mut record = self
            .record
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        record.summary.retained_bytes = self.retained_bytes;
        record.summary.discarded_bytes = self.discarded_bytes;
        record.finished = true;
        notice
    }
}
