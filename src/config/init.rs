//! Create an annotated starter layer without capturing discovered settings.

mod template;

use std::fs;
use std::io::{ErrorKind, Write};

use crate::cli::{ConfigInitOptions, ConfigOverrides};

pub(crate) fn run(options: &ConfigInitOptions, overrides: &ConfigOverrides) -> Result<(), String> {
    if overrides.no_config
        || overrides.no_global_config
        || overrides.no_project_config
        || overrides.config_file.is_some()
        || overrides.values_file.is_some()
        || !overrides.default_values.is_empty()
    {
        return Err("config init accepts -c/--config settings, not --default-config, --config-file, --no-config, --no-global-config, --no-project-config, or --config-overrides-file".into());
    }
    if let Some(directory) = &options.directory {
        // No runtimes or background threads have started. Relative selectors
        // must be validated in the project where the new file will be loaded.
        std::env::set_current_dir(directory).map_err(|error| {
            format!(
                "Cannot enter project directory {}: {error}",
                directory.display()
            )
        })?;
    }
    let directory = std::env::current_dir().map_err(|error| error.to_string())?;
    let path = if options.global {
        crate::console_paths::home_console_directory()?
            .ok_or("Cannot determine Console home for global configuration")?
            .join("config.yaml")
    } else {
        directory.join(".agents/console/config.yaml")
    };
    match fs::symlink_metadata(&path) {
        Ok(_) => {
            return Err(format!(
                "Configuration already exists at {}; no files changed",
                path.display()
            ));
        }
        Err(error) if error.kind() == ErrorKind::NotFound => {}
        Err(error) => return Err(format!("Cannot inspect {}: {error}", path.display())),
    }
    // Seed only the starter's permissions before normal ordered overrides.
    // Read no discovered files or materialized defaults: those remain live
    // configuration layers, rather than becoming frozen in the new file.
    let (captured, value) = crate::settings::read(
        &directory,
        &ConfigOverrides {
            no_config: true,
            values: std::iter::once("sandbox.profile=workspace".into())
                .chain(overrides.values.iter().chain(&options.values).cloned())
                .collect(),
            ..ConfigOverrides::default()
        },
    )?;
    let contents = template::render(value, captured.profile.as_ref())?;
    if !options.dry_run {
        let parent = path.parent().expect("configuration path is absolute");
        fs::create_dir_all(parent)
            .map_err(|error| format!("Cannot create {}: {error}", parent.display()))?;
        let mut file = tempfile::NamedTempFile::new_in(parent)
            .map_err(|error| format!("Cannot create configuration: {error}"))?;
        file.write_all(contents.as_bytes())
            .map_err(|error| format!("Cannot write configuration: {error}"))?;
        // Never replace a file created by another initializer after our check.
        file.persist_noclobber(&path)
            .map_err(|error| format!("Cannot create {}: {}", path.display(), error.error))?;
    }
    let action = if options.dry_run {
        "Would create"
    } else {
        "Created"
    };
    print!("{action} configuration: {}\n\n{contents}", path.display());
    if options.dry_run {
        println!("\nDry run; no files changed.");
    }
    Ok(())
}
