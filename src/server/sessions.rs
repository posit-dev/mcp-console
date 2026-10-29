//! Logical sessions retain independent startup, execution, and retirement owners.
use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};
use std::time::Instant;

use super::startup::Startup;

#[derive(Clone)]
pub(super) struct Sessions(Arc<Mutex<State>>);

struct State {
    closed: bool,
    sessions: BTreeMap<String, Startup>,
}

impl Sessions {
    pub fn new(default: Startup) -> Self {
        Self(Arc::new(Mutex::new(State {
            closed: false,
            sessions: BTreeMap::from([("default".into(), default)]),
        })))
    }

    pub fn get(&self, name: &str) -> Option<Startup> {
        self.0
            .lock()
            .expect("sessions lock")
            .sessions
            .get(name)
            .cloned()
    }

    pub fn select(&self, name: &str, has_cell: bool) -> Result<Startup, String> {
        let mut state = self.0.lock().expect("sessions lock");
        if state.closed {
            return Err("MCP input closed before session execution".into());
        }
        if let Some(session) = state.sessions.get(name) {
            return Ok(session.clone());
        }
        if !has_cell {
            return Err(format!(
                "unknown session {}; submit a code cell to create it",
                serde_json::json!(name)
            ));
        }
        let session = state.sessions["default"].named(name)?;
        state.sessions.insert(name.into(), session.clone());
        Ok(session)
    }

    pub fn snapshot(&self) -> Vec<(String, Startup)> {
        self.0
            .lock()
            .expect("sessions lock")
            .sessions
            .iter()
            .map(|(name, startup)| (name.clone(), startup.clone()))
            .collect()
    }

    /// Close admission before taking the complete set of retirement owners.
    pub async fn shutdown(&self, deadline: Instant) -> Result<(), String> {
        let sessions = {
            let mut state = self.0.lock().expect("sessions lock");
            state.closed = true;
            state.sessions.values().cloned().collect::<Vec<_>>()
        };
        let mut retirements = tokio::task::JoinSet::new();
        for startup in sessions {
            retirements.spawn(async move {
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
                cancellation.and(result)
            });
        }
        let mut failure = None;
        while let Some(result) = retirements.join_next().await {
            if let Err(error) = result
                .map_err(|error| error.to_string())
                .and_then(|result| result)
            {
                failure.get_or_insert(error);
            }
        }
        failure.map_or(Ok(()), Err)
    }

    pub fn finish_recording(&self) {
        for startup in self.0.lock().expect("sessions lock").sessions.values() {
            startup.runtime().worker.finish_recording();
        }
    }
}
