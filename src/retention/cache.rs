//! Whole-cache resets require exclusive ownership of the dependency directory.
//!
//! Interpreters and libraries retain absolute paths into this directory even
//! while a session is idle. Each consumer therefore holds a shared lease until
//! its runtime retires; a due reset is postponed when another consumer exists.

use std::fs::{File, OpenOptions, TryLockError};
use std::io;
use std::num::NonZeroU32;
use std::path::Path;
use std::sync::Arc;
use std::time::{Duration, SystemTime};

pub(crate) fn lease(root: &Path, days: Option<NonZeroU32>) -> Result<Arc<File>, String> {
    acquire(root, days).map(Arc::new).map_err(|error| {
        format!(
            "cannot maintain Console cache '{}': {error}",
            root.display()
        )
    })
}

fn acquire(root: &Path, days: Option<NonZeroU32>) -> io::Result<File> {
    std::fs::create_dir_all(root)?;
    // The marker is outside resolver-writable dependencies. Never replace it:
    // all consumers must continue locking the same filesystem object.
    let marker = root.join(".cache-retention");
    match marker.symlink_metadata() {
        Ok(metadata) if !metadata.is_file() => {
            return Err(io::Error::other(
                "cache retention marker must be a regular file",
            ));
        }
        Ok(_) => {}
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(error),
    }
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(marker)?;
    // Creating the marker starts the first interval, including when adopting
    // caches created before retention existed. Disabled consumers still lease.
    if let Some(days) = days {
        match file.try_lock() {
            Ok(()) => {
                let now = SystemTime::now();
                let interval = Duration::from_secs(u64::from(days.get()) * 86_400);
                if now
                    .duration_since(file.metadata()?.modified()?)
                    .is_ok_and(|age| age >= interval)
                {
                    let dependencies = root.join("dependencies");
                    match dependencies.symlink_metadata() {
                        Ok(metadata) if metadata.is_dir() => {
                            std::fs::remove_dir_all(dependencies)?;
                        }
                        Ok(_) => {
                            return Err(io::Error::other(
                                "managed dependencies must be a directory, not a symlink or file",
                            ));
                        }
                        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
                        Err(error) => return Err(error),
                    }
                    file.set_modified(now)?;
                }
                // Unlock before requesting a different lock mode: converting
                // an existing lock has platform-dependent behavior. No cache
                // paths have escaped yet, so another reset in this gap is safe.
                file.unlock()?;
            }
            Err(TryLockError::WouldBlock) => {}
            Err(TryLockError::Error(error)) => return Err(error),
        }
    }
    file.lock_shared()?;
    Ok(file)
}
