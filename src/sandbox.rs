use std::ffi::OsString;
use std::path::PathBuf;
use std::process::{Command, ExitCode, Stdio};

#[cfg(any(target_os = "macos", target_os = "linux", windows))]
mod installation;
#[cfg(any(target_os = "macos", target_os = "linux", windows))]
mod runner;
#[cfg(not(any(target_os = "macos", target_os = "linux", windows)))]
mod unsupported;

pub fn capture_settings(
    roots: Vec<PathBuf>,
    overrides: &crate::cli::ConfigOverrides,
) -> Result<crate::settings::SandboxSettings, String> {
    let workspace = std::env::current_dir()
        .map_err(|error| format!("cannot find launch workspace: {error}"))?;
    let crate::settings::Captured {
        source,
        profile,
        policy: mut settings,
        ..
    } = crate::settings::discover(&workspace, overrides)?;
    if let Some(profile) = profile {
        match profile.select(false) {
            crate::settings::Profile::ReadOnly => {}
            crate::settings::Profile::Workspace => {
                crate::settings::apply_workspace_profile(&mut settings)
            }
            crate::settings::Profile::InheritCodex => {
                unreachable!("standalone sandbox has no client metadata")
            }
        }
    }
    capture_policy(source.as_deref(), settings, roots, &workspace)
}

fn capture_policy(
    source: Option<&str>,
    settings: crate::settings::SandboxSettings,
    roots: Vec<PathBuf>,
    workspace: &std::path::Path,
) -> Result<crate::settings::SandboxSettings, String> {
    let settings = crate::settings::materialize_sandbox(settings, roots, workspace)?;
    if let Some(source) = source {
        preflight(&settings).map_err(|error| format!("{source}: {error}"))?;
    }
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
