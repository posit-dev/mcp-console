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

/// Preserve components for the launcher's symlink check, even under a verbatim Windows home.
pub(crate) fn expand_permission_home(path: &Path) -> Result<PathBuf, String> {
    #[cfg(windows)]
    if let Ok(relative) = path.strip_prefix("~")
        && relative
            .components()
            .any(|part| part == Component::ParentDir)
    {
        let home = expand_home(Path::new("~"))?;
        return absolute_preserving_parents(&home, relative)
            .map_err(|error| format!("cannot expand '{}': {error}", path.display()));
    }
    expand_home(path)
}

/// Capture an absolute path whose parent traversals can still be inspected at launch.
pub(crate) fn absolute_preserving_parents(base: &Path, path: &Path) -> std::io::Result<PathBuf> {
    #[cfg(windows)]
    validate_verbatim_path(path)?;
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

#[cfg(windows)]
pub(crate) fn validate_verbatim_path(path: &Path) -> std::io::Result<()> {
    // Rust treats '/' literally after this prefix. The runner strips the
    // prefix on drive and UNC paths and would see different components.
    let bytes = path.as_os_str().as_encoded_bytes();
    if bytes.starts_with(br"\\?\") && bytes.contains(&b'/') {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "verbatim Windows paths must use backslashes",
        ));
    }
    Ok(())
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

/// Resolve the existing prefix. Keep the rest as written, including a missing
/// directory followed by `..` that the operating system cannot traverse.
pub(crate) fn canonicalize_existing_prefix(path: &Path) -> std::io::Result<PathBuf> {
    use std::io::ErrorKind;
    for ancestor in path.ancestors() {
        match ancestor.canonicalize() {
            Ok(canonical) => {
                let suffix = path.strip_prefix(ancestor).expect("path ancestor");
                return Ok(if suffix.as_os_str().is_empty() {
                    canonical
                } else {
                    canonical.join(suffix)
                });
            }
            Err(error)
                if matches!(error.kind(), ErrorKind::NotFound | ErrorKind::NotADirectory) => {}
            Err(error) => return Err(error),
        }
    }
    Ok(path.to_owned())
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
