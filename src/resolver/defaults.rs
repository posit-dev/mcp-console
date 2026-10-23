//! Built-in convenience defaults may fail independently of required preparation.

use std::cell::{Cell, RefCell};

use super::ResolverStopHandle;

pub(super) fn resolve<T>(
    label: &str,
    requirements: Vec<String>,
    defaults: Vec<String>,
    mut prepare: impl FnMut(
        Vec<String>,
        &dyn Fn(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<T, String>,
    on_started: &dyn Fn(ResolverStopHandle) -> Result<(), String>,
    report: &dyn Fn(String) -> Result<(), String>,
) -> Result<(T, Vec<String>), String> {
    if defaults.is_empty() {
        return prepare(requirements.clone(), on_started).map(|value| (value, requirements));
    }
    let mut attempt = |requirements: Vec<String>| {
        let handle = RefCell::new(None::<ResolverStopHandle>);
        let registration_failed = Cell::new(false);
        let result = prepare(requirements, &|started| {
            *handle.borrow_mut() = Some(started.clone());
            let result = on_started(started);
            registration_failed.set(registration_failed.get() || result.is_err());
            result
        });
        match result {
            Ok(value) => Ok(Ok(value)),
            Err(error)
                if !registration_failed.get()
                    && handle.borrow().as_ref().is_some_and(|handle| {
                        handle.cleanup_confirmed() && handle.control_outcome().is_none()
                    }) =>
            {
                Ok(Err(error))
            }
            Err(error) => Err(error),
        }
    };
    // Preserve the combined fast path. Only a clean failure permits the
    // explicitly supported recovery; control and unretired children stay fatal.
    if let Ok(value) = attempt(requirements.clone())? {
        return Ok((value, requirements));
    }
    let mut accepted = requirements
        .into_iter()
        .filter(|requirement| !defaults.contains(requirement))
        .collect::<Vec<_>>();
    let mut value = attempt(accepted.clone())??;
    for requirement in defaults {
        let mut candidate = accepted.clone();
        candidate.push(requirement.clone());
        match attempt(candidate.clone())? {
            Ok(prepared) => {
                value = prepared;
                accepted = candidate;
            }
            Err(error) => report(format!(
                "skipping default {label} `{requirement}`: {error}\n"
            ))?,
        }
    }
    Ok((value, accepted))
}
