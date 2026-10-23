/// The unsupported platform runtime keeps the server boundary portable.
pub(super) struct WorkerRuntime;

pub(super) struct Worker;

impl WorkerRuntime {
    pub(super) fn spawn(
        &self,
        _spec: super::WorkerSpec<'_>,
        _output: super::OutputTape,
        _on_started: impl FnOnce(WorkerShutdownHandle) -> Result<(), String>,
        _on_ready: impl FnOnce() -> Result<(), String>,
    ) -> Result<Worker, super::output::SendFailure> {
        Err(super::output::SendFailure::from(
            "workers are supported only on macOS".to_string(),
        ))
    }
}

impl Worker {
    pub(super) fn reserve_environment_preparation(
        &self,
    ) -> Result<(), super::EnvironmentPreparationAdmissionFailure> {
        unreachable!("unsupported workers cannot start")
    }

    pub(super) fn prepare_r(
        &mut self,
        _library: &std::path::Path,
        _commit: super::RPreparationCommit,
    ) -> Result<super::PreparationOutcome, String> {
        unreachable!("unsupported workers cannot start")
    }

    pub(super) fn prepare_python(
        &mut self,
        packages: Vec<String>,
        requirements: crate::worker_protocol::PythonRequirementManifest,
        continue_environment_preparation: bool,
        duckdb_extensions: Option<std::collections::BTreeSet<String>>,
        _commit: super::PythonPreparationCommit,
    ) -> Result<super::PreparationOutcome, String> {
        let _ = (
            packages,
            requirements,
            continue_environment_preparation,
            duckdb_extensions,
        );
        unreachable!("unsupported workers cannot start")
    }

    pub(super) fn evaluate(
        &mut self,
        cell: crate::cell::Cell,
        _evaluation: std::sync::Arc<super::Evaluation>,
        _capture_idle_prelude: bool,
    ) -> Result<(), String> {
        let _ = cell;
        unreachable!("unsupported workers cannot start")
    }

    pub(super) fn write_stdin(&self, _stdin: String) -> Result<(), String> {
        unreachable!("unsupported workers cannot start")
    }

    pub(super) fn idle_response_snapshot(
        &self,
        _output: &super::OutputTape,
    ) -> Result<super::IdleResponseSnapshot, String> {
        unreachable!("unsupported workers cannot start")
    }

    pub(super) fn has_failure(&self) -> Result<bool, String> {
        Ok(false)
    }

    pub(super) fn shutdown_after_failure(
        &mut self,
    ) -> Result<Option<super::WorkerProcessOutcome>, super::WorkerRetirementFailure> {
        Ok(None)
    }

    pub(super) fn finish_retirement(
        &mut self,
    ) -> Result<Option<super::WorkerProcessOutcome>, String> {
        Ok(None)
    }

    pub(super) fn shutdown_handle(&self) -> WorkerShutdownHandle {
        WorkerShutdownHandle
    }
}

#[derive(Clone)]
pub(super) struct WorkerShutdownHandle;

impl WorkerShutdownHandle {
    pub(super) fn is_same_connection(&self, _other: &Self) -> bool {
        unreachable!("unsupported workers cannot start")
    }
    pub(super) fn reserve_failed_shutdown(&self, _deadline: std::time::Instant) {}
    pub(super) fn reserve_shutdown(
        &self,
        _deadline: std::time::Instant,
        _completion_deadline: std::time::Instant,
    ) {
    }
    pub(super) fn interrupt(&self, _evaluation: Option<&super::Evaluation>) -> Result<(), String> {
        Err("worker interrupts are supported only on macOS".to_string())
    }

    pub(super) fn is_bootstrapping(&self) -> Result<bool, String> {
        Ok(false)
    }

    pub(super) fn shutdown(&self, _deadline: std::time::Instant) -> Result<(), String> {
        Ok(())
    }

    pub(super) fn request_shutdown(
        &self,
        _worker_deadline: std::time::Instant,
        _completion_deadline: std::time::Instant,
    ) {
    }

    pub(super) fn finish_shutdown(&self) -> Result<(), String> {
        Ok(())
    }

    pub(super) fn finish_retirement(&self) -> Result<Option<super::WorkerProcessOutcome>, String> {
        Ok(None)
    }
}
