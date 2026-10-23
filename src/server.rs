mod arguments;
#[cfg(windows)]
mod input_windows;
mod presentation;
mod sandbox;
pub(crate) use presentation::{materialized_tool, parameter_schema};
pub(crate) use sandbox::Configuration as SandboxConfiguration;
mod startup;
use std::error::Error;
use std::path::PathBuf;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll};
use std::time::{Duration, Instant};

use arguments::{Requirements, SendArguments, SendControl};
use rmcp::{
    ErrorData, RoleServer, ServerHandler, ServiceExt,
    handler::server::{
        common::Extension, router::tool::ToolRouter, tool::ToolCallContext, wrapper::Parameters,
    },
    model::{
        CallToolRequestParams, CallToolResponse, CallToolResult, ContentBlock, ErrorCode,
        Implementation, ServerCapabilities, ServerConfig,
    },
    service::RequestContext,
    tool, tool_handler, tool_router,
};
use tokio::io::{AsyncRead, ReadBuf};
use tokio::sync::oneshot;

use crate::worker_client::WORKER_SHUTDOWN_GRACE;

use crate::cell::{LANGUAGES_ENV, Languages};

#[derive(Clone)]
struct ConsoleServer {
    startup: startup::Startup,
    deliveries: crate::server_transport::ResponseDeliveries,
    languages: Languages,
    language_setting: &'static str,
    tool_router: ToolRouter<Self>,
    instructions: String,
    tool_settings: Arc<crate::settings::Tool>,
    inheritance: Arc<sandbox::Inheritance>,
}

impl ConsoleServer {
    #[allow(clippy::too_many_arguments)]
    fn new(
        input_closed: InputClosed,
        worker: Option<PathBuf>,
        relay: Option<PathBuf>,
        sandbox: SandboxConfiguration,
        python: Option<crate::settings::PythonChoice>,
        r: crate::settings::R,
        startup: Option<crate::settings::startup::Startup>,
        visibility: Option<Languages>,
        tool_settings: crate::settings::Tool,
    ) -> Result<Self, String> {
        let recording_directory = std::env::current_dir();
        let (languages, language_setting) = match visibility {
            Some(languages) => (languages, "languages"),
            None => (Languages::from_environment()?, LANGUAGES_ENV),
        };
        if worker.is_none() && relay.is_some() {
            return Err("a custom relay requires a custom worker".into());
        }
        // Presentation has no dependency on the client or its discovered capabilities.
        let (tool_router, instructions) = Self::configured_presentation(
            languages,
            worker.is_none(),
            python.as_ref(),
            &sandbox.policy,
            sandbox.no_sandbox,
            sandbox
                .profile
                .as_ref()
                .is_some_and(crate::settings::ProfileSelection::inherits_codex),
            &tool_settings,
        )?;
        let runtime = Arc::new(startup::Runtime {
            worker: crate::worker_client::Client::pending(),
            transcript: crate::transcript::Transcript::pending(
                recording_directory
                    .as_ref()
                    .cloned()
                    .map_err(|error| std::io::Error::new(error.kind(), error.to_string())),
            ),
        });
        let prelaunch = worker.is_none();
        let uses_client_profile = sandbox
            .profile
            .as_ref()
            .is_some_and(crate::settings::ProfileSelection::inherits_codex);
        let inheritance = Arc::new(sandbox::Inheritance::new(sandbox.profile.clone()));
        let startup_inheritance = inheritance.clone();
        let startup = startup::Startup::new(
            input_closed,
            runtime,
            prelaunch,
            move |started, diagnostics| {
                let sandbox = if uses_client_profile {
                    sandbox
                        .clone()
                        .configure(startup_inheritance.policy(), python.as_ref())?
                } else {
                    sandbox.clone()
                };
                let configuration = if let Some(program) = worker.clone() {
                    crate::worker_client::ClientConfiguration::new(
                        program,
                        relay.clone(),
                        sandbox.no_sandbox,
                        sandbox.policy,
                    )
                    .with_resolver_settings(sandbox.resolver)?
                } else {
                    crate::worker_client::ClientConfiguration::builtin(
                        sandbox.no_sandbox,
                        sandbox.policy,
                        python.clone(),
                        r.clone(),
                        sandbox.resolver,
                        diagnostics,
                        started,
                    )?
                };
                let configuration = configuration.with_startup(startup.clone());
                let transcript = crate::transcript::Transcript::configured(
                    recording_directory
                        .as_ref()
                        .cloned()
                        .map_err(|error| std::io::Error::new(error.kind(), error.to_string())),
                    configuration.dynamic_resolution(),
                    configuration.python_preparation(),
                    !configuration.python_only(),
                    configuration.startup_declaration(),
                );
                Ok(startup::PreparedRuntime {
                    configuration,
                    transcript,
                })
            },
        );
        if !uses_client_profile {
            startup.start();
        }
        Ok(Self {
            startup,
            deliveries: crate::server_transport::ResponseDeliveries::default(),
            languages,
            language_setting,
            tool_router,
            instructions,
            tool_settings: Arc::new(tool_settings),
            inheritance,
        })
    }
}

#[tool_router]
impl ConsoleServer {
    #[tool]
    async fn send(
        &self,
        Extension(runtime): Extension<Arc<startup::Runtime>>,
        Extension(call): Extension<crate::transcript::Call>,
        Extension(delivery): Extension<crate::server_transport::ResponseDeliveryCall>,
        Extension(started): Extension<Instant>,
        Parameters(SendArguments {
            r,
            python,
            sql,
            control,
            requirements,
            stdin,
            timeout_ms,
        }): Parameters<SendArguments>,
    ) -> Result<CallToolResult, String> {
        let cell = match (r, python, sql) {
            (Some(source), None, None) => Some(crate::cell::Cell {
                language: crate::cell::Language::R,
                source,
            }),
            (None, Some(source), None) => Some(crate::cell::Cell {
                language: crate::cell::Language::Python,
                source,
            }),
            (None, None, Some(source)) => Some(crate::cell::Cell {
                language: crate::cell::Language::Sql,
                source,
            }),
            (None, None, None) => None,
            _ => {
                return Err(format!(
                    "only one of {} may be supplied",
                    self.languages.cell_fields()
                ));
            }
        };
        if let Some(requirements) = &requirements {
            use crate::worker_client::RequirementsAction::{Get, Reset};
            if matches!(requirements.action, Get | Reset)
                && (requirements.r.is_some()
                    || requirements.python.is_some()
                    || requirements.duckdb.is_some()
                    || requirements.python_version.is_some()
                    || requirements.exclude_newer.is_some())
            {
                return Err(
                    "requirements.action=get/reset takes no package lists or constraint fields"
                        .into(),
                );
            }
            if requirements.action == Get {
                if cell.is_some() || stdin.is_some() || control.is_some() {
                    return Err(
                        "requirements.action=get cannot be combined with code, stdin, or control"
                            .into(),
                    );
                }
                match tokio::time::timeout(
                    Duration::from_millis(timeout_ms).saturating_sub(started.elapsed()),
                    runtime.worker.ready(),
                )
                .await
                {
                    Ok(Ok(())) => {}
                    Ok(Err(error)) => {
                        return Ok(response_to_tool_result(
                            runtime.worker.startup_failure_response(error),
                            &call,
                            &runtime.transcript,
                            &self.deliveries,
                            &delivery,
                        ));
                    }
                    Err(_) => {
                        let response = runtime.worker.starting_response();
                        return Ok(response_to_tool_result(
                            response,
                            &call,
                            &runtime.transcript,
                            &self.deliveries,
                            &delivery,
                        ));
                    }
                }
                if let Some(response) = runtime.worker.take_prelaunch_failure()? {
                    return Ok(response_to_tool_result(
                        response,
                        &call,
                        &runtime.transcript,
                        &self.deliveries,
                        &delivery,
                    ));
                }
                let snapshot = runtime.worker.inspect_requirements();
                let json = serde_json::to_string_pretty(&snapshot).expect("requirements JSON");
                let text = if json.len() <= 8 * 1024 {
                    json
                } else {
                    "The complete requirements declaration is in structuredContent.requirements; supply that object with action=\"set\". The manifest exceeds the text preview limit and is not reproduced here.".into()
                };
                let mut result =
                    CallToolResult::success(vec![rmcp::model::ContentBlock::text(text)]);
                result.structured_content = Some(snapshot);
                return Ok(result);
            }
        }
        let requirements = requirements.map(
            |Requirements {
                 action,
                 duckdb,
                 r,
                 python,
                 python_version,
                 exclude_newer,
             }| {
                crate::worker_client::Requirements {
                    action,
                    call_id: call.id(),
                    duckdb: duckdb.unwrap_or_default(),
                    r: r.unwrap_or_default(),
                    python: python.unwrap_or_default(),
                    python_version: python_version.unwrap_or_default(),
                    exclude_newer,
                }
            },
        );
        let request = crate::worker_client::SendRequest {
            cell,
            stdin,
            requirements,
            control: control.map(|control| match control {
                SendControl::Interrupt => crate::worker_client::SendControl::Interrupt,
                SendControl::Restart => crate::worker_client::SendControl::Restart,
            }),
            deadline: started
                .checked_add(Duration::from_millis(timeout_ms))
                .unwrap_or(started),
            transcript: runtime.transcript.clone(),
            call_id: call.id(),
        };
        let response = async {
            request.validate(true)?;
            let initial_restart = matches!(
                request.control,
                Some(crate::worker_client::SendControl::Restart)
            ) && self.startup.retry_failed()?;
            runtime.worker.send(request, initial_restart).await
        }
        .await
        .unwrap_or_else(crate::worker_client::Response::tool_error);
        Ok(response_to_tool_result(
            response,
            &call,
            &runtime.transcript,
            &self.deliveries,
            &delivery,
        ))
    }
}

fn response_to_tool_result(
    response: crate::worker_client::Response,
    call: &crate::transcript::Call,
    transcript: &crate::transcript::Transcript,
    deliveries: &crate::server_transport::ResponseDeliveries,
    delivery: &crate::server_transport::ResponseDeliveryCall,
) -> CallToolResult {
    let (content, is_error, response_delivery, parts) = response.into_parts();
    call.record_result_parts(parts);
    let mut result_images = Vec::new();
    let content = content
        .into_iter()
        .map(|content| match content {
            crate::worker_client::Content::Text(text) => ContentBlock::text(text),
            crate::worker_client::Content::Image {
                data,
                mime_type,
                artifact,
            } => {
                result_images.push(artifact);
                ContentBlock::image(data, mime_type)
            }
        })
        .collect();
    if let Err(error) = call.record_result_images(result_images) {
        transcript.disable(error);
    }
    if let Some(response_delivery) = response_delivery {
        deliveries.register(delivery, response_delivery);
    }
    if is_error {
        CallToolResult::error(content)
    } else {
        CallToolResult::success(content)
    }
}

#[tool_handler(router = self.tool_router)]
impl ServerHandler for ConsoleServer {
    fn get_info(&self) -> ServerConfig {
        let mut capabilities = ServerCapabilities::builder().enable_tools().build();
        if self.inheritance.enabled() {
            capabilities.experimental = Some(std::collections::BTreeMap::from_iter([
                (sandbox::CAPABILITY.to_string(), serde_json::Map::new()),
                (
                    sandbox::LEGACY_CAPABILITY.to_string(),
                    serde_json::Map::new(),
                ),
            ]));
        }
        ServerConfig::new(capabilities)
            .with_server_info(Implementation::new(
                "mcp-console",
                env!("CARGO_PKG_VERSION"),
            ))
            .with_instructions(self.instructions.clone())
    }

    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        mut context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, ErrorData> {
        context.extensions.insert(Instant::now());
        if request.name.as_ref() != "send" {
            return self
                .tool_router
                .call(ToolCallContext::new(self, request, context))
                .await;
        }
        // A response can be visible before its write future settles. Delay only
        // console operations so transport receipt remains live for cancellation and EOF.
        let admission = context
            .extensions
            .remove::<crate::server_transport::ResponseDeliveryAdmission>()
            .expect("console operations must carry transport admission");
        let recording = context
            .extensions
            .remove::<crate::server_transport::RecordingCall>()
            .expect("send carries its recording call");
        let call = recording.clone().recorded().await;
        let transcript = self.startup.runtime().transcript.clone();
        let admitted = tokio::select! {
            biased;
            _ = context.ct.cancelled() => Err(ErrorData::internal_error(
                    "request cancelled before execution",
                    None,
                )),
            delivery = admission.admit() => {
                match delivery {
                    Ok(delivery) => Ok(delivery),
                    Err(crate::server_transport::ResponseDeliveryAdmissionError::Cancelled) => {
                        Err(ErrorData::internal_error(
                            "request cancelled before execution",
                            None,
                        ))
                    }
                    Err(crate::server_transport::ResponseDeliveryAdmissionError::Closed) => {
                        Err(ErrorData::internal_error(
                            "MCP input closed before request execution",
                            None,
                        ))
                    }
                }
            }
        };
        let (delivery, operation) = match admitted {
            Ok(admitted) => admitted,
            Err(error) => {
                let recorder = transcript.clone();
                let recorded_error = error.clone();
                let completion = recording.clone();
                if let Err(error) = tokio::task::spawn_blocking(move || {
                    let _completion = completion;
                    recorder.finish(call, &Err(recorded_error));
                })
                .await
                {
                    transcript.disable(format!("transcript task failed: {error}"));
                }
                return recording.response(Err(error));
            }
        };
        let sandbox::CaptureResult(inherited) = context
            .extensions
            .remove::<sandbox::CaptureResult>()
            .expect("console operations must carry transport sandbox capture");
        if self.inheritance.enabled() && inherited.is_ok() {
            self.startup.start();
        }
        context.extensions.insert(delivery.clone());
        let runtime = self.startup.runtime();
        let runtime_probe =
            context.meta.get(crate::warmup::RUNTIME_META) == Some(&serde_json::Value::Bool(true));
        let runtime_worker = runtime.worker.clone();
        // A restart can reset completed failed readiness later in this call.
        // Its wait stays cancellable until initial configuration is accepted.
        let waiting_for_startup =
            !runtime.worker.startup_finished() || !runtime.worker.is_configured();
        context.extensions.insert(runtime);
        context.extensions.insert(call.clone());
        // Call the known send route directly so argument decoding errors enter
        // the bounded renderer before the router converts them to plain text.
        let send = self
            .tool_router
            .map
            .get("send")
            .expect("send tool must be registered");
        let cancellation = context.ct.clone();
        let validation = inherited
            .and_then(|()| {
                SendArguments::validate_fields(
                    request.arguments.as_ref(),
                    self.languages,
                    self.language_setting,
                )
            })
            .and_then(|()| {
                self.tool_settings
                    .validate_arguments(request.arguments.as_ref())
            });
        let call_future = async {
            validation.map_err(|message| ErrorData::invalid_params(message, None))?;
            (send.call)(ToolCallContext::new(self, request, context)).await
        };
        let result = if waiting_for_startup {
            tokio::select! {
                biased;
                _ = cancellation.cancelled() => Err(ErrorData::internal_error("request cancelled; poll with an empty send for any admitted cell", None)),
                result = call_future => result,
            }
        } else {
            call_future.await
        };
        let mut result = match result {
            Err(error) if error.code == ErrorCode::INVALID_PARAMS => Ok(response_to_tool_result(
                crate::worker_client::Response::tool_error(error.message.into_owned()),
                &call,
                &transcript,
                &self.deliveries,
                &delivery,
            )
            .into()),
            result => result,
        };
        // The warmup driver asks the established discovery owner, not the host
        // PATH or the wording of a runtime error. Ordinary replies are unchanged.
        if runtime_probe
            && let Ok(CallToolResponse::Complete(result)) = &mut result
            && !result.is_error.unwrap_or(false)
            && let Some(available) = runtime_worker.r_available()
        {
            result.meta.get_or_insert_default().insert(
                crate::warmup::RUNTIME_META.into(),
                serde_json::json!({ "r": available }),
            );
        }
        let result = Arc::new(result);
        let recorder = transcript.clone();
        let recording_result = Arc::clone(&result);
        let completion = recording.clone();
        if let Err(error) = tokio::task::spawn_blocking(move || {
            let _completion = completion;
            recorder.finish(call, recording_result.as_ref());
        })
        .await
        {
            transcript.disable(format!("transcript task failed: {error}"));
        }
        let result =
            Arc::into_inner(result).expect("transcript task should release the tool result");
        operation.complete();
        recording.response(result)
    }
}

/// Runs the MCP stdio server and owns the selected worker.
///
/// Closing MCP input also stops a worker whose evaluation is still running.
#[allow(clippy::too_many_arguments)]
pub async fn run(
    worker: Option<PathBuf>,
    relay: Option<PathBuf>,
    sandbox: SandboxConfiguration,
    python: Option<crate::settings::PythonChoice>,
    r: crate::settings::R,
    startup: Option<crate::settings::startup::Startup>,
    visibility: Option<Languages>,
    tool_settings: crate::settings::Tool,
) -> Result<(), Box<dyn Error>> {
    let (input_closed, wait_for_input_close) = oneshot::channel();
    let input_closed = InputClosed(Arc::new(Mutex::new(Some(input_closed))));
    #[cfg(not(windows))]
    let input = tokio::io::stdin();
    let server = ConsoleServer::new(
        input_closed.clone(),
        worker,
        relay,
        sandbox,
        python,
        r,
        startup,
        visibility,
        tool_settings,
    )
    .map_err(std::io::Error::other)?;
    let startup = server.startup.clone();
    let deliveries = server.deliveries.clone();
    #[cfg(windows)]
    let input = {
        let startup = startup.clone();
        let closed = input_closed.clone();
        input_windows::Input::new(move || {
            // Physical EOF can cancel unfinished preparation even if protocol
            // output is blocked. Once startup finishes, ShutdownReader alone
            // reports EOF after the queued MCP input has been consumed.
            if !startup.runtime().worker.startup_finished() {
                closed.close();
            }
        })?
    };
    let input = ShutdownReader::new(input, input_closed);
    let inheritance = server.inheritance.clone();
    let transport = crate::server_transport::ServerTransport::new(
        input,
        tokio::io::stdout(),
        server.deliveries.clone(),
    )
    .with_recording(server.startup.runtime().transcript.clone())
    .with_send_observer(move |call| {
        // Pending response reservations do not order runnable handlers. Capture
        // before dispatch so cancellation or scheduling cannot select a later policy.
        let inherited = inheritance.capture(call.extensions.get_or_insert_default());
        call.extensions.insert(sandbox::CaptureResult(inherited));
    });
    let recordings = transport.recording_queue();
    let startup_responses = deliveries.clone();
    let shutdown = async move {
        let shutdown_started = wait_for_input_close
            .await
            .unwrap_or_else(|_| Instant::now());
        let deadline = shutdown_started + WORKER_SHUTDOWN_GRACE;
        let cancellation = startup.cancel(deadline).await;
        let result = match startup.ready().await {
            Ok(runtime) => runtime.worker.shutdown(deadline).await,
            Err(error) => {
                let runtime = startup.runtime();
                let retirement = if runtime.worker.is_configured() {
                    runtime.worker.shutdown(deadline).await
                } else {
                    Ok(())
                };
                retirement.and_then(|()| startup.finish_failed_preparation(error))
            }
        };
        deliveries
            .settle_before_close(Instant::now() + WORKER_SHUTDOWN_GRACE)
            .await;
        // Cancellation releases response admission, not capture ownership.
        // Settle every received send before finalizing its journal and projections.
        recordings.close_and_settle().await;
        let runtime = startup.runtime();
        let diagnostics = runtime.transcript.clone();
        if let Err(error) = tokio::task::spawn_blocking(move || {
            runtime.worker.finish_recording();
            runtime.transcript.finalize();
        })
        .await
        {
            diagnostics.disable(format!("recording finalization task failed: {error}"));
        }
        cancellation?;
        result?;
        Ok::<(), String>(())
    };

    tokio::pin!(shutdown);
    const CLOSED_BEFORE_INITIALIZATION: &str = "server startup cancelled because MCP input closed";
    let service = tokio::select! {
        result = server.serve(transport) => match result {
            Ok(service) => service,
            Err(error) => {
                // Shutdown can wake a blocked initialization write before this
                // branch settles. Preserve its input-closure diagnosis.
                let closed_by_shutdown = startup_responses.is_closed();
                shutdown.await.map_err(std::io::Error::other)?;
                if matches!(&error, rmcp::service::ServerInitializeError::ConnectionClosed(_))
                    && startup_responses.bootstrap_complete()
                {
                    return Ok(());
                }
                return Err(match error {
                    rmcp::service::ServerInitializeError::ConnectionClosed(_) =>
                        std::io::Error::other(CLOSED_BEFORE_INITIALIZATION).into(),
                    _ if closed_by_shutdown =>
                        std::io::Error::other(CLOSED_BEFORE_INITIALIZATION).into(),
                    error => error.into(),
                });
            }
        },
        result = &mut shutdown => {
            result.map_err(std::io::Error::other)?;
            if startup_responses.bootstrap_complete() {
                return Ok(());
            }
            return Err(std::io::Error::other(CLOSED_BEFORE_INITIALIZATION).into());
        }
    };
    // Once owned preparation/worker retirement and response settling finish,
    // a blocked protocol write must not keep the process alive.
    tokio::select! {
        result = service.waiting() => {
            shutdown.await.map_err(std::io::Error::other)?;
            result?;
        },
        result = &mut shutdown => result.map_err(std::io::Error::other)?,
    }
    Ok(())
}

#[derive(Clone)]
struct InputClosed(Arc<Mutex<Option<oneshot::Sender<Instant>>>>);

impl InputClosed {
    fn close(&self) {
        if let Some(sender) = self.0.lock().expect("input closure lock").take() {
            let _ = sender.send(Instant::now());
        }
    }
}

/// Reports EOF or reader loss to the owner, sharing one notification with the
/// non-consuming startup observer.
struct ShutdownReader<R> {
    inner: R,
    input_closed: InputClosed,
}

impl<R> ShutdownReader<R> {
    fn new(inner: R, input_closed: InputClosed) -> Self {
        Self {
            inner,
            input_closed,
        }
    }
}

impl<R> Drop for ShutdownReader<R> {
    fn drop(&mut self) {
        self.input_closed.close();
    }
}

impl<R: AsyncRead + Unpin> AsyncRead for ShutdownReader<R> {
    fn poll_read(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        let filled = buffer.filled().len();
        let had_capacity = buffer.remaining() > 0;
        let poll = Pin::new(&mut self.inner).poll_read(context, buffer);
        match poll {
            Poll::Ready(Ok(())) if had_capacity && buffer.filled().len() == filled => {
                self.input_closed.close();
                Poll::Ready(Ok(()))
            }
            Poll::Ready(Err(error)) => {
                self.input_closed.close();
                Poll::Ready(Err(error))
            }
            poll => poll,
        }
    }
}
