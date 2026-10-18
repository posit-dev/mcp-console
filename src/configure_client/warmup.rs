//! Launch one independent warmup after client registration has completed.

use std::path::Path;
use std::process::Stdio;

use super::launch::Launch;
use crate::cli::ConfigureClientOptions;

pub(super) fn start(
    launch: &Launch,
    options: &ConfigureClientOptions,
    directory: &Path,
) -> Result<(), String> {
    if options.dry_run {
        println!("Would start background warmup.");
        return Ok(());
    }
    let logs = crate::console_paths::home_console_directory()?
        .ok_or("cannot locate Console home for the warmup log")?
        .join("logs");
    std::fs::create_dir_all(&logs)
        .map_err(|error| format!("cannot create {}: {error}", logs.display()))?;
    let (log, path) = tempfile::Builder::new()
        .prefix("warmup-")
        .suffix(".log")
        .tempfile_in(&logs)
        .and_then(|file| file.keep().map_err(|error| error.error))
        .map_err(|error| format!("cannot create warmup log: {error}"))?;
    let stdout = log.try_clone().map_err(|error| error.to_string())?;
    // One job per setup invocation, using the first selected client's settings.
    let mut command = launch.warmup_command(options.clients[0], &options.serve_args)?;
    command
        .current_dir(directory)
        .stdin(Stdio::null())
        .stdout(stdout)
        .stderr(log);
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        // SAFETY: setsid is async-signal-safe; the child only detaches its
        // session before exec, without allocation or inherited Rust locks.
        unsafe {
            command.pre_exec(|| {
                if libc::setsid() == -1 {
                    Err(std::io::Error::last_os_error())
                } else {
                    Ok(())
                }
            });
        }
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        use windows_sys::Win32::System::Threading::{CREATE_NEW_PROCESS_GROUP, DETACHED_PROCESS};
        command.creation_flags(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP);
    }
    // Setup exits without waiting; its detached child is reparented on Unix.
    // All streams have independent owners, so captured setup output reaches EOF.
    #[allow(clippy::zombie_processes)]
    let pid = command
        .spawn()
        .map_err(|error| format!("cannot launch warmup: {error}"))?
        .id();
    println!(
        "Background warmup started (PID {pid}). Log: {}",
        path.display()
    );
    Ok(())
}
