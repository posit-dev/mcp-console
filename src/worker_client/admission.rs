use std::sync::Mutex;

/// Server ownership of one accepted cell, retained before and after attachment.
/// Response observation/delivery and worker bootstrap receipts are separate.
pub(super) struct CellAdmission {
    pub(super) generation: super::lifecycle::WorkerGeneration,
    state: Mutex<CellState>,
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
    pub(super) fn new(generation: super::lifecycle::WorkerGeneration) -> Self {
        Self {
            generation,
            state: Mutex::new(CellState {
                phase: AdmissionPhase::Pending,
                outcome: None,
            }),
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

    pub(super) fn finish(&self, outcome: CellOutcome) {
        self.state
            .lock()
            .expect("cell admission lock")
            .outcome
            .get_or_insert(outcome);
    }
}
