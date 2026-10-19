//! Trusted preparation through the retained local resolver owner.
//! Session transactions and environment activation remain with the worker client.

use super::preparation::{Operation, Preparation};
use super::{ManagedPython, ManagedR, ResolverStopHandle};
use crate::worker_protocol::PythonRequirementManifest;

#[derive(Clone)]
pub(crate) struct PythonConfiguration {
    pub(crate) preparation: Preparation,
    pub(crate) has_uv: bool,
}

impl PythonConfiguration {
    pub(crate) fn has_uv(&self) -> bool {
        self.has_uv
    }

    pub(crate) fn set_resolved_uv(&mut self, _uv: std::ffi::OsString) {
        self.has_uv = true;
    }
}

impl Preparation {
    pub(crate) fn prepare(
        &self,
        on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<Self, String> {
        self.call::<()>(Operation::Bootstrap, on_started)?;
        Ok(self.clone())
    }

    pub(crate) fn resolve_uv(
        &self,
        managed_r: &ManagedR,
        on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<std::ffi::OsString, String> {
        self.call(
            Operation::Uv {
                r: managed_r.clone(),
            },
            on_started,
        )
    }

    pub(crate) fn resolve_r(
        &self,
        requirements: Vec<String>,
        defaults: Vec<String>,
        on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<ManagedR, String> {
        self.call(
            Operation::R {
                requirements,
                defaults,
            },
            on_started,
        )
    }
}

pub(crate) fn resolve_python_manifest(
    requirements: PythonRequirementManifest,
    defaults: Vec<String>,
    configuration: &PythonConfiguration,
    managed_r: Option<&ManagedR>,
    selected_python: Option<&std::path::Path>,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<ManagedPython, String> {
    configuration.preparation.call(
        Operation::Python {
            requirements,
            defaults,
            r: managed_r.cloned(),
            selected_python: selected_python.map(std::path::Path::to_path_buf),
        },
        on_started,
    )
}

pub(crate) fn resolve_python_version(
    constraints: Vec<String>,
    configuration: &PythonConfiguration,
    managed_r: Option<&ManagedR>,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<String, String> {
    configuration.preparation.call(
        Operation::PythonVersion {
            constraints,
            r: managed_r.cloned(),
        },
        on_started,
    )
}

pub(crate) fn inspect_native(
    configuration: &PythonConfiguration,
    executable: &std::path::Path,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<crate::python::NativePython, String> {
    configuration.preparation.call(
        Operation::InspectPython {
            executable: executable.to_path_buf(),
        },
        on_started,
    )
}

pub(crate) fn resolve_duckdb_extensions(
    preparation: Option<&Preparation>,
    managed_r: &ManagedR,
    extensions: &[String],
    defaults: &[String],
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<Vec<String>, String> {
    let preparation = preparation.ok_or("DuckDB resolver has no preparation owner")?;
    preparation.call(
        Operation::Duckdb {
            r: managed_r.clone(),
            extensions: extensions.to_vec(),
            defaults: defaults.to_vec(),
        },
        on_started,
    )
}

pub(crate) fn resolve_python_duckdb_extensions(
    configuration: &PythonConfiguration,
    python: &ManagedPython,
    extensions: &[String],
    defaults: &[String],
    extension_directory: &std::path::Path,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<Vec<String>, String> {
    configuration.preparation.call(
        Operation::DuckdbPython {
            python: python.clone(),
            extensions: extensions.to_vec(),
            defaults: defaults.to_vec(),
            extension_directory: extension_directory.to_path_buf(),
        },
        on_started,
    )
}

pub(crate) fn python_duckdb_available(
    configuration: &PythonConfiguration,
    python: &ManagedPython,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<bool, String> {
    configuration.preparation.call(
        Operation::PythonDuckdbAvailable {
            python: python.clone(),
        },
        on_started,
    )
}
