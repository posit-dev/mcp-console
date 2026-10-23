use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
use std::os::unix::net::UnixStream;

pub(crate) struct InputWatch(OwnedFd);

impl InputWatch {
    pub(crate) fn new(completion: RawFd) -> Result<Self, String> {
        // SAFETY: kqueue returns a new owned descriptor on success.
        let descriptor = unsafe { libc::kqueue() };
        if descriptor < 0 {
            return Err(format!(
                "failed to watch MCP input: {}",
                std::io::Error::last_os_error()
            ));
        }
        // SAFETY: `descriptor` is the newly created kqueue, owned only here.
        let queue = unsafe { OwnedFd::from_raw_fd(descriptor) };
        // SAFETY: the queue is live; prevent resolver children from inheriting it.
        if unsafe { libc::fcntl(descriptor, libc::F_SETFD, libc::FD_CLOEXEC) } != 0 {
            return Err(format!(
                "failed to configure MCP input watch: {}",
                std::io::Error::last_os_error()
            ));
        }
        let changes = [libc::STDIN_FILENO, completion].map(|descriptor| libc::kevent {
            ident: descriptor as libc::uintptr_t,
            filter: libc::EVFILT_READ,
            flags: libc::EV_ADD | libc::EV_CLEAR,
            fflags: 0,
            data: 0,
            udata: std::ptr::null_mut(),
        });
        submit(&queue, &changes)?;
        Ok(Self(queue))
    }

    pub(crate) fn wait(self, completion: UnixStream) -> Result<(), String> {
        watch(&self.0, completion)
    }
}

fn submit(queue: &OwnedFd, changes: &[libc::kevent]) -> Result<(), String> {
    loop {
        // SAFETY: changes is valid for its length; this call returns no events.
        let result = unsafe {
            libc::kevent(
                queue.as_raw_fd(),
                changes.as_ptr(),
                changes.len() as _,
                std::ptr::null_mut(),
                0,
                std::ptr::null(),
            )
        };
        if result >= 0 {
            return Ok(());
        }
        let error = std::io::Error::last_os_error();
        if error.kind() != std::io::ErrorKind::Interrupted {
            return Err(format!("failed to register MCP input watch: {error}"));
        }
    }
}

fn watch(queue: &OwnedFd, completion: UnixStream) -> Result<(), String> {
    loop {
        // SAFETY: zero is valid for every kevent field.
        let mut events: [libc::kevent; 2] = unsafe { std::mem::zeroed() };
        // SAFETY: events is writable for its length; no changes or timeout are supplied.
        let count = unsafe {
            libc::kevent(
                queue.as_raw_fd(),
                std::ptr::null(),
                0,
                events.as_mut_ptr(),
                events.len() as _,
                std::ptr::null(),
            )
        };
        if count < 0 {
            let error = std::io::Error::last_os_error();
            if error.kind() == std::io::ErrorKind::Interrupted {
                continue;
            }
            return Err(format!("failed to wait for MCP input closure: {error}"));
        }
        for event in &events[..count as usize] {
            if event.ident == libc::STDIN_FILENO as libc::uintptr_t
                && event.flags & libc::EV_EOF != 0
            {
                return Err("server startup cancelled because MCP input closed".to_string());
            }
        }
        if events[..count as usize]
            .iter()
            .any(|event| event.ident == completion.as_raw_fd() as libc::uintptr_t)
        {
            return Ok(());
        }
    }
}
