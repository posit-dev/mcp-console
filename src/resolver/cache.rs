//! Cache selection shared by preparation, worker launch, and sandbox grants.

use crate::settings::{Cache, SandboxSettings, environment_path};
use serde_json::{Value, json};
use std::path::{Path, PathBuf};

mod directory;

/// Private resolver setting consumed before passing permissions to the native runner.
pub(super) const MANAGED_DIRECTORY: &str = "_console_managed_cache_directory";
const CACHE_ENVIRONMENT_PATHS: &str = "_console_cache_environment_paths";

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
    ("RENV_PATHS_CACHE", "renv/cache"),
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
                for &(name, _) in CACHE_ENVIRONMENT {
                    if let Some(path) = environment_path(resolver, name) {
                        // Leave relative XDG values alone for tools such as uv.
                        if matches!(name, "XDG_CACHE_HOME" | "XDG_DATA_HOME") && path.is_relative()
                        {
                            continue;
                        }
                        retain_environment_path(resolver, &path, &[(name, "")])?;
                        if environment_path(worker, name).as_ref() == Some(&path) {
                            retain_environment_path(worker, &path, &[(name, "")])?;
                        }
                    }
                }
                if let Some(path) = matplotlib_cache_override(resolver) {
                    retain_environment_path(
                        worker,
                        &path,
                        &[("MCP_CONSOLE_MATPLOTLIB_CACHE", "")],
                    )?;
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
    let mut variables: Vec<_> = CACHE_ENVIRONMENT
        .iter()
        .copied()
        .filter(|(name, _)| *name != "PYTHONUSERBASE" || !explicit_python)
        .collect();
    for settings in [&mut *resolver, &mut *worker] {
        for &(name, relative) in &variables {
            set_cache_variable(settings, name, &grant.join(relative))?;
        }
    }
    retain_environment_path(resolver, &grant, &variables)?;
    variables.push(("MCP_CONSOLE_MATPLOTLIB_CACHE", "matplotlib"));
    retain_environment_path(worker, &grant, &variables)?;
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

/// Keep the shared root of managed cache variables so a link in a tool's own
/// cache subtree cannot change how its environment resolves a parent traversal.
fn retain_environment_path(
    settings: &mut SandboxSettings,
    path: &Path,
    variables: &[(&str, &str)],
) -> Result<(), String> {
    if has_parent_traversal(path) {
        let path = path.to_str().ok_or("Console cache paths must be UTF-8")?;
        settings
            .entry(CACHE_ENVIRONMENT_PATHS)
            .or_insert_with(|| json!([]))
            .as_array_mut()
            .expect("captured cache environment paths")
            .push(json!({"directory": path, "variables": variables}));
    }
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
pub(crate) fn resolve_environment(settings: &mut SandboxSettings) -> Result<(), String> {
    let Some(paths) = settings.remove(CACHE_ENVIRONMENT_PATHS) else {
        return Ok(());
    };
    for path in
        serde_json::from_value::<Vec<EnvironmentPath>>(paths).map_err(|error| error.to_string())?
    {
        let root = tool_directory(&path.directory)?;
        for (name, relative) in path.variables {
            // A runtime selection may explicitly replace or remove one of the
            // environment variables before launch. Preserve that selection.
            if root != path.directory
                && environment_path(settings, &name).as_ref()
                    == Some(&path.directory.join(&relative))
            {
                set_cache_variable(settings, &name, &root.join(relative))?;
            }
        }
    }
    Ok(())
}

pub(crate) fn tool_directory(path: &Path) -> Result<PathBuf, String> {
    if !has_parent_traversal(path) {
        return Ok(path.to_owned());
    }
    let absolute = crate::console_paths::absolute_preserving_parents(Path::new("."), path)
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
        return Ok(path.to_owned());
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
        matplotlib_cache_override(settings).unwrap_or_else(|| {
            home.join(if cfg!(target_os = "linux") {
                ".cache/matplotlib"
            } else {
                ".matplotlib"
            })
        }),
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
    for name in [
        "IR_LIBRARY_ROOT",
        "RENV_PATHS_CACHE",
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
