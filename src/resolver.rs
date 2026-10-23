#[derive(Clone, Copy, Debug, Eq, PartialEq, serde::Deserialize, serde::Serialize)]
pub(crate) enum ResolverControlOutcome {
    Interrupted,
    Cancelled,
}

/// Retained terminal evidence from one preparation operation, distinct from
/// control acknowledgment and from its connection's retirement receipt.
#[derive(Clone)]
pub(crate) struct ResolverTerminalReport {
    pub(crate) result: Result<(), String>,
    pub(crate) control: Option<ResolverControlOutcome>,
    pub(crate) confirmed: bool,
}

mod environment;
pub(crate) use environment::{ManagedPython, ManagedR};
pub(crate) mod cache;
pub(crate) mod execution;
pub(crate) mod preparation;
#[cfg(any(unix, windows))]
pub(crate) mod sandbox;

pub(crate) fn run() -> Result<(), String> {
    preparation::run_local()
}

#[cfg(any(unix, windows))]
mod defaults;
#[cfg(any(unix, windows))]
mod managed_duckdb;
#[cfg(any(unix, windows))]
mod managed_duckdb_python;
#[cfg(any(unix, windows))]
mod managed_python;
#[cfg(any(unix, windows))]
mod managed_r;
#[cfg(any(unix, windows))]
pub(crate) mod process;
#[cfg(any(unix, windows))]
mod python_configuration;
#[cfg(any(unix, windows))]
mod python_version;
#[cfg(any(unix, windows))]
mod r_program;
#[cfg(windows)]
pub(crate) use r_program::run_rscript;
pub(crate) mod result_file;
#[cfg(not(any(unix, windows)))]
mod unsupported;

pub(crate) fn find_path_entry(program: &str) -> Option<std::path::PathBuf> {
    #[cfg(windows)]
    let native_program = if std::path::Path::new(program).extension().is_none() {
        format!("{program}.exe")
    } else {
        program.to_string()
    };
    #[cfg(windows)]
    let program = native_program.as_str();
    find_path_entry_from_names(&[program])
}

pub(crate) fn find_r_path_entry() -> Option<std::path::PathBuf> {
    #[cfg(windows)]
    return find_path_entry_from_names(&["R.exe", "R.bat", "R.cmd"]);
    #[cfg(not(windows))]
    find_path_entry("R")
}

fn find_path_entry_from_names(programs: &[&str]) -> Option<std::path::PathBuf> {
    let path = std::env::var_os("PATH")?;
    // A broken symlink or non-executable entry is a broken installation, not
    // permission to select a different resolver.
    std::env::split_paths(&path).find_map(|directory| {
        programs.iter().find_map(|program| {
            let candidate = if directory.as_os_str().is_empty() {
                std::path::PathBuf::from(".").join(program)
            } else {
                directory.join(program)
            };
            std::fs::symlink_metadata(&candidate)
                .is_ok()
                .then_some(candidate)
        })
    })
}

#[cfg(any(unix, windows))]
pub(crate) use python_configuration::ManagedPythonResolverConfiguration;

#[cfg(any(unix, windows))]
pub(crate) use managed_duckdb::resolve_duckdb_extensions;
#[cfg(any(unix, windows))]
pub(crate) use managed_duckdb_python::python_duckdb_available;
#[cfg(any(unix, windows))]
pub(crate) use managed_duckdb_python::resolve_python_duckdb_extensions;
#[cfg(all(test, unix))]
use managed_python::resolve_python_manifest;
#[cfg(any(unix, windows))]
pub(crate) use managed_python::{resolve_python_manifest_for_host, resolve_python_version};
#[cfg(any(unix, windows))]
pub(crate) use managed_r::{
    ManagedRBootstrap, ManagedRResolverConfiguration, discover, resolve_r, resolve_r_with,
};
#[cfg(any(unix, windows))]
pub(crate) use process::{ResolverControl, ResolverPhase, ResolverStopHandle};
#[cfg(not(any(unix, windows)))]
pub(crate) use unsupported::{
    ManagedPython, ManagedR, ManagedRBootstrap, ManagedRResolverConfiguration, ResolverPhase,
    ResolverStopHandle, resolve_duckdb_extensions, resolve_python, resolve_python_manifest,
    resolve_python_version, resolve_r, resolve_r_with,
};

#[cfg(all(test, unix))]
mod tests {
    use std::fs;
    use std::os::unix::fs::PermissionsExt;
    use std::path::Path;
    use std::process::Command;

    use super::{
        ManagedPythonResolverConfiguration, resolve_python_manifest, resolve_python_version,
    };
    use crate::worker_protocol::PythonRequirementManifest;

    #[test]
    fn local_python_resolution_without_r() {
        let Some(mode) = std::env::var_os("MCP_CONSOLE_TEST_LOCAL_PYTHON_MODE") else {
            let directory = std::env::temp_dir().join(format!(
                "mcp-console-local-python-{}-{}",
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap()
                    .as_nanos()
            ));
            fs::create_dir(&directory).unwrap();
            let uv = directory.join("uv");
            fs::write(
                &uv,
                r#"#!/bin/sh
printf '%s\n' "$0 $*" >> "$MCP_CONSOLE_TEST_LOCAL_PYTHON_RECORD"
case "$1 $2" in
  'python list') printf '%s\n' '[{"version":"3.12.7","version_parts":{"major":3,"minor":12,"patch":7},"symlink":null,"variant":"default","implementation":"cpython"}]' ;;
  'tool run') for last in "$@"; do :; done; printf '/usr/bin/true' > "$last" ;;
  *) exit 90 ;;
esac
"#,
            )
            .unwrap();
            fs::set_permissions(&uv, fs::Permissions::from_mode(0o755)).unwrap();
            let explicit_uv = directory.join("explicit-uv");
            fs::copy(&uv, &explicit_uv).unwrap();
            let empty_path = directory.join("empty");
            fs::create_dir(&empty_path).unwrap();
            let record = directory.join("uv.log");
            for mode in ["path", "explicit", "missing", "invalid", "managed"] {
                fs::write(&record, "").unwrap();
                let mut command = Command::new(std::env::current_exe().unwrap());
                command
                    .args([
                        "--exact",
                        "resolver::tests::local_python_resolution_without_r",
                    ])
                    .env("MCP_CONSOLE_TEST_LOCAL_PYTHON_MODE", mode)
                    .env("MCP_CONSOLE_TEST_LOCAL_PYTHON_RECORD", &record)
                    .env(
                        "PATH",
                        if mode == "missing" {
                            &empty_path
                        } else {
                            &directory
                        },
                    )
                    .env_remove("R_HOME")
                    .env_remove("R_LIBS")
                    .env_remove("RETICULATE_UV");
                if mode == "explicit" {
                    command.env("RETICULATE_UV", &explicit_uv);
                } else if mode == "invalid" {
                    command.env("RETICULATE_UV", directory.join("missing-uv"));
                } else if mode == "managed" {
                    command.env("RETICULATE_UV", "managed");
                }
                let output = command.output().unwrap();
                assert!(
                    output.status.success(),
                    "{mode}: {}{}",
                    String::from_utf8_lossy(&output.stdout),
                    String::from_utf8_lossy(&output.stderr)
                );
                let invocations = fs::read_to_string(&record).unwrap();
                assert_eq!(
                    invocations.is_empty(),
                    matches!(mode, "missing" | "invalid" | "managed")
                );
                if mode == "explicit" {
                    assert!(invocations.starts_with(explicit_uv.to_str().unwrap()));
                } else if mode == "path" {
                    assert!(invocations.starts_with(uv.to_str().unwrap()));
                }
                if matches!(mode, "path" | "explicit") {
                    assert!(invocations.contains("python list --all-versions"));
                    assert!(invocations.contains("tool run --isolated --python-preference only-managed --python 3.12.7 --exclude-newer 2026-01-01 --with six>=1"));
                }
            }
            fs::remove_dir_all(directory).unwrap();
            return;
        };

        assert!(std::env::var_os("R_LIBS").is_none());
        for executable in ["R", "Rscript", "ir"] {
            assert!(super::find_path_entry(executable).is_none());
        }
        let configuration = ManagedPythonResolverConfiguration::capture();
        if matches!(mode.to_str(), Some("missing" | "managed")) {
            assert!(!configuration.has_uv());
            assert!(resolve_python_version(vec![], &configuration, |_| Ok(())).is_err());
            return;
        }
        if mode == "invalid" {
            let error = resolve_python_version(vec![], &configuration, |_| Ok(())).unwrap_err();
            assert!(error.contains("missing-uv"), "{error}");
            return;
        }
        assert!(configuration.has_uv());
        let version =
            resolve_python_version(vec![">=3.12".into()], &configuration, |_| Ok(())).unwrap();
        assert_eq!(version, "3.12.7");
        let manifest = PythonRequirementManifest {
            packages: vec!["six>=1".into()],
            python_version: vec![">=3.12".into()],
            exclude_newer: Some("2026-01-01".into()),
        };
        let resolved = resolve_python_manifest(manifest, &configuration, |_| Ok(())).unwrap();
        assert_eq!(resolved.python(), Path::new("/usr/bin/true"));
        assert_eq!(resolved.requirements().packages, ["six>=1"]);
    }
}
