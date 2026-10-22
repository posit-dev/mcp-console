use std::sync::Mutex;

/// Server ownership of one accepted cell, retained before and after attachment.
/// Response observation/delivery and worker bootstrap receipts are separate.
pub(super) struct CellAdmission {
    pub(super) generation: super::lifecycle::WorkerGeneration,
    state: Mutex<CellState>,
    transcript: crate::transcript::Transcript,
    call_id: Option<u64>,
}

struct CellState {
    phase: AdmissionPhase,
    outcome: Option<CellOutcome>,
}

enum AdmissionPhase {
    Pending,
    Dispatched,
    Withheld,
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum CellOutcome {
    Completed,
    Failed,
}

impl CellAdmission {
    pub(super) fn new(
        generation: super::lifecycle::WorkerGeneration,
        transcript: crate::transcript::Transcript,
        call_id: Option<u64>,
    ) -> Self {
        transcript.source_admitted(call_id);
        Self {
            generation,
            state: Mutex::new(CellState {
                phase: AdmissionPhase::Pending,
                outcome: None,
            }),
            transcript,
            call_id,
        }
    }

    pub(super) fn outcome(&self) -> Result<Option<CellOutcome>, String> {
        Ok(self
            .state
            .lock()
            .map_err(|_| "cell admission lock poisoned")?
            .outcome)
    }

    /// Queue control under the same short guard that commits Evaluate.
    /// The closure must enqueue only; acknowledgment and pipe I/O follow outside.
    pub(super) fn interrupt<T>(
        &self,
        enqueue: impl FnOnce() -> Result<T, String>,
    ) -> Result<T, String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "cell admission lock poisoned")?;
        if matches!(state.phase, AdmissionPhase::Pending) {
            state.phase = AdmissionPhase::Withheld;
        }
        enqueue()
    }

    pub(super) fn dispatch(
        &self,
        enqueue: impl FnOnce() -> Result<(), String>,
    ) -> Result<bool, String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "cell admission lock poisoned")?;
        if matches!(state.phase, AdmissionPhase::Withheld) {
            return Ok(false);
        }
        assert!(matches!(state.phase, AdmissionPhase::Pending) && state.outcome.is_none());
        enqueue()?;
        state.phase = AdmissionPhase::Dispatched;
        Ok(true)
    }

    /// Settle recorded source at the first terminal outcome, including failures
    /// committed by operation retirement before evaluation completion runs.
    pub(super) fn finish(&self, outcome: CellOutcome) {
        let mut state = self.state.lock().expect("cell admission lock");
        if state.outcome.is_some() {
            return;
        }
        state.outcome = Some(outcome);
        drop(state);
        self.transcript.source_finished(self.call_id);
    }
}
