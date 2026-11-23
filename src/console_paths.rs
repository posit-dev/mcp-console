//! Console-owned files on the controller host.

use std::path::{Component, Path, PathBuf};

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
    #[cfg(windows)]
    if relative
        .components()
        .any(|part| part == Component::ParentDir)
    {
        return absolute_preserving_parents(&home, relative)
            .map_err(|error| format!("cannot expand '{}': {error}", path.display()));
    }
    Ok(if relative.as_os_str().is_empty() {
        home
    } else {
        home.join(relative)
    })
}

/// Capture an absolute path whose parent traversals can still be inspected at launch.
pub(crate) fn absolute_preserving_parents(base: &Path, path: &Path) -> std::io::Result<PathBuf> {
    #[cfg(windows)]
    if path.components().any(|part| part == Component::ParentDir) {
        // GetFullPathNameW folds `..`, as does PathBuf::push on a verbatim
        // base. Resolve only the drive or UNC root, then append the components.
        let mut parts = path.components().peekable();
        let mut root = base.to_owned();
        while let Some(part) =
            parts.next_if(|part| matches!(part, Component::Prefix(_) | Component::RootDir))
        {
            root.push(part);
        }
        if !root.is_absolute() {
            root = std::path::absolute(root)?;
        }
        for part in parts {
            root.push("");
            root.as_mut_os_string().push(part.as_os_str());
        }
        return Ok(root);
    }
    std::path::absolute(base.join(path))
}

/// Match the runner's lexical normalization, including absent/.. paths.
pub(crate) fn normalize_permission_path(path: &Path) -> PathBuf {
    let mut normalized = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                normalized.pop();
            }
            Component::Prefix(_) | Component::RootDir | Component::Normal(_) => {
                normalized.push(component.as_os_str());
            }
        }
    }
    normalized
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
