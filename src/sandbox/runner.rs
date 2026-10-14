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
    {
        use std::os::windows::ffi::{OsStrExt, OsStringExt};
        use windows_sys::Win32::Storage::FileSystem::GetLongPathNameW;

        // The elevated helper compares cwd with USERPROFILE before granting
        // profile metadata access. Windows TEMP can use an 8.3 profile alias;
        // expand it before handoff so that comparison identifies the profile.
        let cwd = std::env::current_dir().map_err(|error| error.to_string())?;
        let cwd = cwd
            .as_os_str()
            .encode_wide()
            .chain(Some(0))
            .collect::<Vec<_>>();
        let length = unsafe { GetLongPathNameW(cwd.as_ptr(), std::ptr::null_mut(), 0) };
        if length == 0 {
            return Err(format!(
                "cannot expand sandbox working directory: {}",
                std::io::Error::last_os_error()
            ));
        }
        let mut expanded = vec![0; length as usize];
        let written = unsafe { GetLongPathNameW(cwd.as_ptr(), expanded.as_mut_ptr(), length) };
        if written == 0 {
            return Err(format!(
                "cannot expand sandbox working directory: {}",
                std::io::Error::last_os_error()
            ));
        }
        if written >= length {
            return Err("sandbox working directory changed while expanding its short path".into());
        }
        runner.current_dir(OsString::from_wide(&expanded[..written as usize]));
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
