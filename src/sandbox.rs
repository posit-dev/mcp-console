use std::ffi::OsString;
use std::path::PathBuf;
use std::process::{Command, ExitCode, Stdio};

use serde_json::{Value, json};

#[cfg(any(target_os = "macos", target_os = "linux", windows))]
mod installation;
#[cfg(any(target_os = "macos", target_os = "linux", windows))]
mod runner;
#[cfg(not(any(target_os = "macos", target_os = "linux", windows)))]
mod unsupported;

const MARKER: &str = "MCP_CONSOLE_SANDBOX";

pub fn capture_settings(
    roots: Vec<PathBuf>,
    overrides: &crate::cli::ConfigOverrides,
) -> Result<crate::settings::SandboxSettings, String> {
    let workspace = std::env::current_dir()
        .map_err(|error| format!("cannot find launch workspace: {error}"))?;
    let crate::settings::Captured {
        source,
        policy: settings,
        ..
    } = crate::settings::discover(&workspace, overrides)?;
    capture_policy(source.as_deref(), settings, roots, &workspace)
}

fn capture_policy(
    source: Option<&str>,
    settings: crate::settings::SandboxSettings,
    roots: Vec<PathBuf>,
    workspace: &std::path::Path,
) -> Result<crate::settings::SandboxSettings, String> {
    let settings = materialize_settings(settings, roots, workspace)?;
    if let Some(source) = source {
        preflight(&settings).map_err(|error| format!("{source}: {error}"))?;
    }
    Ok(settings)
}

/// Apply execution-host additions to an explicitly selected policy. No discovery.
pub fn materialize_settings(
    mut settings: crate::settings::SandboxSettings,
    roots: Vec<PathBuf>,
    workspace: &std::path::Path,
) -> Result<crate::settings::SandboxSettings, String> {
    let writable_roots = roots
        .into_iter()
        .map(|path| resolve_writable_root(path, workspace))
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
            for entry in entries.iter_mut() {
                if entry.pointer("/path/type").and_then(Value::as_str) == Some("path")
                    && let Some(Value::String(path)) = entry.pointer_mut("/path/path")
                {
                    *path = resolve_writable_root(PathBuf::from(&*path), workspace)?;
                }
            }
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
                json!({
                    "path": {"type": "path", "path": root},
                    "access": "write",
                })
            }));
        }
    }
    settings
        .entry("network")
        .or_insert_with(|| "restricted".into());
    if cfg!(target_os = "macos") && restricted {
        settings
            .entry("macos_seatbelt_profile_extension")
            .or_insert_with(|| include_str!("sandbox/policy_extensions.sbpl").into());
    }
    crate::settings::preserve_environment(&mut settings, [(MARKER.as_ref(), Some("1".as_ref()))])?;
    Ok(settings)
}

/// Validate native policy and setup for the standalone sandbox command.
/// The child explicitly consumes the snapshot, so it cannot rediscover settings.
fn preflight(settings: &crate::settings::SandboxSettings) -> Result<(), String> {
    let executable = std::env::current_exe()
        .map_err(|error| format!("cannot locate sandbox launcher: {error}"))?;
    let payload = serde_json::to_string(settings)
        .map_err(|error| format!("cannot encode sandbox settings: {error}"))?;
    let mut command = Command::new(executable);
    command
        .args(["sandbox", "--exit-with-parent"])
        .arg(std::process::id().to_string())
        .args(["--settings-env", crate::settings::ENVIRONMENT, "--"])
        .env(crate::settings::ENVIRONMENT, payload)
        .stdin(Stdio::null());
    #[cfg(unix)]
    command.arg("/usr/bin/true");
    #[cfg(windows)]
    command
        .arg(
            std::path::PathBuf::from(
                std::env::var_os("SystemRoot").ok_or("SystemRoot is unavailable")?,
            )
            .join("System32")
            .join("cmd.exe"),
        )
        .args(["/d", "/c", "exit", "0"]);
    let output = command
        .output()
        .map_err(|error| format!("cannot start sandbox preflight: {error}"))?;
    if !output.status.success() {
        return Err(format!(
            "sandbox preflight failed ({}): {}",
            output.status,
            String::from_utf8_lossy(&output.stderr).trim_end()
        ));
    }
    Ok(())
}

/// Capture launch-relative paths without hiding symlinks from runner validation.
fn resolve_writable_root(path: PathBuf, workspace: &std::path::Path) -> Result<String, String> {
    let root = std::path::absolute(workspace.join(&path))
        .map_err(|error| format!("cannot resolve writable root '{}': {error}", path.display()))?;
    // The runner configuration carries paths as JSON strings.
    root.into_os_string()
        .into_string()
        .map_err(|_| format!("writable root '{}' is not valid UTF-8", path.display()))
}

pub fn run(
    command: &[OsString],
    exit_with_parent: Option<u32>,
    config_env: Option<&str>,
    settings_env: Option<&str>,
    writable_roots: Vec<PathBuf>,
    overrides: &crate::cli::ConfigOverrides,
) -> Result<ExitCode, String> {
    if config_env.is_some() || settings_env.is_some() {
        let selector = if overrides.no_config {
            Some("--no-config")
        } else if overrides.no_project_config {
            Some("--no-project-config")
        } else if overrides.no_global_config {
            Some("--no-global-config")
        } else if overrides.config_file.is_some() {
            Some("--config-file")
        } else {
            None
        };
        if let Some(flag) = selector {
            return Err(format!(
                "{flag} cannot be combined with --config-env or --settings-env"
            ));
        }
    }
    if !overrides.values.is_empty() && (config_env.is_some() || settings_env.is_some()) {
        return Err(
            "configuration overrides cannot be combined with --config-env or --settings-env".into(),
        );
    }
    let settings = if config_env.is_some() {
        crate::settings::SandboxSettings::default()
    } else if let Some(name) = settings_env {
        crate::settings::from_environment(name)?
    } else {
        capture_settings(writable_roots, overrides)?
    };
    #[cfg(any(target_os = "macos", target_os = "linux", windows))]
    {
        runner::run(
            command,
            exit_with_parent,
            config_env,
            settings_env,
            settings,
        )
    }
    #[cfg(not(any(target_os = "macos", target_os = "linux", windows)))]
    {
        let _ = (exit_with_parent, config_env, settings_env, settings);
        unsupported::run(command)
    }
}
