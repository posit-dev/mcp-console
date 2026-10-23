//! Retained environment descriptions; package preparation is platform-specific.
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::Command;

#[derive(Clone, serde::Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ManagedPython {
    pub(super) python: PathBuf,
    pub(super) requirements: crate::worker_protocol::PythonRequirementManifest,
}

impl ManagedPython {
    pub(crate) fn configure_worker(&self, command: &mut Command) {
        command.env("RETICULATE_PYTHON", "managed");
        command.env(
            "MCP_CONSOLE_MANAGED_PYTHON",
            serde_json::to_string(&self.requirements)
                .expect("managed Python requirements should serialize as JSON"),
        );
    }

    pub(crate) fn python(&self) -> &Path {
        &self.python
    }

    pub(crate) fn requirements(&self) -> &crate::worker_protocol::PythonRequirementManifest {
        &self.requirements
    }

    pub(crate) fn with_retained_requirements(
        mut self,
        requirements: crate::worker_protocol::PythonRequirementManifest,
    ) -> Self {
        self.requirements = requirements;
        self
    }
}
#[derive(Clone, serde::Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ManagedR {
    pub(super) library: PathBuf,
    // Serde's Unix OsString representation preserves native path bytes in JSON.
    pub(super) r_libs: OsString,
    // Executable selection never travels in a preparation request.
    #[cfg(any(unix, windows))]
    #[serde(skip)]
    pub(super) rscript: PathBuf,
    pub(super) requirements: Vec<String>,
}
impl ManagedR {
    #[cfg(any(unix, windows))]
    pub(crate) fn on_host(mut self, rscript: &Path) -> Self {
        self.rscript = rscript.to_path_buf();
        self
    }
    pub(crate) fn configure_worker(&self, command: &mut Command) -> Result<(), String> {
        if !self.library.is_dir() {
            return Err(format!(
                "resolved R library `{}` no longer exists",
                self.library.display()
            ));
        }
        command.env("R_LIBS", &self.r_libs);
        command.env("MCP_CONSOLE_R_LIBRARY", &self.library);
        Ok(())
    }

    pub(crate) fn with_retained_requirements(mut self, requirements: Vec<String>) -> Self {
        self.requirements = requirements;
        self
    }

    pub(crate) fn requirements(&self) -> &[String] {
        &self.requirements
    }

    pub(crate) fn library(&self) -> &Path {
        &self.library
    }

    #[cfg(any(unix, windows))]
    pub(crate) fn rscript(&self) -> &Path {
        &self.rscript
    }
}
