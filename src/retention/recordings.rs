//! Recording leases distinguish inactive Console data from live or user-owned files.

use std::fs::{File, OpenOptions};
use std::io;
use std::num::NonZeroU32;
use std::path::Path;
use std::time::{Duration, SystemTime};

const MARKER: &str = ".retention";

pub(crate) struct Lease(File);

impl Lease {
    pub(crate) fn new(directory: &Path) -> io::Result<Self> {
        let mut options = OpenOptions::new();
        options.read(true).write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt as _;
            options.mode(0o600);
        }
        let file = options.open(directory.join(MARKER))?;
        file.lock()?;
        Ok(Self(file))
    }

    pub(crate) fn finish(self) -> io::Result<()> {
        // Producers and file finalization have settled before the lease expires.
        // An abrupt process exit instead retains the publication timestamp.
        self.0.set_modified(SystemTime::now())
    }
}

pub(crate) fn prune(sessions: &Path, days: Option<NonZeroU32>) -> io::Result<()> {
    let Some(days) = days else {
        return Ok(());
    };
    let age = Duration::from_secs(u64::from(days.get()) * 24 * 60 * 60);
    for entry in std::fs::read_dir(sessions)? {
        let entry = entry?;
        // Legacy recordings lack a lease; do not guess whether an older Console
        // still owns them. Symlinks and unrelated files are never prune targets.
        if !entry.file_type()?.is_dir() {
            continue;
        }
        let directory = entry.path();
        match directory.join(".keep").symlink_metadata() {
            Ok(_) => continue,
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
        let marker = directory.join(MARKER);
        let metadata = match std::fs::symlink_metadata(&marker) {
            Ok(metadata) if metadata.is_file() => metadata,
            Ok(_) => continue,
            Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
            Err(error) => return Err(error),
        };
        if !metadata
            .modified()?
            .elapsed()
            .is_ok_and(|elapsed| elapsed >= age)
        {
            continue;
        }
        let lease = match File::open(marker) {
            Ok(lease) => lease,
            Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
            Err(error) => return Err(error),
        };
        match lease.try_lock() {
            Ok(()) => {}
            Err(std::fs::TryLockError::WouldBlock) => continue,
            Err(std::fs::TryLockError::Error(error)) => return Err(error),
        }
        // Finalization may have refreshed the timestamp just before the lock.
        if lease
            .metadata()?
            .modified()?
            .elapsed()
            .is_ok_and(|elapsed| elapsed >= age)
        {
            match remove_recording(&directory) {
                Ok(()) => {}
                Err(error) if error.kind() == io::ErrorKind::NotFound => {}
                Err(error) => return Err(error),
            }
        }
    }
    Ok(())
}

fn remove_recording(directory: &Path) -> io::Result<()> {
    #[cfg(windows)]
    fn clear_readonly(directory: &Path) -> io::Result<()> {
        for entry in std::fs::read_dir(directory)? {
            let entry = entry?;
            let kind = entry.file_type()?;
            if kind.is_dir() {
                clear_readonly(&entry.path())?;
            } else if kind.is_file() {
                let mut permissions = entry.metadata()?.permissions();
                // Windows attributes only; preserve the existing ACL.
                #[allow(clippy::permissions_set_readonly_false)]
                permissions.set_readonly(false);
                std::fs::set_permissions(entry.path(), permissions)?;
            }
        }
        Ok(())
    }
    #[cfg(windows)]
    clear_readonly(directory)?;
    std::fs::remove_dir_all(directory)
}
