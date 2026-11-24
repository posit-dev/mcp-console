//! Cache selection shared by preparation, worker launch, and sandbox grants.

use crate::settings::{Cache, SandboxSettings, environment_path};
use serde_json::{Value, json};
use std::path::{Path, PathBuf};

mod directory;

/// Private resolver setting consumed before passing permissions to the native runner.
pub(super) const MANAGED_DIRECTORY: &str = "_console_managed_cache_directory";
const CACHE_ENVIRONMENT_PATHS: &str = "_console_cache_environment_paths";
const HOST_RENV_CACHE_LIST: &str = "_console_host_renv_cache_list";
const RENV_CACHE: &str = "RENV_PATHS_CACHE";

const CACHE_ENVIRONMENT: &[(&str, &str)] = &[
    ("XDG_CACHE_HOME", ""),
    ("XDG_DATA_HOME", "data"),
    ("UV_CACHE_DIR", "uv/cache"),
    ("UV_PYTHON_INSTALL_DIR", "uv/python"),
    ("UV_PYTHON_BIN_DIR", "uv/bin"),
    ("UV_TOOL_DIR", "uv/tools"),
    ("UV_TOOL_BIN_DIR", "uv/bin"),
    ("IR_CACHE_DIR", "ir"),
    ("IR_LIBRARY_ROOT", "ir/libraries"),
    ("R_USER_CACHE_DIR", ""),
    ("R_USER_DATA_DIR", "data"),
    ("RENV_PATHS_ROOT", "renv"),
    (RENV_CACHE, "renv/cache"),
    ("RENV_PATHS_SOURCE", "renv/source"),
    ("RENV_PATHS_BINARY", "renv/binary"),
    ("PKG_CACHE_DIR", "R/pkgcache"),
    ("R_PKG_CACHE_DIR", ""),
    (
        crate::local_runtime::DUCKDB_EXTENSION_DIRECTORY,
        "duckdb/extensions",
    ),
    ("MPLCONFIGDIR", "matplotlib"),
    ("PYTHONPYCACHEPREFIX", "python/bytecode"),
    ("PYTHONUSERBASE", "python/user"),
];

pub(crate) fn configure(
    selection: Option<Cache>,
    no_sandbox: bool,
    python: Option<&crate::settings::PythonChoice>,
    resolver: &mut SandboxSettings,
    worker: &mut SandboxSettings,
) -> Result<(), String> {
    let selection = selection.unwrap_or(if no_sandbox {
        Cache::Host
    } else {
        Cache::Managed
    });
    // Console cache contents may have been produced by sandboxed code. Never
    // reuse them in a local session that removes native enforcement.
    // Host-side companion staging uses a separate sandbox cache. Grant only
    // dependency storage, never the shared Console cache parent.
    let mut grant = match selection {
        Cache::Host => {
            // Only adjust a worker environment value when it uses the same
            // host cache selected for preparation.
            if !no_sandbox {
                let r_cache = environment_path(resolver, "R_USER_CACHE_DIR")
                    .or_else(|| environment_path(resolver, "XDG_CACHE_HOME"));
                if let Some(path) =
                    r_cache.filter(|path| path.is_relative() && has_parent_traversal(path))
                {
                    // R uses relative XDG bases verbatim; uv ignores them. Give
                    // R its own variable so each tool keeps its cache selection.
                    if environment_path(resolver, "R_USER_CACHE_DIR").is_none() {
                        set_cache_variable(resolver, "R_USER_CACHE_DIR", &path)?;
                    }
                    // Managed workers redirect XDG_CACHE_HOME to private scratch.
                    if crate::settings::Tempdir::from_settings(worker)
                        != crate::settings::Tempdir::Managed
                        && environment_path(worker, "R_USER_CACHE_DIR").is_none()
                        && environment_path(worker, "XDG_CACHE_HOME").as_ref() == Some(&path)
                    {
                        set_cache_variable(worker, "R_USER_CACHE_DIR", &path)?;
                    }
                }
                // Host tools derive their platform cache defaults from HOME.
                for &(name, _) in CACHE_ENVIRONMENT.iter().chain(&[("HOME", "")]) {
                    if let Some(path) = environment_path(resolver, name) {
                        // uv ignores relative XDG values. Host defaults require an absolute HOME.
                        if path.is_relative()
                            && matches!(name, "XDG_CACHE_HOME" | "XDG_DATA_HOME" | "HOME")
                        {
                            continue;
                        }
                        retain_host_path(resolver, name, &path)?;
                        if environment_path(worker, name).as_ref() == Some(&path) {
                            retain_host_path(worker, name, &path)?;
                        }
                    }
                }
            }
            return Ok(());
        }
        Cache::Managed | Cache::Directory(_) if no_sandbox => {
            return Err(
                "Console caches require sandboxing; use cache: host with --no-sandbox".into(),
            );
        }
        Cache::Managed => console_root(resolver)?,
        Cache::Directory(path) => path,
    };
    // Joining a name normally folds existing '..' in verbatim Windows paths.
    grant.push("");
    grant.as_mut_os_string().push("dependencies");
    prepare_directory(&grant)?;
    let explicit_python = python.map_or_else(
        || {
            std::env::var_os("RETICULATE_PYTHON")
                .is_some_and(|python| !python.is_empty() && python != "managed")
        },
        |python| python.executable.is_some(),
    );
    // Explicit Python keeps its preinstalled user-site packages.
    let variables: Vec<_> = CACHE_ENVIRONMENT
        .iter()
        .copied()
        .filter(|(name, _)| *name != "PYTHONUSERBASE" || !explicit_python)
        .collect();
    for settings in [&mut *resolver, &mut *worker] {
        for &(name, relative) in &variables {
            set_cache_variable(settings, name, &grant.join(relative))?;
        }
        retain_environment_path(settings, &grant, &variables)?;
    }
    resolver.insert(MANAGED_DIRECTORY.into(), json!(grant));
    Ok(())
}

#[derive(serde::Deserialize)]
struct EnvironmentPath {
    directory: PathBuf,
    variables: Vec<(String, String)>,
}

fn has_parent_traversal(path: &Path) -> bool {
    path.components()
        .any(|part| part == std::path::Component::ParentDir)
}

// renv accepts semicolons everywhere and also colons on Unix.
fn renv_separator(c: char) -> bool {
    c == ';' || (cfg!(unix) && c == ':')
}

fn retain_host_path(settings: &mut SandboxSettings, name: &str, path: &Path) -> Result<(), String> {
    if name == RENV_CACHE
        && let Some(list) = path.to_str()
    {
        // ir runs in a private working directory. Even a single relative
        // cache must be anchored to the directory used to capture its grant.
        settings.insert(HOST_RENV_CACHE_LIST.into(), list.into());
        return Ok(());
    }
    if has_parent_traversal(path) {
        retain_environment_path(settings, path, &[(name, "")])?;
    }
    Ok(())
}

/// Keep the shared root of managed cache variables so a link in a tool's own
/// cache subtree cannot change how its environment resolves a parent traversal.
fn retain_environment_path(
    settings: &mut SandboxSettings,
    path: &Path,
    variables: &[(&str, &str)],
) -> Result<(), String> {
    let path = path.to_str().ok_or("Console cache paths must be UTF-8")?;
    settings
        .entry(CACHE_ENVIRONMENT_PATHS)
        .or_insert_with(|| json!([]))
        .as_array_mut()
        .expect("captured cache environment paths")
        .push(json!({"directory": path, "variables": variables}));
    Ok(())
}

fn set_cache_variable(
    settings: &mut SandboxSettings,
    name: &str,
    path: &Path,
) -> Result<(), String> {
    let value = path.to_str().ok_or("Console cache paths must be UTF-8")?;
    if let Some(environment) = settings
        .entry("environment")
        .or_insert_with(|| json!({}))
        .as_object_mut()
    {
        #[cfg(windows)]
        let existing = environment
            .keys()
            .find(|key| key.eq_ignore_ascii_case(name))
            .cloned();
        #[cfg(windows)]
        let name = existing.as_deref().unwrap_or(name);
        // Preserve malformed values for validation by the native runner.
        if environment.get(name).is_none_or(Value::is_string) {
            environment.insert(name.to_owned(), value.into());
        }
    }
    Ok(())
}

/// The retained cache spelling also supplies filesystem grants. Only rewrite
/// the environment for this launch, after its filesystem policy is captured.
pub(crate) fn resolve_environment(
    settings: &mut SandboxSettings,
    managed_directory: Option<&Path>,
) -> Result<(), String> {
    if let Some(Value::String(list)) = settings.remove(HOST_RENV_CACHE_LIST)
        && environment_path(settings, RENV_CACHE).as_deref() == Some(Path::new(&list))
    {
        let mut resolved = String::new();
        for entry in list.split_inclusive(renv_separator) {
            let path = entry.trim_end_matches(renv_separator);
            if path.is_empty() {
                resolved.push_str(entry);
                continue;
            }
            let directory = tool_directory(Path::new(path))?;
            resolved.push_str(
                directory
                    .to_str()
                    .ok_or("Console cache paths must be UTF-8")?,
            );
            resolved.push_str(&entry[path.len()..]);
        }
        set_cache_variable(settings, RENV_CACHE, Path::new(&resolved))?;
    }
    let Some(paths) = settings.remove(CACHE_ENVIRONMENT_PATHS) else {
        return Ok(());
    };
    for path in
        serde_json::from_value::<Vec<EnvironmentPath>>(paths).map_err(|error| error.to_string())?
    {
        let root = match managed_directory {
            Some(directory) => directory.to_owned(),
            None => tool_directory(&path.directory)?,
        };
        for (name, relative) in path.variables {
            // A runtime selection may explicitly replace or remove one of the
            // environment variables before launch. Preserve that selection.
            let value = |base: &Path| {
                if relative.is_empty() {
                    base.to_owned()
                } else {
                    base.join(&relative)
                }
            };
            if root != path.directory
                && environment_path(settings, &name) == Some(value(&path.directory))
            {
                set_cache_variable(settings, &name, &value(&root))?;
            }
        }
    }
    Ok(())
}

/// Share the resolver's selected directory with its workers. Keep the original
/// declaration in settings for independent provisioning and permission checks.
pub(super) fn select_managed_directory(
    settings: &mut SandboxSettings,
) -> Result<Option<PathBuf>, String> {
    let Some(path) = settings.get(MANAGED_DIRECTORY) else {
        return Ok(None);
    };
    let directory = tool_directory(Path::new(
        path.as_str().expect("captured managed cache directory"),
    ))?;
    let directory = retained_cache_target(&directory)?;
    resolve_environment(settings, Some(&directory))?;
    Ok(Some(directory))
}

fn retained_cache_target(directory: &Path) -> Result<PathBuf, String> {
    if crate::settings::has_untrusted_symlink(directory)? {
        return crate::console_paths::canonicalize_existing_prefix(directory)
            .map_err(|error| format!("cannot resolve cache '{}': {error}", directory.display()));
    }
    Ok(directory.to_owned())
}

pub(crate) fn tool_directory(path: &Path) -> Result<PathBuf, String> {
    if path.is_absolute() && !has_parent_traversal(path) {
        return Ok(path.to_owned());
    }
    // Use the same launch home as the declared filesystem grant.
    let expanded = crate::console_paths::expand_permission_home(path)?;
    let absolute = crate::console_paths::absolute_preserving_parents(Path::new("."), &expanded)
        .map_err(|error| {
            format!(
                "cannot resolve cache directory '{}': {error}",
                path.display()
            )
        })?;
    #[cfg(unix)]
    if let Some(prefix) = absolute
        .ancestors()
        .find(|prefix| prefix.components().next_back() == Some(std::path::Component::ParentDir))
        && crate::settings::has_untrusted_symlink(prefix)?
    {
        // Unix follows a symlink before evaluating a subsequent '..'.
        return Ok(absolute);
    }
    Ok(crate::console_paths::normalize_permission_path(&absolute))
}

/// Provision only the selected directory and keep its declaration for revalidation.
pub(super) fn prepare_directory(grant: &Path) -> Result<(), String> {
    grant.to_str().ok_or("Console cache paths must be UTF-8")?;
    if crate::settings::has_untrusted_symlink(grant)? {
        return Ok(());
    }
    let directory = crate::console_paths::normalize_permission_path(grant);
    directory::create(&directory).map_err(|error| {
        format!(
            "cannot create resolver cache '{}': {error}",
            directory.display()
        )
    })
}

fn console_root(settings: &SandboxSettings) -> Result<PathBuf, String> {
    if let Some(cache) = environment_path(settings, "MCP_CONSOLE_CACHE_DIR") {
        if !cache.is_absolute() {
            return Err("MCP_CONSOLE_CACHE_DIR must be an absolute path".into());
        }
        return Ok(cache);
    }
    if let Some(cache) =
        environment_path(settings, "XDG_CACHE_HOME").filter(|path| path.is_absolute())
    {
        return Ok(cache.join("mcp-console"));
    }
    #[cfg(windows)]
    if let Some(cache) =
        environment_path(settings, "LOCALAPPDATA").filter(|path| path.is_absolute())
    {
        return Ok(cache.join("mcp-console/cache"));
    }
    let home = environment_path(settings, "HOME").filter(|path| path.is_absolute());
    #[cfg(windows)]
    let home = home
        .or_else(|| environment_path(settings, "USERPROFILE").filter(|path| path.is_absolute()));
    let home = home.ok_or("Console cache selection requires an absolute HOME")?;
    Ok(home.join(if cfg!(windows) {
        "AppData/Local/mcp-console/cache"
    } else if cfg!(target_os = "macos") {
        "Library/Caches/mcp-console"
    } else {
        ".cache/mcp-console"
    }))
}

pub(crate) fn duckdb_extension_directory(
    settings: &SandboxSettings,
) -> Result<Option<PathBuf>, String> {
    if let Some(path) = environment_path(settings, crate::local_runtime::DUCKDB_EXTENSION_DIRECTORY)
    {
        return std::path::absolute(path)
            .map(Some)
            .map_err(|error| error.to_string());
    }
    let home = environment_path(settings, "HOME").filter(|path| path.is_absolute());
    #[cfg(windows)]
    let home = home
        .or_else(|| environment_path(settings, "USERPROFILE").filter(|path| path.is_absolute()));
    Ok(home.map(|home| home.join(".duckdb/extensions")))
}

pub(crate) fn matplotlib_cache_override(settings: &SandboxSettings) -> Option<PathBuf> {
    let path = environment_path(settings, "MPLCONFIGDIR");
    #[cfg(target_os = "linux")]
    let path = path.or_else(|| {
        environment_path(settings, "XDG_CACHE_HOME").map(|base| base.join("matplotlib"))
    });
    path
}

fn default_matplotlib_cache(home: &Path) -> PathBuf {
    home.join(if cfg!(target_os = "linux") {
        ".cache/matplotlib"
    } else {
        ".matplotlib"
    })
}

pub(super) fn matplotlib_directory(settings: &SandboxSettings) -> Result<Option<PathBuf>, String> {
    matplotlib_cache_override(settings)
        .or_else(|| {
            environment_path(settings, "HOME")
                .as_deref()
                .map(default_matplotlib_cache)
        })
        .as_deref()
        .map(|path| {
            let directory = tool_directory(path)?;
            // Matplotlib can use temporary storage when its font cache is inaccessible.
            Ok(retained_cache_target(&directory).unwrap_or(directory))
        })
        .transpose()
}

#[cfg(any(unix, windows))]
pub(crate) fn writable_roots(settings: &SandboxSettings) -> Result<Vec<PathBuf>, String> {
    // Cache selection uses the resolver's effective environment, including its
    // own trusted YAML overrides. Worker environment settings do not reach here.
    // Config-file cache paths need explicit resolver grants; this covers only
    // default locations and direct environment overrides.
    let env = |name| environment_path(settings, name);
    let home = env("HOME");
    #[cfg(windows)]
    let home = home
        .filter(|path| path.is_absolute())
        .or_else(|| env("USERPROFILE"));
    let home = home.ok_or("resolver sandbox requires HOME")?;
    if !home.is_absolute() {
        return Err("resolver sandbox requires an absolute HOME".into());
    }
    let xdg_cache = env("XDG_CACHE_HOME").unwrap_or_else(|| home.join(".cache"));
    #[cfg(windows)]
    let local = env("LOCALAPPDATA")
        .filter(|path| path.is_absolute())
        .unwrap_or_else(|| home.join("AppData/Local"));
    #[cfg(windows)]
    let roaming = env("APPDATA")
        .filter(|path| path.is_absolute())
        .unwrap_or_else(|| home.join("AppData/Roaming"));
    #[cfg(unix)]
    let uv_cache_base = if xdg_cache.is_absolute() {
        xdg_cache.clone()
    } else {
        home.join(".cache")
    };
    #[cfg(windows)]
    let uv_cache_base = local.join("uv");
    #[cfg(unix)]
    let xdg_data = env("XDG_DATA_HOME")
        .filter(|path| path.is_absolute())
        .unwrap_or_else(|| home.join(".local/share"));
    #[cfg(windows)]
    let xdg_data = roaming;
    let r_cache = env("R_USER_CACHE_DIR")
        .or_else(|| env("XDG_CACHE_HOME"))
        .unwrap_or_else(|| {
            if cfg!(target_os = "macos") {
                home.join("Library/Caches/org.R-project.R")
            } else {
                #[cfg(windows)]
                {
                    local.join("R/cache")
                }
                #[cfg(unix)]
                {
                    xdg_cache.clone()
                }
            }
        });
    let mut caches = vec![
        env("UV_CACHE_DIR")
            .unwrap_or_else(|| uv_cache_base.join(if cfg!(windows) { "cache" } else { "uv" })),
        env("UV_PYTHON_INSTALL_DIR").unwrap_or_else(|| xdg_data.join("uv/python")),
        env("UV_TOOL_DIR").unwrap_or_else(|| xdg_data.join("uv/tools")),
        env("IR_CACHE_DIR").unwrap_or_else(|| r_cache.join("R/ir")),
        // tools::R_user_dir appends R/<package> even to R_USER_CACHE_DIR.
        r_cache.join("R/reticulate"),
        // Keep configured cache roots relative until policy capture checks them.
        env(crate::local_runtime::DUCKDB_EXTENSION_DIRECTORY)
            .unwrap_or_else(|| home.join(".duckdb/extensions")),
        matplotlib_cache_override(settings).unwrap_or_else(|| default_matplotlib_cache(&home)),
        env("RENV_PATHS_ROOT").unwrap_or_else(|| r_cache.join("R/renv")),
        r_cache.join("R/pkgcache"),
    ];
    if cfg!(target_os = "macos")
        && ["UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "UV_TOOL_DIR"]
            .into_iter()
            .any(|name| env(name).is_none())
    {
        // uv retains existing installations in its pre-XDG native locations.
        caches.extend([
            home.join("Library/Caches/uv"),
            home.join("Library/Application Support/uv"),
        ]);
    }
    if let Some(list) = env(RENV_CACHE) {
        let list = list.to_str().ok_or("resolver policy paths must be UTF-8")?;
        caches.extend(
            list.split(renv_separator)
                .filter(|entry| !entry.is_empty())
                .map(PathBuf::from),
        );
    }
    for name in [
        "IR_LIBRARY_ROOT",
        "RENV_PATHS_SOURCE",
        "RENV_PATHS_BINARY",
        "R_PKG_CACHE_DIR",
        "PKG_CACHE_DIR",
        "UV_PYTHON_BIN_DIR",
        "UV_TOOL_BIN_DIR",
        "PYTHONPYCACHEPREFIX",
        "PYTHONUSERBASE",
    ] {
        if let Some(path) = env(name) {
            caches.push(path);
        }
    }
    #[cfg(target_os = "macos")]
    {
        // macOS mktemp and shell here-documents use Darwin's user temporary
        // directory even when the native runner supplies a private TMPDIR.
        let temporary = std::process::Command::new("/usr/bin/getconf")
            .arg("DARWIN_USER_TEMP_DIR")
            .output()
            .map_err(|error| {
                format!("cannot locate the macOS user temporary directory: {error}")
            })?;
        if !temporary.status.success() {
            return Err("cannot locate the macOS user temporary directory".into());
        }
        let temporary = String::from_utf8(temporary.stdout)
            .map_err(|_| "resolver policy paths must be UTF-8")?;
        let temporary = PathBuf::from(temporary.trim_end_matches('\n'));
        if !temporary.is_absolute() {
            return Err("macOS user temporary directory must be absolute".into());
        }
        caches.push(temporary);
    }
    Ok(caches)
}
