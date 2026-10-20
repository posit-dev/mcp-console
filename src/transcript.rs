//! Capture for one Console process. Prelude stays temporary until a received send
//! publishes it; publication preserves event timestamps and producer ownership.
use std::collections::BTreeMap;
use std::fs::{DirBuilder, File, OpenOptions};
use std::io::{BufWriter, Write};
#[cfg(unix)]
use std::os::unix::fs::{DirBuilderExt as _, OpenOptionsExt as _};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use base64::Engine as _;
use chrono::{DateTime, SecondsFormat, Utc};
use event::{Envelope, Event, Outcome, RecordedContent, RecordedResult};
use rmcp::{
    ErrorData,
    model::{
        CallToolRequestParams, CallToolResponse, CallToolResult, ContentBlock, RequestId,
        RequestMetaObject,
    },
};

use serde_json::Value;

mod event;

mod markdown;
mod output;

pub(crate) use output::{CellOutput, OutputRecord};

#[derive(Clone)]
pub(crate) struct Transcript(
    Arc<Mutex<TranscriptState>>,
    Arc<Mutex<ProtocolEvents>>,
    Arc<AtomicU64>,
);

#[derive(Default)]
struct ProtocolEvents {
    pending: Vec<(QueuedEvent, DateTime<Utc>, usize)>,
    // Include batches held by flush_protocol while journal I/O is blocked.
    retained_events: usize,
    retained_bytes: usize,
    failure: Option<String>,
    draining: bool,
}

// Lifecycle observations share the bounded, ordered drain with protocol events.
// Session identity changes only when an actual acquisition is applied.
#[derive(serde::Serialize)]
enum QueuedEvent {
    Event(Event<'static>),
    WorkerStarting,
    WorkerStarted { pid: u32, requirements: Value },
    WorkerReady,
    WorkerEnded { outcome: String },
}

impl QueuedEvent {
    fn append(self, active: &mut ActiveTranscript, at: DateTime<Utc>) -> Result<(), String> {
        match self {
            Self::Event(event) => active.append(event, at),
            Self::WorkerStarting => {
                active.starting_session = None;
                Ok(())
            }
            Self::WorkerStarted { pid, requirements } => {
                active.next_session_id += 1;
                let session_id = active.next_session_id;
                active.live_session = Some(session_id);
                active.starting_session = Some(session_id);
                active.append(
                    Event::WorkerStarted {
                        session_id,
                        pid,
                        requirements: &requirements,
                    },
                    at,
                )
            }
            Self::WorkerReady => {
                let session_id = active
                    .starting_session
                    .take()
                    .ok_or("worker ready without a started session")?;
                active.append(Event::WorkerReady { session_id }, at)
            }
            Self::WorkerEnded { outcome } => {
                let session_id = active
                    .live_session
                    .take()
                    .ok_or("worker outcome without a started session")?;
                active.append(
                    Event::WorkerEnded {
                        session_id,
                        outcome: &outcome,
                    },
                    at,
                )
            }
        }
    }
}

const MAX_PROTOCOL_EVENTS: usize = 1024;
const MAX_PROTOCOL_BYTES: usize = 8 * 1024 * 1024;

impl ProtocolEvents {
    fn stop(&mut self, error: String) {
        self.failure.get_or_insert(error);
        self.retained_events -= self.pending.len();
        self.retained_bytes -= self
            .pending
            .drain(..)
            .map(|(_, _, bytes)| bytes)
            .sum::<usize>();
    }
}

/// Measure serialized retention without allocating another copy of the payload.
#[derive(Default)]
struct ProtocolBytes(usize);

impl Write for ProtocolBytes {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.0 += bytes.len();
        Ok(bytes.len())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

struct TranscriptState {
    started_at: DateTime<Utc>,
    working_directory: Result<PathBuf, String>,
    dynamic_resolution: bool,
    python_preparation: bool,
    r_available: bool,
    startup_requirements: crate::worker_client::Declaration,
    active: Option<ActiveTranscript>,
    failure: Option<String>,
    metadata_known: bool,
}

struct ActiveTranscript {
    temporary: Option<tempfile::TempDir>,
    directory: PathBuf,
    public_directory: Arc<Mutex<PathBuf>>,
    files: BTreeMap<String, Arc<Mutex<CaptureFile>>>,
    writer: BufWriter<File>,
    projections: Option<markdown::Writers>,
    pending_projection_failure: Option<String>,
    run_id: String,
    sequence: u64,
    next_artifact_id: u64,
    next_session_id: u64,
    live_session: Option<u64>,
    starting_session: Option<u64>,
    finished: bool,
}

/// Output producers retain this writer while publication relocates its file.
struct CaptureFile {
    writer: File,
    path: PathBuf,
}

#[derive(Clone)]
pub(crate) struct Call {
    id: Option<u64>,
    result_images: Arc<Mutex<Option<Vec<Artifact>>>>,
}

#[derive(Clone)]
pub(crate) struct Artifact {
    id: u64,
    path: String,
    mime_type: String,
    public_directory: Arc<Mutex<PathBuf>>,
}

impl Transcript {
    #[cfg(test)]
    pub(crate) fn new(dynamic_resolution: bool) -> Self {
        Self::configured(
            std::env::current_dir(),
            dynamic_resolution,
            false,
            true,
            crate::worker_client::Declaration::default(),
        )
    }

    pub(crate) fn configured(
        working_directory: std::io::Result<PathBuf>,
        dynamic_resolution: bool,
        python_preparation: bool,
        r_available: bool,
        startup_requirements: crate::worker_client::Declaration,
    ) -> Self {
        Self(
            Arc::new(Mutex::new(TranscriptState {
                started_at: Utc::now(),
                working_directory: working_directory.map_err(|error| {
                    format!("failed to find the current working directory: {error}")
                }),
                dynamic_resolution,
                python_preparation,
                r_available,
                startup_requirements,
                active: None,
                failure: None,
                metadata_known: true,
            })),
            Arc::default(),
            Arc::default(),
        )
    }

    pub(crate) fn pending(working_directory: std::io::Result<PathBuf>) -> Self {
        let value = Self::configured(working_directory, false, false, false, Default::default());
        value.0.lock().expect("transcript lock").metadata_known = false;
        value
    }
    pub(crate) fn configure(&self, configured: Self) {
        let (configuration, _) = configured.lock();
        self.update(|state| {
            state.capture()?;
            state.dynamic_resolution = configuration.dynamic_resolution;
            state.python_preparation = configuration.python_preparation;
            state.r_available = configuration.r_available;
            state.startup_requirements = configuration.startup_requirements.clone();
            state.metadata_known = true;
            if let Some(projections) = state.active.as_mut().and_then(|a| a.projections.as_mut()) {
                projections.configure(
                    configuration.dynamic_resolution,
                    configuration.r_available,
                    &configuration.startup_requirements,
                );
            }
            state.capture()?.append(
                Event::EnvironmentDiscovered {
                    dynamic_resolution: configuration.dynamic_resolution,
                    python_preparation: configuration.python_preparation,
                    r_available: configuration.r_available,
                    startup_requirements: &configuration.startup_requirements,
                },
                Utc::now(),
            )
        });
    }
    pub(crate) fn activate(&self) {
        self.update(|state| {
            let working_directory = state.working_directory.clone()?;
            state.capture()?.publish(&working_directory)
        });
    }
    pub(crate) fn protocol(&self, direction: &'static str, message: Value) {
        self.queue_event(QueuedEvent::Event(Event::Protocol { direction, message }));
    }

    pub(crate) fn delivery(&self, request_id: &Value, call_id: Option<u64>, delivered: bool) {
        self.queue_event(QueuedEvent::Event(Event::Delivery {
            request_id: request_id.clone(),
            call_id,
            delivered,
        }));
    }

    fn queue_event(&self, event: QueuedEvent) {
        if self
            .1
            .lock()
            .expect("protocol capture lock")
            .failure
            .is_some()
        {
            return;
        }
        let at = Utc::now();
        let mut bytes = ProtocolBytes::default();
        let measured = serde_json::to_writer(&mut bytes, &event);
        let mut events = self.1.lock().expect("protocol capture lock");
        if events.failure.is_some() {
            return;
        }
        if let Err(error) = measured {
            events.stop(format!("protocol capture failed: {error}"));
        } else if events.retained_events == MAX_PROTOCOL_EVENTS
            || bytes.0 > MAX_PROTOCOL_BYTES - events.retained_bytes
        {
            // This lock never protects journal I/O. The drain reports the failure
            // after its current write settles; frame reception remains fail-open.
            events.stop(format!(
                "protocol capture backlog exceeded ({MAX_PROTOCOL_EVENTS} events or {MAX_PROTOCOL_BYTES} bytes)"
            ));
        } else {
            events.retained_events += 1;
            events.retained_bytes += bytes.0;
            events.pending.push((event, at, bytes.0));
        }
        if events.draining {
            return;
        }
        events.draining = true;
        drop(events);
        let recorder = self.clone();
        // Neither journal I/O nor the recorder mutex runs on MCP frame receipt.
        // One retained drain owns this queue, including cancelled deliveries.
        std::thread::spawn(move || {
            loop {
                recorder.update(|_| Ok(()));
                let mut events = recorder.1.lock().expect("protocol capture lock");
                if events.pending.is_empty() {
                    events.draining = false;
                    return;
                }
            }
        });
    }

    fn flush_protocol(&self, state: &mut TranscriptState) -> Result<(), String> {
        let pending = {
            let mut events = self.1.lock().expect("protocol capture lock");
            if let Some(error) = &events.failure {
                return Err(error.clone());
            }
            std::mem::take(&mut events.pending)
        };
        let count = pending.len();
        let bytes = pending.iter().map(|(_, _, bytes)| bytes).sum::<usize>();
        let result = (|| {
            for (event, at, _) in pending {
                event.append(state.capture()?, at)?;
            }
            Ok(())
        })();
        let mut events = self.1.lock().expect("protocol capture lock");
        events.retained_events -= count;
        events.retained_bytes -= bytes;
        result
    }

    pub(crate) fn requirements_selected(
        &self,
        call_id: Option<u64>,
        action: &str,
        snapshot: &serde_json::Value,
    ) {
        self.update(|state| {
            state.capture()?.append(
                Event::RequirementsSelected {
                    call_id,
                    action,
                    snapshot,
                },
                Utc::now(),
            )
        });
    }

    pub(crate) fn startup_failed(&self, message: &str) {
        self.update(|state| {
            let active = state.capture()?;
            let session_id = active.starting_session.take();
            active.append(
                Event::StartupFailed {
                    session_id,
                    message,
                },
                Utc::now(),
            )
        });
    }
    pub(crate) fn python_environment_accepted(&self, packages: &[String]) {
        self.update(|state| {
            state
                .capture()?
                .append(Event::PythonEnvironmentAccepted { packages }, Utc::now())
        });
    }

    pub(crate) fn requirements_accepted(&self, requirements: &Value) {
        self.update(|state| {
            state
                .capture()?
                .append(Event::RequirementsAccepted { requirements }, Utc::now())
        });
    }

    pub(crate) fn worker_starting(&self) {
        self.queue_event(QueuedEvent::WorkerStarting);
    }

    pub(crate) fn worker_started(&self, pid: u32, requirements: &Value) {
        self.queue_event(QueuedEvent::WorkerStarted {
            pid,
            requirements: requirements.clone(),
        });
    }

    pub(crate) fn worker_ready(&self) {
        self.queue_event(QueuedEvent::WorkerReady);
    }

    pub(crate) fn worker_ended(&self, outcome: &str) {
        self.queue_event(QueuedEvent::WorkerEnded {
            outcome: outcome.to_owned(),
        });
    }

    /// Reserve identity without waiting for the journal writer.
    pub(crate) fn reserve_call_id(&self) -> u64 {
        self.2
            .fetch_add(1, Ordering::Relaxed)
            .checked_add(1)
            .expect("recording call ID space exhausted")
    }

    pub(crate) fn begin(
        &self,
        call_id: u64,
        at: DateTime<Utc>,
        request_id: &RequestId,
        request_meta: &RequestMetaObject,
        request: &CallToolRequestParams,
    ) -> Call {
        self.activate();
        self.update(|state| {
            let mut request = request.clone();
            if !request_meta.is_empty() {
                request.meta = Some(request_meta.clone());
            }
            state.capture()?.append(
                Event::ToolCall {
                    call_id,
                    request_id,
                    request: &request,
                },
                at,
            )?;
            Ok(Call {
                id: Some(call_id),
                result_images: Arc::new(Mutex::new(None)),
            })
        })
        .unwrap_or_else(Call::unrecorded)
    }

    pub(crate) fn persist_image(
        &self,
        call_id: Option<u64>,
        data: &str,
        mime_type: &str,
    ) -> Result<Option<Artifact>, String> {
        let bytes = decode_image_data(data)?;
        Ok(self.persist_decoded_image(call_id, &bytes, mime_type))
    }

    pub(crate) fn persist_decoded_image(
        &self,
        call_id: Option<u64>,
        bytes: &[u8],
        mime_type: &str,
    ) -> Option<Artifact> {
        let call_id = call_id?;
        self.update(|state| {
            state
                .active()?
                .persist_image(Some(call_id), bytes, mime_type)
        })
    }

    pub(crate) fn persist_session_image(
        &self,
        data: &str,
        mime_type: &str,
    ) -> Result<Option<Artifact>, String> {
        let bytes = decode_image_data(data)?;
        Ok(self.update(|state| state.capture()?.persist_image(None, &bytes, mime_type)))
    }

    pub(crate) fn finish(&self, call: Call, response: &Result<CallToolResponse, ErrorData>) {
        let Some(call_id) = call.id else {
            return;
        };
        self.update(|state| {
            let images = call.take_result_images()?;
            state
                .active()?
                .finish(call_id, images, response, Utc::now())
        });
    }

    pub(crate) fn finalize(&self) {
        self.update(|state| {
            if let Some(active) = state.active.as_mut()
                && !active.finished
            {
                active.append(Event::RecordingEnded, Utc::now())?;
                active.finished = true;
            }
            Ok(())
        });
    }

    pub(crate) fn disable(&self, error: String) {
        let (mut state, _) = self.lock();
        let should_report = state.disable(error.clone());
        drop(state);
        self.1
            .lock()
            .expect("protocol capture lock")
            .stop(error.clone());
        if should_report {
            report_failure(&error);
        }
    }

    fn lock(&self) -> (std::sync::MutexGuard<'_, TranscriptState>, bool) {
        match self.0.lock() {
            Ok(state) => (state, false),
            Err(poisoned) => (poisoned.into_inner(), true),
        }
    }

    fn update<T>(
        &self,
        operation: impl FnOnce(&mut TranscriptState) -> Result<T, String>,
    ) -> Option<T> {
        let (mut state, poisoned) = self.lock();
        if let Some(error) = &state.failure {
            self.1
                .lock()
                .expect("protocol capture lock")
                .stop(error.clone());
            return None;
        }
        if poisoned {
            let error = "transcript lock poisoned".to_string();
            state.disable(error.clone());
            drop(state);
            self.1
                .lock()
                .expect("protocol capture lock")
                .stop(error.clone());
            report_failure(&error);
            return None;
        }
        let result = self
            .flush_protocol(&mut state)
            .and_then(|()| operation(&mut state))
            .and_then(|value| {
                self.flush_protocol(&mut state)?;
                Ok(value)
            });
        match result {
            Ok(value) => {
                let projection_failure = state
                    .active
                    .as_mut()
                    .and_then(ActiveTranscript::take_projection_failure);
                drop(state);
                if let Some(error) = projection_failure {
                    report_projection_failure(&error);
                }
                Some(value)
            }
            Err(error) => {
                state.disable(error.clone());
                drop(state);
                self.1
                    .lock()
                    .expect("protocol capture lock")
                    .stop(error.clone());
                report_failure(&error);
                None
            }
        }
    }
}

pub(crate) fn validate_image_data(data: &str) -> Result<(), String> {
    decode_image_data(data).map(drop)
}

pub(crate) fn decode_image_data(data: &str) -> Result<Vec<u8>, String> {
    base64::engine::general_purpose::STANDARD
        .decode(data)
        .map_err(|error| format!("worker returned invalid base64 image data: {error}"))
}

impl TranscriptState {
    fn capture(&mut self) -> Result<&mut ActiveTranscript, String> {
        if self.active.is_none() {
            let working_directory = self.working_directory.clone()?;
            self.active = Some(ActiveTranscript::create(
                &working_directory,
                self.started_at,
                self.dynamic_resolution,
                self.python_preparation,
                self.r_available,
                &self.startup_requirements,
                self.metadata_known,
            )?);
        }
        self.active()
    }

    fn active(&mut self) -> Result<&mut ActiveTranscript, String> {
        self.active
            .as_mut()
            .ok_or_else(|| "transcript was not materialized before recording output".to_string())
    }

    fn disable(&mut self, error: String) -> bool {
        if self.failure.is_some() {
            return false;
        }
        self.failure = Some(error);
        true
    }
}

impl ActiveTranscript {
    fn create(
        working_directory: &Path,
        started_at: DateTime<Utc>,
        dynamic_resolution: bool,
        python_preparation: bool,
        r_available: bool,
        startup_requirements: &crate::worker_client::Declaration,
        metadata_known: bool,
    ) -> Result<Self, String> {
        let working_directory_text = working_directory.to_string_lossy();
        // Keep incidental process-ID widths from shifting bounded output previews.
        let run_id = format!(
            "{}-{:010}",
            started_at.format("%Y%m%dT%H%M%S%.9fZ"),
            std::process::id()
        );
        let temporary = tempfile::tempdir()
            .map_err(|error| format!("failed to create temporary capture: {error}"))?;
        let directory = temporary.path().to_owned();
        create_private_directory(&directory.join("artifacts"), false).map_err(|error| {
            format!(
                "failed to create {}: {error}",
                directory.join("artifacts").display()
            )
        })?;
        create_private_directory(&directory.join("outputs"), false).map_err(|error| {
            format!(
                "failed to create {}: {error}",
                directory.join("outputs").display()
            )
        })?;
        let internal = directory.join("internal");
        create_private_directory(&internal, false)
            .map_err(|error| format!("failed to create {}: {error}", internal.display()))?;
        let journal = internal.join("events.jsonl");
        let writer = create_private_file(&journal)
            .map(BufWriter::new)
            .map_err(|error| format!("failed to create {}: {error}", journal.display()))?;
        let projections = (|| {
            let working_directory = working_directory.to_str().ok_or_else(|| {
                "Quarto execution root requires a UTF-8 working directory".to_string()
            })?;
            let markdown_path = directory.join("transcript.md");
            let markdown = create_private_file(&markdown_path).map_err(|error| {
                format!("failed to create {}: {error}", markdown_path.display())
            })?;
            let quarto_path = directory.join("transcript.qmd");
            let quarto = create_private_file(&quarto_path)
                .map_err(|error| format!("failed to create {}: {error}", quarto_path.display()))?;
            drop(quarto);
            Ok(markdown::Writers::new(
                markdown,
                quarto_path,
                working_directory,
                dynamic_resolution,
                r_available,
                startup_requirements,
            ))
        })();
        let (projections, pending_projection_failure) = match projections {
            Ok(projections) => (Some(projections), None),
            Err(error) => (None, Some(error)),
        };

        let mut transcript = Self {
            temporary: Some(temporary),
            directory,
            public_directory: Arc::default(),
            files: BTreeMap::new(),
            writer,
            projections,
            pending_projection_failure,
            run_id,
            sequence: 0,
            next_artifact_id: 0,
            next_session_id: 0,
            live_session: None,
            starting_session: None,
            finished: false,
        };
        transcript.append(
            Event::RecordingStarted {
                working_directory: &working_directory_text,
                dynamic_resolution: metadata_known.then_some(dynamic_resolution),
                python_preparation: metadata_known.then_some(python_preparation),
                startup_requirements: metadata_known.then_some(startup_requirements),
            },
            started_at,
        )?;
        Ok(transcript)
    }
}

impl Call {
    pub(crate) fn unrecorded() -> Self {
        Self {
            id: None,
            result_images: Arc::new(Mutex::new(None)),
        }
    }

    pub(crate) fn id(&self) -> Option<u64> {
        self.id
    }

    pub(crate) fn record_result_images(&self, images: Vec<Artifact>) -> Result<(), String> {
        let mut result_images = self
            .result_images
            .lock()
            .map_err(|_| "tool call artifact lock poisoned".to_string())?;
        if result_images.is_some() {
            return Err(format!(
                "tool call {} already retained result images",
                self.id.unwrap_or_default()
            ));
        }
        *result_images = Some(images);
        Ok(())
    }

    fn take_result_images(&self) -> Result<Vec<Artifact>, String> {
        Ok(self
            .result_images
            .lock()
            .map_err(|_| "tool call artifact lock poisoned".to_string())?
            .take()
            .unwrap_or_default())
    }
}

impl ActiveTranscript {
    fn publish(&mut self, working_directory: &Path) -> Result<(), String> {
        if self.temporary.is_none() {
            return Ok(());
        }
        let project_console = working_directory.join(".agents/console");
        let in_project = match std::fs::metadata(&project_console) {
            Ok(metadata) => metadata.is_dir(),
            Err(e)
                if matches!(
                    e.kind(),
                    std::io::ErrorKind::NotFound | std::io::ErrorKind::NotADirectory
                ) =>
            {
                false
            }
            Err(e) => return Err(format!("cannot inspect {}: {e}", project_console.display())),
        };
        let console = if in_project {
            project_console
        } else {
            crate::console_paths::home_console_directory()?.ok_or("HOME is not set")?
        };
        let sessions = console.join("sessions");
        let directory = sessions.join(&self.run_id);
        let public = if in_project {
            PathBuf::from(".agents/console/sessions").join(&self.run_id)
        } else {
            directory.clone()
        };
        if public.to_str().is_none() {
            return Err("recording path must be UTF-8".into());
        }
        create_private_directory(&sessions, true)
            .map_err(|e| format!("failed to create {}: {e}", sessions.display()))?;
        create_private_directory(&directory, false)
            .map_err(|e| format!("failed to create {}: {e}", directory.display()))?;
        self.writer.flush().map_err(|e| e.to_string())?;
        // The tape can be appending while the transport activates recording.
        // Hold each shared writer across its copy and relocation.
        for (relative, file) in &self.files {
            let mut file = file.lock().map_err(|_| "capture file lock poisoned")?;
            file.relocate(&directory.join(relative))?;
        }
        self.files.clear();
        copy_capture(&self.directory, &directory)?;
        self.writer = BufWriter::new(
            OpenOptions::new()
                .append(true)
                .open(directory.join("internal/events.jsonl"))
                .map_err(|e| e.to_string())?,
        );
        if let Some(projections) = &mut self.projections
            && let Err(error) = projections.relocate(&directory)
        {
            self.projections = None;
            self.pending_projection_failure = Some(error);
        }
        self.directory = directory;
        *self
            .public_directory
            .lock()
            .map_err(|_| "recording path lock poisoned")? = public;
        if let Some(temporary) = self.temporary.take() {
            temporary
                .close()
                .map_err(|error| format!("temporary capture cleanup failed: {error}"))?;
        }
        self.append(Event::RecordingActivated, Utc::now())
    }
    fn append(&mut self, event: Event<'_>, at: DateTime<Utc>) -> Result<(), String> {
        if self.finished {
            return Ok(());
        }
        let sequence = self.sequence + 1;
        let event = Envelope {
            event,
            run_id: &self.run_id,
            sequence,
            at: timestamp(at),
        };

        let mut record = serde_json::to_vec(&event)
            .map_err(|error| format!("failed to serialize transcript event: {error}"))?;
        record.push(b'\n');
        self.writer
            .write_all(&record)
            .and_then(|()| self.writer.flush())
            .map_err(|error| format!("failed to append transcript event: {error}"))?;
        self.sequence = sequence;
        if let Some(projections) = &mut self.projections
            && let Err(error) = projections.append(&event)
        {
            self.projections = None;
            self.pending_projection_failure = Some(error);
        }
        Ok(())
    }

    fn take_projection_failure(&mut self) -> Option<String> {
        self.pending_projection_failure.take()
    }

    fn finish(
        &mut self,
        call_id: u64,
        result_images: Vec<Artifact>,
        response: &Result<CallToolResponse, ErrorData>,
        at: DateTime<Utc>,
    ) -> Result<(), String> {
        let outcome = match response {
            Ok(CallToolResponse::Complete(result)) => Outcome::Result {
                result: Self::project_result(call_id, result_images, result)?,
            },
            Ok(_) => {
                return Err(
                    "console tool unexpectedly returned a non-final MCP response".to_string(),
                );
            }
            Err(error) => {
                if !result_images.is_empty() {
                    return Err("tool error unexpectedly retained result images".to_string());
                }
                Outcome::Error { error }
            }
        };
        self.append(Event::ToolResult { call_id, outcome }, at)
    }

    fn persist_image(
        &mut self,
        call_id: Option<u64>,
        bytes: &[u8],
        mime_type: &str,
    ) -> Result<Artifact, String> {
        let artifact_id = self.next_artifact_id + 1;
        let extension = match mime_type {
            "image/png" => "png",
            _ => "bin",
        };
        let owner = call_id.map_or_else(|| "session".into(), |id| format!("call-{id:06}"));
        let filename = format!("{owner}-image-{artifact_id:06}.{extension}");
        let relative_path = format!("artifacts/{filename}");
        write_new(&self.directory.join(&relative_path), bytes)?;
        self.append(
            Event::ArtifactCreated {
                artifact_id,
                call_id,
                path: &relative_path,
                mime_type,
                bytes: bytes.len(),
            },
            Utc::now(),
        )?;
        self.next_artifact_id = artifact_id;
        Ok(Artifact {
            id: artifact_id,
            public_directory: self.public_directory.clone(),
            path: relative_path,
            mime_type: mime_type.to_string(),
        })
    }

    fn project_result(
        call_id: u64,
        result_images: Vec<Artifact>,
        result: &CallToolResult,
    ) -> Result<RecordedResult<'_>, String> {
        let mut result_images = result_images.into_iter();
        let mut content = Vec::with_capacity(result.content.len());
        for item in &result.content {
            content.push(match item {
                ContentBlock::Text(text) => RecordedContent::Text(text),
                ContentBlock::Image(image) => {
                    let artifact = result_images.next().ok_or_else(|| {
                        format!("tool call {call_id} returned an image without a retained artifact")
                    })?;
                    if image.mime_type != artifact.mime_type {
                        return Err(format!(
                            "tool call {call_id} returned an image with a different MIME type than its artifact"
                        ));
                    }
                    RecordedContent::Image {
                        mime_type: &image.mime_type,
                        meta: image.meta.as_ref(),
                        annotations: image.annotations.as_ref(),
                        artifact_id: artifact.id,
                        path: artifact.path,
                    }
                }
                other => RecordedContent::Other(other),
            });
        }
        if result_images.next().is_some() {
            return Err(format!(
                "tool call {call_id} retained more artifacts than it returned"
            ));
        }
        Ok(RecordedResult {
            content,
            structured_content: result.structured_content.as_ref(),
            is_error: result.is_error,
            meta: result.meta.as_ref(),
        })
    }
}

fn timestamp(at: DateTime<Utc>) -> String {
    at.to_rfc3339_opts(SecondsFormat::Nanos, true)
}

fn report_failure(error: &str) {
    let _ = writeln!(
        std::io::stderr().lock(),
        "mcp-console: transcript recording disabled: {error}"
    );
}

fn report_projection_failure(error: &str) {
    let _ = writeln!(
        std::io::stderr().lock(),
        "mcp-console: transcript projections disabled: {error}"
    );
}

fn create_private_directory(path: &Path, recursive: bool) -> std::io::Result<()> {
    let mut builder = DirBuilder::new();
    builder.recursive(recursive);
    #[cfg(unix)]
    builder.mode(0o700);
    builder.create(path)
}

fn create_private_file(path: &Path) -> std::io::Result<File> {
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    options.mode(0o600);
    options.open(path)
}

fn write_new(path: &Path, bytes: &[u8]) -> Result<(), String> {
    let mut file = create_private_file(path)
        .map_err(|error| format!("failed to create {}: {error}", path.display()))?;
    file.write_all(bytes)
        .and_then(|()| file.flush())
        .map_err(|error| format!("failed to write {}: {error}", path.display()))
}

fn replace_private_file(path: &Path, bytes: &[u8]) -> Result<(), String> {
    let temporary = path.with_file_name(".transcript.qmd.tmp");
    if let Err(error) = write_new(&temporary, bytes) {
        let _ = std::fs::remove_file(&temporary);
        return Err(error);
    }
    if let Err(error) = std::fs::rename(&temporary, path) {
        let _ = std::fs::remove_file(&temporary);
        return Err(format!(
            "failed to replace {} from {}: {error}",
            path.display(),
            temporary.display()
        ));
    }
    Ok(())
}

impl CaptureFile {
    fn relocate(&mut self, destination: &Path) -> Result<(), String> {
        self.writer.flush().map_err(|error| error.to_string())?;
        create_private_directory(destination.parent().expect("capture parent"), true)
            .map_err(|error| error.to_string())?;
        std::fs::copy(&self.path, destination).map_err(|error| error.to_string())?;
        self.writer = OpenOptions::new()
            .append(true)
            .open(destination)
            .map_err(|error| error.to_string())?;
        self.path = destination.to_owned();
        Ok(())
    }
}

impl Artifact {
    pub(crate) fn public_path(&self) -> String {
        self.public_directory
            .lock()
            .expect("recording path lock")
            .join(&self.path)
            .display()
            .to_string()
    }
}
fn copy_capture(source: &Path, destination: &Path) -> Result<(), String> {
    for entry in std::fs::read_dir(source).map_err(|e| e.to_string())? {
        let entry = entry.map_err(|e| e.to_string())?;
        let target = destination.join(entry.file_name());
        if entry.file_type().map_err(|e| e.to_string())?.is_dir() {
            create_private_directory(&target, true).map_err(|e| e.to_string())?;
            copy_capture(&entry.path(), &target)?;
        } else if !target.exists() {
            std::fs::copy(entry.path(), target).map_err(|e| e.to_string())?;
        }
    }
    Ok(())
}
