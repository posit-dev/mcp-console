use super::super::Client;
use super::super::lifecycle::{
    GenerationStatus, LifecycleState, OldGenerationCommitDisposition, RequirementChangeState,
    WorkerGeneration,
};
use super::requirements::{merge_r_requirements, select_r_activation};
use super::resolution::EnvironmentResolutionFailure;
use super::state::commit_managed_r;

pub(in crate::worker_client) enum RuntimeRResolutionFailure {
    Ordinary(String),
    Interrupted,
    Cancelled(String),
    Infrastructure(String),
}

impl From<EnvironmentResolutionFailure> for RuntimeRResolutionFailure {
    fn from(failure: EnvironmentResolutionFailure) -> Self {
        match failure {
            EnvironmentResolutionFailure::Host(message) => Self::Ordinary(message),
            EnvironmentResolutionFailure::Interrupted(_) => Self::Interrupted,
            EnvironmentResolutionFailure::Cancelled(message) => Self::Cancelled(message),
            EnvironmentResolutionFailure::Operation(message) => Self::Infrastructure(message),
        }
    }
}

impl Client {
    pub(in crate::worker_client) fn resolve_runtime_r(
        &self,
        generation: WorkerGeneration,
        packages: Vec<String>,
    ) -> Result<crate::resolver::ManagedR, RuntimeRResolutionFailure> {
        self.ensure_runtime_r_generation(&generation)?;
        crate::r_package_name::validate_all(&packages)
            .map_err(RuntimeRResolutionFailure::Ordinary)?;
        let environment = self.0.environment.as_ref().ok_or_else(|| {
            RuntimeRResolutionFailure::Infrastructure(
                "managed R requirements are unavailable".to_string(),
            )
        })?;
        // This lock serializes the retained-environment snapshot and the one
        // lifecycle-owned host resolver slot through both resolver phases.
        let environment = environment.lock().map_err(|_| {
            RuntimeRResolutionFailure::Infrastructure(
                "worker environment lock poisoned".to_string(),
            )
        })?;
        let (requirements, changed) = merge_r_requirements(&environment, packages);
        if !changed {
            let managed = environment.r.clone().ok_or_else(|| {
                RuntimeRResolutionFailure::Infrastructure(
                    "managed R environment is unavailable".to_string(),
                )
            })?;
            self.ensure_runtime_r_generation(&generation)?;
            return Ok(managed);
        }
        if !environment
            .startup
            .r_resolution
            .permits_implicit_additions()
        {
            return Err(RuntimeRResolutionFailure::Ordinary(
                environment
                    .startup
                    .r_resolution
                    .denial("R", "runtime package request"),
            ));
        }
        match self
            .requirement_change_state(&generation)
            .map_err(RuntimeRResolutionFailure::Infrastructure)?
        {
            RequirementChangeState::Available => {}
            RequirementChangeState::RestartRequired => {
                return Err(RuntimeRResolutionFailure::Ordinary(
                    "requirement changes are unavailable until session restart".to_string(),
                ));
            }
        }

        let managed = self
            .resolve_managed_r(
                &generation,
                &environment.r_resolver,
                requirements,
                Vec::new(),
            )
            .map_err(RuntimeRResolutionFailure::from)?;
        if !environment.duckdb_extensions.is_empty() {
            let extensions = environment
                .duckdb_extensions
                .iter()
                .cloned()
                .collect::<Vec<_>>();
            self.resolve_duckdb_extensions(
                &generation,
                std::slice::from_ref(&managed),
                &extensions,
                &[],
            )
            .map_err(RuntimeRResolutionFailure::from)?;
        }
        self.ensure_runtime_r_generation(&generation)?;
        Ok(managed)
    }

    pub(in crate::worker_client) fn activate_runtime_r(
        &self,
        generation: WorkerGeneration,
        library: String,
        candidates: &mut Vec<crate::resolver::ManagedR>,
    ) -> Result<OldGenerationCommitDisposition, String> {
        let managed = select_r_activation(&library, candidates)?;
        let environment = self
            .0
            .environment
            .as_ref()
            .ok_or_else(|| "managed R environment is unavailable".to_string())?;
        let mut environment = environment
            .lock()
            .map_err(|_| "worker environment lock poisoned".to_string())?;
        let lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        if lifecycle.state == LifecycleState::Ready && lifecycle.generation.is(&generation) {
            commit_managed_r(&mut environment, managed);
            self.publish_requirements(&environment);
            Ok(OldGenerationCommitDisposition::Commit)
        } else {
            Ok(OldGenerationCommitDisposition::DiscardForReplacement)
        }
    }

    pub(in crate::worker_client) fn fail_runtime_r_activation(
        &self,
        generation: WorkerGeneration,
        library: String,
        candidates: &mut Vec<crate::resolver::ManagedR>,
    ) -> Result<OldGenerationCommitDisposition, String> {
        let _managed = select_r_activation(&library, candidates)?;
        let environment = self
            .0
            .environment
            .as_ref()
            .ok_or_else(|| "managed R environment is unavailable".to_string())?;
        let _environment = environment
            .lock()
            .map_err(|_| "worker environment lock poisoned".to_string())?;
        let mut lifecycle = self
            .0
            .lifecycle
            .lock()
            .map_err(|_| "worker lifecycle lock poisoned".to_string())?;
        if lifecycle.state == LifecycleState::Ready && lifecycle.generation.is(&generation) {
            lifecycle.requirement_changes = RequirementChangeState::RestartRequired;
            Ok(OldGenerationCommitDisposition::Commit)
        } else {
            Ok(OldGenerationCommitDisposition::DiscardForReplacement)
        }
    }

    fn ensure_runtime_r_generation(
        &self,
        generation: &WorkerGeneration,
    ) -> Result<(), RuntimeRResolutionFailure> {
        match self.generation_status(generation) {
            Ok(GenerationStatus::CurrentReady) => Ok(()),
            Ok(GenerationStatus::CurrentClosing | GenerationStatus::Changed) => {
                Err(RuntimeRResolutionFailure::Cancelled(
                    "R package resolution cancelled by session lifecycle change".to_string(),
                ))
            }
            Err(error) => Err(RuntimeRResolutionFailure::Infrastructure(error)),
        }
    }
}
