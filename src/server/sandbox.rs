//! Translate opt-in client authority once, before any runtime preparation.
//! Source: openai/codex 16e578dd1ddaf4f5db9cd1f3dbfea0958fd74339,
//! codex-rs/codex-mcp/src/sandbox_metadata.rs and protocol/src/models.rs.
//! This adapter intentionally interprets supported permissions rather than
//! forwarding the opaque state to the client's executable.

use std::path::{Component, Path, PathBuf};
use std::sync::OnceLock;

use rmcp::model::RequestMetaObject;
use serde::Deserialize;
use serde_json::{Value, json};

use crate::settings::{Profile as BaseProfile, ProfileSelection, SandboxSettings};

pub(super) const CAPABILITY: &str = "codex/sandbox-state";
pub(super) const LEGACY_CAPABILITY: &str = "codex/sandbox-state-meta";

/// Permission selection or validation completed in transport request order.
#[derive(Clone)]
pub(super) struct CaptureResult(pub Result<(), String>);

#[derive(Clone)]
pub(crate) struct Configuration {
    pub no_sandbox: bool,
    pub profile: Option<ProfileSelection>,
    pub network_explicit: bool,
    pub policy: SandboxSettings,
    pub resolver: SandboxSettings,
    pub cache: Option<crate::settings::Cache>,
    pub writable_roots: Vec<PathBuf>,
    pub directory: PathBuf,
}

impl Configuration {
    pub(crate) fn configure(
        mut self,
        inherited: Option<Policy>,
        python: Option<&crate::settings::PythonChoice>,
    ) -> Result<Self, String> {
        let selected = self
            .profile
            .as_ref()
            .map(|profile| profile.select(inherited.is_some()));
        if selected == Some(BaseProfile::Workspace) {
            crate::settings::apply_workspace_profile(&mut self.policy);
        }
        match inherited {
            Some(Policy::Disabled) => self.no_sandbox = true,
            Some(Policy::Permissions {
                filesystem,
                network,
            }) if !self.no_sandbox => {
                if filesystem["kind"] == "unrestricted" {
                    let filesystem = if let Some(local) = self.policy.get("filesystem") {
                        let mut entries = local["entries"]
                            .as_array()
                            .expect("captured entries")
                            .clone();
                        // A writable root is the native equivalent of an
                        // unrestricted base that still admits local denials.
                        entries.push(json!({
                            "path": {"type": "special", "value": {"kind": "root"}},
                            "access": "write",
                        }));
                        json!({"kind": "restricted", "entries": entries})
                    } else {
                        filesystem
                    };
                    self.policy.insert("filesystem".into(), filesystem);
                    self.policy.remove("extends");
                } else {
                    let filesystem_policy =
                        self.policy.entry("filesystem").or_insert_with(|| json!({}));
                    let entries = filesystem_policy
                        .as_object_mut()
                        .expect("captured filesystem")
                        .entry("entries")
                        .or_insert_with(|| json!([]));
                    entries.as_array_mut().expect("captured entries").extend(
                        filesystem["entries"]
                            .as_array()
                            .expect("translated entries")
                            .iter()
                            .cloned(),
                    );
                }
                // Explicit network settings override the selected base. Client
                // metadata has no proxy endpoint or destination configuration.
                if !self.network_explicit {
                    self.policy.insert("network".into(), network);
                }
            }
            Some(Policy::Permissions { .. }) | None => {}
        }
        // Choose caches after inherited full access: sandbox-produced caches
        // must never become inputs to a newly unsandboxed session.
        crate::resolver::cache::configure(
            self.cache,
            self.no_sandbox,
            python,
            &mut self.resolver,
            &mut self.policy,
        )?;
        if !self.no_sandbox {
            self.policy = crate::settings::materialize_sandbox(
                self.policy,
                self.writable_roots.clone(),
                &self.directory,
            )?;
        }
        Ok(self)
    }
}

pub(super) struct Inheritance {
    profile: Option<ProfileSelection>,
    captured: OnceLock<Option<Policy>>,
}

impl Inheritance {
    pub fn new(profile: Option<ProfileSelection>) -> Self {
        Self {
            profile,
            captured: OnceLock::new(),
        }
    }

    pub fn enabled(&self) -> bool {
        self.profile
            .as_ref()
            .is_some_and(ProfileSelection::inherits_codex)
    }

    pub fn capture(&self, metadata: &RequestMetaObject) -> Result<(), String> {
        if !self.enabled() {
            return Ok(());
        }
        let state = metadata
            .get(CAPABILITY)
            .or_else(|| metadata.get(LEGACY_CAPABILITY));
        let policy = state.and_then(Policy::translate);
        let captured = self.captured.get_or_init(|| policy.clone());
        if state.is_some() && policy.is_none() {
            let fallback = self.profile.as_ref().expect("client profile").select(false);
            if captured.is_some() {
                eprintln!(
                    "Warning: could not parse Codex sandbox settings; reconnect to apply the {} fallback",
                    fallback.name()
                );
            } else {
                eprintln!(
                    "Warning: could not parse Codex sandbox settings; falling back to {}",
                    fallback.name()
                );
            }
        }
        if state.is_some() && captured != &policy {
            return Err("client sandbox permissions changed; reconnect to apply them before sending more code".into());
        }
        Ok(())
    }

    pub fn policy(&self) -> Option<Policy> {
        if self.enabled() {
            self.captured
                .get()
                .expect("first send captured sandbox authority")
                .clone()
        } else {
            None
        }
    }
}

#[derive(Clone, PartialEq)]
pub(crate) enum Policy {
    Disabled,
    Permissions { filesystem: Value, network: Value },
}

#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum Profile {
    Disabled,
    Managed {
        file_system: Filesystem,
        network: Network,
    },
    External {
        network: Network,
    },
}

#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum Filesystem {
    Restricted { entries: Vec<Value> },
    Unrestricted,
}

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum Network {
    Restricted,
    Enabled,
}

impl Policy {
    fn translate(state: &Value) -> Option<Self> {
        let profile: Profile =
            serde_json::from_value(state.get("permissionProfile")?.clone()).ok()?;
        let (filesystem, network) = match profile {
            Profile::Disabled => return Some(Self::Disabled),
            Profile::External { network } => (Filesystem::Unrestricted, network),
            Profile::Managed {
                file_system,
                network,
            } => (file_system, network),
        };
        let filesystem = match filesystem {
            Filesystem::Unrestricted => json!({"kind": "unrestricted"}),
            Filesystem::Restricted { entries } => {
                let cwd = state
                    .get("sandboxCwd")
                    .and_then(Value::as_str)
                    .and_then(local_path);
                let mut entries: Vec<_> = entries
                    .iter()
                    .map(|entry| translate_entry(entry, cwd.as_deref()))
                    .collect::<Option<_>>()?;
                // Native access is independent of rule order and duplication.
                entries.sort_unstable_by_key(Value::to_string);
                entries.dedup();
                json!({"kind": "restricted", "entries": entries})
            }
        };
        Some(Self::Permissions {
            filesystem,
            network: match network {
                Network::Restricted => "restricted".into(),
                Network::Enabled => "enabled".into(),
            },
        })
    }
}

fn translate_entry(entry: &Value, cwd: Option<&Path>) -> Option<Value> {
    let access = match entry.get("access")?.as_str()? {
        "read" => "read",
        "write" => "write",
        "deny" | "none" => "deny",
        _ => return None,
    };
    let path = entry.get("path")?;
    let path = match path.get("type")?.as_str()? {
        "path" => {
            json!({"type": "path", "path": local_path(path.get("path")?.as_str()?)?.to_str()?})
        }
        "special" => {
            let value = path.get("value")?;
            match value.get("kind")?.as_str()? {
                "project_roots" | "current_working_directory" => {
                    let subpath = match value.get("subpath") {
                        None | Some(Value::Null) => "",
                        Some(Value::String(subpath)) => subpath,
                        _ => return None,
                    };
                    // Symbolic subpaths extend the selected root; they cannot
                    // replace it or traverse to a different root.
                    if subpath.contains('\0')
                        || Path::new(subpath).components().any(|component| {
                            !matches!(component, Component::Normal(_) | Component::CurDir)
                        })
                    {
                        return None;
                    }
                    let path = cwd?.join(subpath);
                    json!({"type": "path", "path": path.to_str()?})
                }
                kind @ ("root" | "minimal" | "tmpdir" | "slash_tmp") => {
                    json!({"type": "special", "value": {"kind": kind}})
                }
                // Unrecognized permissions make the whole profile unavailable;
                // dropping an entry could lose a denial and widen authority.
                _ => return None,
            }
        }
        _ => return None,
    };
    let mut entry_policy = json!({"path": path, "access": access});
    match entry.get("missing_path_behavior") {
        None | Some(Value::Null) => {}
        Some(Value::String(behavior)) if behavior == "skip" => {
            entry_policy["missing_path_behavior"] = "skip".into();
        }
        _ => return None,
    }
    Some(entry_policy)
}

fn local_path(value: &str) -> Option<PathBuf> {
    let path = if value.starts_with("file:") {
        url::Url::parse(value).ok()?.to_file_path().ok()?
    } else {
        PathBuf::from(value)
    };
    (path.is_absolute() && !path.as_os_str().as_encoded_bytes().contains(&0)).then_some(path)
}
