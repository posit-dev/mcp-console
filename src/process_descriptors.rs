use std::os::fd::RawFd;
#[cfg(target_os = "linux")]
use std::os::fd::{AsRawFd as _, FromRawFd as _, OwnedFd};
use std::os::unix::process::CommandExt as _;
use std::process::Command;

#[cfg(target_os = "macos")]
pub(crate) fn close_unlisted_from_multithreaded_parent(
    command: &mut Command,
) -> Result<(), String> {
    unsafe {
        command.pre_exec(cloexec_macos_descriptors);
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn cloexec_macos_descriptors() -> std::io::Result<()> {
    // Enumerate after fork, when the descriptor table is stable. Scanning the
    // soft limit can require a million fcntl calls for a nearly empty table.
    // libproc's proc_pidinfo is a direct __proc_info syscall wrapper. mmap
    // supplies scratch space without using the allocator after a threaded fork.
    fn list(buffer: *mut libc::c_void, size: libc::c_int) -> std::io::Result<usize> {
        loop {
            let count = unsafe {
                libc::proc_pidinfo(libc::getpid(), libc::PROC_PIDLISTFDS, 0, buffer, size)
            };
            if count > 0 {
                return Ok(count as usize);
            }
            let error = std::io::Error::last_os_error();
            if error.kind() != std::io::ErrorKind::Interrupted {
                return Err(error);
            }
        }
    }
    let size = list(std::ptr::null_mut(), 0)?;
    let memory = unsafe {
        libc::mmap(
            std::ptr::null_mut(),
            size,
            libc::PROT_READ | libc::PROT_WRITE,
            libc::MAP_PRIVATE | libc::MAP_ANON,
            -1,
            0,
        )
    };
    if memory == libc::MAP_FAILED {
        return Err(std::io::Error::last_os_error());
    }
    let result = (|| {
        let written = list(memory, size as libc::c_int)?;
        let entry_size = std::mem::size_of::<libc::proc_fdinfo>();
        if written > size || !written.is_multiple_of(entry_size) {
            return Err(std::io::Error::from_raw_os_error(libc::EIO));
        }
        // The successful syscall initialized exactly these records. No code
        // in this child opens a descriptor between the sizing and listing calls.
        let descriptors = unsafe {
            std::slice::from_raw_parts(memory.cast::<libc::proc_fdinfo>(), written / entry_size)
        };
        for descriptor in descriptors {
            if descriptor.proc_fd > libc::STDERR_FILENO {
                set_close_on_exec(descriptor.proc_fd)?;
            }
        }
        Ok(())
    })();
    unsafe { libc::munmap(memory, size) };
    result
}

fn set_close_on_exec(descriptor: RawFd) -> std::io::Result<()> {
    let flags = loop {
        let flags = unsafe { libc::fcntl(descriptor, libc::F_GETFD) };
        if flags >= 0 {
            break flags;
        }
        let error = std::io::Error::last_os_error();
        match error.raw_os_error() {
            Some(libc::EINTR) => continue,
            Some(libc::EBADF) => return Ok(()),
            _ => return Err(error),
        }
    };
    let updated_flags = flags | libc::FD_CLOEXEC;
    if flags == updated_flags {
        return Ok(());
    }

    loop {
        if unsafe { libc::fcntl(descriptor, libc::F_SETFD, updated_flags) } == 0 {
            return Ok(());
        }
        let error = std::io::Error::last_os_error();
        match error.raw_os_error() {
            Some(libc::EINTR) => continue,
            Some(libc::EBADF) => return Ok(()),
            _ => return Err(error),
        }
    }
}

#[cfg(target_os = "linux")]
pub(crate) fn close_unlisted_from_multithreaded_parent(
    command: &mut Command,
) -> Result<(), String> {
    unsafe {
        command.pre_exec(|| {
            // Retain Rust's exec-error pipe until exec. Older kernels either
            // lack close_range (ENOSYS) or its CLOEXEC flag (EINVAL). Seccomp
            // can deny it (EPERM) while still allowing procfs and fcntl.
            if libc::syscall(
                libc::SYS_close_range,
                3u32,
                u32::MAX,
                libc::CLOSE_RANGE_CLOEXEC,
            ) < 0
            {
                let error = std::io::Error::last_os_error();
                return match error.raw_os_error() {
                    Some(libc::ENOSYS | libc::EINVAL | libc::EPERM) => cloexec_proc_descriptors(),
                    _ => Err(error),
                };
            }
            Ok(())
        });
    }
    Ok(())
}

#[cfg(target_os = "linux")]
fn cloexec_proc_descriptors() -> std::io::Result<()> {
    // Enumerate in the child: another parent thread can open descriptors after
    // a parent-side snapshot, and existing descriptors can exceed a lowered
    // RLIMIT_NOFILE. Use only stack storage and syscalls after fork, avoiding
    // libc directory streams and their allocator locks.
    let directory = loop {
        let descriptor = unsafe {
            libc::open(
                c"/proc/self/fd".as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC,
            )
        };
        if descriptor >= 0 {
            break unsafe { OwnedFd::from_raw_fd(descriptor) };
        }
        let error = std::io::Error::last_os_error();
        if error.kind() != std::io::ErrorKind::Interrupted {
            return Err(error);
        }
    };
    let mut buffer = [0u8; 8192];
    loop {
        let count = unsafe {
            libc::syscall(
                libc::SYS_getdents64,
                directory.as_raw_fd(),
                buffer.as_mut_ptr(),
                buffer.len(),
            )
        };
        if count < 0 {
            let error = std::io::Error::last_os_error();
            if error.kind() == std::io::ErrorKind::Interrupted {
                continue;
            }
            return Err(error);
        }
        if count == 0 {
            return Ok(());
        }
        let mut offset = 0;
        while offset < count as usize {
            // Linux getdents64 records have a 19-byte header, followed by the
            // NUL-terminated name. The kernel supplies the record boundaries.
            let length = u16::from_ne_bytes([buffer[offset + 16], buffer[offset + 17]]) as usize;
            let name = &buffer[offset + 19..offset + length];
            if name[0].is_ascii_digit() {
                let descriptor = name
                    .iter()
                    .take_while(|byte| **byte != 0)
                    .fold(0, |number, byte| number * 10 + i32::from(byte - b'0'));
                if descriptor > libc::STDERR_FILENO {
                    set_close_on_exec(descriptor)?;
                }
            }
            offset += length;
        }
    }
}
