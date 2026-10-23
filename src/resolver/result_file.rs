//! Resolver results stay attached to the file created by the caller.

use std::fs::File;
use std::io::{Read as _, Seek as _};
#[cfg(unix)]
use std::os::fd::FromRawFd as _;
#[cfg(unix)]
use std::os::unix::ffi::{OsStrExt as _, OsStringExt as _};
use std::path::{Path, PathBuf};

pub(crate) struct ResultFile {
    path: PathBuf,
    file: File,
    #[cfg(windows)]
    _temporary: tempfile::TempPath,
}

impl ResultFile {
    #[cfg(windows)]
    pub(crate) fn create(directory: &Path) -> Result<Self, String> {
        let result = tempfile::Builder::new()
            .prefix("mcp-console-result-")
            .tempfile_in(directory)
            .map_err(|error| error.to_string())?;
        let (file, temporary) = result.into_parts();
        Ok(Self {
            path: temporary.to_path_buf(),
            file,
            _temporary: temporary,
        })
    }

    #[cfg(unix)]
    pub(crate) fn create(directory: &Path) -> Result<Self, String> {
        let mut template = directory
            .join("mcp-console-result-XXXXXX")
            .as_os_str()
            .as_bytes()
            .to_vec();
        template.push(0);
        // SAFETY: mkstemp creates a new mode-0600 file and transfers its descriptor.
        let descriptor = unsafe { libc::mkstemp(template.as_mut_ptr().cast()) };
        if descriptor < 0 {
            return Err(format!(
                "cannot create resolver result: {}",
                std::io::Error::last_os_error()
            ));
        }
        let file = unsafe { File::from_raw_fd(descriptor) };
        // The child opens the path; it must not inherit the server's descriptor.
        if unsafe { libc::fcntl(descriptor, libc::F_SETFD, libc::FD_CLOEXEC) } < 0 {
            return Err(std::io::Error::last_os_error().to_string());
        }
        template.pop();
        Ok(Self {
            path: std::ffi::OsString::from_vec(template).into(),
            file,
        })
    }

    pub(crate) fn path(&self) -> &Path {
        &self.path
    }

    pub(crate) fn read(&self, limit: u64) -> Result<Vec<u8>, String> {
        let mut file = &self.file;
        file.rewind().map_err(|error| error.to_string())?;
        let mut bytes = Vec::new();
        file.take(limit + 1)
            .read_to_end(&mut bytes)
            .map_err(|error| error.to_string())?;
        if bytes.len() as u64 > limit {
            return Err("resolver result exceeds its size limit".into());
        }
        Ok(bytes)
    }
}

impl Drop for ResultFile {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.path);
    }
}
