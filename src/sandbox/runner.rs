//! Apply launch requirements and hand off to the verified native runner.

use super::installation;
use crate::settings::SANDBOX_MARKER as MARKER;
use crate::settings::SandboxSettings;
use serde_json::{Value, json};
use std::ffi::OsString;
#[cfg(unix)]
use std::os::unix::process::CommandExt as _;
use std::path::PathBuf;
use std::process::{Command, ExitCode};

const CONFIGURATION: &str = "MCP_CONSOLE_SANDBOX_CONFIG";

/// Select the directories the pinned runner binds for an inherited :tmpdir.
fn inherited_temporary_directories(
    settings: &SandboxSettings,
    runner: &Command,
) -> Result<Vec<PathBuf>, String> {
    let entries = settings
        .get("filesystem")
        .and_then(|fs| fs["entries"].as_array());
    if !entries.is_some_and(|entries| {
        entries.iter().any(|entry| {
            entry["access"] == "write"
                && entry.pointer("/path/value/kind").and_then(Value::as_str) == Some("tmpdir")
        })
    }) {
        return Ok(Vec::new());
    }

    let removed = |name: &str| {
        runner.get_envs().any(|(key, value)| {
            value.is_none()
                && if cfg!(windows) {
                    key.eq_ignore_ascii_case(name)
                } else {
                    key == name
                }
        })
    };
    #[cfg(unix)]
    {
        // Unix uses the launcher's TMPDIR, independently of the workload
        // environment. AbsolutePathBuf expands ~ only in Unicode paths.
        let Some(path) = std::env::var_os("TMPDIR").filter(|p| !p.is_empty() && !removed("TMPDIR"))
        else {
            return Ok(Vec::new());
        };
        let path = PathBuf::from(path);
        if path.to_str().is_some()
            && let Ok(suffix) = path.strip_prefix("~")
        {
            if removed("HOME") {
                return Err("cannot inspect a home-relative inherited temporary write grant after removing HOME from the native launch environment".into());
            }
            if let Some(home) = std::env::home_dir().filter(|home| !home.as_os_str().is_empty()) {
                return Ok(vec![home.join(suffix)]);
            }
        }
        Ok(vec![path])
    }
    #[cfg(windows)]
    {
        // MXC uses absolute target TEMP and TMP after managed storage replaces
        // both variables with its independently granted private directory.
        if settings
            .get("lifecycle")
            .and_then(|l| l.get("private_tmp"))
            .is_some()
        {
            return Ok(Vec::new());
        }
        Ok(["TEMP", "TMP"]
            .into_iter()
            .filter_map(|name| {
                let configured = settings
                    .get("environment")
                    .and_then(Value::as_object)
                    .is_some_and(|env| env.keys().any(|key| key.eq_ignore_ascii_case(name)));
                if removed(name) && !configured {
                    return None;
                }
                crate::settings::environment_path(settings, name).filter(|path| path.is_absolute())
            })
            .collect())
    }
}

pub(super) fn run(
    command: &[OsString],
    parent: Option<u32>,
    runner_config_env: Option<&str>,
    consumed_env: Option<&str>,
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
    if let Some(name) = consumed_env {
        runner.env_remove(name);
    }
    if runner_config_env != Some(crate::settings::ENVIRONMENT) {
        runner.env_remove(crate::settings::ENVIRONMENT);
    }
    if runner_config_env.is_none() {
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
                    let path = crate::settings::resolve_writable_root(default.clone(), &workspace)?;
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
                    entries.push(json!({"path": {"type": "path", "path": crate::settings::resolve_writable_root(path, &workspace)?}, "access": "write"}));
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
        let tmpdirs = inherited_temporary_directories(&settings, &runner)?;
        // The native runner binds these roots by name. Their directory entries
        // must stay stable from this check through native sandbox setup.
        crate::settings::revalidate_writable_roots(&mut settings, &tmpdirs)?;
        runner.env(CONFIGURATION, Value::Object(settings).to_string());
    } else if runner_config_env != Some(CONFIGURATION) {
        runner.env_remove(CONFIGURATION);
    }
    if runner_config_env != Some(MARKER) {
        runner.env(MARKER, "1");
    }
    runner
        .args([
            "--config-env",
            runner_config_env.unwrap_or(CONFIGURATION),
            "--",
        ])
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
