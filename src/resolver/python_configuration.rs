use std::collections::BTreeMap;
use std::ffi::{OsStr, OsString};
use std::sync::Arc;

#[derive(Clone)]
pub(crate) struct ManagedPythonResolverConfiguration {
    environment: Arc<BTreeMap<OsString, OsString>>,
    explicit_uv: Option<OsString>,
    reticulate_uv: Option<OsString>,
    uv: Option<OsString>,
}

impl ManagedPythonResolverConfiguration {
    pub(crate) fn capture() -> Self {
        let environment = std::env::vars_os()
            .filter(|(name, _)| is_uv_environment_variable(name) && name != "UV_OFFLINE")
            .collect::<BTreeMap<_, _>>();
        let explicit_uv = std::env::var_os("RETICULATE_UV");
        let reticulate_uv = explicit_uv
            .clone()
            .or_else(|| super::find_path_entry("uv").map(Into::into));
        let uv = reticulate_uv
            .as_ref()
            .filter(|uv| uv.as_os_str() != OsStr::new("managed"))
            .cloned();
        Self {
            environment: Arc::new(environment),
            explicit_uv,
            reticulate_uv,
            uv,
        }
    }

    pub(crate) fn without_r_bootstrap(mut self) -> Self {
        // Sans-R preparation uses the startup PATH selection. RETICULATE_UV
        // belongs to reticulate's R bootstrap, not this mode.
        self.uv = super::find_path_entry("uv").map(Into::into);
        self.reticulate_uv = self.uv.clone();
        self.explicit_uv = None;
        self
    }

    pub(super) fn explicit_uv(&self) -> Option<&OsStr> {
        self.explicit_uv.as_deref()
    }

    pub(super) fn uv(&self) -> Result<&OsStr, String> {
        self.uv
            .as_deref()
            .ok_or_else(|| "managed Python resolver has no `uv` executable".to_string())
    }

    fn reticulate_uv(&self) -> Result<&OsStr, String> {
        self.reticulate_uv
            .as_deref()
            .or(self.uv.as_deref())
            .ok_or_else(|| "managed Python resolver has no reticulate `uv` selection".to_string())
    }

    pub(crate) fn has_uv(&self) -> bool {
        self.uv.is_some()
    }

    pub(crate) fn selected_uv(&self) -> Option<OsString> {
        self.uv.clone()
    }

    pub(crate) fn set_resolved_uv(&mut self, uv: impl Into<OsString>) {
        let uv = uv.into();
        if self.uv.is_none() {
            self.uv = Some(uv.clone());
        }
        if self.reticulate_uv.is_none() {
            self.reticulate_uv = Some(uv);
        }
    }

    fn configure_uv(&self, command: &mut std::process::Command, uv: &OsStr) {
        for (name, _) in std::env::vars_os().filter(|(name, _)| is_uv_environment_variable(name)) {
            command.env_remove(name);
        }
        command
            .envs(self.environment.iter())
            .env("RETICULATE_UV", uv)
            .env_remove("UV_OFFLINE")
            .env_remove("UV_PYTHON")
            .env_remove("UV_MANAGED_PYTHON")
            .env_remove("UV_NO_MANAGED_PYTHON")
            .env("UV_PYTHON_PREFERENCE", "only-managed");
    }

    pub(super) fn configure_uv_bootstrap(&self, command: &mut std::process::Command) {
        self.configure_uv(command, OsStr::new("managed"));
    }

    pub(super) fn configure_direct(
        &self,
        command: &mut std::process::Command,
    ) -> Result<(), String> {
        let uv = self.reticulate_uv()?;
        self.configure_uv(command, uv);
        if uv == OsStr::new("managed") {
            let executable = std::path::Path::new(self.uv()?);
            let root = executable
                .parent()
                .and_then(std::path::Path::parent)
                .ok_or_else(|| {
                    format!(
                        "reticulate managed `uv` executable has no cache root: `{}`",
                        executable.display()
                    )
                })?;
            command
                .env("UV_CACHE_DIR", root.join("cache"))
                .env("UV_PYTHON_INSTALL_DIR", root.join("python"));
        }
        Ok(())
    }
}

fn is_uv_environment_variable(name: &OsStr) -> bool {
    name.as_encoded_bytes().starts_with(b"UV_")
}
