//! Bounded local named-pipe handshakes for trusted host test fixtures only.
use std::ffi::c_void;
use std::io::{self, Read, Write};
use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
use std::process::Child;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

#[repr(C)]
struct Overlapped {
    internal: usize,
    internal_high: usize,
    offset: u32,
    offset_high: u32,
    event: *mut c_void,
}

#[link(name = "kernel32")]
unsafe extern "system" {
    fn CreateNamedPipeW(
        n: *const u16,
        a: u32,
        m: u32,
        i: u32,
        o: u32,
        b: u32,
        t: u32,
        s: *const c_void,
    ) -> *mut c_void;
    fn CreateFileW(
        n: *const u16,
        a: u32,
        s: u32,
        p: *const c_void,
        c: u32,
        f: u32,
        t: *mut c_void,
    ) -> *mut c_void;
    fn CreateEventW(s: *const c_void, m: i32, i: i32, n: *const u16) -> *mut c_void;
    fn ConnectNamedPipe(h: *mut c_void, o: *mut Overlapped) -> i32;
    fn ReadFile(h: *mut c_void, b: *mut u8, n: u32, c: *mut u32, o: *mut Overlapped) -> i32;
    fn WriteFile(h: *mut c_void, b: *const u8, n: u32, c: *mut u32, o: *mut Overlapped) -> i32;
    fn GetOverlappedResult(h: *mut c_void, o: *mut Overlapped, c: *mut u32, w: i32) -> i32;
    fn CancelIoEx(h: *mut c_void, o: *mut Overlapped) -> i32;
    fn WaitForMultipleObjects(n: u32, h: *const *mut c_void, all: i32, t: u32) -> u32;
    fn GetCurrentProcess() -> *mut c_void;
    fn DuplicateHandle(
        p: *mut c_void,
        h: *mut c_void,
        q: *mut c_void,
        d: *mut *mut c_void,
        a: u32,
        i: i32,
        o: u32,
    ) -> i32;
}

pub struct Gate {
    handle: OwnedHandle,
    pub name: String,
}

fn owned(handle: *mut c_void) -> io::Result<OwnedHandle> {
    if handle.is_null() || handle as isize == -1 {
        Err(io::Error::last_os_error())
    } else {
        Ok(unsafe { OwnedHandle::from_raw_handle(handle) })
    }
}

fn wide(name: &str) -> Vec<u16> {
    name.encode_utf16().chain(Some(0)).collect()
}

impl Gate {
    pub fn connect(name: String) -> io::Result<Self> {
        // The owner creates the single instance before spawning this peer.
        let handle = owned(unsafe {
            CreateFileW(
                wide(&name).as_ptr(),
                0xc0000000,
                0,
                std::ptr::null(),
                3,
                0x40000000,
                std::ptr::null_mut(),
            )
        })?;
        Ok(Self { handle, name })
    }

    pub fn listen() -> io::Result<Self> {
        let name = format!(
            r"\\.\pipe\console-test-holder-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        );
        // First instance, duplex/overlapped, reject remote clients. Ordinary
        // host ACLs; neither endpoint is inherited by a child.
        let handle = owned(unsafe {
            CreateNamedPipeW(
                wide(&name).as_ptr(),
                3 | 0x40000000 | 0x80000,
                8,
                1,
                4096,
                4096,
                0,
                std::ptr::null(),
            )
        })?;
        Ok(Self { handle, name })
    }

    pub fn accept(&self, peer: &Child) -> io::Result<()> {
        self.operation(
            "accept",
            deadline(),
            Some(peer.as_raw_handle()),
            |overlapped| unsafe { ConnectNamedPipe(self.handle.as_raw_handle(), overlapped) },
        )?;
        Ok(())
    }

    pub fn try_clone(&self) -> io::Result<Self> {
        let process = unsafe { GetCurrentProcess() };
        let mut handle = std::ptr::null_mut();
        if unsafe {
            DuplicateHandle(
                process,
                self.handle.as_raw_handle(),
                process,
                &mut handle,
                0,
                0,
                2,
            )
        } == 0
        {
            return Err(io::Error::last_os_error());
        }
        Ok(Self {
            handle: owned(handle)?,
            name: self.name.clone(),
        })
    }

    fn operation(
        &self,
        label: &str,
        deadline: Instant,
        peer: Option<*mut c_void>,
        start: impl FnOnce(*mut Overlapped) -> i32,
    ) -> io::Result<usize> {
        if Instant::now() >= deadline {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                format!("{label} {}: deadline expired", self.name),
            ));
        }
        let event = owned(unsafe { CreateEventW(std::ptr::null(), 1, 0, std::ptr::null()) })?;
        let mut overlapped: Overlapped = unsafe { std::mem::zeroed() };
        overlapped.event = event.as_raw_handle();
        if start(&mut overlapped) == 0 {
            let error = io::Error::last_os_error();
            if label == "accept" && error.raw_os_error() == Some(535) {
                return Ok(0); // Connected before ConnectNamedPipe.
            }
            if error.raw_os_error() != Some(997) {
                return Err(io::Error::new(
                    error.kind(),
                    format!("{label} {}: {error}", self.name),
                ));
            }
        }
        let mut handles = vec![event.as_raw_handle()];
        if let Some(peer) = peer {
            handles.push(peer);
        }
        let milliseconds = deadline
            .saturating_duration_since(Instant::now())
            .as_millis()
            .min(u32::MAX as u128 - 1) as u32;
        let result = unsafe {
            WaitForMultipleObjects(handles.len() as u32, handles.as_ptr(), 0, milliseconds)
        };
        let mut count = 0;
        if result != 0 {
            let error = match result {
                258 => io::Error::new(io::ErrorKind::TimedOut, "deadline expired"),
                1 => io::Error::new(
                    io::ErrorKind::BrokenPipe,
                    format!("peer process {peer:?} exited before rendezvous"),
                ),
                _ => io::Error::last_os_error(),
            };
            // Cancellation is a request, not completion. Retain all operation
            // storage and handles until the cancelled (or raced) I/O finishes.
            unsafe {
                CancelIoEx(self.handle.as_raw_handle(), &mut overlapped);
                GetOverlappedResult(self.handle.as_raw_handle(), &mut overlapped, &mut count, 1);
            }
            return Err(io::Error::new(
                error.kind(),
                format!("{label} {}: {error}", self.name),
            ));
        }
        if unsafe {
            GetOverlappedResult(self.handle.as_raw_handle(), &mut overlapped, &mut count, 0)
        } == 0
        {
            let error = io::Error::last_os_error();
            return Err(io::Error::new(
                error.kind(),
                format!("{label} {}: {error}", self.name),
            ));
        }
        Ok(count as usize)
    }

    fn read_until(&self, bytes: &mut [u8], deadline: Instant) -> io::Result<usize> {
        self.operation("read", deadline, None, |overlapped| unsafe {
            ReadFile(
                self.handle.as_raw_handle(),
                bytes.as_mut_ptr(),
                bytes.len() as u32,
                std::ptr::null_mut(),
                overlapped,
            )
        })
    }

    fn write_until(&self, bytes: &[u8], deadline: Instant) -> io::Result<usize> {
        self.operation("write", deadline, None, |overlapped| unsafe {
            WriteFile(
                self.handle.as_raw_handle(),
                bytes.as_ptr(),
                bytes.len() as u32,
                std::ptr::null_mut(),
                overlapped,
            )
        })
    }
}

fn deadline() -> Instant {
    Instant::now() + Duration::from_secs(10)
}

impl Read for Gate {
    fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
        self.read_until(bytes, deadline())
    }

    fn read_exact(&mut self, mut bytes: &mut [u8]) -> io::Result<()> {
        let deadline = deadline();
        while !bytes.is_empty() {
            let count = self.read_until(bytes, deadline)?;
            if count == 0 {
                return Err(io::ErrorKind::UnexpectedEof.into());
            }
            bytes = &mut bytes[count..];
        }
        Ok(())
    }
}

impl Write for Gate {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.write_until(bytes, deadline())
    }

    fn write_all(&mut self, mut bytes: &[u8]) -> io::Result<()> {
        let deadline = deadline();
        while !bytes.is_empty() {
            let count = self.write_until(bytes, deadline)?;
            if count == 0 {
                return Err(io::ErrorKind::WriteZero.into());
            }
            bytes = &bytes[count..];
        }
        Ok(())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}
