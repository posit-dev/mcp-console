//! Managed path activation and its inspected runtime metadata.
//! Requirement candidates and publication remain with `requirements`.

use std::cell::RefCell;

use serde_json::{Value, json};

use super::{PreparationOutcome, library};
use crate::worker_protocol::PythonRequirementManifest;

thread_local! {
    static ACTIVE: RefCell<Option<Value>> = const { RefCell::new(None) };
}

pub(super) fn initialize(libpython: &str) -> Result<bool, String> {
    if ACTIVE.with_borrow(Option::is_some) {
        return Ok(true);
    }
    library::install_environment().map_err(infrastructure)?;
    let Some(response) =
        library::environment_call(c"initialize", &json!({"libpython": libpython}).to_string())
            .map_err(infrastructure)?
    else {
        return Ok(false);
    };
    let active = serde_json::from_str(&response)
        .map_err(|error| infrastructure(format!("invalid Python startup state: {error}")))?;
    accept(active);
    Ok(true)
}

pub(super) fn prepare(
    inspected: Value,
    manifest: &PythonRequirementManifest,
) -> Result<Option<PreparationOutcome>, String> {
    let response = library::environment_call(
        c"prepare",
        &json!({"environment": inspected, "manifest": manifest}).to_string(),
    )
    .map_err(infrastructure)?;
    let Some(response) = response else {
        return Ok(None);
    };
    serde_json::from_str(&response)
        .map(Some)
        .map_err(|error| infrastructure(format!("invalid Python preparation response: {error}")))
}

pub(super) fn accept(active: Value) {
    ACTIVE.with_borrow_mut(|state| *state = Some(active));
}

#[derive(serde::Deserialize)]
pub(super) struct Activation {
    pub(super) manifest: PythonRequirementManifest,
    pub(super) environment: Value,
}

pub(super) fn infrastructure(message: String) -> String {
    crate::worker::record_worker_failure(message.clone());
    message
}

pub(super) fn version() -> Result<String, String> {
    ACTIVE.with_borrow(|state| {
        state
            .as_ref()
            .and_then(|active| active["version"].as_str())
            .map(str::to_owned)
            .ok_or_else(|| "Python environment omitted its initialized version".into())
    })
}
