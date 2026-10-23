use std::process::Stdio;

use serde::Serialize;

use super::process::{ResolverProcess, ResolverStopHandle, resolver_command};

const MANAGED_DUCKDB_EXTENSION_RESOLVER_SOURCE: &str = include_str!("programs/duckdb_extensions.R");

#[derive(Serialize)]
struct ResolverInput<'a> {
    extensions: &'a [String],
}

pub(crate) fn resolve_duckdb_extensions(
    managed_r: &super::ManagedR,
    extensions: &[String],
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<(), String> {
    let input = serde_json::to_vec(&ResolverInput { extensions })
        .expect("DuckDB extension resolver input should serialize as JSON");

    let rscript = managed_r.rscript();
    let mut command = resolver_command(rscript);
    command.arg("--vanilla");
    let _program =
        super::r_program::RProgram::append(&mut command, MANAGED_DUCKDB_EXTENSION_RESOLVER_SOURCE)?;
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    managed_r.configure_worker(&mut command)?;
    // DuckDB installs under the preparation process's cache and network policy.
    // Names are JSON input, never R or SQL source.
    let resolver = ResolverProcess::new();
    let invocation = resolver.spawn(&mut command, Some(input)).map_err(|error| {
        format!(
            "failed to run DuckDB extension resolver with `{}`: {error}",
            rscript.display()
        )
    })?;
    let output = resolver.collect(invocation, rscript, "DuckDB extension", on_started)?;
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
    Ok(())
}
