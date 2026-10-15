//! Captured runtime selection, retained for every worker generation.

use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::Command;
mod r_selection;

// Internal handoffs retain native filenames instead of requiring UTF-8.
pub(crate) mod native_path {
    use serde::{Deserialize as _, Serialize as _};
    use std::ffi::OsString;
    use std::path::{Path, PathBuf};

    pub(crate) fn serialize<S: serde::Serializer>(
        path: &Path,
        serializer: S,
    ) -> Result<S::Ok, S::Error> {
        path.as_os_str().serialize(serializer)
    }

    pub(crate) fn deserialize<'de, D: serde::Deserializer<'de>>(
        deserializer: D,
    ) -> Result<PathBuf, D::Error> {
        OsString::deserialize(deserializer).map(PathBuf::from)
    }
}

#[cfg(any(unix, windows))]
use crate::resolver::{ManagedPython, ResolverStopHandle};

pub(crate) const ENVIRONMENT: &str = "MCP_CONSOLE_LOCAL_RUNTIME";
pub(crate) const DUCKDB_EXTENSION_DIRECTORY: &str = "MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY";
pub(crate) const DEFAULT_DUCKDB_EXTENSIONS: &[&str] = &["icu", "json", "sqlite"];
pub(crate) const PREPARATION_DISABLED: &str = "Python requirements are unavailable in this non-managed Python session; install packages before starting the session";
pub(crate) const RESOLUTION_UNAVAILABLE: &str =
    "dynamic environment resolution is unavailable; install `ir` or `uv` and restart MCP Console";
pub(crate) const LIVE_PREPARATION_DISABLED: &str = "changed requirements other than idle Python package or DuckDB extension additions require control: restart in a Python session without R";

#[derive(Clone, Default, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Selection {
    pub(crate) r_home: Option<PathBuf>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) installation: Option<RInstallation>,
    #[serde(default)]
    pub(crate) r_settings: crate::settings::R,
    // In managed sessions, None leaves R declarations and selection hints lazy.
    pub(crate) python: Option<Python>,
}

#[derive(Clone, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Python {
    pub(crate) selected: Box<crate::python::NativePython>,
    pub(crate) explicit: Option<OsString>,
    // The server owns requirements; this is a captured runtime capability.
    pub(crate) managed: bool,
    pub(crate) duckdb_extension_directory: Option<PathBuf>,
}

#[derive(serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct WorkerSelection {
    pub(crate) r: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) installation: Option<RInstallation>,
    #[serde(default)]
    pub(crate) r_settings: crate::settings::R,
    pub(crate) python: Option<Python>,
}

impl Selection {
    pub(crate) fn r_is_present() -> bool {
        // An explicit but invalid R_HOME, or a broken discovered installation,
        // must stay on the R path and report its own failure.
        std::env::var_os("R_HOME").is_some() || crate::resolver::find_r_path_entry().is_some()
    }

    #[cfg(any(unix, windows))]
    pub(crate) fn python(
        configured: Option<OsString>,
        requirements: crate::worker_protocol::PythonRequirementManifest,
        resolver: &crate::resolver::execution::PythonConfiguration,
        extension_directory: Option<PathBuf>,
        inspect_explicit: impl FnOnce(
            &Path,
            &dyn Fn(ResolverStopHandle) -> Result<(), String>,
        ) -> Result<crate::python::NativePython, String>,
        on_started: &dyn Fn(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<(Self, Option<ManagedPython>), String> {
        let explicit = configured
            .as_ref()
            .is_some_and(|python| !python.is_empty() && python != "managed");
        Self::python_with(
            configured,
            resolver.has_uv(),
            extension_directory,
            |started| {
                crate::resolver::execution::resolve_python_manifest(
                    requirements,
                    resolver,
                    None,
                    None,
                    started,
                )
            },
            |executable, started| {
                if explicit {
                    inspect_explicit(executable, started)
                } else {
                    crate::resolver::execution::inspect_native(resolver, executable, started)
                }
            },
            on_started,
        )
    }

    #[cfg(any(unix, windows))]
    fn python_with(
        configured: Option<OsString>,
        has_uv: bool,
        extension_directory: Option<PathBuf>,
        resolve: impl FnOnce(
            &dyn Fn(ResolverStopHandle) -> Result<(), String>,
        ) -> Result<ManagedPython, String>,
        inspect: impl FnOnce(
            &Path,
            &dyn Fn(ResolverStopHandle) -> Result<(), String>,
        ) -> Result<crate::python::NativePython, String>,
        on_started: &dyn Fn(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<(Self, Option<ManagedPython>), String> {
        let explicit = configured.filter(|value| !value.is_empty() && value != "managed");
        let (executable, managed) = if let Some(explicit) = &explicit {
            let executable = crate::python::explicit_executable(explicit)?;
            (executable, None)
        } else {
            if !has_uv {
                return Err("Python sessions without R require `uv` on PATH; set python in .agents/console/config.yaml to use an existing environment".into());
            }
            let managed = resolve(on_started)?;
            (managed.python().to_path_buf(), Some(managed))
        };
        // Preserve virtualenv symlinks: canonicalizing here would lose the
        // environment even though its base executable has the same identity.
        let executable = std::path::absolute(executable)
            .map_err(|error| format!("cannot locate selected Python: {error}"))?;
        let selected = inspect(&executable, on_started)?;
        // Default and requested extensions share the host cache across generations.
        let duckdb_extension_directory = managed.as_ref().and(extension_directory);
        let selection = Self {
            r_home: None,
            installation: None,
            r_settings: Default::default(),
            python: Some(Python {
                selected: Box::new(selected),
                explicit,
                managed: managed.is_some(),
                duckdb_extension_directory,
            }),
        };
        Ok((selection, managed))
    }

    pub(crate) fn python_only(&self) -> bool {
        self.r_home.is_none()
    }

    #[cfg(any(unix, windows))]
    pub(crate) fn prepare_default_duckdb_extensions(
        &self,
        managed: Option<&ManagedPython>,
        resolver: &crate::resolver::execution::PythonConfiguration,
        on_started: &dyn Fn(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<std::collections::BTreeSet<String>, String> {
        let Some(managed) = managed.filter(|_| !DEFAULT_DUCKDB_EXTENSIONS.is_empty()) else {
            return Ok(Default::default());
        };
        if !crate::resolver::execution::python_duckdb_available(resolver, managed, on_started)? {
            return Ok(Default::default());
        }
        let directory = self
            .duckdb_extension_directory()
            .ok_or("DuckDB extension preparation requires an absolute HOME at server startup")?;
        let extensions = DEFAULT_DUCKDB_EXTENSIONS
            .iter()
            .map(|name| (*name).to_string())
            .collect::<Vec<_>>();
        crate::resolver::execution::resolve_python_duckdb_extensions(
            resolver,
            managed,
            &extensions,
            directory,
            on_started,
        )?;
        Ok(extensions.into_iter().collect())
    }

    pub(crate) fn duckdb_extension_directory(&self) -> Option<&Path> {
        self.python
            .as_ref()
            .and_then(|python| python.duckdb_extension_directory.as_deref())
    }

    pub(crate) fn configure(&self, command: &mut Command) -> Result<(), String> {
        if let Some(installation) = &self.installation {
            installation.configure(command)?;
        }
        if let Some(home) = &self.r_home {
            // Preserve native filename bytes through R_HOME. The structured
            // Python handoff does not need to encode the R path as UTF-8.
            command.env("R_HOME", home);
        }
        command.env(
            ENVIRONMENT,
            serde_json::to_string(&WorkerSelection {
                r: self.r_home.is_some(),
                installation: self.installation.clone(),
                r_settings: self.r_settings.clone(),
                python: self.python.clone(),
            })
            .map_err(|error| format!("cannot encode runtime selections: {error}"))?,
        );
        if let Some(python) = &self.python {
            command
                .env_remove("PYTHONHOME")
                .env_remove("PYTHONPLATLIBDIR");
            if let Some(explicit) = &python.explicit {
                command.env("RETICULATE_PYTHON", explicit);
            } else if !python.managed {
                command.env("RETICULATE_PYTHON", &python.selected.embedding.python);
            } else if self.r_home.is_some() {
                command.env("RETICULATE_PYTHON", "managed");
            } else {
                command.env_remove("RETICULATE_PYTHON");
            }
            if let Some(directory) = &python.duckdb_extension_directory {
                command.env(DUCKDB_EXTENSION_DIRECTORY, directory);
            } else {
                command.env_remove(DUCKDB_EXTENSION_DIRECTORY);
            }
            if !python.managed {
                command.env_remove("MCP_CONSOLE_MANAGED_PYTHON");
            }
        }
        Ok(())
    }

    pub(crate) fn from_environment() -> Result<Option<WorkerSelection>, String> {
        std::env::var(ENVIRONMENT)
            .ok()
            .map(|value| {
                serde_json::from_str(&value)
                    .map_err(|error| format!("invalid internal local runtime selection: {error}"))
            })
            .transpose()
    }
}

#[derive(Clone, serde::Serialize, serde::Deserialize)]
pub(crate) struct RInstallation {
    #[serde(with = "native_path")]
    pub(crate) home: PathBuf,
    resources: [OsString; 3],
    #[serde(default)]
    identity: Vec<r_selection::FileIdentity>,
    #[serde(default)]
    resource_targets: Vec<OsString>,
}

impl RInstallation {
    pub(crate) fn configure_environment(&self) {
        unsafe { std::env::set_var("R_HOME", &self.home) };
        for (name, value) in ["R_SHARE_DIR", "R_INCLUDE_DIR", "R_DOC_DIR"]
            .into_iter()
            .zip(&self.resources)
        {
            unsafe { std::env::set_var(name, value) };
        }
    }
}

#[cfg(unix)]
pub(crate) fn r_installation() -> Result<RInstallation, Box<dyn std::error::Error>> {
    use std::os::unix::ffi::OsStringExt;

    // Harp's setup reads R_HOME with env::var and mistakes non-UTF-8 values
    // for absence. Preserve its validation using the native path in that case.
    let home = if let Some(home) = std::env::var_os("R_HOME").filter(|home| home.to_str().is_none())
    {
        let home = PathBuf::from(home);
        if !home
            .try_exists()
            .map_err(|error| format!("Can't check if `R_HOME` path exists: {error}"))?
        {
            return Err(format!("The `R_HOME` path '{}' does not exist.", home.display()).into());
        }
        home
    } else {
        harp::command::r_home_setup()?
    };
    // Read the selected launcher's resource paths without starting R. Capture
    // them before either interpreter starts; R's install paths can be split.
    let resources = harp::command::r_command(&home, |command| {
        command
            .args([
                "CMD",
                "/bin/sh",
                "-c",
                r#"printf '%s\000' "$R_SHARE_DIR" "$R_INCLUDE_DIR" "$R_DOC_DIR""#,
            ])
            .stdin(std::process::Stdio::null());
    })?;
    if !resources.status.success() {
        return Err(format!(
            "Can't read R resource directories: {}: {}",
            resources.status,
            String::from_utf8_lossy(&resources.stderr)
        )
        .into());
    }
    let values = resources
        .stdout
        .strip_suffix(b"\0")
        .ok_or("R launcher did not terminate its resource directories")?
        .split(|byte| *byte == 0)
        .collect::<Vec<_>>();
    if values.len() != 3 || values.iter().any(|value| value.is_empty()) {
        return Err("R launcher did not supply three resource directories".into());
    }
    let installation = RInstallation {
        home,
        identity: Vec::new(),
        resource_targets: Vec::new(),
        resources: std::array::from_fn(|index| OsString::from_vec(values[index].to_vec())),
    };
    installation.configure_environment();
    Ok(installation)
}

#[cfg(windows)]
pub(crate) fn r_installation() -> Result<RInstallation, Box<dyn std::error::Error>> {
    let home = harp::command::r_home_setup()?;
    let installation = RInstallation {
        identity: Vec::new(),
        resource_targets: Vec::new(),
        resources: ["share", "include", "doc"].map(|name| home.join(name).into_os_string()),
        home,
    };
    installation.configure_environment();
    Ok(installation)
}
