//! Exercise a real stdio server using the same launch settings as `serve`.

use std::ffi::OsString;
use std::path::PathBuf;
use std::process::Stdio;
use std::time::Duration;

use rmcp::{RoleClient, ServiceExt, model::CallToolRequestParams, service::Peer};
use tokio::process::Command;

use crate::cli::ConfigOverrides;

const SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(15);

pub(crate) fn run(
    overrides: &ConfigOverrides,
    worker: Option<PathBuf>,
    relay: Option<PathBuf>,
    no_sandbox: bool,
    writable_roots: Vec<PathBuf>,
    timeout: u32,
) -> Result<bool, Box<dyn std::error::Error>> {
    let mut command = Command::new(std::env::current_exe()?);
    command.arg("serve");
    for (flag, path) in [("--worker", worker), ("--relay", relay)] {
        if let Some(path) = path {
            command.arg(flag).arg(path);
        }
    }
    for (flag, enabled) in [
        ("--no-config", overrides.no_config),
        ("--no-global-config", overrides.no_global_config),
        ("--no-project-config", overrides.no_project_config),
        ("--no-sandbox", no_sandbox),
    ] {
        if enabled {
            command.arg(flag);
        }
    }
    if let Some(path) = &overrides.config_file {
        command.arg("--config-file").arg(path);
    }
    if let Some(path) = &overrides.values_file {
        command.arg("--config-overrides-file").arg(path);
    }
    for value in &overrides.default_values {
        command.arg("--default-config").arg(value);
    }
    for value in &overrides.values {
        command.arg("-c").arg(value);
    }
    for root in writable_roots {
        command.arg("--writable-root").arg(root);
    }
    check(command, Duration::from_secs(timeout.into()))
}

/// Client setup supplies its exact saved server launch; older releases only
/// need to implement `serve` and the public MCP interface, not `warmup`.
pub(crate) fn run_server(arguments: &[OsString]) -> Result<bool, Box<dyn std::error::Error>> {
    let (program, arguments) = arguments.split_first().expect("required server command");
    let mut command = Command::new(program);
    command.args(arguments);
    check(command, Duration::from_secs(600))
}

fn check(mut command: Command, timeout: Duration) -> Result<bool, Box<dyn std::error::Error>> {
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        // Preserve preparation, configuration, and shutdown diagnostics live.
        .stderr(Stdio::inherit())
        .kill_on_drop(true);

    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?;
    runtime.block_on(async {
        println!("MCP Console warmup");
        let mut child = command.spawn()?;
        let stdout = child.stdout.take().expect("piped server stdout");
        let stdin = child.stdin.take().expect("piped server stdin");
        let mut failures = 0;
        match tokio::time::timeout(timeout, ().serve((stdout, stdin))).await {
            Ok(Ok(mut client)) => {
                match tokio::time::timeout(timeout, client.list_all_tools()).await {
                    Ok(Ok(tools)) => {
                        if let Some(tool) = tools.iter().find(|tool| tool.name == "send") {
                            failures += report("Server", Ok(()));
                            failures += check_languages(&client, tool, timeout).await;
                        } else {
                            failures += report("Server", Err("send tool is missing".into()));
                        }
                    }
                    Ok(Err(error)) => failures += report("Server", Err(error.to_string())),
                    Err(_) => {
                        failures += report("Server", Err("tool discovery timed out".into()));
                    }
                }
                // Closing MCP input lets the server retire its existing owners.
                // Keep the child handle to verify its actual exit status below.
                match client.close_with_timeout(SHUTDOWN_TIMEOUT).await {
                    Ok(Some(_)) => {}
                    Ok(None) => {
                        failures +=
                            report("MCP shutdown", Err("connection close timed out".into()));
                    }
                    Err(error) => failures += report("MCP shutdown", Err(error.to_string())),
                }
            }
            Ok(Err(error)) => failures += report("Server", Err(error.to_string())),
            Err(_) => failures += report("Server", Err("initialization timed out".into())),
        }
        let shutdown = match tokio::time::timeout(SHUTDOWN_TIMEOUT, child.wait()).await {
            Ok(Ok(status)) if status.success() => Ok(()),
            Ok(Ok(status)) => Err(match status.code() {
                Some(code) => format!("server exited with status {code}"),
                None => format!("server exited with {status}"),
            }),
            Ok(Err(error)) => Err(error.to_string()),
            Err(_) => {
                child.kill().await?;
                Err("server shutdown timed out; server was terminated".into())
            }
        };
        failures += report("Shutdown", shutdown);
        if failures == 0 {
            println!("All checks passed. Runtime caches are warm.");
        } else {
            let suffix = if failures == 1 { "" } else { "s" };
            println!("Warmup found {failures} failed check{suffix}.");
        }
        Ok(failures == 0)
    })
}

async fn check_languages(
    client: &Peer<RoleClient>,
    tool: &rmcp::model::Tool,
    timeout: Duration,
) -> usize {
    let properties = tool.input_schema["properties"]
        .as_object()
        .expect("send parameters are an object");
    let mut failures = 0;
    for (field, label, source, expected) in [
        (
            "r",
            "R",
            "stopifnot(1 + 1 == 2)\ncat('mcp-console warmup: R OK\\n')",
            "mcp-console warmup: R OK",
        ),
        (
            "python",
            "Python",
            "assert sum((1, 1)) == 2\nprint('mcp-console warmup: Python OK')",
            "mcp-console warmup: Python OK",
        ),
        (
            "sql",
            "SQL",
            "SELECT CASE WHEN 1 + 1 = 2 THEN 'mcp-console warmup: SQL OK' ELSE 'arithmetic failed' END AS result",
            "mcp-console warmup: SQL OK",
        ),
    ] {
        if !properties.contains_key(field) {
            println!("{label}: SKIP (disabled)");
            continue;
        }
        println!("Checking {label}...");
        match tokio::time::timeout(timeout, evaluate(client, field, source)).await {
            Ok(Ok((output, is_error))) => {
                let succeeded = !is_error
                    && if field == "sql" {
                        output.contains(expected)
                    } else {
                        output.lines().any(|line| line == expected)
                    };
                if succeeded {
                    failures += report(label, Ok(()));
                    if field != "sql" {
                        // Bootstrap diagnostics can accompany a successful cell,
                        // including a failed optional SQL startup. Retain them.
                        let diagnostics = output
                            .lines()
                            .filter(|line| *line != expected)
                            .collect::<Vec<_>>()
                            .join("\n");
                        if !diagnostics.trim().is_empty() {
                            println!("{diagnostics}");
                        }
                    }
                } else {
                    failures += report(label, Err(output));
                }
            }
            Ok(Err(error)) => {
                failures += report(label, Err(error));
                break;
            }
            Err(_) => {
                failures += report(label, Err("check timed out; stopping the server".into()));
                break;
            }
        }
    }
    failures
}

async fn evaluate(
    client: &Peer<RoleClient>,
    field: &str,
    source: &str,
) -> Result<(String, bool), String> {
    let mut arguments = serde_json::Map::from_iter([(field.into(), source.into())]);
    let mut output = String::new();
    let mut is_error = false;
    loop {
        let result = client
            .call_tool(CallToolRequestParams::new("send").with_arguments(arguments))
            .await
            .map_err(|error| format!("{output}{error}"))?;
        let text = result
            .content
            .iter()
            .filter_map(|content| content.as_text())
            .map(|content| content.text.as_str())
            .collect::<String>();
        output.push_str(&text);
        is_error |= result.is_error.unwrap_or(false);
        if text.ends_with("[waiting for stdin]") {
            return Err(format!("{output}\ncheck requested interactive input"));
        }
        if !text.ends_with("[running; poll with an empty send]")
            && !text.ends_with("[worker starting]")
        {
            return Ok((output, is_error));
        }
        // An observation timeout does not cancel or authorize replaying the cell.
        arguments = serde_json::Map::new();
    }
}

fn report(label: &str, result: Result<(), String>) -> usize {
    match result {
        Ok(()) => {
            println!("{label}: OK");
            0
        }
        Err(error) => {
            println!("{label}: FAIL\n{}", error.trim_end());
            1
        }
    }
}
