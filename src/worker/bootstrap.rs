//! Native process setup before the shared interpreter bootstrap.

use std::error::Error;

#[cfg(windows)]
pub(super) use crate::windows::configure_worker_stdio as configure_stdio;

#[cfg(unix)]
pub(super) fn configure_stdio() -> std::io::Result<()> {
    Ok(())
}

#[cfg(not(target_os = "linux"))]
pub(super) fn prepare_r_library_path(
    _installation: Option<&crate::local_runtime::RInstallation>,
    _reader: &crate::sideband::Reader,
    _writer: &crate::sideband::Writer,
    _startup: Option<&std::ffi::OsStr>,
) -> Result<(), Box<dyn Error>> {
    Ok(())
}

#[cfg(target_os = "linux")]
pub(super) fn prepare_r_library_path(
    installation: Option<&crate::local_runtime::RInstallation>,
    reader: &crate::sideband::Reader,
    writer: &crate::sideband::Writer,
    startup: Option<&std::ffi::OsStr>,
) -> Result<(), Box<dyn Error>> {
    if let Some(installation) = installation {
        reexec_with_r_library_path(&installation.home, reader, writer, startup)?;
    }
    Ok(())
}

#[cfg(target_os = "linux")]
fn reexec_with_r_library_path(
    r_home: &std::path::Path,
    reader: &crate::sideband::Reader,
    writer: &crate::sideband::Writer,
    startup: Option<&std::ffi::OsStr>,
) -> Result<(), Box<dyn Error>> {
    use std::os::unix::process::CommandExt;

    let library = r_home.join("lib");
    let mut paths: Vec<_> = std::env::var_os("LD_LIBRARY_PATH")
        .map(|value| std::env::split_paths(&value).collect())
        .unwrap_or_default();
    if paths.first() == Some(&library) {
        return Ok(());
    }
    paths.insert(0, library);
    // The ELF loader reads LD_LIBRARY_PATH at exec, before native R packages
    // need to resolve libR.so and its companion libraries.
    let mut command = std::process::Command::new(std::env::current_exe()?);
    command
        .args(std::env::args_os().skip(1))
        .env("LD_LIBRARY_PATH", std::env::join_paths(paths)?);
    // Entry consumed the path from the environment. Forward only that path
    // across loader re-exec; the replacement unlinks it before runtime setup.
    if let Some(startup) = startup {
        command.env(crate::settings::startup::ENVIRONMENT, startup);
    }
    crate::sideband::configure_exec(reader, writer, &mut command)?;
    Err(command.exec().into())
}
