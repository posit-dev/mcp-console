//! Optional local integrator transport. MCP still owns the process lifetime.

use std::io;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::PathBuf;
use std::time::Duration;

use rmcp::model::{CallToolResponse, CallToolResult, ContentBlock, RequestId};
use serde::Deserialize;
use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::net::{UnixListener, UnixStream};
use tokio::sync::{mpsc, watch};
use tokio::task::{JoinHandle, JoinSet};

use super::ConsoleServer;
use crate::worker_client::{Content, IntegratorError, Response};

const MAX_FRAME: usize = 1024 * 1024;
const MAX_CONNECTIONS: usize = 64;

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
enum Request {
    Status {
        id: String,
    },
    Preview {
        id: String,
    },
    Requirements {
        id: String,
    },
    Manifest {
        id: String,
    },
    Evaluate {
        id: String,
        language: crate::cell::Language,
        code: String,
        #[serde(default)]
        busy: Busy,
        wait_ms: Option<u32>,
        #[serde(default = "record_by_default")]
        record: bool,
    },
}

#[derive(Default, Deserialize)]
#[serde(rename_all = "snake_case")]
enum Busy {
    #[default]
    Reject,
    Wait,
}

fn record_by_default() -> bool {
    true
}

impl Request {
    fn id(&self) -> &str {
        match self {
            Self::Status { id }
            | Self::Preview { id }
            | Self::Requirements { id }
            | Self::Manifest { id }
            | Self::Evaluate { id, .. } => id,
        }
    }
}

pub(super) struct Listener {
    listener: UnixListener,
    path: PathBuf,
    identity: (u64, u64),
}

impl Listener {
    pub fn bind(path: PathBuf) -> io::Result<Self> {
        let path = std::path::absolute(path)?;
        let parent = std::fs::metadata(path.parent().expect("absolute socket path"))?;
        // macOS does not enforce socket mode bits on connect. Require a private
        // parent before binding so no permissive creation window is exposed.
        if !parent.is_dir()
            || parent.uid() != unsafe { libc::geteuid() }
            || parent.mode() & 0o077 != 0
        {
            return Err(io::Error::other(
                "control socket requires a private parent directory owned by the current user (mode 0700)",
            ));
        }
        // Never unlink an existing path, including a stale socket.
        let listener = UnixListener::bind(&path).map_err(|error| {
            if error.kind() == io::ErrorKind::AddrInUse {
                io::Error::new(error.kind(), "control socket path already exists")
            } else {
                error
            }
        })?;
        let metadata = std::fs::symlink_metadata(&path)?;
        let bound = Self {
            listener,
            path,
            identity: (metadata.dev(), metadata.ino()),
        };
        std::fs::set_permissions(&bound.path, std::fs::Permissions::from_mode(0o600))?;
        Ok(bound)
    }

    pub fn start(self, server: ConsoleServer) -> Server {
        let (closed, mut closing) = watch::channel(false);
        let failed = closed.clone();
        let task = tokio::spawn(async move {
            let mut connections = JoinSet::new();
            loop {
                tokio::select! {
                    biased;
                    _ = closing.changed() => break,
                    Some(result) = connections.join_next() => {
                        if let Err(error) = result {
                            eprintln!("control connection task failed: {error}");
                        }
                    }
                    accepted = self.listener.accept() => match accepted {
                        Ok((stream, _)) if connections.len() < MAX_CONNECTIONS => {
                            connections.spawn(connection(stream, server.clone(), closing.clone()));
                        }
                        Ok(_) => {},
                        Err(error) => {
                            eprintln!("control socket accept failed: {error}");
                            let _ = failed.send(true);
                            break;
                        }
                    }
                }
            }
            while connections.join_next().await.is_some() {}
            // Keep the path owner alive until all handlers have recorded results.
            drop(self);
        });
        Server { closed, task }
    }
}

impl Drop for Listener {
    fn drop(&mut self) {
        if std::fs::symlink_metadata(&self.path)
            .is_ok_and(|metadata| (metadata.dev(), metadata.ino()) == self.identity)
        {
            let _ = std::fs::remove_file(&self.path);
        }
    }
}

pub(super) struct Server {
    closed: watch::Sender<bool>,
    task: JoinHandle<()>,
}

impl Server {
    pub fn close(&self) {
        let _ = self.closed.send(true);
    }

    pub async fn finish(self) {
        if let Err(error) = self.task.await {
            eprintln!("control socket task failed: {error}");
        }
    }
}

async fn connection(stream: UnixStream, server: ConsoleServer, mut closing: watch::Receiver<bool>) {
    let (read, mut write) = stream.into_split();
    let (frames, mut received) = mpsc::channel(16);
    let (disconnected, mut disconnect) = watch::channel(false);
    let mut reader_closing = closing.clone();
    let incoming = server.startup.runtime().transcript.clone();
    let reader = tokio::spawn(async move {
        let mut read = BufReader::new(read);
        let mut activated = false;
        loop {
            let mut frame = Vec::new();
            let mut bounded = (&mut read).take((MAX_FRAME + 1) as u64);
            let result = tokio::select! {
                biased;
                _ = reader_closing.changed() => break,
                _ = frames.closed() => break,
                result = bounded.read_until(b'\n', &mut frame) => result,
            };
            if !frame.is_empty() {
                if !activated {
                    let activation = incoming.clone();
                    let _ = tokio::task::spawn_blocking(move || activation.activate()).await;
                    activated = true;
                }
                incoming.protocol(
                    "integrator_to_server",
                    serde_json::from_slice(&frame).unwrap_or_else(|_| {
                        Value::String(String::from_utf8_lossy(&frame).into_owned())
                    }),
                );
            }
            match result {
                Ok(0) | Err(_) => break,
                Ok(_) if frame.len() > MAX_FRAME || !frame.ends_with(b"\n") => break,
                Ok(_) => {
                    if frames.try_send(frame).is_err() {
                        break;
                    }
                }
            }
        }
        let _ = disconnected.send(true);
    });
    loop {
        let frame = tokio::select! {
            biased;
            _ = closing.changed() => break,
            frame = received.recv() => match frame {
                Some(frame) => frame,
                None => break,
            },
        };
        let transcript = server.startup.runtime().transcript.clone();
        let parsed = serde_json::from_slice::<Request>(&frame);
        let (response, call_id) = match parsed {
            Ok(request) => handle(&server, request, &mut closing, &mut disconnect).await,
            Err(error) => (
                failure(Value::Null, "invalid_request", error.to_string()),
                None,
            ),
        };
        // The canonical result owns image artifacts. Keep transport evidence
        // correlated without duplicating potentially large base64 payloads.
        transcript.protocol("server_to_integrator", match call_id {
            Some(call_id) => json!({"id": response["id"], "tool_result_call_id": call_id, "error": response.get("error")}),
            None => response.clone(),
        });
        let mut bytes = serde_json::to_vec(&response).expect("control response serialization");
        bytes.push(b'\n');
        let delivered = if *closing.borrow() || *disconnect.borrow() {
            false
        } else {
            tokio::select! {
                biased;
                _ = closing.changed() => false,
                _ = disconnect.changed() => false,
                result = write.write_all(&bytes) => result.is_ok(),
            }
        };
        transcript.delivery(&response["id"], call_id, delivered);
        if !delivered {
            break;
        }
    }
    drop(received);
    let _ = reader.await;
}

fn failure(id: Value, code: &str, message: impl Into<String>) -> Value {
    json!({"id": id, "error": {"code": code, "message": message.into()}})
}

async fn handle(
    server: &ConsoleServer,
    request: Request,
    closing: &mut watch::Receiver<bool>,
    disconnect: &mut watch::Receiver<bool>,
) -> (Value, Option<u64>) {
    let runtime = server.startup.runtime();
    let id = Value::String(request.id().to_string());
    let arguments = match &request {
        Request::Evaluate { language, code, .. } => serde_json::Map::from_iter([(
            crate::cell::Languages::field(*language).into(),
            json!(code),
        )]),
        _ => serde_json::Map::new(),
    };
    let record = matches!(&request, Request::Evaluate { record: true, .. });
    let recording = runtime.transcript.clone();
    let request_id = RequestId::String(request.id().to_string().into());
    let call = tokio::task::spawn_blocking(move || {
        recording.begin_integrator(&request_id, &arguments, record)
    })
    .await
    .expect("integrator recording task");
    let call_id = call.id();
    let operation_call = call.clone();
    let operation = async {
        match request {
            Request::Status { .. } => Ok(CallToolResult::structured(
                runtime.worker.integrator_status(),
            )),
            Request::Preview { .. } => {
                let content = runtime.worker.integrator_preview().map_err(|_| {
                    (
                        "busy",
                        "runner state is changing; retry preview".to_string(),
                    )
                })?;
                record_images(&content, &operation_call, &runtime.transcript);
                Ok(CallToolResult::success(content_blocks(content)))
            }
            Request::Requirements { .. } | Request::Manifest { .. }
                if !runtime.worker.is_configured() =>
            {
                Err((
                    "not_ready",
                    "runtime configuration is not ready".to_string(),
                ))
            }
            Request::Requirements { .. } => Ok(CallToolResult::structured(
                runtime.worker.inspect_requirements(),
            )),
            Request::Manifest { .. } => Ok(CallToolResult::structured(
                runtime.worker.inspect_requirements()["requirements"].clone(),
            )),
            Request::Evaluate {
                language,
                code,
                busy,
                wait_ms,
                ..
            } => {
                let wait = match (busy, wait_ms) {
                    (Busy::Reject, None) => None,
                    (Busy::Wait, Some(milliseconds)) => {
                        Some(Duration::from_millis(milliseconds.into()))
                    }
                    _ => {
                        return Err((
                            "invalid_request",
                            "wait_ms is required with busy=wait and forbidden with busy=reject"
                                .into(),
                        ));
                    }
                };
                if server.inheritance.enabled() && !server.inheritance.captured() {
                    return Err(("not_ready", "waiting for MCP client permissions".into()));
                }
                let response = runtime
                    .worker
                    .integrator_evaluate(
                        crate::cell::Cell {
                            language,
                            source: code,
                        },
                        wait,
                        runtime.transcript.clone(),
                        operation_call.id(),
                        disconnect.clone(),
                    )
                    .await
                    .map_err(|error| match error {
                        IntegratorError::Busy => {
                            ("busy", "runner is busy; code was not run".into())
                        }
                        IntegratorError::Timeout => (
                            "timeout",
                            "runner did not become idle before the deadline; code was not run"
                                .into(),
                        ),
                        IntegratorError::Failed(message) => ("execution_failed", message),
                        IntegratorError::Cancelled => (
                            "connection_closed",
                            "control connection closed; code was not run".into(),
                        ),
                    })?;
                Ok(evaluation_result(
                    response,
                    &operation_call,
                    &runtime.transcript,
                ))
            }
        }
    };
    let result = if *closing.borrow() || *disconnect.borrow() {
        Err(("connection_closed", "control connection closed".to_string()))
    } else {
        operation.await
    };
    let response = match &result {
        Ok(result) => {
            let result = result.structured_content.clone().unwrap_or_else(|| {
                serde_json::to_value(result).expect("tool result serialization")
            });
            json!({"id": id, "result": result})
        }
        Err((code, message)) => failure(id, code, message),
    };
    let recorded: Result<CallToolResponse, rmcp::ErrorData> = result
        .map(Into::into)
        .map_err(|(_, message)| rmcp::ErrorData::internal_error(message, None));
    let recording = runtime.transcript.clone();
    tokio::task::spawn_blocking(move || recording.finish(call, &recorded))
        .await
        .expect("integrator result recording task");
    (response, call_id)
}

fn content_blocks(content: Vec<Content>) -> Vec<ContentBlock> {
    content
        .into_iter()
        .map(|content| match content {
            Content::Text(text) => ContentBlock::text(text),
            Content::Image {
                data, mime_type, ..
            } => ContentBlock::image(data, mime_type),
        })
        .collect()
}

fn evaluation_result(
    response: Response,
    call: &crate::transcript::Call,
    transcript: &crate::transcript::Transcript,
) -> CallToolResult {
    let (content, is_error, delivery, parts) = response.into_parts();
    call.record_result_parts(parts);
    record_images(&content, call, transcript);
    // A private response is never recovered into MCP, even when its socket closes.
    if let Some(delivery) = delivery {
        delivery.delivered();
    }
    if is_error {
        CallToolResult::error(content_blocks(content))
    } else {
        CallToolResult::success(content_blocks(content))
    }
}

fn record_images(
    content: &[Content],
    call: &crate::transcript::Call,
    transcript: &crate::transcript::Transcript,
) {
    let images = content
        .iter()
        .filter_map(|content| match content {
            Content::Image { artifact, .. } => Some(artifact.clone()),
            Content::Text(_) => None,
        })
        .collect();
    if let Err(error) = call.record_result_images(images) {
        transcript.disable(error);
    }
}
