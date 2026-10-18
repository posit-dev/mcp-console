use super::super::{Client, RResolver};
use super::state::{Environment, PythonEnvironment};
use crate::worker_protocol::PythonRequirementManifest;

/// The declaration is derived from retained resolver results, never a candidate.
#[derive(Clone, Default, PartialEq, Eq, serde::Serialize)]
pub(crate) struct Declaration {
    pub r: Vec<String>,
    pub python: Vec<String>,
    pub duckdb: Vec<String>,
    pub python_version: Vec<String>,
    pub exclude_newer: Option<String>,
}

impl Declaration {
    pub(super) fn python_manifest(&self) -> PythonRequirementManifest {
        PythonRequirementManifest {
            packages: self.python.clone(),
            python_version: self.python_version.clone(),
            exclude_newer: self.exclude_newer.clone(),
        }
    }

    pub(super) fn normalized(mut self) -> Self {
        self.r.sort();
        self.r.dedup();
        self.duckdb.sort();
        self.duckdb.dedup();
        let python = self.python_manifest().normalized();
        self.python = python.packages;
        self.python_version = python.python_version;
        self
    }
}

// Direct dependencies of the implemented R/Python bridge and SQL adapters.
// Their transitive dependencies are also infrastructure, not optional declarations.
const R_RUNTIME_REQUIREMENTS: &[&str] = &[
    "DBI",
    "duckdb",
    "jsonlite",
    "nanoarrow",
    "pillar",
    "reticulate",
    "tibble",
    "utf8",
];

impl Environment {
    pub(super) fn manages_python(&self) -> bool {
        match &self.r_resolver {
            RResolver::Pending(setup) => {
                PythonEnvironment::uses_managed(setup.configured_python.as_deref())
            }
            _ => self
                .python
                .as_ref()
                .and_then(PythonEnvironment::managed)
                .is_some(),
        }
    }

    pub(in crate::worker_client) fn startup_declaration(&self) -> Declaration {
        let managed_r = !self.custom_worker && !matches!(self.r_resolver, RResolver::Disabled);
        let python = if self.manages_python() {
            self.startup.python.manifest(
                self.local_runtime
                    .as_ref()
                    .is_some_and(crate::local_runtime::Selection::python_only),
            )
        } else {
            Default::default()
        };
        Declaration {
            r: if managed_r {
                self.startup.r.clone().unwrap_or_else(|| {
                    super::super::DEFAULT_R_REQUIREMENTS
                        .iter()
                        .map(|s| (*s).into())
                        .collect()
                })
            } else {
                vec![]
            },
            python: python.packages,
            python_version: python.python_version,
            duckdb: if managed_r {
                super::super::DEFAULT_DUCKDB_EXTENSIONS
                    .iter()
                    .map(|s| (*s).into())
                    .collect()
            } else {
                self.startup.native_duckdb.iter().cloned().collect()
            },
            ..Default::default()
        }
        .normalized()
    }

    pub(super) fn declaration(&self) -> Declaration {
        if matches!(self.r_resolver, RResolver::Pending(_)) {
            return self.startup_declaration();
        }
        let python = self
            .python
            .as_ref()
            .and_then(PythonEnvironment::managed)
            .map(|python| python.requirements().clone())
            .unwrap_or_default();
        Declaration {
            r: self
                .r
                .as_ref()
                .map(|r| r.requirements().to_vec())
                .unwrap_or_default(),
            python: python.packages,
            duckdb: self.duckdb_extensions.iter().cloned().collect(),
            python_version: python.python_version,
            exclude_newer: python.exclude_newer,
        }
        .normalized()
    }

    pub(in crate::worker_client) fn runtime_r_requirements(&self) -> &'static [&'static str] {
        if self.custom_worker {
            super::super::CUSTOM_DUCKDB_R_REQUIREMENTS
        } else if matches!(self.r_resolver, RResolver::Disabled) {
            &[]
        } else {
            R_RUNTIME_REQUIREMENTS
        }
    }

    pub(in crate::worker_client) fn inspection(&self) -> serde_json::Value {
        serde_json::json!({
            "requirements": self.declaration(),
            "prepared": self.r.is_some() || self.python.as_ref().and_then(PythonEnvironment::managed).is_some(),
            "runtime_requirements": {"r": self.runtime_r_requirements(), "python": []},
        })
    }
}

impl Client {
    pub(crate) fn inspect_requirements(&self) -> serde_json::Value {
        self.0
            .requirements_snapshot
            .lock()
            .expect("requirements snapshot lock")
            .clone()
    }

    pub(in crate::worker_client) fn record_requirements(
        &self,
        action: super::RequirementsAction,
        call_id: Option<u64>,
        environment: &Environment,
    ) {
        let action = match action {
            super::RequirementsAction::Set => "set",
            super::RequirementsAction::Reset => "reset",
            _ => return,
        };
        if let Some(transcript) = self.0.recording.lock().expect("recording lock").as_ref() {
            transcript.requirements_selected(call_id, action, &environment.inspection());
        }
    }

    /// Publish only at existing generation-checked commit points. Inspection
    /// never acquires the environment lock held across resolver execution.
    pub(in crate::worker_client) fn publish_requirements(&self, environment: &Environment) {
        *self
            .0
            .requirements_snapshot
            .lock()
            .expect("requirements snapshot lock") = environment.inspection();
    }
}
