use std::io::{self, BufRead, Write};

use base64::Engine as _;
use serde::{Deserialize, Serialize};

use crate::cell::Language;
use crate::worker_protocol::{
    NativePythonActivation, PythonRequirementManifest, PythonResolveRequest,
    PythonVersionResolveRequest, RResolutionFailureKind, WorkerMessage, deserialize_payload_free,
};

pub(crate) const PARTIAL_COMMAND_EOF: &str = "relay stdin closed midway through a frame";

#[derive(Clone, Deserialize, Serialize)]
#[serde(transparent)]
pub(crate) struct EncodedBytes(String);

#[derive(Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum RelayCommand {
    Evaluate {
        language: Language,
        source: String,
    },
    PrepareR {
        library: String,
    },
    RResolved {
        library: String,
    },
    RResolutionFailed {
        failure: RResolutionFailureKind,
        message: String,
    },
    PreparePython {
        packages: Vec<String>,
    },
    PythonResolved {
        python: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        native: Option<Box<NativePythonActivation>>,
    },
    PythonResolutionFailed {
        message: String,
    },
    PythonVersionResolved {
        version: String,
    },
    PythonVersionResolutionFailed {
        message: String,
    },
    Stdin {
        data: String,
    },
    Interrupt {
        request_id: u64,
    },
    Shutdown {
        grace_millis: u64,
    },
}

#[derive(Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum RelayEvent {
    #[serde(deserialize_with = "deserialize_payload_free")]
    CellStarted,
    WorkerStarted {
        pid: u32,
    },
    #[serde(deserialize_with = "deserialize_payload_free")]
    Ready,
    RuntimeInitialized {
        interrupted: bool,
    },
    RInitialization {
        complete: bool,
    },
    ConsoleOutput {
        data: String,
    },
    ConsoleDiagnostic {
        data: String,
    },
    Image {
        data: String,
        mime_type: String,
    },
    InputRequested {
        prompt: String,
    },
    #[serde(deserialize_with = "deserialize_payload_free")]
    InputReceived,
    #[serde(deserialize_with = "deserialize_payload_free")]
    InputCancelled,
    RPrepared {
        library: String,
    },
    RPreparationFailed {
        message: String,
    },
    ResolveR {
        packages: Vec<String>,
    },
    RActivated {
        library: String,
    },
    RActivationFailed {
        library: String,
        message: String,
    },
    ResolvePython {
        request: PythonResolveRequest,
    },
    ResolvePythonVersion {
        request: PythonVersionResolveRequest,
    },
    PythonActivated {
        requirements: PythonRequirementManifest,
    },
    PythonActivationFailed {
        requirements: PythonRequirementManifest,
    },
    #[serde(deserialize_with = "deserialize_payload_free")]
    PythonPrepared,
    PythonPreparationFailed {
        message: String,
    },
    PythonPreparationRejected {
        message: String,
    },
    #[serde(deserialize_with = "deserialize_payload_free")]
    Completed,
    Stdout {
        data: String,
    },
    Stderr {
        data: String,
    },
    StdoutBytes {
        data: EncodedBytes,
    },
    StderrBytes {
        data: EncodedBytes,
    },
    #[serde(deserialize_with = "deserialize_payload_free")]
    StdoutClosed,
    #[serde(deserialize_with = "deserialize_payload_free")]
    StderrClosed,
    #[serde(deserialize_with = "deserialize_payload_free")]
    WorkerSidebandClosed,
    InterruptResult {
        request_id: u64,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        error: Option<String>,
    },
    #[serde(deserialize_with = "deserialize_payload_free")]
    ShutdownStarted,
    WorkerExited {
        code: i32,
    },
    WorkerSignaled {
        signal: i32,
    },
    Fatal {
        message: String,
    },
}

impl From<WorkerMessage> for RelayEvent {
    fn from(message: WorkerMessage) -> Self {
        match message {
            WorkerMessage::CellStarted => Self::CellStarted,
            WorkerMessage::Ready => Self::Ready,
            WorkerMessage::RuntimeInitialized { interrupted } => {
                Self::RuntimeInitialized { interrupted }
            }
            WorkerMessage::RInitialization { complete } => Self::RInitialization { complete },
            WorkerMessage::ConsoleOutput { data } => Self::ConsoleOutput { data },
            WorkerMessage::ConsoleDiagnostic { data } => Self::ConsoleDiagnostic { data },
            WorkerMessage::Image { data, mime_type } => Self::Image { data, mime_type },
            WorkerMessage::InputRequested { prompt } => Self::InputRequested { prompt },
            WorkerMessage::InputReceived => Self::InputReceived,
            WorkerMessage::InputCancelled => Self::InputCancelled,
            WorkerMessage::RPrepared { library } => Self::RPrepared { library },
            WorkerMessage::RPreparationFailed { message } => Self::RPreparationFailed { message },
            WorkerMessage::ResolveR { packages } => Self::ResolveR { packages },
            WorkerMessage::RActivated { library } => Self::RActivated { library },
            WorkerMessage::RActivationFailed { library, message } => {
                Self::RActivationFailed { library, message }
            }
            WorkerMessage::ResolvePython { request } => Self::ResolvePython { request },
            WorkerMessage::ResolvePythonVersion { request } => {
                Self::ResolvePythonVersion { request }
            }
            WorkerMessage::PythonActivated { requirements } => {
                Self::PythonActivated { requirements }
            }
            WorkerMessage::PythonActivationFailed { requirements } => {
                Self::PythonActivationFailed { requirements }
            }
            WorkerMessage::PythonPrepared => Self::PythonPrepared,
            WorkerMessage::PythonPreparationFailed { message } => {
                Self::PythonPreparationFailed { message }
            }
            WorkerMessage::PythonPreparationRejected { message } => {
                Self::PythonPreparationRejected { message }
            }
            WorkerMessage::Completed => Self::Completed,
        }
    }
}

impl EncodedBytes {
    pub(crate) fn from_bytes(bytes: &[u8]) -> Self {
        Self(base64::engine::general_purpose::STANDARD.encode(bytes))
    }

    pub(crate) fn decode(&self) -> Result<Vec<u8>, String> {
        base64::engine::general_purpose::STANDARD
            .decode(&self.0)
            .map_err(|error| format!("relay received invalid base64 data: {error}"))
    }
}

pub(crate) struct JsonlReader<R> {
    reader: R,
    buffer: Vec<u8>,
}

impl<R: BufRead> JsonlReader<R> {
    pub(crate) fn new(reader: R) -> Self {
        Self {
            reader,
            buffer: Vec::new(),
        }
    }

    pub(crate) fn receive<T: serde::de::DeserializeOwned>(&mut self) -> io::Result<Option<T>> {
        self.buffer.clear();
        let length = self.reader.read_until(b'\n', &mut self.buffer)?;
        if length == 0 {
            return Ok(None);
        }
        if self.buffer.last() != Some(&b'\n') {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "relay stream closed midway through a frame",
            ));
        }
        self.buffer.pop();
        if self.buffer.last() == Some(&b'\r') {
            self.buffer.pop();
        }
        serde_json::from_slice(&self.buffer)
            .map(Some)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))
    }
}

pub(crate) struct JsonlWriter<W> {
    writer: W,
}

impl<W: Write> JsonlWriter<W> {
    pub(crate) fn new(writer: W) -> Self {
        Self { writer }
    }

    pub(crate) fn send<T: Serialize>(&mut self, message: &T) -> io::Result<()> {
        serde_json::to_writer(&mut self.writer, message)?;
        self.writer.write_all(b"\n")?;
        self.writer.flush()
    }
}

#[cfg(test)]
mod tests {
    use super::{RelayCommand, RelayEvent};
    use crate::worker_protocol::RResolutionFailureKind;

    fn assert_encoding(message: &impl serde::Serialize, expected: &str) {
        assert_eq!(serde_json::to_string(message).unwrap(), expected);
    }

    #[test]
    fn payload_free_relay_events_reject_unknown_fields() {
        let messages = [
            r#"{"kind":"ready","obsolete":true}"#,
            r#"{"kind":"input_received","obsolete":true}"#,
            r#"{"kind":"input_cancelled","obsolete":true}"#,
            r#"{"kind":"python_prepared","obsolete":true}"#,
            r#"{"kind":"completed","obsolete":true}"#,
            r#"{"kind":"stdout_closed","obsolete":true}"#,
            r#"{"kind":"stderr_closed","obsolete":true}"#,
            r#"{"kind":"worker_sideband_closed","obsolete":true}"#,
            r#"{"kind":"shutdown_started","obsolete":true}"#,
            r#"{"kind":"python_prepared","python_checkpoint":{"packages":[]}}"#,
            r#"{"kind":"completed","python_checkpoint":{"packages":[]}}"#,
        ];
        let accepted = messages
            .into_iter()
            .filter(|message| serde_json::from_str::<RelayEvent>(message).is_ok())
            .collect::<Vec<_>>();
        assert!(accepted.is_empty(), "accepted unknown fields: {accepted:?}");
    }

    #[test]
    fn payload_free_relay_events_retain_their_encoding() {
        for (message, expected) in [
            (RelayEvent::Ready, r#"{"kind":"ready"}"#),
            (RelayEvent::InputReceived, r#"{"kind":"input_received"}"#),
            (RelayEvent::InputCancelled, r#"{"kind":"input_cancelled"}"#),
            (RelayEvent::PythonPrepared, r#"{"kind":"python_prepared"}"#),
            (RelayEvent::Completed, r#"{"kind":"completed"}"#),
            (RelayEvent::StdoutClosed, r#"{"kind":"stdout_closed"}"#),
            (RelayEvent::StderrClosed, r#"{"kind":"stderr_closed"}"#),
            (
                RelayEvent::WorkerSidebandClosed,
                r#"{"kind":"worker_sideband_closed"}"#,
            ),
            (
                RelayEvent::ShutdownStarted,
                r#"{"kind":"shutdown_started"}"#,
            ),
        ] {
            assert_encoding(&message, expected);
        }
    }

    #[test]
    fn runtime_r_relay_commands_retain_their_encoding() {
        assert_encoding(
            &RelayCommand::RResolved {
                library: "/managed/r".to_string(),
            },
            r#"{"kind":"r_resolved","library":"/managed/r"}"#,
        );
        for (failure, encoded) in [
            (RResolutionFailureKind::Host, "host"),
            (RResolutionFailureKind::Interrupted, "interrupted"),
            (RResolutionFailureKind::Operation, "operation"),
        ] {
            assert_encoding(
                &RelayCommand::RResolutionFailed {
                    failure,
                    message: "resolution failed".to_string(),
                },
                &format!(
                    r#"{{"kind":"r_resolution_failed","failure":"{encoded}","message":"resolution failed"}}"#
                ),
            );
        }
    }

    #[test]
    fn runtime_r_relay_events_retain_their_encoding() {
        for (event, expected) in [
            (
                RelayEvent::ResolveR {
                    packages: vec!["cli".to_string(), "glue".to_string()],
                },
                r#"{"kind":"resolve_r","packages":["cli","glue"]}"#,
            ),
            (
                RelayEvent::RActivated {
                    library: "/managed/r".to_string(),
                },
                r#"{"kind":"r_activated","library":"/managed/r"}"#,
            ),
            (
                RelayEvent::RActivationFailed {
                    library: "/managed/r".to_string(),
                    message: "activation failed".to_string(),
                },
                r#"{"kind":"r_activation_failed","library":"/managed/r","message":"activation failed"}"#,
            ),
        ] {
            assert_encoding(&event, expected);
        }
    }
}
