//! Human-facing permissions and execution-host additions after configuration layering.

use std::path::{Component, Path, PathBuf};

use serde::{Deserialize, Deserializer, Serialize, de::Error as _};
use serde_json::{Value, json};

use super::{IF_EXISTS, SandboxSettings};

// Option means omitted, not an explicit YAML null.
pub(super) fn supplied<'de, D, T>(deserializer: D) -> Result<Option<T>, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de>,
{
    T::deserialize(deserializer).map(Some)
}

pub(super) fn mapping<'de, D, T>(deserializer: D) -> Result<T, D::Error>
where
    D: Deserializer<'de>,
    T: serde::de::DeserializeOwned,
{
    let value = Value::deserialize(deserializer)?;
    if !value.is_object() {
        return Err(D::Error::custom("expected a mapping"));
    }
    serde_path_to_error::deserialize(value).map_err(D::Error::custom)
}

pub(super) fn supplied_mapping<'de, D, T>(deserializer: D) -> Result<Option<T>, D::Error>
where
    D: Deserializer<'de>,
    T: serde::de::DeserializeOwned,
{
    mapping(deserializer).map(Some)
}

// A whole permission node can be cleared by a later configuration layer.
pub(super) fn optional_mapping<'de, D, T>(deserializer: D) -> Result<Option<T>, D::Error>
where
    D: Deserializer<'de>,
    T: serde::de::DeserializeOwned,
{
    let value = Value::deserialize(deserializer)?;
    if value.is_null() {
        Ok(None)
    } else {
        mapping(value).map(Some).map_err(D::Error::custom)
    }
}

#[derive(Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub(super) struct Sandbox {
    pub(super) profile: Option<ProfileSelection>,
    tempdir: Tempdir,
    #[serde(deserialize_with = "supplied_mapping")]
    filesystem: Option<Filesystem>,
    #[serde(deserialize_with = "supplied")]
    network: Option<Network>,
}

#[derive(Clone, Copy, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Profile {
    ReadOnly,
    Workspace,
    InheritCodex,
}

impl Profile {
    pub(crate) fn name(self) -> &'static str {
        match self {
            Self::ReadOnly => "read_only",
            Self::Workspace => "workspace",
            Self::InheritCodex => "inherit_codex",
        }
    }
}

#[derive(Clone, Serialize)]
#[serde(untagged)]
pub(crate) enum ProfileSelection {
    Named(Profile),
    InheritCodex { inherit_codex: InheritCodexProfile },
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct InheritCodexProfile {
    #[serde(skip_serializing_if = "Option::is_none")]
    fallback: Option<FallbackProfile>,
}

#[derive(Clone, Copy, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
enum FallbackProfile {
    ReadOnly,
    Workspace,
}

impl<'de> Deserialize<'de> for ProfileSelection {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        #[serde(rename_all = "snake_case")]
        enum Mapping {
            InheritCodex(InheritCodexProfile),
        }
        let value = Value::deserialize(deserializer)?;
        if value.is_string() {
            return serde_json::from_value(value)
                .map(Self::Named)
                .map_err(D::Error::custom);
        }
        let Mapping::InheritCodex(inherit_codex) =
            serde_json::from_value(value).map_err(D::Error::custom)?;
        Ok(Self::InheritCodex { inherit_codex })
    }
}

impl ProfileSelection {
    pub(crate) fn inherits_codex(&self) -> bool {
        match self {
            Self::Named(Profile::ReadOnly | Profile::Workspace) => false,
            Self::Named(Profile::InheritCodex) | Self::InheritCodex { .. } => true,
        }
    }

    pub(crate) fn select(&self, client_available: bool) -> Profile {
        let fallback = match self {
            Self::Named(Profile::ReadOnly) => return Profile::ReadOnly,
            Self::Named(Profile::Workspace) => return Profile::Workspace,
            Self::Named(Profile::InheritCodex) => FallbackProfile::ReadOnly,
            Self::InheritCodex { inherit_codex } => {
                inherit_codex.fallback.unwrap_or(FallbackProfile::ReadOnly)
            }
        };
        match (client_available, fallback) {
            (true, _) => Profile::InheritCodex,
            (false, FallbackProfile::ReadOnly) => Profile::ReadOnly,
            (false, FallbackProfile::Workspace) => Profile::Workspace,
        }
    }
}

pub(crate) fn apply_workspace_profile(policy: &mut SandboxSettings) {
    policy.insert("extends".into(), ":workspace".into());
    // Temp permissions have one owner, independently of the
    // native workspace profile's broader temporary defaults.
    policy.insert(
        "workspace_options".into(),
        json!({"exclude_tmpdir_env_var": true, "exclude_slash_tmp": true}),
    );
}

#[derive(Clone, Copy, Default, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Tempdir {
    #[default]
    Managed,
    ReadWrite,
    ReadOnly,
}

impl Tempdir {
    pub(crate) fn from_settings(settings: &SandboxSettings) -> Self {
        settings.get("tempdir").map_or(Self::Managed, |value| {
            serde_json::from_value(value.clone()).expect("captured tempdir choice")
        })
    }

    pub(crate) fn name(self) -> &'static str {
        match self {
            Self::Managed => "managed",
            Self::ReadWrite => "read_write",
            Self::ReadOnly => "read_only",
        }
    }
}

#[derive(Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct Filesystem {
    read_only: Vec<String>,
    read_write: Vec<String>,
    deny: Vec<String>,
    read_only_if_exists: Vec<String>,
    deny_if_exists: Vec<String>,
}

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum DirectNetwork {
    Restricted,
    Enabled,
}

enum Network {
    Direct(DirectNetwork),
    Managed(ManagedNetwork),
}

impl<'de> Deserialize<'de> for Network {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let value = Value::deserialize(deserializer)?;
        if value.is_string() {
            serde_json::from_value(value)
                .map(Self::Direct)
                .map_err(D::Error::custom)
        } else {
            if !value.is_object() {
                return Err(D::Error::custom(
                    "expected restricted, enabled, or a mapping containing proxy",
                ));
            }
            serde_path_to_error::deserialize(value)
                .map(Self::Managed)
                .map_err(D::Error::custom)
        }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ManagedNetwork {
    #[serde(deserialize_with = "mapping")]
    proxy: Proxy,
    #[serde(default, deserialize_with = "mapping")]
    sockets: Sockets,
    #[serde(default)]
    allow_local_binding: LocalBinding,
}

#[derive(Default)]
enum LocalBinding {
    #[default]
    Auto,
    Allow,
    Prohibit,
}

impl<'de> Deserialize<'de> for LocalBinding {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        match Value::deserialize(deserializer)? {
            Value::Null => Ok(Self::Auto),
            Value::String(value) if value == "auto" => Ok(Self::Auto),
            Value::Bool(true) => Ok(Self::Allow),
            Value::Bool(false) => Ok(Self::Prohibit),
            _ => Err(D::Error::custom("expected auto, null, true, or false")),
        }
    }
}

#[derive(Default, Deserialize)]
#[serde(rename_all = "snake_case")]
enum Mode {
    #[default]
    Full,
    Limited,
}

#[derive(Clone, Copy, Default, Deserialize)]
#[serde(rename_all = "snake_case")]
enum Socks {
    Disabled,
    #[default]
    Tcp,
    TcpUdp,
}

enum Proxy {
    Full {
        domains: Option<Domains>,
        upstream: bool,
        socks: Socks,
    },
    Limited {
        domains: Option<Domains>,
        upstream: bool,
    },
}

impl<'de> Deserialize<'de> for Proxy {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        #[derive(Default, Deserialize)]
        #[serde(default, deny_unknown_fields)]
        struct Input {
            mode: Mode,
            #[serde(deserialize_with = "supplied_mapping")]
            domains: Option<Domains>,
            allow_upstream_proxy: bool,
            #[serde(deserialize_with = "supplied")]
            socks5: Option<Socks>,
        }
        let input = Input::deserialize(deserializer)?;
        match input.mode {
            Mode::Full => Ok(Self::Full {
                domains: input.domains,
                upstream: input.allow_upstream_proxy,
                socks: input.socks5.unwrap_or_default(),
            }),
            Mode::Limited => {
                if input.socks5.is_some() {
                    return Err(D::Error::custom(
                        "socks5 is not allowed in limited mode; remove the explicitly configured socks5 field",
                    ));
                }
                Ok(Self::Limited {
                    domains: input.domains,
                    upstream: input.allow_upstream_proxy,
                })
            }
        }
    }
}

#[derive(Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct Domains {
    allow: Vec<String>,
    deny: Vec<String>,
}

#[derive(Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct Sockets {
    unix_sockets: UnixSockets,
}

#[derive(Default)]
enum UnixSockets {
    #[default]
    Empty,
    Allow(Vec<String>),
    All,
}

impl<'de> Deserialize<'de> for UnixSockets {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let value = Value::deserialize(deserializer)?;
        match value {
            Value::String(name) if name == "dangerously_allow_all" => Ok(Self::All),
            Value::Array(_) => serde_json::from_value(value)
                .map(Self::Allow)
                .map_err(D::Error::custom),
            _ => Err(D::Error::custom(
                "expected an absolute-path list or dangerously_allow_all",
            )),
        }
    }
}

impl Sandbox {
    pub(super) fn compile(self, resolver: bool) -> Result<SandboxSettings, String> {
        if resolver
            && self
                .profile
                .as_ref()
                .is_some_and(ProfileSelection::inherits_codex)
        {
            return Err("resolver.sandbox.profile: inherit_codex is a session profile; configure sandbox.profile instead".into());
        }
        let prefix = if resolver {
            "resolver.sandbox"
        } else {
            "sandbox"
        };
        let mut policy = SandboxSettings::new();
        policy.insert("tempdir".into(), self.tempdir.name().into());
        if matches!(
            self.profile,
            Some(ProfileSelection::Named(Profile::Workspace))
        ) {
            apply_workspace_profile(&mut policy);
        }
        if let Some(filesystem) = self.filesystem {
            let entries: Vec<Value> = [
                (filesystem.read_only, "read"),
                (filesystem.read_write, "write"),
                (filesystem.deny, "deny"),
                (filesystem.read_only_if_exists, "read_only_if_exists"),
                (filesystem.deny_if_exists, "deny_if_exists"),
            ]
            .into_iter()
            .flat_map(|(paths, access)| {
                paths.into_iter().map(move |path| {
                    let path = crate::console_paths::expand_home(std::path::Path::new(&path))
                        .map_err(|error| format!("{prefix}.filesystem: {error}"))?;
                    let path = path.to_str().ok_or_else(|| {
                        format!("{prefix}.filesystem: expanded path must be UTF-8")
                    })?;
                    let native_access = match access {
                        "read_only_if_exists" => "read",
                        "deny_if_exists" => "deny",
                        _ => access,
                    };
                    let mut entry =
                        json!({"path": {"type": "path", "path": path}, "access": native_access});
                    if matches!(access, "read_only_if_exists" | "deny_if_exists") {
                        entry[IF_EXISTS] = true.into();
                    }
                    Ok(entry)
                })
            })
            .collect::<Result<_, String>>()?;
            policy.insert(
                "filesystem".into(),
                json!({"kind": "restricted", "entries": entries}),
            );
        }
        let network = self.network.or_else(|| {
            (resolver && cfg!(any(unix, windows))).then(|| {
                Network::Managed(ManagedNetwork {
                    proxy: Proxy::Full {
                        domains: None,
                        upstream: false,
                        socks: Socks::Tcp,
                    },
                    sockets: Sockets::default(),
                    allow_local_binding: LocalBinding::Auto,
                })
            })
        });
        match network {
            None | Some(Network::Direct(DirectNetwork::Restricted)) => {
                policy.insert("network".into(), "restricted".into());
            }
            Some(Network::Direct(DirectNetwork::Enabled)) => {
                policy.insert("network".into(), "enabled".into());
            }
            Some(Network::Managed(network)) => {
                // Normalize once, after all configuration layers. Preserve Unix
                // resolver coordination/DNS and Windows MXC's bidirectional
                // host-loopback requirement without weakening explicit false.
                let local = match network.allow_local_binding {
                    LocalBinding::Auto => resolver || cfg!(windows),
                    LocalBinding::Allow => true,
                    LocalBinding::Prohibit => false,
                };
                if cfg!(windows) && !local {
                    return Err(format!(
                        "{prefix}.network.allow_local_binding: false cannot be enforced with a Windows MXC managed proxy; use auto or true, or remove the proxy"
                    ));
                }
                let (mode, socks, domains, upstream) = match network.proxy {
                    Proxy::Full {
                        domains,
                        upstream,
                        socks,
                    } => ("full", socks, domains, upstream),
                    Proxy::Limited { domains, upstream } => {
                        ("limited", Socks::Disabled, domains, upstream)
                    }
                };
                if matches!(socks, Socks::TcpUdp) {
                    if cfg!(windows) {
                        return Err(format!(
                            "{prefix}.network.proxy.socks5: SOCKS5 UDP is not supported on Windows"
                        ));
                    }
                    // This pin's Linux bridge is TCP-only. Its native async
                    // UDP inspector also routes received replies back south,
                    // so macOS cannot provide a working request/reply channel.
                    return Err(format!(
                        "{prefix}.network.proxy.socks5: tcp_udp is unsupported by the pinned runner (Linux has a TCP-only bridge; native UDP replies are routed back to the destination)"
                    ));
                }
                let mut permissions = SandboxSettings::new();
                if let Some(domains) = domains {
                    for (hosts, access) in [(domains.allow, "allow"), (domains.deny, "deny")] {
                        for (index, host) in hosts.into_iter().enumerate() {
                            validate_domain(&host).map_err(|error| {
                                format!("{prefix}.network.proxy.domains.{access}[{index}]: {error}")
                            })?;
                            // Native matching still owns normalization and deny precedence.
                            permissions.insert(host, access.into());
                        }
                    }
                } else if resolver {
                    permissions.extend(
                        DEFAULT_DOWNLOAD_HOSTS
                            .iter()
                            .map(|host| ((*host).into(), "allow".into())),
                    );
                }
                let (mut sockets, all) = match network.sockets.unix_sockets {
                    UnixSockets::Empty => (Vec::new(), false),
                    UnixSockets::Allow(paths) => (paths, false),
                    UnixSockets::All => (Vec::new(), true),
                };
                if cfg!(windows) && (all || !sockets.is_empty()) {
                    return Err(format!(
                        "{prefix}.network.sockets.unix_sockets: Unix sockets are not supported on Windows"
                    ));
                }
                for (index, path) in sockets.iter_mut().enumerate() {
                    let expanded = crate::console_paths::expand_home(std::path::Path::new(path))
                        .map_err(|error| {
                            format!("{prefix}.network.sockets.unix_sockets[{index}]: {error}")
                        })?;
                    *path = expanded.into_os_string().into_string().map_err(|_| {
                        format!("{prefix}.network.sockets.unix_sockets[{index}]: expanded path must be UTF-8")
                    })?;
                    if !std::path::Path::new(path).is_absolute() {
                        return Err(format!(
                            "{prefix}.network.sockets.unix_sockets[{index}]: socket paths must be literal absolute paths"
                        ));
                    }
                }
                if cfg!(target_os = "linux") && !sockets.is_empty() {
                    return Err(format!(
                        "{prefix}.network.sockets.unix_sockets: the pinned Linux runner cannot enforce direct Unix-socket path allowlists"
                    ));
                }
                let sockets: SandboxSettings = sockets
                    .into_iter()
                    .map(|path| (path, "allow".into()))
                    .collect();
                policy.insert("network".into(), "restricted".into());
                policy.insert(
                    "proxy".into(),
                    json!({
                        "enabled": true, "mode": mode,
                        "enableSocks5": !matches!(socks, Socks::Disabled),
                        "enableSocks5Udp": matches!(socks, Socks::TcpUdp),
                        "allowUpstreamProxy": upstream, "domains": permissions,
                        "unixSockets": sockets, "dangerouslyAllowAllUnixSockets": all,
                        "allowLocalBinding": local,
                    }),
                );
            }
        }
        Ok(policy)
    }
}

/// Configuration inspection preserves permissions; launch capture guards writes.
#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum PolicyContext {
    Configuration,
    Launch,
}

// Console's private settings transport retains relative-grant provenance across
// worker generations. Consume this field before forwarding native policy.
const RELATIVE_WRITE_ROOT: &str = "_console_relative_write_root";

struct WritableRoot {
    path: String,
    guarded: bool,
}

/// Add filesystem baselines and capture paths without runtime discovery.
pub(crate) fn materialize_settings(
    mut settings: SandboxSettings,
    roots: Vec<PathBuf>,
    workspace: &Path,
    context: PolicyContext,
) -> Result<SandboxSettings, String> {
    capture_filesystem_paths(&mut settings, workspace, context)?;
    let writable_roots = roots
        .into_iter()
        .filter_map(|path| capture_writable_root(path, workspace, context).transpose())
        .collect::<Result<Vec<_>, _>>()?;
    settings.entry("filesystem").or_insert_with(|| json!({}));
    let mut restricted = false;
    if let Some(Value::Object(filesystem)) = settings.get_mut("filesystem") {
        restricted = crate::settings::native_variant_name(
            filesystem
                .entry("kind")
                .or_insert_with(|| "restricted".into()),
        ) == Some("restricted");
        if restricted || !writable_roots.is_empty() {
            filesystem.entry("entries").or_insert_with(|| json!([]));
        }
        if let Some(Value::Array(entries)) = filesystem.get_mut("entries") {
            if restricted {
                entries.insert(
                    0,
                    json!({
                        "path": {"type": "special", "value": {"kind": "root"}},
                        "access": "read",
                    }),
                );
            }
            entries.extend(writable_roots.into_iter().map(|root| {
                let mut entry = json!({
                    "path": {"type": "path", "path": root.path},
                    "access": "write",
                });
                if root.guarded {
                    entry[RELATIVE_WRITE_ROOT] = true.into();
                }
                entry
            }));
        }
    }
    settings
        .entry("network")
        .or_insert_with(|| "restricted".into());
    if cfg!(target_os = "macos") && restricted {
        settings
            .entry("macos_seatbelt_profile_extension")
            .or_insert_with(|| include_str!("../sandbox/policy_extensions.sbpl").into());
    }
    super::preserve_environment(
        &mut settings,
        [(super::SANDBOX_MARKER.as_ref(), Some("1".as_ref()))],
    )?;
    Ok(settings)
}

/// Capture relative paths, guarding implicit writes only when launching.
/// Each caller retains ownership of its filesystem baseline and cache grants.
pub(crate) fn capture_filesystem_paths(
    settings: &mut SandboxSettings,
    workspace: &Path,
    context: PolicyContext,
) -> Result<(), String> {
    if context == PolicyContext::Launch
        && settings.get("extends").and_then(Value::as_str) == Some(":workspace")
        && is_broad_write_root(workspace)?
    {
        settings.remove("extends");
        settings.remove("workspace_options");
        eprintln!(
            "Warning: refusing automatic workspace writes to '{}'; falling back to read_only",
            workspace.display()
        );
    }
    if let Some(Value::Array(entries)) = settings
        .get_mut("filesystem")
        .and_then(|filesystem| filesystem.get_mut("entries"))
    {
        let mut captured = Vec::with_capacity(entries.len());
        for mut entry in entries.drain(..) {
            let if_exists = context == PolicyContext::Launch
                && entry
                    .as_object_mut()
                    .expect("filesystem entry")
                    .remove(IF_EXISTS)
                    .is_some();
            let write = entry.get("access").and_then(Value::as_str) == Some("write");
            let mut guarded = false;
            if entry.pointer("/path/type").and_then(Value::as_str) == Some("path")
                && let Some(Value::String(path)) = entry.pointer_mut("/path/path")
            {
                if write {
                    let Some(root) =
                        capture_writable_root(PathBuf::from(&*path), workspace, context)?
                    else {
                        continue;
                    };
                    *path = root.path;
                    guarded = root.guarded;
                } else {
                    *path = resolve_writable_root(PathBuf::from(&*path), workspace)?;
                }
                if if_exists {
                    // Inspect the link itself, retaining native symlink validation.
                    // This is a policy snapshot, not synchronization with host edits
                    // between capture and the runner's mount setup.
                    match std::fs::symlink_metadata(&*path) {
                        Ok(_) => {}
                        Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                        Err(error) => {
                            return Err(format!(
                                "cannot inspect conditional filesystem path '{path}': {error}"
                            ));
                        }
                    }
                }
            }
            if guarded {
                entry[RELATIVE_WRITE_ROOT] = true.into();
            }
            captured.push(entry);
        }
        *entries = captured;
    }
    Ok(())
}

fn capture_writable_root(
    path: PathBuf,
    workspace: &Path,
    context: PolicyContext,
) -> Result<Option<WritableRoot>, String> {
    let path = crate::console_paths::expand_home(&path)?;
    let root = resolve_writable_root(path.clone(), workspace)?;
    let guarded = context == PolicyContext::Launch && !path.is_absolute();
    if guarded && is_broad_write_root(Path::new(&root))? {
        eprintln!(
            "Warning: refusing relative writable root '{}'; use an absolute path to grant writes to the home directory or filesystem root",
            path.display()
        );
        return Ok(None);
    }
    Ok(Some(WritableRoot {
        path: root,
        guarded,
    }))
}

/// Check relative grants again after the previous generation has retired. The
/// native runner receives ordinary entries without Console's private markers.
pub(crate) fn revalidate_writable_roots(settings: &mut SandboxSettings) -> Result<(), String> {
    if let Some(Value::Array(entries)) = settings
        .get_mut("filesystem")
        .and_then(|filesystem| filesystem.get_mut("entries"))
    {
        let mut checked = Vec::with_capacity(entries.len());
        for mut entry in entries.drain(..) {
            let guarded = entry
                .as_object_mut()
                .and_then(|entry| entry.remove(RELATIVE_WRITE_ROOT))
                == Some(Value::Bool(true));
            if guarded
                && let Some(path) = entry.pointer("/path/path").and_then(Value::as_str)
                && is_broad_write_root(Path::new(path))?
            {
                eprintln!(
                    "Warning: refusing changed writable root '{path}'; broad writes require an explicit absolute grant"
                );
                continue;
            }
            checked.push(entry);
        }
        *entries = checked;
    }
    Ok(())
}

/// A launch directory must not implicitly authorize the host or the entire home.
/// Inspect aliases without rewriting the paths passed to native validation.
fn is_broad_write_root(path: &Path) -> Result<bool, String> {
    let normalized = normalize_permission_path(path);
    if normalized.parent().is_none() {
        return Ok(true);
    }
    let root = inspect_path_identity(&normalized)
        .map_err(|error| format!("cannot inspect writable root '{}': {error}", path.display()))?;
    let Some(home) = crate::console_paths::home_directory()? else {
        // Unknown home identity cannot authorize implicit broad writes.
        return Ok(true);
    };
    let home = inspect_path_identity(&home).map_err(|error| {
        format!(
            "cannot resolve home directory '{}': {error}",
            home.display()
        )
    })?;
    Ok(home.starts_with(root))
}

fn normalize_permission_path(path: &Path) -> PathBuf {
    // Match the runner's lexical normalization, including absent/.. paths.
    let mut normalized = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                normalized.pop();
            }
            Component::Prefix(_) | Component::RootDir | Component::Normal(_) => {
                normalized.push(component.as_os_str());
            }
        }
    }
    normalized
}

/// Resolve the existing prefix while retaining absent suffixes. Comparing only
/// the existing ancestor would conflate unrelated missing sibling directories.
fn inspect_path_identity(path: &Path) -> std::io::Result<PathBuf> {
    for ancestor in path.ancestors() {
        match ancestor.canonicalize() {
            Ok(canonical) => {
                return Ok(normalize_permission_path(
                    &canonical.join(path.strip_prefix(ancestor).expect("path ancestor")),
                ));
            }
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::NotFound | std::io::ErrorKind::NotADirectory
                ) => {}
            Err(error) => return Err(error),
        }
    }
    Ok(normalize_permission_path(path))
}

/// Capture launch-relative paths without hiding symlinks from runner validation.
pub(crate) fn resolve_writable_root(
    path: PathBuf,
    workspace: &std::path::Path,
) -> Result<String, String> {
    let root = std::path::absolute(workspace.join(&path))
        .map_err(|error| format!("cannot resolve writable root '{}': {error}", path.display()))?;
    // The runner configuration carries paths as JSON strings.
    root.into_os_string()
        .into_string()
        .map_err(|_| format!("writable root '{}' is not valid UTF-8", path.display()))
}

fn validate_domain(host: &str) -> Result<(), &'static str> {
    let host = host.trim();
    let literal = host
        .strip_prefix('[')
        .and_then(|host| host.strip_suffix(']'))
        .unwrap_or(host);
    let ip = if let Some((address, zone)) = literal.split_once('%') {
        address.parse::<std::net::Ipv6Addr>().is_ok()
            && !zone.is_empty()
            && zone
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || b"-._~".contains(&byte))
    } else {
        literal.parse::<std::net::IpAddr>().is_ok()
    };
    if host.is_empty()
        || host.contains(['/', '@'])
        || ((host.contains([':', '%']) || host.starts_with('[')) && !ip)
    {
        return Err(
            "expected a hostname pattern or IP literal without a port; native rules do not restrict destination ports or URL paths",
        );
    }
    Ok(())
}

const DEFAULT_DOWNLOAD_HOSTS: &[&str] = &[
    "pypi.org",
    "files.pythonhosted.org",
    "astral.sh",
    "releases.astral.sh",
    "github.com",
    "api.github.com",
    "codeload.github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "r-lib.github.io",
    "packagemanager.posit.co",
    "rspm-sync.rstudio.com",
    "bioconductor.posit.co",
    "bioconductor.org",
    "cran.r-project.org",
    "cloud.r-project.org",
    "cran.rstudio.com",
    "extensions.duckdb.org",
];
