//! Private JSONL transport; the integrator lifecycle remains the execution owner.
use std::io::BufRead;
use std::sync::Arc;
use std::time::Instant;

use rmcp::model::{CallToolResponse, CallToolResult, ContentBlock, RequestId};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tokio::io::AsyncWriteExt;
use tokio::sync::{mpsc, watch};
use tokio::task::{JoinHandle, JoinSet};

use super::Language;
use crate::server::startup::{Runtime, Startup};
use crate::worker_client::{Content, IntegratorError, IntegratorWait, KernelEvent, KernelState};
use crate::worker_protocol::ExecutionError;

#[derive(Deserialize, Serialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
enum Command {
    Execute {
        id: String,
        code: String,
        allow_stdin: bool,
    },
    Input {
        id: String,
        execution_id: String,
        data: String,
    },
    Interrupt {
        id: String,
    },
    Shutdown {
        id: String,
    },
}

struct Execution {
    id: String,
    allow_stdin: bool,
    input: bool,
    error: Option<ExecutionError>,
    cancelled: watch::Sender<bool>,
    task: JoinHandle<Result<(), String>>,
}

type Output = mpsc::UnboundedSender<Value>;

pub(super) async fn run(
    language: Language,
    startup: Startup,
    mut events: mpsc::UnboundedReceiver<KernelEvent>,
) -> Result<(), String> {
    let (incoming, mut commands) = mpsc::unbounded_channel();
    let recording = startup.runtime().transcript.clone();
    std::thread::Builder::new()
        .name("kernel-input".into())
        .spawn(move || {
            let mut input = std::io::stdin().lock();
            loop {
                let mut line = String::new();
                let command = match input.read_line(&mut line) {
                    Ok(0) => break,
                    Ok(_) if line.ends_with('\n') => {
                        serde_json::from_str::<Command>(&line).map_err(|error| error.to_string())
                    }
                    Ok(_) => Err("controller input closed midway through a frame".into()),
                    Err(error) => Err(error.to_string()),
                };
                let failed = command.is_err();
                let shutting_down = matches!(command, Ok(Command::Shutdown { .. }));
                if let Ok(command) = &command {
                    recording.protocol(
                        "adapter_to_controller",
                        serde_json::to_value(command).expect("controller command serialization"),
                    );
                }
                if incoming.send(command).is_err() || failed || shutting_down {
                    break;
                }
            }
        })
        .map_err(|error| error.to_string())?;
    let (output, mut outgoing) = mpsc::unbounded_channel::<Value>();
    let mut writer = tokio::spawn(async move {
        let mut stdout = tokio::io::stdout();
        while let Some(frame) = outgoing.recv().await {
            let mut bytes = serde_json::to_vec(&frame).expect("controller event serialization");
            bytes.push(b'\n');
            stdout.write_all(&bytes).await?;
            stdout.flush().await?;
        }
        Ok::<(), std::io::Error>(())
    });
    emit(
        &output,
        json!({"event":"ready", "language":language.name(), "implementation_version":env!("CARGO_PKG_VERSION")}),
    );
    let runtime = startup.runtime();
    let mut active: Option<Execution> = None;
    let mut previous = None;
    let mut controls = JoinSet::new();
    let mut writer_finished = false;
    let stopped: Result<Option<String>, String> = loop {
        tokio::select! {
            result = &mut writer => {
                writer_finished = true;
                break Err(match result {
                    Ok(Err(error)) => error.to_string(),
                    Err(error) => error.to_string(),
                    Ok(Ok(())) => "controller output closed".into(),
                });
            }
            command = commands.recv() => match command {
                None => break Ok(None),
                Some(Err(error)) => break Err(error),
                Some(Ok(Command::Shutdown { id })) => break Ok(Some(id)),
                Some(Ok(Command::Execute { id, code, allow_stdin })) => {
                    if active.is_some() { break Err("only one controller execution may be active".into()); }
                    // Assign startup output/input to this request before preparation begins.
                    startup.start();
                    let (cancelled, cancellation) = watch::channel(false);
                    let task = tokio::spawn(execute(language, runtime.clone(), id.clone(), code, cancellation));
                    active = Some(Execution { id, allow_stdin, input:false, error:None, cancelled, task });
                }
                Some(Ok(Command::Input { id, execution_id, data })) => {
                    let result = match active.as_mut().filter(|execution| execution.id == execution_id && execution.input) {
                        Some(execution) => {
                            execution.input = false;
                            runtime.worker.integrator_stdin(data)
                        }
                        None => Err("input has no outstanding request in this execution".into()),
                    };
                    reply(&output, id, result);
                }
                Some(Ok(Command::Interrupt { id })) => {
                    if let Some(execution) = &active {
                        // A preparation interrupt must also withdraw code waiting for admission.
                        let _ = execution.cancelled.send(true);
                    }
                    let target = runtime.worker.select_interrupt();
                    let output = output.clone();
                    controls.spawn(async move {
                        let result = tokio::task::spawn_blocking(move || target.and_then(|target| target.interrupt())).await
                            .map_err(|error| error.to_string()).and_then(|result| result);
                        reply(&output, id, result);
                    });
                }
            },
            event = events.recv() => if let Some(event) = event {
                handle_event(event, &mut active, previous.as_deref(), &runtime, &output, &mut controls);
            },
            result = completed(&mut active) => {
                // Native events are published before the completion receipt. Preserve their order.
                while let Ok(event) = events.try_recv() {
                    handle_event(event, &mut active, previous.as_deref(), &runtime, &output, &mut controls);
                }
                let execution = active.take().expect("completed execution");
                previous = Some(execution.id.clone());
                finish(execution, result, &output);
            },
            Some(result) = controls.join_next(), if !controls.is_empty() => {
                if let Err(error) = result { break Err(error.to_string()); }
            }
        }
    };
    if let Some(execution) = &active {
        let _ = execution.cancelled.send(true);
    }
    let cleanup = retire(&startup).await;
    if let Some(mut execution) = active.take() {
        let result = (&mut execution.task).await;
        while let Ok(event) = events.try_recv() {
            // Retirement may end input without publishing a cancellation event.
            if let KernelEvent::ExecutionError(error) = event {
                execution.error.get_or_insert(error);
            } else {
                publish(event, &execution.id, &output);
            }
        }
        finish(execution, result, &output);
    }
    while controls.join_next().await.is_some() {}
    let recording = runtime.clone();
    let finalized = tokio::task::spawn_blocking(move || {
        recording.worker.finish_recording();
        recording.transcript.finalize();
    })
    .await
    .map_err(|error| error.to_string());
    let cleanup = cleanup.and(finalized);
    if let Ok(Some(id)) = &stopped {
        reply(&output, id.clone(), cleanup.clone());
    }
    drop(output);
    let delivery = if writer_finished {
        Ok(())
    } else {
        tokio::time::timeout(crate::worker_client::WORKER_SHUTDOWN_GRACE, writer)
            .await
            .map_err(|_| "controller output did not close".to_string())
            .and_then(|result| result.map_err(|error| error.to_string()))
            .and_then(|result| result.map_err(|error| error.to_string()))
    };
    stopped?;
    cleanup?;
    delivery
}

async fn completed(
    active: &mut Option<Execution>,
) -> Result<Result<(), String>, tokio::task::JoinError> {
    match active {
        Some(execution) => (&mut execution.task).await,
        None => std::future::pending().await,
    }
}

fn emit(output: &Output, frame: Value) {
    let _ = output.send(frame);
}

fn reply(output: &Output, id: String, result: Result<(), String>) {
    emit(
        output,
        match result {
            Ok(()) => json!({"id":id,"event":"reply","ok":true}),
            Err(error) => json!({"id":id,"event":"reply","ok":false,"error":error}),
        },
    );
}

fn error(name: &str, message: impl Into<String>) -> ExecutionError {
    ExecutionError {
        ename: name.into(),
        evalue: message.into(),
        traceback: Vec::new(),
    }
}

fn finish(
    mut execution: Execution,
    result: Result<Result<(), String>, tokio::task::JoinError>,
    output: &Output,
) {
    if let Err(message) = result
        .map_err(|error| error.to_string())
        .and_then(|result| result)
        && execution.error.is_none()
    {
        execution.error = Some(error("ConsoleError", message));
    }
    let status = if execution.error.is_some() {
        "error"
    } else {
        "ok"
    };
    if let Some(error) = execution.error {
        emit(
            output,
            json!({"id":execution.id,"event":"error","ename":error.ename,"evalue":error.evalue,"traceback":error.traceback}),
        );
    }
    emit(
        output,
        json!({"id":execution.id,"event":"complete","status":status}),
    );
}

fn handle_event(
    event: KernelEvent,
    active: &mut Option<Execution>,
    previous: Option<&str>,
    runtime: &Runtime,
    output: &Output,
    controls: &mut JoinSet<()>,
) {
    let Some(execution) = active else {
        if let Some(id) = previous {
            publish(event, id, output);
        }
        return;
    };
    match event {
        KernelEvent::InputRequested { prompt } => {
            if execution.allow_stdin {
                execution.input = true;
                emit(
                    output,
                    json!({"id":execution.id,"event":"input","prompt":prompt}),
                );
            } else {
                execution.error = Some(error(
                    "StdinNotImplementedError",
                    "stdin is disabled for this execute request",
                ));
                let _ = execution.cancelled.send(true);
                let target = runtime.worker.select_interrupt();
                controls.spawn(async move {
                    let _ = tokio::task::spawn_blocking(move || {
                        target.and_then(|target| target.interrupt())
                    })
                    .await;
                });
            }
        }
        KernelEvent::InputEnded => {
            execution.input = false;
            emit(output, json!({"id":execution.id,"event":"input_ended"}));
        }
        KernelEvent::ExecutionError(error) => {
            execution.error.get_or_insert(error);
        }
        event => publish(event, &execution.id, output),
    }
}

fn publish(event: KernelEvent, id: &str, output: &Output) {
    emit(
        output,
        match event {
            KernelEvent::Stream { name, text } => {
                json!({"id":id,"event":"stream","name":name,"text":text})
            }
            KernelEvent::Display { data, mime_type } => {
                json!({"id":id,"event":"display","data":data,"mime_type":mime_type})
            }
            KernelEvent::Result { text } => json!({"id":id,"event":"result","text":text}),
            KernelEvent::InputRequested { .. }
            | KernelEvent::InputEnded
            | KernelEvent::ExecutionError(_) => return,
        },
    );
}

async fn execute(
    language: Language,
    runtime: Arc<Runtime>,
    id: String,
    code: String,
    cancellation: watch::Receiver<bool>,
) -> Result<(), String> {
    let arguments = serde_json::Map::from_iter([(language.name().into(), json!(code))]);
    let recording = runtime.transcript.clone();
    let request_id = RequestId::String(id.clone().into());
    let call = tokio::task::spawn_blocking(move || {
        recording.begin_integrator(&request_id, &arguments, true)
    })
    .await
    .map_err(|error| error.to_string())?;
    let result = runtime
        .worker
        .integrator_evaluate(
            crate::cell::Cell {
                language: language.source(),
                source: code,
            },
            IntegratorWait::Forever,
            runtime.transcript.clone(),
            call.id(),
            cancellation,
        )
        .await;
    let (outcome, recorded) = match result {
        Ok(response) => {
            let outcome = if response.is_error() {
                Err(response
                    .failure_message()
                    .unwrap_or("Console execution failed")
                    .to_string())
            } else if response.kernel_state() != Some(KernelState::Completed) {
                Err("execution ended without a successful completion receipt".into())
            } else {
                Ok(())
            };
            let (content, failed, delivery, parts) = response.into_parts();
            call.record_result_parts(parts);
            let mut images = Vec::new();
            let content = content
                .into_iter()
                .map(|content| match content {
                    Content::Text(text) => ContentBlock::text(text),
                    Content::Image {
                        data,
                        mime_type,
                        artifact,
                    } => {
                        images.push(artifact);
                        ContentBlock::image(data, mime_type)
                    }
                })
                .collect();
            if let Err(error) = call.record_result_images(images) {
                runtime.transcript.disable(error);
            }
            if let Some(delivery) = delivery {
                delivery.delivered();
            }
            let result = if failed {
                CallToolResult::error(content)
            } else {
                CallToolResult::success(content)
            };
            (outcome, Ok(CallToolResponse::Complete(result)))
        }
        Err(failure) => {
            let message = match failure {
                IntegratorError::Busy => "controller execution admission is busy".into(),
                IntegratorError::Timeout => "controller execution admission timed out".into(),
                IntegratorError::Cancelled => "execution cancelled before admission".into(),
                IntegratorError::Failed(message) => message,
            };
            (
                Err(message.clone()),
                Err(rmcp::ErrorData::internal_error(message, None)),
            )
        }
    };
    let recording = runtime.transcript.clone();
    tokio::task::spawn_blocking(move || {
        recording.finish(call, &recorded);
        // Canonical capture is complete. No frontend delivery receipt is claimed:
        // Jupyter IOPub does not acknowledge observation by connected clients.
    })
    .await
    .map_err(|error| error.to_string())?;
    outcome
}

async fn retire(startup: &Startup) -> Result<(), String> {
    let deadline = Instant::now() + crate::worker_client::WORKER_SHUTDOWN_GRACE;
    let cancellation = startup.cancel(deadline).await;
    let runtime = startup.runtime();
    let retirement = match startup.ready().await {
        Ok(runtime) => runtime.worker.shutdown(deadline).await,
        Err(error) => {
            if runtime.worker.is_configured() {
                runtime.worker.shutdown(deadline).await?;
            }
            startup.confirm_preparation_retired(error)
        }
    };
    cancellation.and(retirement)
}
