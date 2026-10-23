//! Console-owned files on the controller host.

use std::path::{Path, PathBuf};

pub(crate) fn home_console_directory() -> Result<Option<PathBuf>, String> {
    if let Some(directory) = std::env::var_os("MCP_CONSOLE_HOME") {
        let directory = expand_home(Path::new(&directory))?;
        if !directory.is_absolute() {
            return Err("MCP_CONSOLE_HOME must be an absolute path".into());
        }
        return Ok(Some(directory));
    }
    Ok(home_directory()?.map(|home| home.join(".agents").join("console")))
}

/// Expand only a leading home component in public paths. Use the controller's
/// launch environment, independently of R and child environment overrides.
/// Keep symlinks and all other path components for their existing owners.
pub(crate) fn expand_home(path: &Path) -> Result<PathBuf, String> {
    let Ok(relative) = path.strip_prefix("~") else {
        return Ok(path.to_path_buf());
    };
    let home = home_directory()?.ok_or("cannot expand '~' without a user home directory")?;
    Ok(if relative.as_os_str().is_empty() {
        home
    } else {
        home.join(relative)
    })
}

pub(crate) fn home_directory() -> Result<Option<PathBuf>, String> {
    let (home, name) = match std::env::var_os("HOME").filter(|value| !value.is_empty()) {
        Some(home) => (PathBuf::from(home), "HOME"),
        None => {
            #[cfg(windows)]
            {
                // Rust's native discovery uses USERPROFILE, then the Windows
                // user-profile API. Explicit HOME always takes precedence.
                let Some(home) = std::env::home_dir() else {
                    return Ok(None);
                };
                (home, "Windows user home")
            }
            #[cfg(not(windows))]
            return Ok(None);
        }
    };
    if !home.is_absolute() {
        return Err(format!("{name} must be an absolute path"));
    }
    Ok(Some(home))
}
