//! Bundle writes stay relative to a pinned recording directory, including when
//! submitted code can modify the managed tree. Never reopen an ambient path.
use cap_std::fs::{Dir, DirBuilder, OpenOptions};
use std::fs::File;
use std::io::{self, Write};
use std::path::{Component, Path};

pub(super) struct Root(pub(super) Dir);

impl Root {
    pub(super) fn open(path: &Path) -> io::Result<Self> {
        // Initial roots are Console-created, before source admission. All later
        // traversal and writes use the directory capability, not this path.
        if !std::fs::symlink_metadata(path)?.file_type().is_dir() {
            return Err(io::Error::other("recording root is not a directory"));
        }
        Dir::open_ambient_dir(path, cap_std::ambient_authority()).map(Self)
    }

    fn directory(&self, relative: &Path) -> io::Result<Dir> {
        let mut directory = self.0.try_clone()?;
        for component in relative.components() {
            let Component::Normal(name) = component else {
                return Err(io::Error::other("bundle paths must be relative components"));
            };
            match directory.symlink_metadata(name) {
                Ok(metadata) if metadata.is_dir() && !metadata.is_symlink() => {}
                Ok(_) => return Err(io::Error::other("bundle directory was replaced")),
                Err(error) if error.kind() == io::ErrorKind::NotFound => {
                    let mut options = DirBuilder::new();
                    #[cfg(unix)]
                    {
                        use cap_std::fs::DirBuilderExt as _;
                        options.mode(0o700);
                    }
                    directory.create_dir_with(name, options.recursive(false))?;
                }
                Err(error) => return Err(error),
            }
            // Even a concurrent replacement cannot make cap-std traverse out
            // of this pinned parent. Windows also pins directory names.
            directory = directory.open_dir(name)?;
        }
        Ok(directory)
    }

    pub(super) fn session(&self, session: u64) -> io::Result<Dir> {
        self.directory(Path::new(&format!("sessions/session-{session:03}")))
    }

    pub(super) fn image(&self, relative: &Path) -> io::Result<File> {
        let directory = self.directory(relative.parent().expect("bundle image parent"))?;
        create_file(&directory, relative.file_name().expect("bundle image name"))
    }
}

pub(super) fn create_file(directory: &Dir, name: impl AsRef<Path>) -> io::Result<File> {
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use cap_std::fs::OpenOptionsExt as _;
        options.mode(0o600);
    }
    directory
        .open_with(name, &options)
        .map(|file| file.into_std())
}

pub(super) fn replace_source(directory: &Dir, bytes: &[u8]) -> Result<(), String> {
    let temporary = ".transcript.qmd.tmp";
    let result = (|| {
        let mut file = create_file(directory, temporary)?;
        file.write_all(bytes)?;
        file.flush()?;
        drop(file);
        directory.rename(temporary, directory, "session.qmd")
    })();
    if result.is_err() {
        let _ = directory.remove_file(temporary);
    }
    result.map_err(|e| e.to_string())
}
