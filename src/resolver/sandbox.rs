//! Resolver launch preserves its environment independently of worker grants.

use crate::settings::SandboxSettings;
use serde_json::{Value, json};
use std::path::{Path, PathBuf};
use std::process::Command;

pub(crate) fn command(
    declared: SandboxSettings,
    parent: u32,
    captured_duckdb: Option<&Path>,
    retained_environment: Option<&super::cache::Environment>,
) -> Result<(Command, super::preparation::PreparationCaches), String> {
    // Validate the tool environment before provisioning any directories.
    // Filesystem permissions still come from the original path spellings.
    let mut settings = declared.clone();
    let environment = super::cache::resolve_environment(&mut settings, retained_environment)?;
    let directory = match captured_duckdb {
        Some(directory) => directory.to_owned(),
        None => {
            let directory = super::cache::duckdb_extension_directory(&settings)?.ok_or(
                "resolver.environment: preparation requires an absolute HOME or MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY; configure one when inherit_environment is false",
            )?;
            super::cache::selected_tool_directory(&directory)?
        }
    };
    let caches = super::preparation::PreparationCaches {
        environment,
        matplotlib: super::cache::matplotlib_directory(&settings)?,
    };
    materialize(&mut settings, &declared)?;
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
    if crate::warmup::trusted_preparation() {
        // Installation warmup populates the same caches, including platform
        // package-tool settings, without native filesystem/network enforcement.
        crate::settings::configure_environment(&mut command, &settings);
    } else {
        command
            .args(["sandbox", "--exit-with-parent"])
            .arg(parent.to_string())
            .args(["--settings-env", crate::settings::ENVIRONMENT, "--"])
            .arg(executable)
            .env(
                crate::settings::ENVIRONMENT,
                serde_json::to_string(&settings).map_err(|error| error.to_string())?,
            );
    }
    command.env(crate::local_runtime::DUCKDB_EXTENSION_DIRECTORY, directory);
    #[cfg(windows)]
    command
        .env("RENV_CONFIG_CACHE_SYMLINKS", "FALSE")
        .env("RENV_CONFIG_COPY_METHOD", "r");
    Ok((command, caches))
}

fn materialize(settings: &mut SandboxSettings, declared: &SandboxSettings) -> Result<(), String> {
    let workspace = std::env::current_dir().map_err(|error| error.to_string())?;
    let managed = declared
        .get(super::cache::MANAGED_DIRECTORY)
        .map(|path| PathBuf::from(path.as_str().expect("captured managed cache directory")));
    // Explicit entries replace the default permissions for both cache modes.
    let caches = if settings
        .get("filesystem")
        .is_some_and(|filesystem| filesystem.get("entries").is_some())
    {
        Vec::new()
    } else if let Some(directory) = &managed {
        vec![directory.clone()]
    } else {
        super::cache::writable_roots(declared)?
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
                let path = path.to_str().ok_or("resolver policy paths must be UTF-8")?;
                entries.push(json!({"path": {"type": "path", "path": path}, "access": "write"}));
            }
            filesystem.insert("entries".into(), entries.into());
        }
    }
    crate::settings::capture_filesystem_paths(
        settings,
        &workspace,
        crate::settings::PolicyContext::Launch,
    )?;
    if let Some(directory) = managed {
        // A managed cache may have been missing or symlinked at server startup.
        super::cache::prepare_directory(&directory)?;
    } else if create_caches {
        super::cache::validate_default_renv_cache(
            settings,
            super::cache::RenvLookup::Generated(declared),
        )?;
        // Linux cannot bind absent roots. Create only the surviving cache grants.
        for entry in settings["filesystem"]["entries"]
            .as_array()
            .expect("cache entries")
        {
            if entry["access"] == "write" {
                let path = entry["path"]["path"].as_str().expect("captured cache path");
                super::cache::prepare_directory(path.as_ref())?;
            }
        }
    }
    settings.entry("network").or_insert("restricted".into());
    if cfg!(target_os = "macos") {
        settings
            .entry("macos_seatbelt_profile_extension")
            .or_insert_with(|| include_str!("../sandbox/policy_extensions.sbpl").into());
    }
    Ok(())
}
