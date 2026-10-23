use std::path::Path;
use std::process::Stdio;

use serde::Serialize;

use super::process::{ResolverProcess, ResolverStopHandle, resolver_command};

const SOURCE: &str = include_str!("programs/duckdb_extensions.py");

#[derive(Serialize)]
struct ResolverInput<'a> {
    extensions: &'a [String],
    extension_directory: &'a Path,
}

pub(crate) fn python_duckdb_available(
    managed_python: &super::ManagedPython,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<bool, String> {
    let output = run(
        managed_python,
        br#"{"check_available":true}"#.to_vec(),
        on_started,
    )?;
    serde_json::from_slice(&output)
        .map_err(|error| format!("invalid DuckDB availability result: {error}"))
}

pub(crate) fn resolve_python_duckdb_extensions(
    managed_python: &super::ManagedPython,
    extensions: &[String],
    extension_directory: &Path,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<(), String> {
    let input = serde_json::to_vec(&ResolverInput {
        extensions,
        extension_directory,
    })
    .expect("DuckDB extension resolver input should serialize as JSON");
    run(managed_python, input, on_started).map(|_| ())
}

fn run(
    managed_python: &super::ManagedPython,
    input: Vec<u8>,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<Vec<u8>, String> {
    let python = managed_python.python();
    let mut command = resolver_command(python);
    command
        .args(["-I", "-c", SOURCE])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let resolver = ResolverProcess::new();
    let invocation = resolver.spawn(&mut command, Some(input)).map_err(|error| {
        format!(
            "failed to run DuckDB extension resolver with `{}`: {error}",
            python.display()
        )
    })?;
    let output = resolver.collect(invocation, python, "DuckDB extension", on_started)?;
    if !output.status.success() {
        let stdout = String::from_utf8_lossy(&output.stdout);
        let stderr = String::from_utf8_lossy(&output.stderr);
        let detail = if stderr.trim().is_empty() {
            stdout.trim()
        } else {
            stderr.trim()
        };
        return Err(output.failure(format!(
            "DuckDB extension resolution failed with {}: {detail}",
            output.status
        )));
    }
    output
        .write_result
        .map_err(|error| format!("failed to write DuckDB extension requirements: {error}"))?;
    Ok(output.stdout)
}
