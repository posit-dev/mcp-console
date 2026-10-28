use std::sync::mpsc::SyncSender;
use std::sync::{Arc, Mutex};

mod terminal;

mod preview;
mod tape;
use preview::{Part, Preview};
use tape::OutputTapeState;

pub(super) const WORKER_STARTING_NOTICE: &str = "starting new worker";
const WORKER_STARTING_STATE: &str = "worker starting";
pub(super) const WORKER_STOPPED_NOTICE: &str = "worker stopped: in-memory state lost";
pub(super) const WORKER_IDLE_NOTICE: &str = "idle";
pub(super) const EVALUATION_STOPPED_BY_RESTART_NOTICE: &str =
    "stopped by session restart request before evaluation finished";
pub(super) const ACTIVE_EVALUATION_STOPPED_NOTICE: &str =
    "active evaluation stopped by session restart request";

/// Stores pending session output in publication order until one response drains it.
#[derive(Clone)]
pub(super) struct OutputTape(Arc<Mutex<OutputTapeState>>);

/// An opaque boundary between sealed response intervals.
#[derive(Clone, Copy)]
pub(super) struct OutputCut {
    sequence: u64,
    pub(super) worker_revision: u64,
}

pub(super) struct DirectOutput {
    output: OutputTape,
    stream: DirectOutputStream,
}

#[derive(Clone, Copy)]
enum DirectOutputStream {
    Stdout,
    Stderr,
}

#[derive(Default)]
pub(crate) struct Response {
    preview: Box<Preview>,
    is_error: bool,
    delivery: Option<ResponseDeliveryTarget>,
    phase_terminal: Option<TerminalState>,
    phase: Option<super::status::Source>,
    kernel_state: Option<KernelState>,
    failure_message: Option<Box<str>>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum KernelState {
    Completed,
    Running,
    InputRequested,
    Idle,
    Starting,
    ReplacementReady,
    Failed,
    Restarted,
}

/// Ordered publications before the MCP response projection or its budgets.
/// The kernel process retains this subscription across frontend reconnects.
pub(crate) enum KernelEvent {
    Stream { name: &'static str, text: String },
    Display { data: String, mime_type: String },
    Result { text: String },
    InputRequested { prompt: String },
    InputEnded,
    ExecutionError(crate::worker_protocol::ExecutionError),
}

pub(super) enum ResponseAcknowledgment {
    Delivered,
    Unclaimed(Response),
}

enum ResponseDeliveryTarget {
    Evaluation(SyncSender<ResponseAcknowledgment>, Arc<tokio::sync::Notify>),
    Output(OutputTape),
}

/// Reports whether an assembled console response reached the MCP transport.
///
/// The bounded recovery response remains owned here after MCP projection so transport
/// cancellation or write failure can return the complete reply to restart.
pub(crate) struct ResponseDelivery {
    target: Option<ResponseDeliveryTarget>,
    unclaimed: Option<Response>,
}

#[derive(Clone)]
pub(crate) enum Content {
    Text(String),
    Image {
        data: String,
        mime_type: String,
        artifact: Option<crate::transcript::Artifact>,
    },
}

/// Constructs response regions and control state before bounded MCP projection.
#[derive(Default)]
pub(super) struct ResponseBuilder {
    response: Response,
}

#[derive(Clone, Copy)]
pub(super) enum TerminalState {
    Completed,
    Running,
    StdinNeeded,
    Idle,
    WorkerStarting,
    ReplacementReady,
}

pub(super) enum SendResponse {
    Idle(Response),
    Failed(Response),
    Running(Response),
    InputRequested(Response),
    Completed(Response),
    ReplacementStarting(Response),
    ReplacementReady(Response),
    Restarted(Response),
}

pub(super) struct SendFailure {
    pub(super) message: String,
    pub(super) worker_stopped: bool,
    preceded_restart: bool,
    worker_outcome: Option<super::WorkerProcessOutcome>,
}

impl From<String> for SendFailure {
    fn from(message: String) -> Self {
        Self {
            message,
            worker_stopped: false,
            preceded_restart: false,
            worker_outcome: None,
        }
    }
}

impl SendFailure {
    pub(super) fn worker_stopped(mut self) -> Self {
        self.worker_stopped = true;
        self
    }

    pub(super) fn worker_outcome(mut self, outcome: Option<super::WorkerProcessOutcome>) -> Self {
        self.worker_outcome = outcome;
        self
    }

    pub(super) fn preceded_restart(mut self) -> Self {
        self.preceded_restart = true;
        self
    }

    pub(super) fn should_survive_restart(&self) -> bool {
        self.worker_stopped || self.preceded_restart
    }
}

impl Response {
    pub(crate) fn kernel_state(&self) -> Option<KernelState> {
        self.kernel_state
    }

    pub(crate) fn failure_message(&self) -> Option<&str> {
        self.failure_message.as_deref()
    }

    pub(super) fn restarted(&mut self) {
        self.kernel_state = Some(KernelState::Restarted);
    }

    /// Copy presentation state without claiming or acknowledging its delivery.
    pub(super) fn snapshot(&self) -> Self {
        Self {
            preview: Box::new(self.preview.inspection_copy()),
            is_error: self.is_error,
            delivery: None,
            phase_terminal: None,
            phase: None,
            kernel_state: self.kernel_state,
            failure_message: self.failure_message.clone(),
        }
    }

    pub(super) fn preview(&self) -> Vec<Content> {
        self.preview.inspection_copy().render_preview()
    }

    /// The integrator collector owns its result even if its socket disconnects.
    /// Acknowledge that ownership so MCP cannot recover the private response.
    pub(super) fn settle_delivery(&mut self) {
        if let Some(target) = self.delivery.take() {
            target.delivered();
        }
    }

    pub(crate) fn tool_error(message: String) -> Self {
        let mut response = Self::default();
        response.push_tool_error(message);
        response
    }

    /// Consumes the response for the MCP adapter.
    pub(crate) fn into_parts(
        mut self,
    ) -> (
        Vec<Content>,
        bool,
        Option<ResponseDelivery>,
        Vec<crate::transcript::ResultPart>,
    ) {
        // Phase observations are rendered inside the ordinary complete-response
        // budget, but never enter retained output or delivery recovery history.
        let (content, recorded_parts) = match self
            .phase
            .as_ref()
            .filter(|_| self.phase_terminal.is_some())
            .and_then(|source| source.phase())
        {
            Some(phase) => {
                self.preview.bound();
                let mut preview = self.preview.clone();
                preview.phase_before_terminal(
                    phase,
                    matches!(self.phase_terminal, Some(TerminalState::Running)),
                );
                preview.render_recorded()
            }
            None => self.preview.render_recorded(),
        };
        let is_error = self.is_error;
        let delivery = self.delivery.take().map(|target| ResponseDelivery {
            target: Some(target),
            unclaimed: Some(Response {
                preview: std::mem::take(&mut self.preview),
                is_error,
                delivery: None,
                phase_terminal: self.phase_terminal,
                phase: self.phase.take(),
                kernel_state: self.kernel_state,
                failure_message: self.failure_message.take(),
            }),
        });
        (content, is_error, delivery, recorded_parts)
    }

    pub(super) fn observe_phase(&mut self, source: Option<super::status::Source>) {
        // Preserve a retained observation only when current capture is unavailable.
        if let Some(source) = source {
            self.phase = Some(source);
        }
    }

    pub(super) fn extend(&mut self, mut other: Self) {
        self.with_builder(|builder| builder.append_response(&mut other));
    }

    /// Appends another logical response region without inserting a server notice.
    pub(super) fn extend_logical_region(&mut self, other: Self) {
        self.with_builder(|builder| builder.append_logical_region(other));
    }

    /// Appends cell output after this response's owned idle prelude.
    ///
    /// The canonical builder inserts the separator only when both regions are
    /// nonempty, preserving images and their order on either side.
    pub(super) fn extend_cell_after_idle_prelude(&mut self, other: Self) {
        self.with_builder(|builder| builder.append_cell_after_idle_prelude(other));
    }

    pub(super) fn acknowledge_with(
        &mut self,
        acknowledgment: SyncSender<ResponseAcknowledgment>,
        changed: Arc<tokio::sync::Notify>,
    ) {
        assert!(
            self.delivery.is_none(),
            "a response can carry only one acknowledgment"
        );
        self.delivery = Some(ResponseDeliveryTarget::Evaluation(acknowledgment, changed));
    }

    pub(super) fn recover_to(&mut self, output: OutputTape) {
        assert!(
            self.delivery.is_none(),
            "a response can carry only one delivery target"
        );
        self.delivery = Some(ResponseDeliveryTarget::Output(output));
    }

    fn is_empty(&self) -> bool {
        self.preview.is_empty()
    }

    pub(crate) fn is_error(&self) -> bool {
        self.is_error
    }

    pub(crate) fn push_notice(&mut self, message: impl Into<String>) {
        self.with_builder(|builder| builder.notice(message));
    }

    /// Adds a server notice and ends its line for any output appended later.
    pub(super) fn push_notice_line(&mut self, message: impl Into<String>) {
        self.with_builder(|builder| builder.notice_line(message));
    }

    pub(super) fn evaluation_progress(
        &mut self,
        elapsed: std::time::Duration,
        no_new_output: bool,
    ) {
        self.with_builder(|builder| {
            let silence = if no_new_output { "; no new output" } else { "" };
            builder.state_banner(&format!(
                "elapsed: {:.1}s since admission{silence}",
                elapsed.as_secs_f64()
            ));
        });
    }

    pub(super) fn push_tool_error(&mut self, message: impl Into<String>) {
        self.with_builder(|builder| builder.tool_error(message));
    }

    pub(super) fn push_failure(&mut self, failure: SendFailure) {
        self.with_builder(|builder| builder.send_failure(failure));
    }

    pub(super) fn mark_error(&mut self) {
        self.with_builder(ResponseBuilder::mark_error);
    }

    fn with_builder(&mut self, operation: impl FnOnce(&mut ResponseBuilder)) {
        let mut builder = ResponseBuilder::from_response(std::mem::take(self));
        operation(&mut builder);
        *self = builder.finish();
    }
}

impl ResponseBuilder {
    pub(super) fn new() -> Self {
        Self::default()
    }

    pub(super) fn from_response(response: Response) -> Self {
        Self { response }
    }

    pub(super) fn finish(self) -> Response {
        self.response
    }

    pub(super) fn image(
        &mut self,
        data: String,
        mime_type: String,
        artifact: Option<crate::transcript::Artifact>,
    ) {
        self.response.preview.image(Arc::new(Content::Image {
            data,
            mime_type,
            artifact,
        }));
    }

    pub(super) fn append_response(&mut self, other: &mut Response) {
        if other.delivery.is_some() {
            assert!(
                self.response.delivery.is_none(),
                "a response can carry only one delivery target"
            );
            self.response.delivery = other.delivery.take();
        }
        self.response
            .preview
            .extend(*std::mem::take(&mut other.preview));
        self.response.is_error |= other.is_error;
        if other.kernel_state.is_some() {
            self.response.kernel_state = other.kernel_state;
        }
        if other.failure_message.is_some() {
            self.response.failure_message = other.failure_message.take();
        }
    }

    pub(super) fn append_logical_region(&mut self, mut other: Response) {
        if matches!(
            self.response.preview.last_visible(),
            Some(Part::Text(_) | Part::Notice(_))
        ) && !self.response.preview.ends_with_newline()
            && other.preview.starts_with_text()
        {
            self.response.preview.notice("\n".to_owned());
        }
        self.append_response(&mut other);
    }

    pub(super) fn append_cell_after_idle_prelude(&mut self, mut cell: Response) {
        if !self.response.is_empty() && !cell.is_empty() {
            self.notice_line("output produced while idle");
        }
        self.append_response(&mut cell);
    }

    pub(super) fn notice(&mut self, message: impl Into<String>) {
        self.control_text(render_notice(message));
    }

    pub(super) fn notice_line(&mut self, message: impl Into<String>) {
        self.control_text(format!("{}\n", render_notice(message)));
    }

    fn control_text(&mut self, text: String) {
        let prefix = if !self.response.is_empty() && self.needs_line_break() {
            "\n"
        } else {
            ""
        };
        self.response.preview.notice(format!("{prefix}{text}"));
    }

    pub(super) fn server_failure(&mut self, message: impl Into<String>) {
        let message = message.into();
        self.response.failure_message = Some(message.clone().into_boxed_str());
        self.notice(message);
        self.mark_error();
    }

    fn send_failure(&mut self, failure: SendFailure) {
        self.server_failure(failure.message);
        if let Some(outcome) = failure.worker_outcome {
            self.notice(outcome.diagnostic());
        }
        if failure.worker_stopped {
            self.notice(WORKER_STOPPED_NOTICE);
        }
    }

    pub(super) fn tool_error(&mut self, message: impl Into<String>) {
        let message = message.into();
        self.response.failure_message = Some(message.clone().into_boxed_str());
        self.control_text(message);
        self.mark_error();
    }

    pub(super) fn terminal(&mut self, state: TerminalState) {
        self.response.kernel_state = Some(match state {
            TerminalState::Completed => KernelState::Completed,
            TerminalState::Running => KernelState::Running,
            TerminalState::StdinNeeded => KernelState::InputRequested,
            TerminalState::Idle => KernelState::Idle,
            TerminalState::WorkerStarting => KernelState::Starting,
            TerminalState::ReplacementReady => KernelState::ReplacementReady,
        });
        self.response.phase_terminal = match state {
            TerminalState::Running | TerminalState::WorkerStarting => Some(state),
            _ => None,
        };
        match state {
            TerminalState::Completed => {
                if self.response.is_empty() {
                    self.notice("done");
                }
            }
            TerminalState::Running => self.state_banner("running; poll with an empty send"),
            TerminalState::StdinNeeded => {
                let prefix = if self.needs_line_break() { "\n" } else { "" };
                self.response
                    .preview
                    .notice(format!("{prefix}{}", render_notice("waiting for stdin")));
            }
            TerminalState::Idle => {
                if !self.response.is_error() {
                    self.state_banner(WORKER_IDLE_NOTICE);
                }
            }
            TerminalState::WorkerStarting => self.notice(WORKER_STARTING_STATE),
            TerminalState::ReplacementReady => self.notice(WORKER_IDLE_NOTICE),
        }
    }

    pub(super) fn mark_error(&mut self) {
        self.response.is_error = true;
    }

    fn state_banner(&mut self, state: &str) {
        self.response
            .preview
            .notice(format!("\n{}", render_notice(state)));
    }

    fn needs_line_break(&self) -> bool {
        !self.response.preview.ends_with_newline()
    }
}

impl ResponseDelivery {
    pub(crate) fn delivered(mut self) {
        self.unclaimed = None;
        if let Some(target) = self.target.take() {
            target.delivered();
        }
    }

    pub(crate) fn unclaimed(mut self) {
        self.return_unclaimed();
    }

    fn return_unclaimed(&mut self) {
        let Some(target) = self.target.take() else {
            return;
        };
        let response = self
            .unclaimed
            .take()
            .expect("response delivery with a target must retain its reply");
        target.unclaimed(response);
    }
}

impl Drop for ResponseDelivery {
    fn drop(&mut self) {
        self.return_unclaimed();
    }
}

impl Drop for Response {
    fn drop(&mut self) {
        let Some(target) = self.delivery.take() else {
            return;
        };
        let response = Self {
            preview: std::mem::take(&mut self.preview),
            is_error: self.is_error,
            delivery: None,
            phase_terminal: self.phase_terminal,
            phase: self.phase.take(),
            kernel_state: self.kernel_state,
            failure_message: self.failure_message.take(),
        };
        target.unclaimed(response);
    }
}

impl ResponseDeliveryTarget {
    fn delivered(self) {
        if let Self::Evaluation(acknowledgment, changed) = self {
            let _ = acknowledgment.send(ResponseAcknowledgment::Delivered);
            changed.notify_waiters();
        }
    }

    fn unclaimed(self, response: Response) {
        match self {
            Self::Evaluation(acknowledgment, changed) => {
                let _ = acknowledgment.send(ResponseAcknowledgment::Unclaimed(response));
                changed.notify_waiters();
            }
            Self::Output(output) => output.recover(response),
        }
    }
}

pub(super) fn project_completed(output: Response) -> Response {
    project_terminal(output, TerminalState::Completed)
}

pub(super) fn project_controlled_completed(output: Response) -> Response {
    if output.is_error() {
        return output;
    }
    let mut builder = ResponseBuilder::from_response(output);
    builder.notice("done");
    builder.finish()
}

pub(super) fn project_replacement_ready(output: Response) -> Response {
    project_terminal(output, TerminalState::ReplacementReady)
}

pub(super) fn render_response(response: SendResponse) -> Response {
    let (output, terminal) = match response {
        SendResponse::Completed(output) => (output, Some(TerminalState::Completed)),
        SendResponse::Failed(mut output) => {
            output.kernel_state = Some(KernelState::Failed);
            (output, None)
        }
        SendResponse::Restarted(output) => (output, None),
        SendResponse::InputRequested(output) => (output, Some(TerminalState::StdinNeeded)),
        SendResponse::Running(output) => (output, Some(TerminalState::Running)),
        SendResponse::Idle(output) => (output, Some(TerminalState::Idle)),
        SendResponse::ReplacementStarting(output) => (output, Some(TerminalState::WorkerStarting)),
        SendResponse::ReplacementReady(output) => (output, Some(TerminalState::ReplacementReady)),
    };
    match terminal {
        Some(terminal) => project_terminal(output, terminal),
        None => output,
    }
}

fn project_terminal(output: Response, terminal: TerminalState) -> Response {
    let mut builder = ResponseBuilder::from_response(output);
    builder.terminal(terminal);
    builder.finish()
}

pub(super) fn direct_failure(message: impl Into<String>) -> Response {
    let mut builder = ResponseBuilder::new();
    builder.server_failure(message);
    builder.finish()
}

fn render_notice(message: impl Into<String>) -> String {
    format!("[{}]", message.into())
}

fn utf8_prefix_length(text: &str, limit: usize) -> usize {
    let mut length = text.len().min(limit);
    while !text.is_char_boundary(length) {
        length -= 1;
    }
    length
}
