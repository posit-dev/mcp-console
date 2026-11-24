//! Controller-side cache creation. Each child is opened or created relative
//! to its pinned parent, so changing a pathname cannot redirect a later mkdir.

use std::ffi::OsStr;
use std::fs::File;
use std::io;
use std::path::Path;

pub(super) fn create(path: &Path) -> io::Result<()> {
    let prefixes: Vec<_> = path.ancestors().collect();
    let mut prefixes = prefixes.into_iter().rev().peekable();
    let root = prefixes.next().expect("cache path has a root");
    #[cfg(target_os = "macos")]
    // The standard aliases live directly in the read-only system root. Open
    // that trusted prefix once; all subsequent opens must refuse links.
    let root = prefixes
        .next_if(|path| {
            path.symlink_metadata().is_ok_and(|metadata| {
                metadata.is_symlink() && crate::settings::is_macos_system_alias(path, &metadata)
            })
        })
        .unwrap_or(root);
    let mut options = File::options();
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt as _;
        use windows_sys::Win32::Storage::FileSystem::{FILE_FLAG_BACKUP_SEMANTICS, FILE_TRAVERSE};
        options
            .access_mode(FILE_TRAVERSE)
            .custom_flags(FILE_FLAG_BACKUP_SEMANTICS);
    }
    #[cfg(unix)]
    options.read(true);
    let mut parent = options.open(root)?;
    for prefix in prefixes {
        let name = prefix.file_name().expect("normalized cache component");
        parent = open_or_create(&parent, name)?;
    }
    Ok(())
}

#[cfg(unix)]
fn open_or_create(parent: &File, name: &OsStr) -> io::Result<File> {
    use rustix::fs::{Mode, OFlags, mkdirat, openat};
    use rustix::io::Errno;

    #[cfg(target_os = "linux")]
    let access = OFlags::PATH;
    #[cfg(target_os = "macos")]
    let access = OFlags::from_bits_retain(libc::O_SEARCH as _);
    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    let access = OFlags::RDONLY;
    let flags = access | OFlags::CLOEXEC | OFlags::DIRECTORY | OFlags::NOFOLLOW;
    match openat(parent, name, flags, Mode::empty()) {
        Ok(file) => Ok(file.into()),
        Err(Errno::NOENT) => {
            match mkdirat(parent, name, Mode::from_raw_mode(0o777)) {
                Ok(()) | Err(Errno::EXIST) => {}
                Err(error) => return Err(error.into()),
            }
            Ok(openat(parent, name, flags, Mode::empty())?.into())
        }
        Err(error) => Err(error.into()),
    }
}

#[cfg(windows)]
fn open_or_create(parent: &File, name: &OsStr) -> io::Result<File> {
    use std::os::windows::ffi::OsStrExt as _;
    use std::os::windows::io::{AsRawHandle as _, FromRawHandle as _};
    use std::ptr;
    use windows_sys::Wdk::Foundation::OBJECT_ATTRIBUTES;
    use windows_sys::Wdk::Storage::FileSystem::{
        FILE_DIRECTORY_FILE, FILE_OPEN_IF, FILE_SYNCHRONOUS_IO_NONALERT, NtCreateFile,
    };
    use windows_sys::Win32::Foundation::{
        OBJ_CASE_INSENSITIVE, OBJ_DONT_REPARSE, RtlNtStatusToDosError, UNICODE_STRING,
    };
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_ATTRIBUTE_NORMAL, FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE, FILE_TRAVERSE,
        SYNCHRONIZE,
    };
    use windows_sys::Win32::System::IO::IO_STATUS_BLOCK;

    let mut name: Vec<_> = name.encode_wide().collect();
    let length = u16::try_from(size_of_val(name.as_slice()))
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "cache component too long"))?;
    let name = UNICODE_STRING {
        Buffer: name.as_mut_ptr(),
        Length: length,
        MaximumLength: length,
    };
    let attributes = OBJECT_ATTRIBUTES {
        Length: size_of::<OBJECT_ATTRIBUTES>() as u32,
        RootDirectory: parent.as_raw_handle(),
        ObjectName: &name,
        Attributes: OBJ_CASE_INSENSITIVE | OBJ_DONT_REPARSE,
        ..Default::default()
    };
    let mut handle = ptr::null_mut();
    // All pointers live through this synchronous call. OBJ_DONT_REPARSE
    // refuses junctions and links during the same operation that creates or
    // opens the child. A successful call gives us ownership of its handle.
    let status = unsafe {
        NtCreateFile(
            &mut handle,
            FILE_TRAVERSE | SYNCHRONIZE,
            &attributes,
            &mut IO_STATUS_BLOCK::default(),
            ptr::null(),
            FILE_ATTRIBUTE_NORMAL,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            FILE_OPEN_IF,
            FILE_DIRECTORY_FILE | FILE_SYNCHRONOUS_IO_NONALERT,
            ptr::null(),
            0,
        )
    };
    if status < 0 {
        return Err(io::Error::from_raw_os_error(
            unsafe { RtlNtStatusToDosError(status) } as i32,
        ));
    }
    Ok(unsafe { File::from_raw_handle(handle) })
}
