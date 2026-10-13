//! Apply launch requirements and hand off to the verified native runner.

use super::{MARKER, installation};
use crate::settings::SandboxSettings;
use serde_json::{Value, json};
use std::ffi::OsString;
#[cfg(unix)]
use std::os::unix::process::CommandExt as _;
use std::process::{Command, ExitCode};

const CONFIGURATION: &str = "MCP_CONSOLE_SANDBOX_CONFIG";

pub(super) fn run(
    command: &[OsString],
    parent: Option<u32>,
    config_env: Option<&str>,
    settings_env: Option<&str>,
    mut settings: SandboxSettings,
) -> Result<ExitCode, String> {
    // Complete native policies bypass application defaults, but not Console's
    // supported backend contract. Reject retired selectors, including null and
    // the runner's old alias, before either launch path can reach the runner.
    let mut complete = config_env
        .and_then(|name| std::env::var(name).ok())
        .and_then(|value| serde_json::from_str::<SandboxSettings>(&value).ok());
    let policy = complete.as_mut().unwrap_or(&mut settings);
    for field in [
        "windows_backend",
        "windows_sandbox_level",
        "windows_state_dir",
    ] {
        if policy.contains_key(field) {
            return Err(format!(
                "{field} has been removed; Windows sandbox execution requires MXC; see docs/WINDOWS.md"
            ));
        }
    }
    #[cfg(windows)]
    {
        // External policies can suppress native enforcement even with an
        // explicit backend. The sandbox command always requires MXC on Windows.
        if policy
            .get("filesystem")
            .and_then(|fs| fs.get("kind"))
            .and_then(crate::settings::native_variant_name)
            == Some("external-sandbox")
        {
            return Err(
                "Windows sandbox execution requires MXC; external-sandbox policies are unsupported"
                    .into(),
            );
        }
        policy.insert("windows_backend".into(), "mxc".into());
    }
    #[cfg(unix)]
    if let Some(pid) = parent
        && unsafe { libc::getppid() } as u32 != pid
    {
        return Err(format!(
            "sandbox owner {pid} is not the launcher's current parent"
        ));
    }
    // Unix exec preserves the caller. Windows waits while the native runner
    // watches both this frontend and the supplied generation owner.
    let mut runner = Command::new(installation::private_runner()?);
    #[cfg(windows)]
    if let (Some(name), Some(complete)) = (config_env, complete) {
        runner.env(name, Value::Object(complete).to_string());
    }
    if settings.get("inherit_environment") == Some(&Value::Bool(false)) {
        // The native Linux launcher serializes its host environment before
        // applying workload controls. Non-inherited bytes are not policy input;
        // retain the trusted tool environment without serializing those values.
        for (name, value) in std::env::vars_os() {
            if name.to_str().is_none() || value.to_str().is_none() {
                runner.env_remove(name);
            }
        }
    }
    if let Some(name) = settings_env {
        runner.env_remove(name);
    }
    if config_env != Some(crate::settings::ENVIRONMENT) {
        runner.env_remove(crate::settings::ENVIRONMENT);
    }
    if config_env.is_none() {
        // This is also the serve path: an ambient value never selects policy.
        let mut lifecycle = json!({"private_tmp": {"environment": ["TMPDIR"]}});
        #[cfg(windows)]
        {
            lifecycle["private_tmp"]["environment"] = json!(["TMPDIR", "TEMP", "TMP"]);
        }
        if let Some(parent) = parent {
            lifecycle["parent_pid"] = parent.into();
            lifecycle["sigterm"] = "retire".into();
        }
        settings.insert("lifecycle".into(), lifecycle);
        runner.env(CONFIGURATION, Value::Object(settings).to_string());
    } else if config_env != Some(CONFIGURATION) {
        runner.env_remove(CONFIGURATION);
    }
    if config_env != Some(MARKER) {
        runner.env(MARKER, "1");
    }
    runner
        .args(["--config-env", config_env.unwrap_or(CONFIGURATION), "--"])
        .args(command)
        .env_remove("DYLD_INSERT_LIBRARIES")
        .env_remove("LD_PRELOAD");
    #[cfg(windows)]
    {
        let status = runner
            .status()
            .map_err(|error| format!("failed to launch private sandbox runner: {error}"))?;
        // Preserve all 32 status bits, including Windows exception statuses.
        std::process::exit(status.code().unwrap_or(1));
    }
    #[cfg(unix)]
    let error = runner.exec();
    #[cfg(unix)]
    {
        let detail = if error.raw_os_error() == Some(libc::E2BIG) {
            "argument/environment size limit exceeded (E2BIG); reduce the launch environment or use the private runner descriptor transport".to_owned()
        } else {
            error.to_string()
        };
        Err(format!("failed to launch private sandbox runner: {detail}"))
    }
}
