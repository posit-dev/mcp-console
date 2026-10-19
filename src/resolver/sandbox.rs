//! Resolver policy is independent of worker grants and materialized on its host.

use crate::settings::SandboxSettings;
use serde_json::{Value, json};
use std::process::Command;

pub(crate) fn command(settings: SandboxSettings, parent: u32) -> Result<Command, String> {
    let directory = super::cache::duckdb_extension_directory(&settings)?.ok_or(
        "resolver.environment: preparation requires an absolute HOME or MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY; configure one when inherit_environment is false",
    )?;
    let mut settings = materialize(settings)?;
    #[cfg(windows)]
    // MXC denies renv's junctions and Robocopy's restartable-copy operations.
    // Use R's copier within the same cache grants, without privileged setup.
    crate::settings::preserve_environment(
        &mut settings,
        [
            (
                "RENV_CONFIG_CACHE_SYMLINKS".as_ref(),
                Some("FALSE".as_ref()),
            ),
            ("RENV_CONFIG_COPY_METHOD".as_ref(), Some("r".as_ref())),
        ],
    )?;
    crate::settings::preserve_environment(
        &mut settings,
        [(
            crate::local_runtime::DUCKDB_EXTENSION_DIRECTORY.as_ref(),
            Some(directory.as_os_str()),
        )],
    )?;
    let executable = std::env::current_exe().map_err(|error| error.to_string())?;
    let mut command = Command::new(&executable);
    command
        .args(["sandbox", "--exit-with-parent"])
        .arg(parent.to_string())
        .args(["--settings-env", crate::settings::ENVIRONMENT, "--"])
        .arg(executable)
        .env(
            crate::settings::ENVIRONMENT,
            serde_json::to_string(&settings).map_err(|error| error.to_string())?,
        )
        .env(crate::local_runtime::DUCKDB_EXTENSION_DIRECTORY, directory);
    #[cfg(windows)]
    command
        .env("RENV_CONFIG_CACHE_SYMLINKS", "FALSE")
        .env("RENV_CONFIG_COPY_METHOD", "r");
    Ok(command)
}

fn materialize(mut settings: SandboxSettings) -> Result<SandboxSettings, String> {
    let workspace = std::env::current_dir().map_err(|error| error.to_string())?;
    // Explicit entries already define the write grants, including Console caches.
    let caches = if settings
        .get("filesystem")
        .is_some_and(|filesystem| filesystem.get("entries").is_some())
    {
        Vec::new()
    } else {
        super::cache::writable_roots(&settings)?
    };
    let create_caches = !caches.is_empty();
    let filesystem = settings.entry("filesystem").or_insert_with(|| json!({}));
    if let Value::Object(filesystem) = filesystem {
        filesystem.entry("kind").or_insert("restricted".into());
        if !filesystem.contains_key("entries") {
            let mut entries = vec![json!({
                "path": {"type": "special", "value": {"kind": "root"}}, "access": "read"
            })];
            for path in caches {
                // Retain relative spelling until the shared guard has checked it.
                entries.push(super::cache::writable_cache_entry(&path)?);
            }
            filesystem.insert("entries".into(), entries.into());
        }
    }
    crate::settings::capture_filesystem_paths(
        &mut settings,
        &workspace,
        crate::settings::PolicyContext::Launch,
    )?;
    if create_caches {
        // Linux cannot bind absent roots. Create only the surviving cache grants.
        for entry in settings["filesystem"]["entries"]
            .as_array()
            .expect("cache entries")
        {
            if entry["access"] == "write" {
                let path = entry["path"]["path"].as_str().expect("captured cache path");
                std::fs::create_dir_all(path)
                    .map_err(|error| format!("cannot create resolver cache '{path}': {error}"))?;
            }
        }
    }
    settings.entry("network").or_insert("restricted".into());
    if cfg!(target_os = "macos") {
        settings
            .entry("macos_seatbelt_profile_extension")
            .or_insert_with(|| include_str!("../sandbox/policy_extensions.sbpl").into());
    }
    Ok(settings)
}
