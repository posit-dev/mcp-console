//! Mandatory limits supplied by the trusted launcher, outside configuration
//! merging. Captured snapshots retain them through preparation and replacement;
//! the runner intersects them after adding its own runtime grants.

use std::path::{Component, Path};
use std::process::Command;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::SandboxSettings;

const SOURCE_ENVIRONMENT: &str = "_console_limits_environment";

pub(crate) struct Captured {
    environment: String,
    policy: Option<Value>,
}

#[derive(Default, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
struct Limits {
    #[serde(
        deserialize_with = "super::sandbox::mapping",
        skip_serializing_if = "Filesystem::is_empty"
    )]
    filesystem: Filesystem,
    #[serde(
        deserialize_with = "super::sandbox::mapping",
        skip_serializing_if = "Network::is_empty"
    )]
    network: Network,
}

#[derive(Default, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
struct Filesystem {
    deny: Vec<String>,
}

impl Filesystem {
    fn is_empty(&self) -> bool {
        self.deny.is_empty()
    }
}

#[derive(Default, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
struct Network {
    #[serde(
        deserialize_with = "super::sandbox::mapping",
        skip_serializing_if = "Domains::is_empty"
    )]
    domains: Domains,
    #[serde(skip_serializing_if = "Option::is_none")]
    allow_local_binding: Option<bool>,
}

impl Network {
    fn is_empty(&self) -> bool {
        self.domains.is_empty() && self.allow_local_binding != Some(false)
    }
}

#[derive(Default, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
struct Domains {
    #[serde(skip_serializing_if = "Option::is_none")]
    allow: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    deny: Vec<String>,
}

impl Domains {
    fn is_empty(&self) -> bool {
        self.allow.is_none() && self.deny.is_empty()
    }
}

impl Captured {
    pub(crate) fn from_environment(name: &str, directory: &Path) -> Result<Self, String> {
        if name.is_empty() || name.contains(['=', '\0']) {
            return Err("--limits-env requires a valid environment variable name".into());
        }
        // Console regenerates its private transports on each launch. They
        // cannot also name an input whose removal must dominate every overlay.
        let reserved = if cfg!(windows) {
            same_environment_name(
                &name.chars().take("MCP_CONSOLE_".len()).collect::<String>(),
                "MCP_CONSOLE_",
            )
        } else {
            name.starts_with("MCP_CONSOLE_")
        };
        if reserved {
            return Err("--limits-env cannot use the reserved MCP_CONSOLE_ namespace".into());
        }
        let source = std::env::var(name).map_err(|_| {
            format!("sandbox limits environment '{name}' is missing or is not valid UTF-8")
        })?;
        // Parser and deserializer diagnostics can quote source values. Keep
        // this trusted transport's contents out of error output.
        let value = crate::config::parse_document(&source).map_err(|_| {
            format!("sandbox limits environment '{name}': expected one YAML or JSON mapping")
        })?;
        let mut limits: Limits = serde_json::from_value(value).map_err(|_| {
            format!("sandbox limits environment '{name}': invalid fields or types; expected filesystem.deny, network.domains.allow/deny, and network.allow_local_binding")
        })?;
        for (index, path) in limits.filesystem.deny.iter_mut().enumerate() {
            if path.is_empty()
                || path.contains('\0')
                || Path::new(path).components().any(|component| {
                    matches!(component, Component::Normal(value)
                        if value.to_string_lossy().contains(['*', '?', '[', ']', '{', '}']))
                })
            {
                return Err(format!(
                    "sandbox limits filesystem.deny[{index}]: expected a nonempty literal path without wildcards"
                ));
            }
            let expanded = crate::console_paths::expand_home(Path::new(path)).map_err(|_| {
                format!("sandbox limits filesystem.deny[{index}]: cannot expand home directory")
            })?;
            *path = std::path::absolute(directory.join(expanded))
                .map_err(|_| {
                    format!("sandbox limits filesystem.deny[{index}]: cannot resolve path")
                })?
                .into_os_string()
                .into_string()
                .map_err(|_| {
                    format!("sandbox limits filesystem.deny[{index}]: path must be UTF-8")
                })?;
        }
        for (access, domains) in [
            (
                "allow",
                limits.network.domains.allow.as_deref().unwrap_or_default(),
            ),
            ("deny", limits.network.domains.deny.as_slice()),
        ] {
            for (index, domain) in domains.iter().enumerate() {
                crate::settings::sandbox::validate_domain(domain).map_err(|message| {
                    format!("sandbox limits network.domains.{access}[{index}]: {message}")
                })?;
            }
        }
        let restrictive = !limits.filesystem.is_empty() || !limits.network.is_empty();
        if restrictive && crate::warmup::trusted_preparation() {
            return Err(
                "sandbox limits cannot be combined with trusted installation warmup".into(),
            );
        }
        Ok(Self {
            environment: name.into(),
            policy: restrictive.then(|| serde_json::to_value(limits).expect("limits serialize")),
        })
    }

    pub(crate) fn apply(&self, settings: &mut SandboxSettings) {
        if let Some(policy) = &self.policy {
            settings.insert("limits".into(), policy.clone());
        }
        settings.insert(SOURCE_ENVIRONMENT.into(), self.environment.clone().into());
        scrub_environment(settings, &self.environment);
    }
}

pub(crate) fn require_enforcement(
    settings: &SandboxSettings,
    no_sandbox: bool,
) -> Result<(), String> {
    if settings.contains_key("limits") {
        if no_sandbox {
            return Err("sandbox limits require sandboxing; --no-sandbox and inherited full access are unavailable".into());
        }
        if crate::warmup::trusted_preparation() {
            return Err(
                "sandbox limits cannot be combined with trusted installation warmup".into(),
            );
        }
    }
    Ok(())
}

pub(crate) fn remove_source_environment(command: &mut Command, settings: &SandboxSettings) {
    if let Some(name) = source_environment(settings) {
        command.env_remove(name);
    }
}

pub(crate) fn source_environment(settings: &SandboxSettings) -> Option<&str> {
    settings.get(SOURCE_ENVIRONMENT).and_then(Value::as_str)
}

/// Remove the private transport marker and any late workload environment
/// assignment. The native request retains only normalized limits.
pub(crate) fn prepare_runner(command: &mut Command, settings: &mut SandboxSettings) {
    if let Some(Value::String(name)) = settings.remove(SOURCE_ENVIRONMENT) {
        command.env_remove(&name);
        scrub_environment(settings, &name);
    }
}

fn scrub_environment(settings: &mut SandboxSettings, name: &str) {
    if let Some(Value::Object(environment)) = settings.get_mut("environment") {
        environment.retain(|key, _| !same_environment_name(key, name));
    }
}

fn same_environment_name(left: &str, right: &str) -> bool {
    if cfg!(windows) {
        let mut names = Command::new("");
        names.env(left, "").env(right, "");
        names.get_envs().count() == 1
    } else {
        left == right
    }
}
