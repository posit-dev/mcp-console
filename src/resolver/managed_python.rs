use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use serde::Serialize;

use super::ManagedPython;

use super::process::{ResolverOutput, ResolverProcess, ResolverStopHandle, resolver_command};

const PYTHON_PATH_SOURCE: &str = r#"
import sys

with open(sys.argv[-1], "w", encoding="utf-8") as stream:
    stream.write(sys.executable)
"#;

#[derive(Serialize)]
struct ResolverInput<'a> {
    python: &'a str,
    packages: Vec<&'a str>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    python_version: Vec<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    exclude_newer: Option<&'a str>,
}

#[cfg(all(test, unix))]
pub(crate) fn resolve_python_manifest(
    requirements: crate::worker_protocol::PythonRequirementManifest,
    configuration: &super::ManagedPythonResolverConfiguration,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<ManagedPython, String> {
    resolve_python_manifest_for_host(requirements, configuration, None, on_started)
}

pub(crate) fn resolve_python_manifest_for_host(
    requirements: crate::worker_protocol::PythonRequirementManifest,
    configuration: &super::ManagedPythonResolverConfiguration,
    selected_python: Option<&Path>,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<ManagedPython, String> {
    crate::python_requirement::validate_all(&requirements.packages)?;
    crate::python_requirement::validate_version_constraints(&requirements.python_version)?;
    let requirements = requirements.normalized();
    let resolver = ResolverProcess::new();
    let mut on_started = Some(on_started);
    let resolved_python = if let Some(selected) = selected_python {
        if !selected.is_absolute() || !selected.is_file() {
            return Err(format!(
                "running Python executable is unavailable: {}",
                selected.display()
            ));
        }
        selected
            .to_str()
            .ok_or("running Python executable is not UTF-8")?
            .to_owned()
    } else {
        let versions = resolve_python_versions_with(configuration, &resolver, &mut on_started)?;
        versions
            .resolve(&requirements.python_version)
            .map_err(|error| {
                format!("managed Python version resolution failed: {}", error.trim())
            })?
    };
    let output_path = super::result_file::ResultFile::create(&std::env::temp_dir())?;
    let output = run_managed_python_resolver(
        &requirements,
        &resolved_python,
        output_path.path(),
        configuration,
        &resolver,
        &mut on_started,
    )?;
    if !output.status.success() {
        let error = resolver_error(&output);
        let python = if selected_python.is_some() {
            resolved_python.clone()
        } else if requirements.python_version.is_empty() {
            format!("{resolved_python} (reticulate default)")
        } else {
            requirements.python_version.join(", ")
        };
        let packages = requirements.packages.iter().map(String::as_str).collect();
        let python_version = requirements
            .python_version
            .iter()
            .map(String::as_str)
            .collect();
        let input = serde_json::to_string_pretty(&ResolverInput {
            python: &python,
            packages,
            python_version,
            exclude_newer: requirements.exclude_newer.as_deref(),
        })
        .expect("resolver input strings should serialize as JSON");
        return Err(output.failure(format!(
            "managed Python resolution failed:
resolver input:
{input}
uv output:
{error}"
        )));
    }
    resolver.check_control("managed Python resolution")?;
    let output = String::from_utf8(output_path.read(4096)?)
        .map_err(|_| "managed Python resolver returned a non-UTF-8 path".to_string())?;
    let python = PathBuf::from(output.trim());
    if !python.is_absolute() || !python.is_file() {
        return Err(format!(
            "managed Python resolver returned invalid interpreter `{}`",
            python.display()
        ));
    }
    warm_matplotlib(&python, &resolver, &mut on_started)?;
    Ok(ManagedPython {
        python,
        requirements,
    })
}

pub(crate) fn resolve_python_version(
    constraints: Vec<String>,
    configuration: &super::ManagedPythonResolverConfiguration,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<String, String> {
    crate::python_requirement::validate_version_constraints(&constraints)?;
    let versions = resolve_python_versions(configuration, on_started)?;
    versions
        .resolve(&constraints)
        .map_err(|error| format!("managed Python version resolution failed: {}", error.trim()))
}

fn resolve_python_versions<F>(
    configuration: &super::ManagedPythonResolverConfiguration,
    on_started: F,
) -> Result<super::python_version::PythonVersions, String>
where
    F: FnOnce(ResolverStopHandle) -> Result<(), String>,
{
    let resolver = ResolverProcess::new();
    let mut on_started = Some(on_started);
    resolve_python_versions_with(configuration, &resolver, &mut on_started)
}

fn resolve_python_versions_with<F>(
    configuration: &super::ManagedPythonResolverConfiguration,
    resolver: &ResolverProcess,
    on_started: &mut Option<F>,
) -> Result<super::python_version::PythonVersions, String>
where
    F: FnOnce(ResolverStopHandle) -> Result<(), String>,
{
    Ok(run_uv_python_list(configuration, resolver, on_started)?.rank())
}

fn run_uv_python_list<F>(
    configuration: &super::ManagedPythonResolverConfiguration,
    resolver: &ResolverProcess,
    on_started: &mut Option<F>,
) -> Result<super::python_version::PythonVersions, String>
where
    F: FnOnce(ResolverStopHandle) -> Result<(), String>,
{
    let uv = configuration.uv()?;
    let program = Path::new(uv);
    let mut command = resolver_command(program);
    command
        .args([
            "python",
            "list",
            "--all-versions",
            "--color",
            "never",
            "--output-format",
            "json",
            "--python-preference",
        ])
        .arg("only-managed")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .env_remove("VIRTUAL_ENV");
    configuration.configure_direct(&mut command)?;
    let output = run_resolver_command(
        command,
        resolver,
        on_started,
        program,
        "managed Python version",
    )?;
    if !output.status.success() {
        return Err(output.failure(format!(
            "managed Python version resolution failed with {}: {}",
            output.status,
            resolver_error(&output)
        )));
    }
    resolver.check_control("managed Python version resolution")?;
    super::python_version::PythonVersions::parse(&output.stdout).map_err(|error| {
        format!("managed Python version resolver returned invalid output: {error}")
    })
}

fn run_managed_python_resolver<F>(
    requirements: &crate::worker_protocol::PythonRequirementManifest,
    resolved_python: &str,
    output_path: &Path,
    configuration: &super::ManagedPythonResolverConfiguration,
    resolver: &ResolverProcess,
    on_started: &mut Option<F>,
) -> Result<ResolverOutput, String>
where
    F: FnOnce(ResolverStopHandle) -> Result<(), String>,
{
    let uv = configuration.uv()?;
    let program = Path::new(uv);
    let mut command = resolver_command(program);
    command
        .args([
            "tool",
            "run",
            "--isolated",
            "--python-preference",
            "only-managed",
            "--python",
        ])
        .arg(resolved_python);
    if let Some(exclude_newer) = requirements.exclude_newer.as_deref() {
        command.args(["--exclude-newer", exclude_newer]);
    }
    for package in &requirements.packages {
        command.arg("--with").arg(package);
    }
    command
        .args(["--", "python", "-c", PYTHON_PATH_SOURCE])
        .arg(output_path)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .env_remove("VIRTUAL_ENV");
    configuration.configure_direct(&mut command)?;
    // A no-cache tool environment is deleted when `uv tool run` exits,
    // so it cannot back a retained MCP Console session.
    command.env_remove("UV_NO_CACHE");
    run_resolver_command(command, resolver, on_started, program, "managed Python")
}

fn warm_matplotlib<F>(
    python: &Path,
    resolver: &ResolverProcess,
    on_started: &mut Option<F>,
) -> Result<(), String>
where
    F: FnOnce(ResolverStopHandle) -> Result<(), String>,
{
    let mut command = resolver_command(python);
    // Matplotlib checks its config directory before its separate Linux cache.
    // A read-only host config must not redirect warmup to private temporary data.
    if let Some(cache) = crate::python::matplotlib_cache_directory() {
        command.env("MPLCONFIGDIR", cache);
    }
    command
        .args(["-I", "-c", "import matplotlib.font_manager"])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    run_resolver_command(
        command,
        resolver,
        on_started,
        python,
        "managed Python cache warmup",
    )?;
    resolver.check_control("managed Python cache warmup")?;
    Ok(())
}

fn resolver_error(output: &ResolverOutput) -> String {
    let stderr = String::from_utf8_lossy(&output.stderr);
    let stderr = stderr.trim();
    if !stderr.is_empty() {
        return stderr.to_string();
    }
    String::from_utf8_lossy(&output.stdout).trim().to_string()
}

fn run_resolver_command<F>(
    mut command: Command,
    resolver: &ResolverProcess,
    on_started: &mut Option<F>,
    program: &Path,
    kind: &str,
) -> Result<ResolverOutput, String>
where
    F: FnOnce(ResolverStopHandle) -> Result<(), String>,
{
    let mut resumed_download = false;
    loop {
        let invocation = resolver.spawn(&mut command, None).map_err(|error| {
            format!(
                "failed to run {kind} resolver with `{}`: {error}",
                program.display()
            )
        })?;
        let output = resolver.collect(invocation, program, kind, |handle| {
            match on_started.take() {
                Some(started) => started(handle),
                None => Ok(()),
            }
        })?;
        // MXC denies junction creation. uv has already installed its concrete
        // interpreter when creating the optional minor-version junction fails,
        // before package preparation begins. Resume that same sandboxed uv
        // invocation once; never retry build failures or unconfirmed cleanup.
        let diagnostic = String::from_utf8_lossy(&output.stderr);
        if cfg!(windows)
            && !resumed_download
            && std::env::var_os(crate::settings::SANDBOX_MARKER).is_some()
            && command
                .get_args()
                .take(2)
                .eq([std::ffi::OsStr::new("tool"), std::ffi::OsStr::new("run")])
            && !output.status.success()
            && diagnostic.lines().find(|line| line.starts_with("error: "))
                == Some("error: Failed to create Python minor version link directory")
            && diagnostic.contains("(os error 5)")
        {
            resolver.check_control(kind)?;
            resumed_download = true;
            continue;
        }
        return Ok(output);
    }
}
