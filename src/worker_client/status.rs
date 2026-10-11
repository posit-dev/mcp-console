//! Disposable response observations of the existing operation owners.

use std::sync::Weak;
use std::sync::atomic::{AtomicBool, Ordering};

use super::lifecycle::{LifecycleState, WorkerGeneration};
use super::{Client, ClientInner};

pub(super) struct Source {
    client: Weak<ClientInner>,
    generation: WorkerGeneration,
    owner: Owner,
}

enum Owner {
    Evaluation(Weak<super::Evaluation>),
    Resolver(crate::resolver::ResolverPhase),
    Startup(Weak<AtomicBool>),
    ConnectionStartup,
    Replacement,
}

impl Source {
    pub(super) fn phase(&self) -> Option<&'static str> {
        let client = self.client.upgrade()?;
        let replacing = match &self.owner {
            Owner::Evaluation(evaluation) => match evaluation.upgrade() {
                Some(evaluation) => evaluation.replacement_observation()?,
                None => false,
            },
            _ => false,
        };
        // Status is disposable: contention must not extend a response deadline
        // or keep a cancelled request waiting for the operation owner.
        let lifecycle = client.lifecycle.try_lock().ok()?;
        if !lifecycle.generation.is(&self.generation)
            || matches!(lifecycle.state, LifecycleState::ShuttingDown { .. })
        {
            return None;
        }
        let observed = match &self.owner {
            Owner::Resolver(resolver) => resolver.phase(),
            Owner::Startup(startup)
                if startup
                    .upgrade()
                    .is_some_and(|ready| !ready.load(Ordering::Acquire)) =>
            {
                Some("startup")
            }
            Owner::ConnectionStartup
                if !client.startup_observation_complete.load(Ordering::Acquire)
                    && self.generation.is(&client.startup_generation) =>
            {
                Some("startup")
            }
            Owner::Replacement if matches!(lifecycle.state, LifecycleState::Restarting { .. }) => {
                Some("replacement")
            }
            Owner::Evaluation(_) if replacing => Some("replacement"),
            _ => None,
        };
        // Completion can leave a captured owner installed until delivery, or
        // hand preparation over to startup before this response is projected.
        observed.or_else(|| {
            // Retained sources can outlive a failed refresh. Observe the current
            // evaluation before falling back to lifecycle work.
            let active = client.evaluation.try_lock().ok()?;
            if let Some(active) = active
                .as_ref()
                .filter(|active| active.evaluation.admission.generation.is(&self.generation))
                && active.evaluation.replacement_observation()?
            {
                return Some("replacement");
            }
            drop(active);
            if let Some(phase) = lifecycle
                .processes
                .resolver
                .as_ref()
                .and_then(|handle| handle.phase_observation().phase())
            {
                Some(phase)
            } else if matches!(lifecycle.state, LifecycleState::Restarting { .. }) {
                Some("replacement")
            } else if (!client.startup_observation_complete.load(Ordering::Acquire)
                && self.generation.is(&client.startup_generation))
                || lifecycle.starting()
            {
                Some("startup")
            } else {
                None
            }
        })
    }
}

impl Client {
    pub(super) fn status_generation(&self) -> Option<WorkerGeneration> {
        Some(self.0.lifecycle.try_lock().ok()?.generation.clone())
    }

    pub(super) fn status_source(&self, generation: Option<WorkerGeneration>) -> Option<Source> {
        let generation = generation?;
        let evaluation = self
            .0
            .evaluation
            .try_lock()
            .ok()?
            .as_ref()
            .filter(|active| active.evaluation.admission.generation.is(&generation))
            .map(|active| std::sync::Arc::downgrade(&active.evaluation));
        let lifecycle = self.0.lifecycle.try_lock().ok()?;
        if !lifecycle.generation.is(&generation) {
            return None;
        }
        let owner = if let Some(evaluation) = evaluation {
            Owner::Evaluation(evaluation)
        } else if let Some(resolver) = lifecycle.processes.resolver.as_ref() {
            Owner::Resolver(resolver.phase_observation())
        } else if matches!(lifecycle.state, LifecycleState::Restarting { .. }) {
            Owner::Replacement
        } else if let Some(startup) = lifecycle.startup_observation() {
            Owner::Startup(startup)
        } else if !self.0.startup_observation_complete.load(Ordering::Acquire)
            && generation.is(&self.0.startup_generation)
        {
            Owner::ConnectionStartup
        } else {
            return None;
        };
        Some(Source {
            client: std::sync::Arc::downgrade(&self.0),
            generation,
            owner,
        })
    }

    pub(crate) fn starting_response(&self) -> super::Response {
        let mut response = super::output::render_response(
            super::output::SendResponse::ReplacementStarting(super::Response::default()),
        );
        response.observe_phase(self.status_source(self.status_generation()));
        response
    }
}
