use std::collections::BTreeSet;
use std::ffi::OsString;
#[cfg(any(unix, windows))]
use std::process::Command;

use super::requirements::push_duckdb_r_target;

impl super::super::Client {
    pub(in crate::worker_client) fn record_accepted_python(&self, environment: &Environment) {
        if !self.0.python_preparation {
            return;
        }
        let selected = environment
            .python
            .as_ref()
            .and_then(PythonEnvironment::managed)
            .expect("managed Python preparation retains an environment");
        if let Some(transcript) = self.0.recording.lock().expect("recording lock").as_ref() {
            transcript.python_environment_accepted(&selected.requirements().packages);
        }
    }
}

#[derive(Clone)]
pub(in crate::worker_client) struct Environment {
    pub(in crate::worker_client) startup: StartupRequirements,
    /// Launch configuration commits with the managed executable and manifest.
    pub(in crate::worker_client) local_runtime: Option<crate::local_runtime::Selection>,
    pub(in crate::worker_client) custom_worker: bool,
    pub(in crate::worker_client) duckdb_extensions: BTreeSet<String>,
    /// R libraries that may have supplied DuckDB in the current worker generation.
    pub(in crate::worker_client) duckdb_r_targets: Vec<crate::resolver::ManagedR>,
    pub(in crate::worker_client) python: Option<PythonEnvironment>,
    pub(in crate::worker_client) r: Option<crate::resolver::ManagedR>,
    pub(in crate::worker_client) r_resolver: super::super::RResolver,
}

/// Effective configuration intent stays independent of later accepted declarations.
#[derive(Clone, Default)]
pub(in crate::worker_client) struct StartupRequirements {
    pub(in crate::worker_client) r: Option<Vec<String>>,
    pub(in crate::worker_client) r_resolution: crate::settings::Resolution,
    pub(in crate::worker_client) python: crate::settings::ManagedPythonSettings,
    pub(in crate::worker_client) native_duckdb: BTreeSet<String>,
}

const USER_SELECTED_PYTHON_ERROR: &str = "managed Python requirements are disabled because the session uses a user-selected Python environment";

#[derive(Clone)]
pub(in crate::worker_client) enum PythonEnvironment {
    Managed {
        selected: crate::resolver::ManagedPython,
        resolver: crate::resolver::execution::PythonConfiguration,
    },
    UserSelected(OsString),
    Ambient,
}

impl PythonEnvironment {
    pub(in crate::worker_client) fn uses_managed(configured: Option<&std::ffi::OsStr>) -> bool {
        !configured.is_some_and(|configured| !configured.is_empty() && configured != "managed")
    }

    pub(in crate::worker_client) fn bare(configured: Option<OsString>) -> Self {
        if let Some(configured) = configured
            && !configured.is_empty()
            && configured != "managed"
        {
            Self::UserSelected(configured)
        } else {
            Self::Ambient
        }
    }

    pub(in crate::worker_client) fn managed(&self) -> Option<&crate::resolver::ManagedPython> {
        match self {
            Self::Managed { selected, .. } => Some(selected),
            Self::UserSelected(_) | Self::Ambient => None,
        }
    }

    pub(super) fn managed_parts(
        &self,
    ) -> Result<
        (
            &crate::resolver::ManagedPython,
            &crate::resolver::execution::PythonConfiguration,
        ),
        String,
    > {
        match self {
            Self::Managed { selected, resolver } => Ok((selected, resolver)),
            Self::UserSelected(_) => Err(USER_SELECTED_PYTHON_ERROR.to_string()),
            Self::Ambient => Err("dynamic environment resolution is unavailable".to_string()),
        }
    }

    pub(in crate::worker_client) fn replace_managed(
        &mut self,
        managed: crate::resolver::ManagedPython,
    ) -> Result<(), String> {
        match self {
            Self::Managed { selected, .. } => {
                *selected = managed;
                Ok(())
            }
            Self::UserSelected(_) => Err(USER_SELECTED_PYTHON_ERROR.to_string()),
            Self::Ambient => Err("dynamic environment resolution is unavailable".to_string()),
        }
    }

    #[cfg(any(unix, windows))]
    pub(in crate::worker_client) fn configure_worker(&self, command: &mut Command) {
        match self {
            Self::Managed { selected, .. } => selected.configure_worker(command),
            Self::UserSelected(python) => {
                command
                    .env("RETICULATE_PYTHON", python)
                    .env_remove("MCP_CONSOLE_MANAGED_PYTHON");
            }
            Self::Ambient => {
                command
                    .env_remove("RETICULATE_PYTHON")
                    .env_remove("MCP_CONSOLE_MANAGED_PYTHON");
            }
        }
    }
}

pub(super) fn ensure_managed_python_available(environment: &Environment) -> Result<(), String> {
    if environment.custom_worker {
        return Err("Python requirements are unavailable with a custom worker".to_string());
    }
    if let super::super::RResolver::Pending(setup) = &environment.r_resolver {
        return if PythonEnvironment::uses_managed(setup.configured_python.as_deref()) {
            Ok(())
        } else {
            Err(USER_SELECTED_PYTHON_ERROR.to_string())
        };
    }
    match environment.python.as_ref() {
        Some(PythonEnvironment::Managed { .. }) => Ok(()),
        Some(PythonEnvironment::UserSelected(_)) => Err(USER_SELECTED_PYTHON_ERROR.to_string()),
        Some(PythonEnvironment::Ambient) => {
            Err("dynamic environment resolution is unavailable".to_string())
        }
        None => Err("managed Python environment is unavailable".to_string()),
    }
}

pub(super) fn commit_managed_r(
    environment: &mut Environment,
    managed_r: crate::resolver::ManagedR,
) {
    push_duckdb_r_target(&mut environment.duckdb_r_targets, managed_r.clone());
    environment.r = Some(managed_r);
}
