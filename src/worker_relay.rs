use std::ffi::OsString;

#[cfg(any(unix, windows))]
mod event_writer;
#[cfg(any(unix, windows))]
mod lifecycle;
#[cfg(any(unix, windows))]
mod routing;
#[cfg(windows)]
mod windows;
#[cfg(any(unix, windows))]
pub(crate) fn run(command_line: &[OsString]) -> Result<(), String> {
    let (program, arguments) = command_line
        .split_first()
        .ok_or("worker relay command must include an executable")?;
    // SAFETY: consume before either native supervisor starts its I/O threads.
    let startup = unsafe { crate::settings::startup::take_environment() };
    let mut command = std::process::Command::new(program);
    command.args(arguments);
    if let Some(startup) = startup {
        command.env(crate::settings::startup::ENVIRONMENT, startup);
    }
    #[cfg(unix)]
    return supervisor::run(command);
    #[cfg(windows)]
    windows::run(command)
}

#[cfg(not(any(unix, windows)))]
pub(crate) fn run(_command_line: &[OsString]) -> Result<(), String> {
    Err("the worker relay is currently supported only on macOS".to_string())
}

#[cfg(unix)]
mod commands;
#[cfg(unix)]
mod io;
#[cfg(unix)]
mod streams;
#[cfg(unix)]
mod supervisor;
