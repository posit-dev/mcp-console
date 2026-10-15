//! Trusted application settings, captured before starting a session or workload.

use std::ffi::OsStr;
use std::path::{Path, PathBuf};

use serde::Deserialize;
use serde_json::{Map, Value};

pub(crate) mod materialize;
mod python;
mod resolution;
mod sandbox;
pub(crate) use sandbox::Tempdir;
pub(crate) const MANAGED_TEMPDIR_ENVIRONMENT: &str = "MCP_CONSOLE_MANAGED_TEMPDIR";
mod tool;
pub(crate) use python::Managed as ManagedPythonSettings;
pub(crate) use python::PythonChoice;
pub(crate) use resolution::Resolution;
pub(crate) use tool::Tool;
pub(crate) mod startup;

pub const ENVIRONMENT: &str = "MCP_CONSOLE_SANDBOX_SETTINGS";

/// Apply workload environment controls to a child, never the supervisor.
pub fn configure_environment(command: &mut std::process::Command, settings: &SandboxSettings) {
    if settings.get("inherit_environment") == Some(&Value::Bool(false)) {
        command.env_clear();
    }
    if let Some(Value::Object(environment)) = settings.get("environment") {
        command.envs(
            environment
                .iter()
                .map(|(name, value)| (name, value.as_str().expect("captured string environment"))),
        );
    }
}

/// Captured native policy and Console tempdir selection. Launch consumes the
/// selector before forwarding the native policy on the execution host.
pub type SandboxSettings = Map<String, Value>;

pub(crate) fn environment_path(settings: &SandboxSettings, name: &str) -> Option<PathBuf> {
    let inherit = settings.get("inherit_environment") != Some(&Value::Bool(false));
    settings
        .get("environment")
        .and_then(|values| {
            if cfg!(windows) {
                values
                    .as_object()?
                    .iter()
                    .find(|(key, _)| key.eq_ignore_ascii_case(name))
                    .map(|(_, value)| value)
            } else {
                values.get(name)
            }
        })
        .and_then(Value::as_str)
        .map(PathBuf::from)
        .or_else(|| {
            inherit
                .then(|| std::env::var_os(name).map(PathBuf::from))
                .flatten()
        })
        .filter(|path| !path.as_os_str().is_empty())
}

#[derive(Clone, Copy, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Cache {
    Console,
    Host,
}

/// Preserve Console's assignments and removals after project environment controls.
pub fn preserve_environment<'a>(
    policy: &mut SandboxSettings,
    values: impl IntoIterator<Item = (&'a OsStr, Option<&'a OsStr>)>,
) -> Result<(), String> {
    let inherit = policy.get("inherit_environment") != Some(&Value::Bool(false));
    if !inherit {
        policy
            .entry("environment")
            .or_insert_with(|| Value::Object(Map::new()));
    }
    if let Some(Value::Object(environment)) = policy.get_mut("environment") {
        for (name, value) in values {
            let name = name
                .to_str()
                .ok_or_else(|| "worker environment name must be UTF-8".to_string())?;
            // Keep invalid native values for execution-host validation, even
            // when Console owns the assignment or removal of a valid value.
            if environment
                .get(name)
                .is_some_and(|value| !value.is_string())
            {
                continue;
            }
            if !inherit && let Some(value) = value {
                let value = value.to_str().ok_or_else(|| {
                    format!("worker environment value for '{name}' must be UTF-8")
                })?;
                environment.insert(name.into(), value.into());
            } else {
                environment.remove(name);
            }
        }
    }
    Ok(())
}

/// Recognize native unit variants for application additions and descriptions.
/// This does not validate or transform policy values sent to the runner.
pub fn native_variant_name(value: &Value) -> Option<&str> {
    match value {
        Value::String(name) => Some(name),
        Value::Object(object) if object.len() == 1 => object
            .iter()
            .next()
            .and_then(|(name, value)| value.is_null().then_some(name.as_str())),
        _ => None,
    }
}

#[derive(Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct Project {
    tool: Tool,
    startup: Option<startup::Startup>,
    cache: Option<Cache>,
    python: Option<python::Python>,
    #[serde(deserialize_with = "r_settings")]
    r: Option<R>,
    languages: Option<Vec<crate::cell::Language>>,
    #[serde(default = "inherit_by_default")]
    inherit_environment: bool,
    #[serde(deserialize_with = "environment")]
    environment: std::collections::BTreeMap<String, String>,
    #[serde(deserialize_with = "sandbox::optional_mapping")]
    sandbox: Option<sandbox::Sandbox>,
    #[serde(deserialize_with = "sandbox::mapping")]
    resolver: Resolver,
}

#[derive(Clone, Default, serde::Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub(crate) struct R {
    // Workers receive the accepted installation; only the controller selects a launcher.
    #[serde(deserialize_with = "sandbox::supplied", skip_serializing)]
    pub executable: Option<PathBuf>,
    #[serde(deserialize_with = "sandbox::supplied", skip_serializing)]
    pub packages: Option<Vec<String>>,
    #[serde(skip_serializing)]
    pub resolution: Resolution,
    pub vanilla: bool,
}

fn r_settings<'de, D: serde::Deserializer<'de>>(deserializer: D) -> Result<Option<R>, D::Error> {
    let value = Value::deserialize(deserializer)?;
    if value.is_null() {
        return Ok(None);
    }
    sandbox::mapping(value)
        .map(Some)
        .map_err(serde::de::Error::custom)
}

fn inherit_by_default() -> bool {
    true
}

#[derive(Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct Resolver {
    #[serde(deserialize_with = "sandbox::supplied")]
    inherit_environment: Option<bool>,
    #[serde(deserialize_with = "environment")]
    environment: std::collections::BTreeMap<String, String>,
    #[serde(deserialize_with = "sandbox::optional_mapping")]
    sandbox: Option<sandbox::Sandbox>,
}

fn environment<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> Result<std::collections::BTreeMap<String, String>, D::Error> {
    use serde::de::Error as _;
    let values: std::collections::BTreeMap<String, Value> = sandbox::mapping(deserializer)?;
    values
        .into_iter()
        .map(|(name, value)| {
            let value = value.as_str().ok_or_else(|| {
                D::Error::custom(format!("{name}: environment values must be strings"))
            })?;
            Ok((name, value.to_owned()))
        })
        .collect()
}

#[derive(Default)]
pub(crate) struct Captured {
    pub tool: Tool,
    pub startup: Option<startup::Startup>,
    pub cache: Option<Cache>,
    pub python: Option<PythonChoice>,
    pub r: Option<R>,
    pub languages: Option<crate::cell::Languages>,
    pub source: Option<String>,
    pub policy: SandboxSettings,
    pub resolver: SandboxSettings,
    pub sandbox_requested: bool,
    pub resolver_sandbox_requested: bool,
}

pub fn discover(
    directory: &Path,
    overrides: &crate::cli::ConfigOverrides,
) -> Result<Captured, String> {
    read(directory, overrides).map(|(captured, _)| captured)
}

#[derive(Default, Deserialize)]
#[serde(deny_unknown_fields)]
struct AdapterOverrides {
    files: Vec<PathBuf>,
    values: Vec<String>,
}

pub(crate) fn read(
    directory: &Path,
    overrides: &crate::cli::ConfigOverrides,
) -> Result<(Captured, Value), String> {
    // The R adapter transports the same ordered overrides in a file so tool
    // prose and valid startup programs are not bounded by process argv limits.
    let adapter: AdapterOverrides = if let Some(path) = &overrides.values_file {
        let source = std::fs::read(directory.join(path)).map_err(|error| {
            format!("cannot read config overrides '{}': {error}", path.display())
        })?;
        serde_json::from_slice(&source)
            .map_err(|error| format!("invalid config overrides '{}': {error}", path.display()))?
    } else {
        AdapterOverrides::default()
    };
    let mut values = adapter.values;
    values.extend(overrides.values.iter().cloned());
    let mut paths = adapter.files;
    if !paths.is_empty() && (!overrides.no_config || overrides.config_file.is_some()) {
        return Err("adapter configuration files require --no-config".into());
    }
    // Select sources before resolving or inspecting them. Explicit selection and
    // global exclusions must not resolve Console home for configuration discovery.
    if let Some(path) = &overrides.config_file {
        if overrides.no_config || overrides.no_global_config || overrides.no_project_config {
            return Err("--config-file cannot be combined with --no-config, --no-global-config, or --no-project-config".into());
        }
        paths.push(path.clone());
    } else if !overrides.no_config {
        if !overrides.no_global_config
            && let Some(home) = crate::console_paths::home_console_directory()?
        {
            paths.push(home.join("config.yaml"));
        }
        if !overrides.no_project_config {
            paths.push(PathBuf::from(".agents/console/config.yaml"));
        }
    }
    let crate::config::Loaded { value, paths } =
        crate::config::load(directory, &paths, &values, overrides.config_file.is_some())?;
    let configured = value.is_some();
    let files = paths
        .iter()
        .map(|path| path.display().to_string())
        .collect::<Vec<_>>()
        .join(", ");
    let name = match (paths.len(), values.is_empty()) {
        (0, true) => "configuration".into(),
        (0, false) => "configuration with CLI overrides".into(),
        (1, true) => files,
        (_, true) => format!("configuration from {files}"),
        (_, false) => format!("configuration from {files} with CLI overrides"),
    };
    let value = value.unwrap_or_else(|| serde_json::json!({}));
    let mut project: Project =
        serde_path_to_error::deserialize(value.clone()).map_err(|error| {
            format!("{name}: {error}; see docs/CONFIGURATION.md for the public format")
        })?;
    if let Some(packages) = project.r.as_ref().and_then(|r| r.packages.as_ref()) {
        crate::worker_client::validate_r_requirements(packages)
            .map_err(|error| format!("{name}: r.packages: {error}"))?;
        if !packages.is_empty() && project.r.as_ref().unwrap().resolution == Resolution::Disabled {
            return Err(format!(
                "{name}: r.packages must be empty under r.resolution=disabled; use startup_only to prepare configured packages"
            ));
        }
    }
    if let Some(r) = &mut project.r
        && let Some(path) = &mut r.executable
    {
        if path.as_os_str().is_empty() {
            return Err(format!(
                "{name}: r.executable must name an R executable or launcher"
            ));
        }
        if let Ok(relative) = path.strip_prefix("~") {
            let home = std::env::var_os("HOME")
                .map(PathBuf::from)
                .filter(|home| home.is_absolute())
                .ok_or("r.executable home expansion requires an absolute HOME")?;
            *path = home.join(relative);
        }
        *path = std::path::absolute(&*path)
            .map_err(|error| format!("{name}: cannot locate r.executable: {error}"))?;
    }
    if let Some(startup) = &project.startup {
        startup
            .validate()
            .map_err(|error| format!("{name}: {error}"))?;
    }
    let languages = project
        .languages
        .map(|selected| {
            if selected.is_empty() {
                return Err(format!(
                    "{name}: languages must contain at least one of r, python, or sql"
                ));
            }
            let mut languages = crate::cell::Languages::default();
            for language in selected {
                match language {
                    crate::cell::Language::R => languages.r = true,
                    crate::cell::Language::Python => languages.python = true,
                    crate::cell::Language::Sql => languages.sql = true,
                }
            }
            Ok(languages)
        })
        .transpose()?;
    project
        .tool
        .validate(languages)
        .map_err(|error| format!("{name}: {error}"))?;
    let sandbox_requested = project.sandbox.is_some();
    let resolver_sandbox_requested = project.resolver.sandbox.is_some();
    if cfg!(windows) && resolver_sandbox_requested {
        return Err(format!(
            "{name}: resolver.sandbox: explicit permissions are unsupported because Windows preparation runs with host permissions"
        ));
    }
    let mut policy = project
        .sandbox
        .unwrap_or_default()
        .compile(false)
        .map_err(|error| format!("{name}: {error}"))?;
    let mut resolver = project
        .resolver
        .sandbox
        .unwrap_or_default()
        .compile(true)
        .map_err(|error| format!("{name}: {error}"))?;
    let mut resolver_environment = project.environment.clone();
    resolver_environment.extend(project.resolver.environment);
    for (settings, inherit, environment) in [
        (
            &mut policy,
            project.inherit_environment,
            project.environment,
        ),
        (
            &mut resolver,
            project
                .resolver
                .inherit_environment
                .unwrap_or(project.inherit_environment),
            resolver_environment,
        ),
    ] {
        if !inherit {
            settings.insert("inherit_environment".into(), false.into());
        }
        if !environment.is_empty() {
            settings.insert(
                "environment".into(),
                serde_json::to_value(environment).expect("string environment"),
            );
        }
    }
    Ok((
        Captured {
            tool: project.tool,
            r: project.r,
            startup: project.startup,
            cache: project.cache,
            languages,
            python: project.python.map(python::Python::capture).transpose()?,
            source: configured.then_some(name),
            policy,
            resolver,
            sandbox_requested,
            resolver_sandbox_requested,
        },
        value,
    ))
}

pub fn from_environment(name: &str) -> Result<SandboxSettings, String> {
    let value = std::env::var(name)
        .map_err(|error| format!("cannot read sandbox settings environment '{name}': {error}"))?;
    serde_json::from_str(&value)
        .map_err(|error| format!("invalid sandbox settings environment '{name}': {error}"))
}
