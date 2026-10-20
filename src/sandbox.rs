use std::ffi::OsString;
use std::path::PathBuf;
use std::process::{Command, ExitCode, Stdio};

#[cfg(any(target_os = "macos", target_os = "linux", windows))]
mod installation;
#[cfg(any(target_os = "macos", target_os = "linux", windows))]
mod runner;
#[cfg(not(any(target_os = "macos", target_os = "linux", windows)))]
mod unsupported;

fn capture_settings(
    roots: Vec<PathBuf>,
    config_env: Option<&str>,
    overrides: &crate::cli::ConfigOverrides,
) -> Result<crate::settings::SandboxSettings, String> {
    let workspace = std::env::current_dir()
        .map_err(|error| format!("cannot find launch workspace: {error}"))?;
    let crate::settings::Captured {
        source,
        profile,
        policy: mut settings,
        ..
    } = if let Some(name) = config_env {
        crate::settings::from_configuration_environment(&workspace, name, overrides)?
    } else {
        crate::settings::discover(&workspace, overrides)?
    };
    if let Some(name) = config_env {
        // The selected document is launch input, including when its environment
        // mapping tries to reintroduce the transport variable into the workload.
        crate::settings::preserve_environment(&mut settings, [(std::ffi::OsStr::new(name), None)])?;
    }
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
    let settings = crate::settings::materialize_sandbox(
        settings,
        roots,
        workspace,
        crate::settings::PolicyContext::Launch,
    )?;
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
    runner_config_env: Option<&str>,
    settings_env: Option<&str>,
    writable_roots: Vec<PathBuf>,
    overrides: &crate::cli::ConfigOverrides,
) -> Result<ExitCode, String> {
    let private_selector = runner_config_env
        .map(|_| "--runner-config-env")
        .or(settings_env.map(|_| "--settings-env"));
    if let Some(selector) = private_selector.or(config_env.map(|_| "--config-env")) {
        let conflicting = if overrides.no_config {
            Some("--no-config")
        } else if overrides.no_project_config {
            Some("--no-project-config")
        } else if overrides.no_global_config {
            Some("--no-global-config")
        } else if overrides.config_file.is_some() {
            Some("--config-file")
        } else if overrides.values_file.is_some() {
            Some("--config-overrides-file")
        } else if private_selector.is_some() && !overrides.default_values.is_empty() {
            Some("--default-config")
        } else if private_selector.is_some() && !overrides.values.is_empty() {
            Some("configuration overrides")
        } else {
            None
        };
        if let Some(flag) = conflicting {
            return Err(format!("{flag} cannot be combined with {selector}"));
        }
    }
    let settings = if runner_config_env.is_some() {
        crate::settings::SandboxSettings::default()
    } else if let Some(name) = settings_env {
        crate::settings::from_environment(name)?
    } else {
        capture_settings(writable_roots, config_env, overrides)?
    };
    #[cfg(any(target_os = "macos", target_os = "linux", windows))]
    {
        runner::run(
            command,
            exit_with_parent,
            runner_config_env,
            config_env.or(settings_env),
            settings,
        )
    }
    #[cfg(not(any(target_os = "macos", target_os = "linux", windows)))]
    {
        let _ = (exit_with_parent, runner_config_env, settings_env, settings);
        unsupported::run(command)
    }
}
