use sha2::{Digest as _, Sha256};
use std::fs::File;
use std::io::{self, Read as _};
#[cfg(unix)]
use std::os::unix::fs::{OpenOptionsExt as _, PermissionsExt as _};
use std::path::PathBuf;

include!(concat!(env!("OUT_DIR"), "/sandbox_runner_installation.rs"));

pub(super) fn private_runner() -> Result<PathBuf, String> {
    let verify = || -> io::Result<PathBuf> {
        let executable = std::env::current_exe()?.canonicalize()?;
        let prefix = executable
            .parent()
            .and_then(|directory| directory.parent())
            .ok_or_else(|| io::Error::other("executable has no installation prefix"))?;
        #[cfg(windows)]
        let native_prefix = prefix.join(NATIVE_BUNDLE);
        #[cfg(windows)]
        let prefix = if native_prefix.is_dir() {
            native_prefix.as_path()
        } else {
            prefix // Installed wheels carry the same verified bundle flat.
        };
        for (relative, expected) in ARTIFACTS {
            // Native helper selection prefers a suitable trusted host bwrap.
            // The runner verifies the bundled helper's embedded digest and
            // executes that same open file only when it actually selects it.
            if *relative == "libexec/bwrap" {
                continue;
            }
            // A replaced FIFO must not block before its file type is checked.
            let mut options = File::options();
            options.read(true);
            #[cfg(unix)]
            options.custom_flags(libc::O_NONBLOCK);
            let mut file = options.open(prefix.join(relative))?;
            let metadata = file.metadata()?;
            #[cfg(unix)]
            let executable =
                !relative.starts_with("libexec/") || metadata.permissions().mode() & 0o111 != 0;
            #[cfg(windows)]
            let executable = true;
            if !metadata.is_file() || !executable {
                return Err(io::Error::other(
                    "private artifact is not a readable file or executable",
                ));
            }
            let mut digest = Sha256::new();
            let mut buffer = [0; 64 * 1024];
            loop {
                let count = match file.read(&mut buffer) {
                    Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                    result => result?,
                };
                if count == 0 {
                    break;
                }
                digest.update(&buffer[..count]);
            }
            if digest.finalize().as_slice() != expected {
                return Err(io::Error::other(
                    "private artifact does not match this installation",
                ));
            }
        }
        Ok(prefix.join(if cfg!(windows) {
            "libexec/mcp-console-sandbox.exe"
        } else {
            "libexec/mcp-console-sandbox"
        }))
    };
    verify().map_err(|error| format!("failed to verify the private sandbox runner: {error}"))
}
