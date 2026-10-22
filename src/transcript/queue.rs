//! The existing bounded drain owns journal I/O. Observed identities never need
//! its writer mutex, including during worker retirement and response assembly.
use super::{ActiveTranscript, Event, Interval, Owner, ProcessOutput, Transcript, output};
use chrono::{DateTime, Utc};
use serde::Serialize;
use serde_json::Value;
use std::io::Write;

#[derive(Default)]
pub(super) struct ProtocolEvents {
    pending: Vec<(QueuedEvent, DateTime<Utc>, usize)>,
    // Include batches held by flush_protocol while journal I/O is blocked.
    retained_events: usize,
    retained_bytes: usize,
    pub(super) failure: Option<String>,
    draining: bool,
    next_session_id: u64,
    owner: Option<Owner>,
    cell_call: Option<u64>,
    starting_session: Option<u64>,
    image_number: u64,
    image_bytes: usize,
    image_metadata_bytes: usize,
    next_artifact_id: u64,
    process_output_bytes: u64,
}

#[derive(Serialize)]
pub(super) enum QueuedEvent {
    Event(Event<'static>),
    WorkerStarting,
    AssociateWaitingCall {
        call_id: u64,
        session_id: u64,
    },
    WorkerStarted {
        session_id: u64,
        pid: u32,
        requirements: Value,
    },
    WorkerReady {
        session_id: u64,
    },
    WorkerEnded {
        session_id: u64,
        outcome: String,
    },
    CellStarted {
        call_id: u64,
        owner: Owner,
    },
    CellEnded {
        call_id: u64,
        owner: Owner,
        completion_confirmed: bool,
    },
    OutputText {
        owner: Owner,
        path: String,
        offset: u64,
        bytes: usize,
        discarded_bytes: usize,
    },
    ImageOmitted {
        owner: Option<Owner>,
        encoded_bytes: usize,
        mime_type: String,
    },
    ImageRetained {
        owner: Owner,
        artifact_id: u64,
        call_id: Option<u64>,
        path: String,
        mime_type: String,
        bytes: usize,
    },
    OutputSummary {
        call_id: Option<u64>,
        owner: Owner,
        path: String,
        summary: output::CellOutputSummary,
    },
    ProcessOutput {
        data: String,
        discarded_bytes: usize,
    },
    StartupFailed {
        session_id: Option<u64>,
        message: String,
    },
    PythonEnvironmentAccepted {
        packages: Vec<String>,
    },
    RequirementsAccepted {
        requirements: Value,
    },
    RequirementsSelected {
        call_id: Option<u64>,
        action: String,
        snapshot: Value,
    },
}

impl QueuedEvent {
    fn append(self, active: &mut ActiveTranscript, at: DateTime<Utc>) -> Result<(), String> {
        let event = match &self {
            Self::Event(_) => {
                let Self::Event(event) = self else {
                    unreachable!()
                };
                return active.append(event, at);
            }
            Self::WorkerStarting => return Ok(()),
            Self::AssociateWaitingCall {
                call_id,
                session_id,
            } => {
                // Replacement readiness can arrive after a failed source and
                // its result have both settled. Never associate that retired
                // call with the replacement worker.
                let Some(call) = active.call_sessions.get(call_id) else {
                    return Ok(());
                };
                if call.session_id.is_some() {
                    return Ok(());
                }
                Event::SourceAssociated {
                    call_id: *call_id,
                    session_id: *session_id,
                }
            }
            Self::WorkerStarted {
                session_id,
                pid,
                requirements,
            } => Event::WorkerStarted {
                session_id: *session_id,
                pid: *pid,
                requirements,
            },
            Self::WorkerReady { session_id } => Event::WorkerReady {
                session_id: *session_id,
            },
            Self::WorkerEnded {
                session_id,
                outcome,
            } => Event::WorkerEnded {
                session_id: *session_id,
                outcome,
            },
            Self::CellStarted { call_id, owner } => Event::CellStarted {
                call_id: *call_id,
                owner: *owner,
            },
            Self::CellEnded {
                call_id,
                owner,
                completion_confirmed,
            } => Event::CellEnded {
                call_id: *call_id,
                owner: *owner,
                completion_confirmed: *completion_confirmed,
            },
            Self::OutputText {
                owner,
                path,
                offset,
                bytes,
                discarded_bytes,
            } => Event::OutputText {
                owner: *owner,
                path,
                offset: *offset,
                bytes: *bytes,
                discarded_bytes: *discarded_bytes,
            },
            Self::ImageOmitted {
                owner,
                encoded_bytes,
                mime_type,
            } => Event::ImageOmitted {
                owner: *owner,
                encoded_bytes: *encoded_bytes,
                mime_type,
            },
            Self::ImageRetained {
                owner,
                artifact_id,
                call_id,
                path,
                mime_type,
                bytes,
            } => Event::ImageRetained {
                owner: *owner,
                artifact_id: *artifact_id,
                call_id: *call_id,
                path,
                mime_type,
                bytes: *bytes,
            },
            Self::OutputSummary {
                call_id,
                owner,
                path,
                summary,
            } => match call_id {
                Some(call_id) => Event::CellOutput {
                    owner: *owner,
                    call_id: *call_id,
                    path,
                    retained_bytes: summary.retained_bytes,
                    inline_omitted_bytes: summary.inline_omitted_bytes,
                    discarded_bytes: summary.discarded_bytes,
                    retention_limit_bytes: output::MAX_CELL_OUTPUT_BYTES,
                },
                None => Event::SessionOutput {
                    owner: *owner,
                    path,
                    retained_bytes: summary.retained_bytes,
                    inline_omitted_bytes: summary.inline_omitted_bytes,
                    discarded_bytes: summary.discarded_bytes,
                    retention_limit_bytes: output::MAX_CELL_OUTPUT_BYTES,
                },
            },
            Self::ProcessOutput {
                data,
                discarded_bytes,
            } => Event::ProcessOutput {
                data,
                discarded_bytes: *discarded_bytes,
            },
            Self::StartupFailed {
                session_id,
                message,
            } => Event::StartupFailed {
                session_id: *session_id,
                message,
            },
            Self::PythonEnvironmentAccepted { packages } => {
                Event::PythonEnvironmentAccepted { packages }
            }
            Self::RequirementsAccepted { requirements } => {
                Event::RequirementsAccepted { requirements }
            }
            Self::RequirementsSelected {
                call_id,
                action,
                snapshot,
            } => Event::RequirementsSelected {
                call_id: *call_id,
                action,
                snapshot,
            },
        };
        active.append(event, at)
    }
}

const MAX_PROTOCOL_EVENTS: usize = 1024;
const MAX_PROTOCOL_BYTES: usize = 8 * 1024 * 1024;
// Raw image files are bounded per observed interval, independently of previews.
const MAX_INTERVAL_IMAGE_BYTES: usize = 64 * 1024 * 1024;
const MAX_INTERVAL_IMAGES: u64 = 4096;
const MAX_INTERVAL_IMAGE_METADATA_BYTES: usize = 64 * 1024;

impl ProtocolEvents {
    fn push(&mut self, event: QueuedEvent, at: DateTime<Utc>) -> bool {
        // Consecutive raw ranges have one observed order and no intervening
        // image or boundary. Merge only pending ranges, retaining their first
        // timestamp; tiny writes must not become a queue of per-write objects.
        if let Some((
            QueuedEvent::OutputText {
                owner,
                path,
                offset,
                bytes,
                discarded_bytes,
            },
            _,
            _,
        )) = self.pending.last_mut()
            && let QueuedEvent::OutputText {
                owner: next_owner,
                path: next_path,
                offset: next_offset,
                bytes: next_bytes,
                discarded_bytes: next_discarded,
            } = &event
            && owner == next_owner
            && path == next_path
            && *offset + *bytes as u64 == *next_offset
        {
            *bytes += next_bytes;
            *discarded_bytes += next_discarded;
            let mut size = ProtocolBytes::default();
            let merged = &self.pending.last().expect("pending text range").0;
            let result = serde_json::to_writer(&mut size, merged);
            let measured = &mut self.pending.last_mut().expect("pending text range").2;
            self.retained_bytes = self.retained_bytes - *measured + size.0;
            *measured = size.0;
            if let Err(error) = result {
                self.stop(format!("protocol capture failed: {error}"));
                return false;
            }
            if self.retained_bytes > MAX_PROTOCOL_BYTES {
                self.stop(format!("protocol capture backlog exceeded ({MAX_PROTOCOL_EVENTS} events or {MAX_PROTOCOL_BYTES} bytes)"));
                return false;
            }
            return true;
        }
        let mut size = ProtocolBytes::default();
        if let Err(error) = serde_json::to_writer(&mut size, &event) {
            self.stop(format!("protocol capture failed: {error}"));
            return false;
        }
        if self.retained_events == MAX_PROTOCOL_EVENTS
            || size.0 > MAX_PROTOCOL_BYTES - self.retained_bytes
        {
            self.stop(format!("protocol capture backlog exceeded ({MAX_PROTOCOL_EVENTS} events or {MAX_PROTOCOL_BYTES} bytes)"));
            return false;
        }
        self.retained_events += 1;
        self.retained_bytes += size.0;
        self.pending.push((event, at, size.0));
        true
    }

    pub(super) fn stop(&mut self, error: String) {
        self.failure.get_or_insert(error);
        self.retained_events -= self.pending.len();
        self.retained_bytes -= self
            .pending
            .drain(..)
            .map(|(_, _, bytes)| bytes)
            .sum::<usize>();
    }
}

#[derive(Default)]
// Measure serialized retention without allocating another copy of the event.
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

impl Transcript {
    pub(super) fn queue_event(&self, event: QueuedEvent) {
        self.observe(|_| Some((event, ())));
    }

    fn observe<T>(
        &self,
        operation: impl FnOnce(&mut ProtocolEvents) -> Option<(QueuedEvent, T)>,
    ) -> Option<T> {
        let mut events = self.1.lock().expect("protocol capture lock");
        if events.failure.is_some() {
            return None;
        }
        let (event, value) = operation(&mut events)?;
        let accepted = events.push(event, Utc::now());
        self.start_drain(events);
        accepted.then_some(value)
    }

    fn start_drain(&self, mut events: std::sync::MutexGuard<'_, ProtocolEvents>) {
        if events.draining {
            return;
        }
        events.draining = true;
        drop(events);
        let recorder = self.clone();
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

    pub(crate) fn disable(&self, error: String) {
        let mut events = self.1.lock().expect("protocol capture lock");
        events.stop(error);
        self.start_drain(events);
    }

    pub(super) fn flush_protocol(&self, state: &mut super::TranscriptState) -> Result<(), String> {
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

    pub(crate) fn source_admitted(&self, call_id: Option<u64>) {
        if let Some(call_id) = call_id {
            self.queue_event(QueuedEvent::Event(Event::SourceAdmitted { call_id }));
        }
    }
    pub(crate) fn source_finished(&self, call_id: Option<u64>) {
        if let Some(call_id) = call_id {
            self.queue_event(QueuedEvent::Event(Event::SourceFinished { call_id }));
        }
    }
    /// Bind waiting source to its first worker without replaying it onto replacement.
    pub(crate) fn associate_waiting_call(&self, call_id: Option<u64>) {
        self.observe(|events| {
            Some((
                QueuedEvent::AssociateWaitingCall {
                    call_id: call_id?,
                    session_id: events.owner?.session_id,
                },
                (),
            ))
        });
    }
    pub(crate) fn associate_current_call(&self, call_id: Option<u64>) {
        self.observe(|events| {
            Some((
                QueuedEvent::Event(Event::SourceAssociated {
                    call_id: call_id?,
                    session_id: events.owner?.session_id,
                }),
                (),
            ))
        });
    }
    pub(crate) fn owner(&self) -> Option<Owner> {
        let events = self.1.lock().expect("protocol capture lock");
        events.failure.is_none().then_some(events.owner).flatten()
    }
    pub(crate) fn worker_starting(&self) {
        self.observe(|events| {
            events.starting_session = None;
            Some((QueuedEvent::WorkerStarting, ()))
        });
    }
    pub(crate) fn worker_started(&self, pid: u32, requirements: &Value) {
        self.observe(|events| {
            events.next_session_id += 1;
            let session_id = events.next_session_id;
            events.owner = Some(Owner {
                session_id,
                cell_id: None,
                interval: Interval::Startup,
            });
            events.image_number = 0;
            events.image_bytes = 0;
            events.image_metadata_bytes = 0;
            events.starting_session = Some(session_id);
            Some((
                QueuedEvent::WorkerStarted {
                    session_id,
                    pid,
                    requirements: requirements.clone(),
                },
                (),
            ))
        });
    }
    pub(crate) fn worker_ready(&self) {
        self.observe(|events| {
            Some((
                QueuedEvent::WorkerReady {
                    session_id: events.starting_session.take()?,
                },
                (),
            ))
        });
    }
    pub(crate) fn worker_ended(&self, outcome: &str) {
        self.observe(|events| {
            Some((
                QueuedEvent::WorkerEnded {
                    session_id: events.owner.take()?.session_id,
                    outcome: outcome.to_owned(),
                },
                (),
            ))
        });
    }
    pub(crate) fn worker_retired(&self) {
        let mut events = self.1.lock().expect("protocol capture lock");
        events.owner = None;
        events.cell_call = None;
    }
    pub(super) fn start_cell(&self, call_id: Option<u64>) -> Option<Owner> {
        let call_id = call_id?;
        self.observe(|events| {
            let previous = events.owner?;
            let owner = Owner {
                session_id: previous.session_id,
                cell_id: Some(previous.cell_id.unwrap_or(0) + 1),
                interval: Interval::Cell,
            };
            events.cell_call = Some(call_id);
            events.owner = Some(owner);
            events.image_number = 0;
            events.image_bytes = 0;
            events.image_metadata_bytes = 0;
            Some((QueuedEvent::CellStarted { call_id, owner }, owner))
        })
    }
    pub(crate) fn end_cell(&self, completion_confirmed: bool) {
        self.observe(|events| {
            let owner = events.owner.filter(|o| o.interval == Interval::Cell)?;
            let call_id = events.cell_call.take()?;
            events.owner = Some(Owner {
                interval: Interval::Post,
                ..owner
            });
            events.image_number = 0;
            events.image_bytes = 0;
            events.image_metadata_bytes = 0;
            Some((
                QueuedEvent::CellEnded {
                    call_id,
                    owner,
                    completion_confirmed,
                },
                (),
            ))
        });
    }
    pub(super) fn reserve_image(
        &self,
        bytes: usize,
        encoded_bytes: usize,
        mime_type: &str,
    ) -> Option<(u64, Owner, String)> {
        let mut events = self.1.lock().expect("protocol capture lock");
        if events.failure.is_some() {
            return None;
        }
        let owner = events.owner?;
        if events.image_number == MAX_INTERVAL_IMAGES
            || bytes > MAX_INTERVAL_IMAGE_BYTES - events.image_bytes
            || mime_type.len() > MAX_INTERVAL_IMAGE_METADATA_BYTES - events.image_metadata_bytes
        {
            events.push(
                QueuedEvent::ImageOmitted {
                    owner: Some(owner),
                    encoded_bytes,
                    mime_type: mime_type.to_owned(),
                },
                Utc::now(),
            );
            self.start_drain(events);
            return None;
        }
        events.image_bytes += bytes;
        events.image_metadata_bytes += mime_type.len();
        events.next_artifact_id += 1;
        events.image_number += 1;
        Some((
            events.next_artifact_id,
            owner,
            owner.path(&format!("-{:03}", events.image_number)),
        ))
    }
    pub(crate) fn unowned_text(&self, bytes: &[u8]) -> Option<ProcessOutput> {
        use base64::Engine as _;
        self.observe(|events| {
            let retained = bytes.len().min(
                output::MAX_CELL_OUTPUT_BYTES.saturating_sub(events.process_output_bytes) as usize,
            );
            events.process_output_bytes += retained as u64;
            Some((
                QueuedEvent::ProcessOutput {
                    data: base64::engine::general_purpose::STANDARD.encode(&bytes[..retained]),
                    discarded_bytes: bytes.len() - retained,
                },
                ProcessOutput {
                    public_directory: self.3.public_directory.clone(),
                    retained_bytes: retained,
                },
            ))
        })
    }
    pub(crate) fn startup_failed(&self, message: &str) {
        self.observe(|events| {
            Some((
                QueuedEvent::StartupFailed {
                    session_id: events.starting_session.take(),
                    message: message.to_owned(),
                },
                (),
            ))
        });
    }
    pub(crate) fn requirements_selected(
        &self,
        call_id: Option<u64>,
        action: &str,
        snapshot: &Value,
    ) {
        self.queue_event(QueuedEvent::RequirementsSelected {
            call_id,
            action: action.to_owned(),
            snapshot: snapshot.clone(),
        });
    }
    pub(crate) fn requirements_accepted(&self, requirements: &Value) {
        self.queue_event(QueuedEvent::RequirementsAccepted {
            requirements: requirements.clone(),
        });
    }
    pub(crate) fn python_environment_accepted(&self, packages: &[String]) {
        self.queue_event(QueuedEvent::PythonEnvironmentAccepted {
            packages: packages.to_vec(),
        });
    }
}
