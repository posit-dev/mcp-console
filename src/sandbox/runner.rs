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
        // Resolve Windows 8.3 working-directory aliases through the directory
        // handle without requiring enumeration of its parents.
        let cwd = std::fs::canonicalize(".")
            .map_err(|error| format!("cannot resolve sandbox working directory: {error}"))?;
        runner.current_dir(cwd);
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
        let mut lifecycle = json!({});
        match crate::settings::Tempdir::from_settings(&settings) {
            crate::settings::Tempdir::Managed => {
                lifecycle["private_tmp"] = json!({"environment": if cfg!(windows) {
                    vec!["TMPDIR", "TEMP", "TMP"]
                } else {
                    vec!["TMPDIR"]
                }});
            }
            crate::settings::Tempdir::ReadWrite => {
                // Resolve from the workload environment, not the supervisor's
                // TMPDIR: trusted YAML can select another host temp directory.
                let workspace = std::env::current_dir().map_err(|error| error.to_string())?;
                let mut directories: std::collections::BTreeSet<_> = ["TMPDIR", "TEMP", "TMP"]
                    .into_iter()
                    .filter_map(|name| crate::settings::environment_path(&settings, name))
                    .collect();
                let default = if cfg!(unix) {
                    "/tmp".into()
                } else {
                    std::env::temp_dir()
                };
                if cfg!(windows) && directories.is_empty() {
                    // Windows runtimes cannot discover a supervisor-only temp
                    // selection after environment clearing. Export the same
                    // default we grant, without creating or owning storage.
                    let path = super::resolve_writable_root(default.clone(), &workspace)?;
                    let environment = settings
                        .entry("environment")
                        .or_insert_with(|| json!({}))
                        .as_object_mut()
                        .expect("captured environment");
                    for name in ["TMPDIR", "TEMP", "TMP"] {
                        environment.retain(|key, _| !key.eq_ignore_ascii_case(name));
                        environment.insert(name.into(), path.clone().into());
                    }
                }
                directories.insert(default);
                let entries = settings
                    .entry("filesystem")
                    .or_insert_with(|| json!({"kind": "restricted", "entries": []}))
                    .as_object_mut()
                    .expect("captured filesystem")
                    .entry("entries")
                    .or_insert_with(|| json!([]))
                    .as_array_mut()
                    .expect("captured filesystem entries");
                for path in directories {
                    // Capture grants at launch. Relative environment values
                    // retain their meaning, but later cwd changes do not move
                    // these permissions.
                    entries.push(json!({"path": {"type": "path", "path": super::resolve_writable_root(path, &workspace)?}, "access": "write"}));
                }
            }
            crate::settings::Tempdir::ReadOnly => {}
        }
        settings.retain(|key, _| key != "tempdir");
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
        use windows_sys::Win32::Foundation::INVALID_HANDLE_VALUE;
        use windows_sys::Win32::System::Console::{
            GetStdHandle, STD_ERROR_HANDLE, STD_INPUT_HANDLE, STD_OUTPUT_HANDLE,
        };

        // Command duplicates stdio for the runner and also inherits marked
        // originals. Descendants must not retain those extra frontend
        // handles after the root exits. Explicit stdio handoffs still duplicate.
        for selector in [STD_INPUT_HANDLE, STD_OUTPUT_HANDLE, STD_ERROR_HANDLE] {
            let handle = unsafe { GetStdHandle(selector) };
            if !handle.is_null() && handle != INVALID_HANDLE_VALUE {
                crate::windows::inherit(handle, false)
                    .map_err(|error| format!("cannot isolate sandbox frontend stdio: {error}"))?;
            }
        }
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
