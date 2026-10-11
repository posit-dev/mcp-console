use std::sync::{Arc, Mutex, mpsc};
use std::time::{Duration, Instant};

use super::output::{
    OutputCut, OutputTape, Response, ResponseAcknowledgment, SendFailure, project_completed,
    project_controlled_completed, project_replacement_ready,
};

const INPUT_REQUEST_GRACE: Duration = Duration::from_millis(10);
const CELL_COMPLETION_GRACE: Duration = Duration::from_millis(1);

pub(super) struct Evaluation {
    pub(super) admission: super::admission::CellAdmission,
    state: Mutex<EvaluationState>,
    changed: tokio::sync::Notify,
    delivery_changed: Arc<tokio::sync::Notify>,
    transcript: crate::transcript::Transcript,
    call_id: Option<u64>,
    output: OutputTape,
    runtime: tokio::runtime::Handle,
    admitted_at: Instant,
}

struct EvaluationState {
    phase: EvaluationPhase,
    completion_cut: Option<OutputCut>,
    /// Prior operation and lifecycle output transferred into this cell's response.
    control_prelude: Option<Response>,
    /// Idle output captured immediately before this cell was admitted.
    idle_prelude: Option<Response>,
    /// A response returned by the server but not claimed by its MCP transport.
    reclaimed: Option<Response>,
    /// Delivery of the most recently assembled response, until one owner settles it.
    delivery: Option<mpsc::Receiver<ResponseAcknowledgment>>,
    delivery_changed: Arc<tokio::sync::Notify>,
    /// Whether a waiter already drained the response for a completion phase.
    completion_collected: bool,
    /// Whether successful cell completion must end with an explicit final marker.
    controlled_completion: bool,
    input_request: Option<InputRequest>,
    /// Whether one `send` currently owns the right to drain this evaluation's response.
    waiting: bool,
    /// Restart or controlled handoff permanently retires this evaluation.
    /// Releasing its response reservation must not revive late task failures.
    retired: bool,
    restart_handoff: Option<Response>,
    #[cfg(any(unix, windows))]
    stdin: Option<super::platform::StdinSender>,
    pending_stdin: String,
    observed_worker_revision: u64,
}

struct InputRequest {
    prompt: String,
    report_at: Instant,
}

#[derive(Clone, Copy)]
enum EvaluationPhase {
    Evaluating,
    CellCompletionGrace(Instant),
    ReplacementStarting,
    Complete(CompletionKind),
}

#[derive(Clone, Copy)]
enum CompletionKind {
    Cell,
    ReplacementReady,
    ReplacementFailed,
}

pub(super) enum EvaluationWait {
    Running(Response),
    InputRequested(Response),
    Completed(Response),
    ReplacementStarting(Response),
    ReplacementReady(Response),
    Reclaimed(Response),
    Restarted(Response),
}

enum EvaluationStatus {
    Waiting,
    Grace(Duration),
    Report(EvaluationWait),
}

pub(super) struct EvaluationReservation {
    evaluation: Arc<Evaluation>,
    unfinished: bool,
    project_completion: bool,
    controlled_completion: bool,
    completion: Option<CompletionKind>,
    completion_cut: Option<OutputCut>,
    reclaimed: Option<Response>,
    delivery: Option<mpsc::Receiver<ResponseAcknowledgment>>,
    pub(super) waiting: bool,
}

pub(super) enum RestartDelivery {
    Waiting(mpsc::Receiver<ResponseAcknowledgment>),
    Unclaimed(Response),
}

pub(super) struct RestartDeliveryFailure {
    pub(super) message: String,
    pub(super) response: Response,
}

/// Releases one response-draining claim whenever its `send` path exits.
pub(super) struct WaitClaim {
    evaluation: Arc<Evaluation>,
}

#[derive(Clone, Copy)]
enum WaitKind {
    Poll,
    Interrupt,
}

impl Evaluation {
    #[allow(clippy::too_many_arguments)]
    pub(super) fn new(
        generation: super::lifecycle::WorkerGeneration,
        transcript: crate::transcript::Transcript,
        call_id: Option<u64>,
        output: OutputTape,
        control_prelude: Response,
        idle_prelude: Response,
        controlled_completion: bool,
        worker_revision: u64,
    ) -> Self {
        let delivery_changed = Arc::new(tokio::sync::Notify::new());
        Self {
            admission: super::admission::CellAdmission::new(generation),
            state: Mutex::new(EvaluationState {
                phase: EvaluationPhase::Evaluating,
                completion_cut: None,
                control_prelude: Some(control_prelude),
                idle_prelude: Some(idle_prelude),
                reclaimed: None,
                delivery: None,
                delivery_changed: Arc::clone(&delivery_changed),
                completion_collected: false,
                controlled_completion,
                input_request: None,
                waiting: false,
                retired: false,
                restart_handoff: None,
                #[cfg(any(unix, windows))]
                stdin: None,
                pending_stdin: String::new(),
                observed_worker_revision: worker_revision,
            }),
            changed: tokio::sync::Notify::new(),
            delivery_changed,
            transcript,
            call_id,
            output,
            runtime: tokio::runtime::Handle::current(),
            admitted_at: Instant::now(),
        }
    }

    fn claim_wait(self: &Arc<Self>) -> Result<WaitClaim, String> {
        self.try_claim_wait(WaitKind::Poll)?
            .ok_or_else(|| "previous send response delivery is still pending".to_string())
    }

    fn try_claim_wait(self: &Arc<Self>, kind: WaitKind) -> Result<Option<WaitClaim>, String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        if !state.settle_delivery()? {
            return Ok(None);
        }
        if state.completion_collected && state.reclaimed.is_none() {
            if matches!(kind, WaitKind::Interrupt) {
                return Ok(None);
            }
            return Err("evaluation response was already delivered".to_string());
        }
        if state.retired {
            return Err("session restart began before this send could wait".to_string());
        }
        if state.waiting {
            if matches!(kind, WaitKind::Interrupt) {
                return Ok(None);
            }
            return Err("worker evaluation is already being polled".to_string());
        }
        state.waiting = true;
        Ok(Some(WaitClaim {
            evaluation: self.clone(),
        }))
    }

    /// None omits a busy observation; false leaves other owners observable.
    pub(super) fn replacement_observation(&self) -> Option<bool> {
        let state = self.state.try_lock().ok()?;
        Some(!state.retired && matches!(state.phase, EvaluationPhase::ReplacementStarting))
    }

    /// Reserves an open response until restart finishes retiring the worker.
    pub(super) fn reserve_for_restart(self: &Arc<Self>) -> Result<EvaluationReservation, String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        if let EvaluationPhase::CellCompletionGrace(deadline) = state.phase {
            drop(state);
            std::thread::sleep(deadline.saturating_duration_since(Instant::now()));
            self.finish_cell_completion_grace();
            state = self
                .state
                .lock()
                .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        }
        state.retired = true;
        let waiting = state.waiting;
        let unfinished = !matches!(state.phase, EvaluationPhase::Complete(_));
        let completion = match state.phase {
            EvaluationPhase::Complete(completion) if !state.completion_collected => {
                Some(completion)
            }
            EvaluationPhase::Complete(_) => None,
            EvaluationPhase::Evaluating | EvaluationPhase::ReplacementStarting => None,
            EvaluationPhase::CellCompletionGrace(_) => {
                unreachable!("completion grace is handled before restart reservation")
            }
        };
        Ok(EvaluationReservation {
            evaluation: self.clone(),
            unfinished,
            project_completion: true,
            controlled_completion: state.controlled_completion,
            completion,
            completion_cut: completion.and(state.completion_cut),
            reclaimed: state.reclaimed.take(),
            delivery: state.delivery.take(),
            waiting,
        })
    }

    /// Reserves a completed response for a following cell without adding a terminal marker.
    pub(super) fn reserve_completed_for_handoff(
        self: &Arc<Self>,
    ) -> Result<Option<EvaluationReservation>, String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        if let EvaluationPhase::CellCompletionGrace(deadline) = state.phase {
            drop(state);
            std::thread::sleep(deadline.saturating_duration_since(Instant::now()));
            self.finish_cell_completion_grace();
            state = self
                .state
                .lock()
                .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        }
        let EvaluationPhase::Complete(completion_kind) = state.phase else {
            return Ok(None);
        };
        state.retired = true;
        let completion = (!state.completion_collected).then_some(completion_kind);
        Ok(Some(EvaluationReservation {
            evaluation: self.clone(),
            unfinished: false,
            project_completion: false,
            controlled_completion: state.controlled_completion,
            completion,
            completion_cut: completion.and(state.completion_cut),
            reclaimed: state.reclaimed.take(),
            delivery: state.delivery.take(),
            waiting: state.waiting,
        }))
    }

    /// Reaps a completed evaluation only after its assembled response was delivered.
    pub(super) fn reap_delivered_completion(&self) -> Result<bool, String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        if !state.settle_delivery()? {
            return Ok(false);
        }
        Ok(matches!(state.phase, EvaluationPhase::Complete(_))
            && state.completion_collected
            && state.delivery.is_none()
            && state.reclaimed.is_none()
            && !state.waiting
            && !state.retired)
    }

    /// Adopts output that remained idle until the worker operation became active.
    pub(super) fn capture_prelude_before(
        &self,
        capture_idle_prelude: bool,
        boundary: impl FnOnce(),
    ) -> Result<(), String> {
        let (cell_output, failure) = match self.transcript.create_cell_output(self.call_id) {
            Ok(cell_output) => (cell_output, None),
            Err(error) => (
                None,
                Some(format!(
                    "cell output file was not created: {error}; text omitted from inline responses will be permanently discarded"
                )),
            ),
        };
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        let additional =
            self.output
                .begin_cell_output_before(cell_output, capture_idle_prelude, boundary);
        if let Some(prelude) = state.idle_prelude.as_mut() {
            prelude.extend(additional);
        } else {
            state.idle_prelude = Some(additional);
        }
        drop(state);
        if let Some(failure) = failure {
            self.output.push_notice_line(failure);
        }
        Ok(())
    }

    /// Queues text and briefly defers any outstanding input report for its receipt.
    pub(super) fn submit_stdin(&self, stdin: String) -> Result<(), String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        if stdin.is_empty() {
            return Ok(());
        }

        if let Some(request) = state.input_request.as_mut() {
            request.report_at = Instant::now() + INPUT_REQUEST_GRACE;
        }
        #[cfg(any(unix, windows))]
        if let Some(writer) = &state.stdin {
            writer.send(stdin)?;
            return Ok(());
        }
        state.pending_stdin.push_str(&stdin);
        Ok(())
    }

    #[cfg(any(unix, windows))]
    pub(super) fn attach_writer(&self, writer: super::platform::StdinSender) -> Result<(), String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        if state.stdin.is_some() {
            return Err("worker stdin was already attached to this evaluation".to_string());
        }
        if !state.pending_stdin.is_empty() {
            writer.send(std::mem::take(&mut state.pending_stdin))?;
        }
        state.stdin = Some(writer);
        Ok(())
    }

    pub(super) fn output(
        &self,
        channel: crate::worker_protocol::ConsoleChannel,
        output: String,
    ) -> Result<(), String> {
        self.output.push_console_text(channel, output);
        Ok(())
    }

    pub(super) fn bounded_notice(&self, message: String) -> Result<(), String> {
        self.output.push_bounded_notice_line(message);
        Ok(())
    }

    pub(super) fn image(&self, data: String, mime_type: String) -> Result<(), String> {
        let bytes = crate::transcript::decode_image_data(&data)?;
        self.output
            .push_image_with_artifact(data, mime_type, |_, mime_type| {
                Ok(self
                    .transcript
                    .persist_decoded_image(self.call_id, &bytes, mime_type))
            })
    }

    pub(super) fn input_requested(&self, prompt: String) -> Result<(), String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        if state.input_request.is_some() {
            return Err("worker requested new input before receiving prior input".to_string());
        }
        let prompt = serde_json::to_string(&prompt)
            .map_err(|error| format!("failed to render worker input prompt: {error}"))?;
        self.output
            .push_notice_line(format!("input requested: {prompt}"));
        state.input_request = Some(InputRequest {
            prompt,
            report_at: Instant::now() + INPUT_REQUEST_GRACE,
        });
        self.changed.notify_one();
        Ok(())
    }

    pub(super) fn resume_input_request(&self, prompt: String) -> Result<(), String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        if state.input_request.is_some() {
            return Err("worker evaluation already has an outstanding input request".to_string());
        }
        let grace = if state.pending_stdin.is_empty() {
            Duration::ZERO
        } else {
            INPUT_REQUEST_GRACE
        };
        state.input_request = Some(InputRequest {
            prompt,
            report_at: Instant::now() + grace,
        });
        self.changed.notify_one();
        Ok(())
    }

    pub(super) fn take_input_request(&self) -> Result<Option<String>, String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        Ok(state.input_request.take().map(|request| request.prompt))
    }

    pub(super) fn input_received(&self) -> Result<(), String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        state
            .input_request
            .take()
            .ok_or_else(|| "worker reported received input without requesting it".to_string())?;
        self.changed.notify_one();
        Ok(())
    }

    pub(super) fn input_complete(&self) -> Result<(), String> {
        let state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        if state.input_request.is_some() {
            return Err("worker completed with an outstanding input request".to_string());
        }
        Ok(())
    }

    pub(super) fn complete_cell(&self, result: Result<(), SendFailure>) {
        self.admission.finish(if result.is_ok() {
            super::admission::CellOutcome::Completed
        } else {
            super::admission::CellOutcome::Failed
        });
        self.complete(result, CompletionKind::Cell, None);
    }

    pub(super) fn complete_cell_after_grace(self: &Arc<Self>) {
        self.admission
            .finish(super::admission::CellOutcome::Completed);
        // Give raw-output readers a brief window to publish cross-pipe output that
        // arrived with Completed. The cut still leaves later observations for the
        // next response; it does not claim cross-pipe chronology.
        let deadline = Instant::now() + CELL_COMPLETION_GRACE;
        let Ok(mut state) = self.state.lock() else {
            return;
        };
        state.input_request = None;
        state.phase = EvaluationPhase::CellCompletionGrace(deadline);
        state.completion_cut = None;
        state.completion_collected = false;
        self.changed.notify_one();
        drop(state);

        let evaluation = self.clone();
        self.runtime.spawn(async move {
            tokio::time::sleep_until(tokio::time::Instant::from_std(deadline)).await;
            evaluation.finish_cell_completion_grace();
        });
    }

    fn finish_cell_completion_grace(&self) {
        let Ok(mut state) = self.state.lock() else {
            return;
        };
        let EvaluationPhase::CellCompletionGrace(deadline) = state.phase else {
            return;
        };
        if Instant::now() < deadline {
            return;
        }
        state.phase = EvaluationPhase::Complete(CompletionKind::Cell);
        state.completion_cut = Some(self.finish_cell_output());
        state.completion_collected = false;
        self.changed.notify_one();
    }

    pub(super) fn reject_new_cell_message(&self) -> &'static str {
        "worker is already evaluating a cell; poll without a code field"
    }

    pub(super) fn reject_preparation_message(&self) -> &'static str {
        "worker is already evaluating a cell; poll it before preparing requirements"
    }

    pub(super) fn start_replacement(&self, failure: SendFailure) {
        self.admission.finish(super::admission::CellOutcome::Failed);
        let Ok(mut state) = self.state.lock() else {
            return;
        };
        state.input_request = None;
        state.completion_cut = None;
        self.finish_cell_output();
        self.output.push_failure(failure);
        state.phase = EvaluationPhase::ReplacementStarting;
        self.changed.notify_one();
    }

    pub(super) fn finish_replacement(&self, result: Result<(), SendFailure>) {
        let completion = if result.is_ok() {
            CompletionKind::ReplacementReady
        } else {
            CompletionKind::ReplacementFailed
        };
        self.complete(result, completion, None);
    }

    fn complete(
        &self,
        result: Result<(), SendFailure>,
        completion: CompletionKind,
        cut: Option<OutputCut>,
    ) {
        let Ok(mut state) = self.state.lock() else {
            return;
        };
        state.input_request = None;
        if let Err(failure) = result
            && (!state.retired || failure.should_survive_restart())
        {
            self.output.push_failure(failure);
        }
        state.phase = EvaluationPhase::Complete(completion);
        state.completion_cut = Some(cut.unwrap_or_else(|| self.finish_cell_output()));
        state.completion_collected = false;
        self.changed.notify_one();
    }

    /// Records whether a worker failure became observable before restart
    /// cancellation took ownership of this evaluation's response.
    pub(super) fn classify_failure(&self, message: String) -> SendFailure {
        let failure = SendFailure::from(message);
        match self.state.lock() {
            Ok(state) if !state.retired => failure.preceded_restart(),
            Ok(_) | Err(_) => failure,
        }
    }

    fn finish_cell_output(&self) -> OutputCut {
        self.output.finish_cell_output()
    }

    pub(super) fn claim(self: &Arc<Self>) -> Result<WaitClaim, String> {
        self.claim_wait()
    }

    /// Signaling does not transfer another send's polling or delivery ownership.
    /// An empty interrupt observes output only when it can claim it atomically.
    pub(super) fn claim_for_interrupt(self: &Arc<Self>) -> Result<Option<WaitClaim>, String> {
        self.try_claim_wait(WaitKind::Interrupt)
    }

    /// A control response without output ownership cannot claim a silent interval.
    pub(super) fn unobserved_progress(&self) -> Response {
        let mut response = Response::default();
        response.evaluation_progress(self.admitted_at.elapsed(), false);
        response
    }

    /// A poll already waiting for startup can encounter a later call's reply
    /// before its transport write settles. Retain observation within the call's
    /// deadline; expiry or cancellation leaves the response unclaimed.
    pub(super) async fn claim_after_delivery(
        self: &Arc<Self>,
        deadline: Instant,
    ) -> Result<WaitClaim, String> {
        loop {
            let delivered = self.delivery_changed.notified();
            tokio::pin!(delivered);
            delivered.as_mut().enable();
            if let Some(claim) = self.try_claim_wait(WaitKind::Poll)? {
                return Ok(claim);
            }
            tokio::time::timeout(
                deadline.saturating_duration_since(Instant::now()),
                delivered,
            )
            .await
            .map_err(|_| "previous send response delivery is still pending".to_string())?;
        }
    }

    pub(super) async fn wait(
        &self,
        _claim: WaitClaim,
        timeout: Duration,
    ) -> Result<EvaluationWait, String> {
        let started = Instant::now();
        loop {
            let changed = self.changed.notified();
            let grace = match self.reported_state(false)? {
                EvaluationStatus::Waiting => None,
                EvaluationStatus::Grace(grace) => Some(grace),
                EvaluationStatus::Report(state) => break Ok(state),
            };
            let remaining = timeout.saturating_sub(started.elapsed());
            if remaining.is_zero() {
                match self.reported_state(true)? {
                    EvaluationStatus::Report(state) => break Ok(state),
                    EvaluationStatus::Waiting => {
                        changed.await;
                        continue;
                    }
                    EvaluationStatus::Grace(grace) => {
                        let _ = tokio::time::timeout(grace, changed).await;
                        continue;
                    }
                }
            }
            let wait = grace.map_or(remaining, |grace| grace.min(remaining));
            if tokio::time::timeout(wait, changed).await.is_err() {
                if grace.is_some_and(|grace| grace <= remaining) {
                    continue;
                }
                match self.reported_state(true)? {
                    EvaluationStatus::Report(state) => break Ok(state),
                    EvaluationStatus::Waiting => continue,
                    EvaluationStatus::Grace(grace) => {
                        let _ = tokio::time::timeout(grace, self.changed.notified()).await;
                        continue;
                    }
                }
            }
        }
    }

    fn reported_state(&self, at_deadline: bool) -> Result<EvaluationStatus, String> {
        self.finish_cell_completion_grace();
        let mut state = self
            .state
            .lock()
            .map_err(|_| "worker evaluation state lock poisoned".to_string())?;
        if state.retired {
            return Ok(match state.restart_handoff.take() {
                Some(response) => EvaluationStatus::Report(EvaluationWait::Restarted(response)),
                None => EvaluationStatus::Waiting,
            });
        }
        if let Some(mut response) = state.reclaimed.take() {
            state.track_delivery(&mut response);
            return Ok(EvaluationStatus::Report(EvaluationWait::Reclaimed(
                response,
            )));
        }
        match state.phase {
            EvaluationPhase::Complete(
                completion @ (CompletionKind::Cell | CompletionKind::ReplacementFailed),
            ) => {
                state.completion_collected = true;
                let cut = state
                    .completion_cut
                    .take()
                    .expect("a completed evaluation must retain its output cut");
                let mut output = take_owned_response(&mut state, &self.output, cut);
                if matches!(completion, CompletionKind::Cell) && state.controlled_completion {
                    output = project_controlled_completed(output);
                }
                state.track_delivery(&mut output);
                return Ok(EvaluationStatus::Report(EvaluationWait::Completed(output)));
            }
            EvaluationPhase::Complete(CompletionKind::ReplacementReady) => {
                state.completion_collected = true;
                let cut = state
                    .completion_cut
                    .take()
                    .expect("a completed replacement must retain its output cut");
                let mut output = take_owned_response(&mut state, &self.output, cut);
                state.track_delivery(&mut output);
                return Ok(EvaluationStatus::Report(EvaluationWait::ReplacementReady(
                    output,
                )));
            }
            EvaluationPhase::CellCompletionGrace(deadline) => {
                return Ok(EvaluationStatus::Grace(
                    deadline.saturating_duration_since(Instant::now()),
                ));
            }
            EvaluationPhase::ReplacementStarting if at_deadline => {
                let cut = self.output.cut();
                let mut output = take_owned_response(&mut state, &self.output, cut);
                state.track_delivery(&mut output);
                return Ok(EvaluationStatus::Report(
                    EvaluationWait::ReplacementStarting(output),
                ));
            }
            EvaluationPhase::Evaluating | EvaluationPhase::ReplacementStarting => {}
        }
        let Some(request) = state.input_request.as_ref() else {
            if at_deadline {
                let cut = self.output.cut();
                let mut output = take_owned_response(&mut state, &self.output, cut);
                let no_new_output = cut.worker_revision == state.observed_worker_revision;
                state.observed_worker_revision = cut.worker_revision;
                output.evaluation_progress(self.admitted_at.elapsed(), no_new_output);
                state.track_delivery(&mut output);
                return Ok(EvaluationStatus::Report(EvaluationWait::Running(output)));
            }
            return Ok(EvaluationStatus::Waiting);
        };
        let grace = request.report_at.saturating_duration_since(Instant::now());
        if !at_deadline && !grace.is_zero() {
            return Ok(EvaluationStatus::Grace(grace));
        }
        let cut = self.output.cut();
        let mut output = take_owned_response(&mut state, &self.output, cut);
        state.observed_worker_revision = cut.worker_revision;
        state.track_delivery(&mut output);
        Ok(EvaluationStatus::Report(EvaluationWait::InputRequested(
            output,
        )))
    }
}

impl EvaluationState {
    fn track_delivery(&mut self, response: &mut Response) {
        assert!(
            self.delivery.is_none(),
            "an evaluation can have only one response awaiting delivery"
        );
        let (acknowledgment, delivered) = mpsc::sync_channel(1);
        response.acknowledge_with(acknowledgment, Arc::clone(&self.delivery_changed));
        self.delivery = Some(delivered);
    }

    fn settle_delivery(&mut self) -> Result<bool, String> {
        let Some(delivery) = self.delivery.as_ref() else {
            return Ok(true);
        };
        match delivery.try_recv() {
            Ok(ResponseAcknowledgment::Delivered) => {
                self.delivery = None;
                Ok(true)
            }
            Ok(ResponseAcknowledgment::Unclaimed(response)) => {
                self.delivery = None;
                assert!(
                    self.reclaimed.replace(response).is_none(),
                    "an evaluation can reclaim only one response at a time"
                );
                Ok(true)
            }
            Err(mpsc::TryRecvError::Empty) => Ok(false),
            Err(mpsc::TryRecvError::Disconnected) => {
                Err("previous send response delivery acknowledgment stopped".to_string())
            }
        }
    }
}

impl EvaluationReservation {
    pub(super) fn unfinished(&self) -> bool {
        self.unfinished
    }

    fn project_response(&self, response: Response) -> Response {
        if !self.project_completion {
            return response;
        }
        match self.completion {
            Some(CompletionKind::Cell) => {
                if self.controlled_completion {
                    project_controlled_completed(response)
                } else {
                    project_completed(response)
                }
            }
            Some(CompletionKind::ReplacementFailed) => project_completed(response),
            Some(CompletionKind::ReplacementReady) => project_replacement_ready(response),
            None => response,
        }
    }

    pub(super) fn take_output(&mut self, output: &OutputTape) -> (Response, Response) {
        let cut = self
            .completion_cut
            .unwrap_or_else(|| self.evaluation.finish_cell_output());
        let mut state = self
            .evaluation
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let current_output = take_owned_response(&mut state, output, cut);
        drop(state);
        let post_completion = if self.completion_cut.is_some() {
            output.take()
        } else {
            Response::default()
        };
        let current_output = self.project_response(current_output);
        let mut old_output = self.reclaimed.take().unwrap_or_default();
        old_output.extend_logical_region(current_output);
        (old_output, post_completion)
    }

    /// Transfers an already assembled response's delivery claim to restart.
    pub(super) fn take_pending_delivery(
        &mut self,
    ) -> Option<mpsc::Receiver<ResponseAcknowledgment>> {
        self.delivery.take()
    }

    pub(super) fn stopped_notice(&self) -> &'static str {
        super::output::EVALUATION_STOPPED_BY_RESTART_NOTICE
    }

    pub(super) fn active_stopped_notice(&self) -> &'static str {
        super::output::ACTIVE_EVALUATION_STOPPED_NOTICE
    }

    pub(super) fn deliver(
        self,
        mut response: Response,
    ) -> Result<RestartDelivery, RestartDeliveryFailure> {
        let mut state = match self.evaluation.state.lock() {
            Ok(state) => state,
            Err(_) => {
                return Err(RestartDeliveryFailure {
                    message: "worker evaluation state lock poisoned".to_string(),
                    response,
                });
            }
        };
        if !state.waiting {
            return Ok(RestartDelivery::Unclaimed(response));
        }
        // Publish the single settlement without waiting for restart's receiver
        // to be scheduled. Restart still receives it before composing its reply.
        let (acknowledged, wait_for_acknowledgment) = mpsc::sync_channel(1);
        response.acknowledge_with(acknowledged, Arc::clone(&self.evaluation.delivery_changed));
        state.restart_handoff = Some(response);
        self.evaluation.changed.notify_one();
        Ok(RestartDelivery::Waiting(wait_for_acknowledgment))
    }
}

fn take_owned_response(
    state: &mut EvaluationState,
    output: &OutputTape,
    cut: OutputCut,
) -> Response {
    let mut cell = output.drain_through(cut);
    if let Some(mut idle) = state.idle_prelude.take() {
        idle.extend_cell_after_idle_prelude(cell);
        cell = idle;
    }
    let Some(mut control) = state.control_prelude.take() else {
        return cell;
    };
    control.extend_logical_region(cell);
    control
}

impl Drop for WaitClaim {
    fn drop(&mut self) {
        let Ok(mut state) = self.evaluation.state.lock() else {
            return;
        };
        state.waiting = false;
        let handoff = state.restart_handoff.take();
        self.evaluation.changed.notify_one();
        drop(state);
        drop(handoff);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::worker_client::output::{Content, SendResponse, render_response};

    async fn completed_response() -> (Arc<Evaluation>, Response) {
        let output = OutputTape::new();
        let evaluation = Arc::new(Evaluation::new(
            super::super::lifecycle::WorkerGeneration::new(),
            crate::transcript::Transcript::new(true),
            None,
            output,
            Response::default(),
            Response::default(),
            false,
            0,
        ));
        evaluation.complete_cell(Ok(()));
        let claim = evaluation.claim().unwrap();
        let EvaluationWait::Completed(response) =
            evaluation.wait(claim, Duration::ZERO).await.unwrap()
        else {
            panic!("completed evaluation did not assemble its response")
        };
        (
            evaluation,
            render_response(SendResponse::Completed(response)),
        )
    }

    fn response_text(response: Response) -> String {
        let (content, is_error, delivery) = response.into_parts();
        assert!(!is_error);
        assert!(delivery.is_none());
        let [Content::Text(text)] = content.as_slice() else {
            panic!("expected one text response block")
        };
        text.clone()
    }

    #[tokio::test]
    async fn completed_evaluation_remains_until_its_response_is_delivered() {
        let (evaluation, response) = completed_response().await;
        assert!(!evaluation.reap_delivered_completion().unwrap());

        let (_, _, delivery) = response.into_parts();
        delivery.unwrap().delivered();

        assert!(evaluation.reap_delivered_completion().unwrap());
    }

    #[tokio::test]
    async fn unclaimed_completed_response_is_replayed_once() {
        let (evaluation, response) = completed_response().await;
        let (_, _, delivery) = response.into_parts();
        delivery.unwrap().unclaimed();
        assert!(!evaluation.reap_delivered_completion().unwrap());

        let claim = evaluation.claim().unwrap();
        let EvaluationWait::Reclaimed(response) =
            evaluation.wait(claim, Duration::ZERO).await.unwrap()
        else {
            panic!("unclaimed response was not replayed")
        };
        let response = render_response(SendResponse::Restarted(response));
        let (content, is_error, delivery) = response.into_parts();
        assert!(!is_error);
        assert!(matches!(content.as_slice(), [Content::Text(text)] if text == "[done]"));
        delivery.unwrap().delivered();

        assert!(evaluation.reap_delivered_completion().unwrap());
    }

    #[tokio::test]
    async fn restart_claims_an_assembled_response_until_delivery_resolves() {
        let (evaluation, response) = completed_response().await;
        let mut restart = evaluation.reserve_for_restart().unwrap();
        let (_, post_completion) = restart.take_output(&evaluation.output);
        let (post_completion, is_error, delivery) = post_completion.into_parts();
        assert!(post_completion.is_empty());
        assert!(!is_error);
        assert!(delivery.is_none());
        let delivery_result = restart.take_pending_delivery().unwrap();
        assert!(matches!(
            delivery_result.try_recv(),
            Err(mpsc::TryRecvError::Empty)
        ));

        let (_, _, delivery) = response.into_parts();
        delivery.unwrap().unclaimed();
        let ResponseAcknowledgment::Unclaimed(response) = delivery_result.recv().unwrap() else {
            panic!("cancelled response was reported as delivered")
        };
        assert_eq!(response_text(response), "[done]");
        assert!(delivery_result.try_recv().is_err());
    }
}
