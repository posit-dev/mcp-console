//! Cache selection shared by preparation, worker launch, and sandbox grants.

use crate::settings::{Cache, SandboxSettings, environment_path};
use serde_json::{Value, json};
use std::path::PathBuf;

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
        Cache::Console
    });
    if matches!(selection, Cache::Host) {
        return Ok(());
    }
    // Console cache contents may have been produced by sandboxed code. Never
    // reuse them in a local session that removes native enforcement.
    if no_sandbox {
        return Err("cache: console requires sandboxing; use cache: host with --no-sandbox".into());
    }
    let explicit_python = python.map_or_else(
        || {
            std::env::var_os("RETICULATE_PYTHON")
                .is_some_and(|python| !python.is_empty() && python != "managed")
        },
        |python| python.executable.is_some(),
    );
    // Host-side companion staging uses the sibling mcp-console/sandbox cache.
    // Grant only dependency storage, never the shared Console cache parent.
    let root = console_root(resolver)?.join("dependencies");
    let path = |relative: &str| -> Result<Value, String> {
        root.join(relative)
            .to_str()
            .map(|path| Value::String(path.into()))
            .ok_or_else(|| "Console cache paths must be UTF-8".into())
    };
    let environment = [
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
    ]
    .into_iter()
    // Explicit Python keeps its preinstalled user-site packages without adding
    // resolver write grants for their host locations.
    .filter(|(name, _)| *name != "PYTHONUSERBASE" || !explicit_python)
    .map(|(name, relative)| Ok((name.to_owned(), path(relative)?)))
    .collect::<Result<SandboxSettings, String>>()?;
    for settings in [&mut *resolver, worker] {
        let values = settings.entry("environment").or_insert_with(|| json!({}));
        if let Value::Object(values) = values {
            // Preserve malformed native values for the runner's validation.
            for (name, path) in &environment {
                if values.get(name).is_none_or(Value::is_string) {
                    values.insert(name.clone(), path.clone());
                }
            }
        }
    }
    std::fs::create_dir_all(&root)
        .map_err(|error| format!("cannot create Console cache '{}': {error}", root.display()))?;
    let filesystem = resolver.entry("filesystem").or_insert_with(|| json!({}));
    if let Value::Object(filesystem) = filesystem
        && !filesystem.contains_key("entries")
    {
        let mut entries = vec![json!({
            "path": {"type": "special", "value": {"kind": "root"}}, "access": "read"
        })];
        entries.push(writable_cache_entry(&root)?);
        filesystem.insert("entries".into(), entries.into());
    }
    Ok(())
}

pub(super) fn writable_cache_entry(root: &std::path::Path) -> Result<Value, String> {
    // Keep native metadata protections at the cache root. Tools' nested Git
    // checkouts are covered by the ordinary cache grant.
    let path = root.to_str().ok_or("resolver policy paths must be UTF-8")?;
    Ok(json!({"path": {"type": "path", "path": path}, "access": "write"}))
}

fn console_root(settings: &SandboxSettings) -> Result<PathBuf, String> {
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
    let home = environment_path(settings, "HOME");
    #[cfg(windows)]
    let home = home.or_else(|| environment_path(settings, "USERPROFILE"));
    Ok(home
        .filter(|home| home.is_absolute())
        .map(|home| home.join(".duckdb/extensions")))
}

#[cfg(unix)]
pub(crate) fn writable_roots(settings: &SandboxSettings) -> Result<Vec<PathBuf>, String> {
    // Cache selection uses the resolver's effective environment, including its
    // own trusted YAML overrides. Worker environment settings do not reach here.
    // Config-file cache paths need explicit resolver grants; this covers only
    // default locations and direct environment overrides.
    let env = |name| environment_path(settings, name);
    let home = env("HOME").ok_or("resolver sandbox requires HOME")?;
    if !home.is_absolute() {
        return Err("resolver sandbox requires an absolute HOME".into());
    }
    let xdg_cache = env("XDG_CACHE_HOME").unwrap_or_else(|| home.join(".cache"));
    let uv_cache_base = if xdg_cache.is_absolute() {
        xdg_cache.clone()
    } else {
        home.join(".cache")
    };
    let xdg_data = env("XDG_DATA_HOME")
        .filter(|path| path.is_absolute())
        .unwrap_or_else(|| home.join(".local/share"));
    let r_cache = env("R_USER_CACHE_DIR")
        .or_else(|| env("XDG_CACHE_HOME"))
        .unwrap_or_else(|| {
            if cfg!(target_os = "macos") {
                home.join("Library/Caches/org.R-project.R")
            } else {
                xdg_cache.clone()
            }
        });
    let mut caches = vec![
        env("UV_CACHE_DIR").unwrap_or_else(|| uv_cache_base.join("uv")),
        env("UV_PYTHON_INSTALL_DIR").unwrap_or_else(|| xdg_data.join("uv/python")),
        env("UV_TOOL_DIR").unwrap_or_else(|| xdg_data.join("uv/tools")),
        env("IR_CACHE_DIR").unwrap_or_else(|| r_cache.join("R/ir")),
        // tools::R_user_dir appends R/<package> even to R_USER_CACHE_DIR.
        r_cache.join("R/reticulate"),
        duckdb_extension_directory(settings)?.expect("absolute resolver HOME"),
        env("MPLCONFIGDIR").unwrap_or_else(|| {
            if cfg!(target_os = "macos") {
                home.join(".matplotlib")
            } else {
                xdg_cache.join("matplotlib")
            }
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
