//! A native uv fixture for resolver discovery and manifest handoff on every host.

use std::process;

fn main() {
    let arguments: Vec<_> = std::env::args_os().skip(1).collect();
    if arguments.iter().take(2).eq(["python", "list"]) {
        println!(
            r#"[{{"version":"3.12.7","version_parts":{{"major":3,"minor":12,"patch":7}},"symlink":null,"variant":"default","implementation":"cpython"}}]"#
        );
    } else if arguments.iter().take(2).eq(["tool", "run"]) {
        std::fs::write(
            arguments.last().unwrap(),
            std::env::var("MCP_CONSOLE_TEST_RESOLVED_PYTHON").unwrap(),
        )
        .unwrap();
    } else {
        eprintln!("unexpected uv arguments: {arguments:?}");
        process::exit(90);
    }
}
