//! Select a real resolver through a native wrapper so its Python probe and
//! descendants exercise the same enforced permissions as package tools.
use std::process::Command;

fn main() {
    let arguments: Vec<_> = std::env::args_os().skip(1).collect();
    let probe = Command::new(std::env::var_os("PREPARATION_PYTHON").unwrap())
        .arg("-I")
        .arg(std::env::var_os("PREPARATION_PROBE").unwrap())
        .args(&arguments)
        .status()
        .unwrap();
    if !probe.success() {
        std::process::exit(probe.code().unwrap_or(1));
    }
    let executable = std::env::current_exe().unwrap();
    let tool = match executable.file_stem().unwrap().to_str().unwrap() {
        "ir" => "PREPARATION_IR",
        "R" => "PREPARATION_R",
        _ => "PREPARATION_UV",
    };
    let mut command = Command::new(std::env::var_os(tool).unwrap());
    if tool == "PREPARATION_UV"
        && std::env::var_os("PREPARATION_REFRESH_CACHE").is_some()
        && arguments.first().is_some_and(|argument| argument == "tool")
    {
        command
            .args(&arguments[..2])
            .arg("--refresh")
            .args(&arguments[2..]);
    } else {
        command.args(&arguments);
    }
    let status = command.status().unwrap();
    if status.success()
        && std::env::var_os("PREPARATION_INJECT_HOOK").is_some()
        && arguments.first().is_some_and(|argument| argument == "tool")
    {
        let python = std::fs::read_to_string(arguments.last().unwrap()).unwrap();
        let site = std::path::Path::new(python.trim())
            .parent()
            .unwrap()
            .parent()
            .unwrap()
            .join("Lib/site-packages/sitecustomize.py");
        std::fs::write(site, include_str!("resolver/windows_sitecustomize.py")).unwrap();
    }
    std::process::exit(status.code().unwrap_or(1));
}
