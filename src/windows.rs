//! Native handles used by the Windows process boundary.
use std::io::{self, Read, Write};
use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle, RawHandle};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use windows_sys::Win32::Foundation::*;
use windows_sys::Win32::Storage::FileSystem::*;
use windows_sys::Win32::System::IO::*;
use windows_sys::Win32::System::Pipes::*;
use windows_sys::Win32::System::Threading::*;
use windows_sys::Win32::UI::WindowsAndMessaging::{
    MWMO_INPUTAVAILABLE, MsgWaitForMultipleObjectsEx, QS_ALLINPUT,
};

pub(crate) fn configure_worker_stdio() -> io::Result<()> {
    unsafe extern "C" {
        fn _setmode(descriptor: libc::c_int, mode: libc::c_int) -> libc::c_int;
    }
    // The protocol owns byte streams. CRT text mode would translate CRLF and
    // interpret a queued Ctrl-Z as EOF before a console callback can see it.
    for descriptor in 0..=2 {
        if unsafe { _setmode(descriptor, libc::O_BINARY) } == -1 {
            return Err(io::Error::last_os_error());
        }
    }
    Ok(())
}

pub(crate) fn restore_worker_stdio() -> io::Result<()> {
    use windows_sys::Win32::System::Console::{
        STD_ERROR_HANDLE, STD_INPUT_HANDLE, STD_OUTPUT_HANDLE, SetStdHandle,
    };
    // R's system2 can clear the Win32 standard handles while the CRT retains
    // its live descriptors. Python subprocesses use the Win32 handles.
    for (descriptor, kind) in [STD_INPUT_HANDLE, STD_OUTPUT_HANDLE, STD_ERROR_HANDLE]
        .into_iter()
        .enumerate()
    {
        let handle = unsafe { libc::get_osfhandle(descriptor as _) } as HANDLE;
        if handle == INVALID_HANDLE_VALUE || unsafe { SetStdHandle(kind, handle) } == 0 {
            return Err(io::Error::last_os_error());
        }
    }
    Ok(())
}

#[derive(Clone)]
pub(crate) struct Event(Arc<OwnedHandle>);

impl Event {
    pub(crate) fn new() -> io::Result<Self> {
        // SAFETY: unnamed manual-reset event, owned by the returned handle.
        let handle = unsafe { CreateEventW(std::ptr::null(), 1, 0, std::ptr::null()) };
        if handle.is_null() {
            return Err(io::Error::last_os_error());
        }
        Ok(Self(Arc::new(unsafe {
            OwnedHandle::from_raw_handle(handle)
        })))
    }
    pub(crate) fn set(&self) {
        unsafe {
            SetEvent(self.as_raw_handle());
        }
    }
    pub(crate) fn reset(&self) {
        unsafe {
            ResetEvent(self.as_raw_handle());
        }
    }
    pub(crate) unsafe fn from_inherited(handle: RawHandle) -> io::Result<Self> {
        inherit(handle, false)?;
        Ok(Self(Arc::new(unsafe {
            OwnedHandle::from_raw_handle(handle)
        })))
    }
    pub(crate) fn wait(&self, timeout: Option<Duration>) -> io::Result<bool> {
        wait(self.as_raw_handle(), timeout)
    }
}

pub(crate) struct Notify(Event);
impl Drop for Notify {
    fn drop(&mut self) {
        self.0.set();
    }
}
pub(crate) fn notification() -> io::Result<(Event, Notify)> {
    let event = Event::new()?;
    Ok((event.clone(), Notify(event)))
}

pub(crate) fn command_pipes(
    command: &mut std::process::Command,
    cancelled: Event,
) -> io::Result<(Pipe, Pipe)> {
    use std::os::windows::process::CommandExt;
    let (input, writer) = pipe(false, true)?;
    let (reader, output) = pipe(true, false)?;
    command
        .stdin(std::process::Stdio::from(input))
        .stdout(std::process::Stdio::from(output))
        .creation_flags(CREATE_NO_WINDOW);
    Ok((
        Pipe::from(writer).with_cancel(cancelled),
        Pipe::from(reader),
    ))
}

pub(crate) trait ExitStatusExt {
    fn signal(&self) -> Option<i32>;
}
impl ExitStatusExt for std::process::ExitStatus {
    fn signal(&self) -> Option<i32> {
        None
    }
}

impl AsRawHandle for Event {
    fn as_raw_handle(&self) -> RawHandle {
        self.0.as_raw_handle()
    }
}

pub(crate) fn wait(handle: RawHandle, timeout: Option<Duration>) -> io::Result<bool> {
    let milliseconds = timeout.map_or(INFINITE, |t| t.as_millis().min(u32::MAX as u128 - 1) as u32);
    match unsafe { WaitForSingleObject(handle, milliseconds) } {
        WAIT_OBJECT_0 => Ok(true),
        WAIT_TIMEOUT => Ok(false),
        _ => Err(io::Error::last_os_error()),
    }
}

pub(crate) fn process_handle(pid: u32) -> io::Result<OwnedHandle> {
    let handle = unsafe { OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
    if handle.is_null() {
        return Err(io::Error::last_os_error());
    }
    Ok(unsafe { OwnedHandle::from_raw_handle(handle) })
}

/// An overlapped pipe endpoint. Every operation owns its buffer and OVERLAPPED
/// until completion, including after cancellation. Waiting never polls.
pub(crate) struct Pipe {
    handle: OwnedHandle,
    cancel: Option<Event>,
}

impl From<OwnedHandle> for Pipe {
    fn from(handle: OwnedHandle) -> Self {
        Self {
            handle,
            cancel: None,
        }
    }
}

impl Pipe {
    pub(crate) fn with_cancel(mut self, cancel: Event) -> Self {
        self.set_cancel(cancel);
        self
    }
    pub(crate) fn set_cancel(&mut self, cancel: Event) {
        self.cancel = Some(cancel);
    }
    pub(crate) fn clear_cancel(&mut self) {
        self.cancel = None;
    }
    pub(crate) fn available(&self) -> io::Result<usize> {
        available(self.as_raw_handle())
    }
    pub(crate) fn read_or_message(&self, bytes: &mut [u8]) -> io::Result<usize> {
        self.transfer(bytes.as_mut_ptr(), bytes.len(), true, true)
    }
    fn transfer(
        &self,
        bytes: *mut u8,
        length: usize,
        read: bool,
        message_wakeup: bool,
    ) -> io::Result<usize> {
        if length == 0 {
            return Ok(0);
        }
        // A continuously ready writer must not win every wait after retirement.
        // The owner explicitly removes cancellation for its bounded final drain.
        if let Some(cancel) = &self.cancel
            && cancel.wait(Some(Duration::ZERO))?
        {
            return Err(io::Error::new(
                io::ErrorKind::ConnectionAborted,
                "pipe operation cancelled",
            ));
        }
        let ready = Event::new()?;
        let mut overlapped: OVERLAPPED = unsafe { std::mem::zeroed() };
        overlapped.hEvent = ready.as_raw_handle();
        // SAFETY: bytes and overlapped stay live until GetOverlappedResult has
        // confirmed completion. This endpoint was opened with FILE_FLAG_OVERLAPPED.
        let started = unsafe {
            if read {
                ReadFile(
                    self.as_raw_handle(),
                    bytes,
                    length.min(u32::MAX as usize) as u32,
                    std::ptr::null_mut(),
                    &mut overlapped,
                )
            } else {
                WriteFile(
                    self.as_raw_handle(),
                    bytes,
                    length.min(u32::MAX as usize) as u32,
                    std::ptr::null_mut(),
                    &mut overlapped,
                )
            }
        };
        if started == 0 {
            let error = io::Error::last_os_error();
            if error.raw_os_error() == Some(ERROR_BROKEN_PIPE as i32) && read {
                return Ok(0);
            }
            if error.raw_os_error() != Some(ERROR_IO_PENDING as i32) {
                return Err(error);
            }
        }
        let handles = [
            ready.as_raw_handle(),
            self.cancel
                .as_ref()
                .map_or(std::ptr::null_mut(), AsRawHandle::as_raw_handle),
        ];
        let count = if self.cancel.is_some() { 2 } else { 1 };
        let result = unsafe {
            if message_wakeup {
                // Wake the interpreter owner without dispatching callbacks here:
                // it must first release the command-reader lock and retire this I/O.
                MsgWaitForMultipleObjectsEx(
                    count,
                    handles.as_ptr(),
                    INFINITE,
                    QS_ALLINPUT,
                    MWMO_INPUTAVAILABLE,
                )
            } else {
                WaitForMultipleObjects(count, handles.as_ptr(), 0, INFINITE)
            }
        };
        let cancelled = result != WAIT_OBJECT_0;
        if cancelled {
            unsafe {
                CancelIoEx(self.as_raw_handle(), &overlapped);
            }
        }
        let mut transferred = 0;
        let completed =
            unsafe { GetOverlappedResult(self.as_raw_handle(), &overlapped, &mut transferred, 1) };
        if completed != 0 {
            return Ok(transferred as usize);
        }
        let error = io::Error::last_os_error();
        if read && error.raw_os_error() == Some(ERROR_BROKEN_PIPE as i32) {
            return Ok(0);
        }
        if cancelled {
            return Err(io::Error::new(
                io::ErrorKind::ConnectionAborted,
                "pipe operation cancelled",
            ));
        }
        Err(error)
    }
}

impl AsRawHandle for Pipe {
    fn as_raw_handle(&self) -> RawHandle {
        self.handle.as_raw_handle()
    }
}
impl Read for Pipe {
    fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
        self.transfer(bytes.as_mut_ptr(), bytes.len(), true, false)
    }
}
impl Write for Pipe {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.transfer(bytes.as_ptr().cast_mut(), bytes.len(), false, false)
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

pub(crate) fn available(handle: RawHandle) -> io::Result<usize> {
    let mut bytes = 0;
    if unsafe {
        PeekNamedPipe(
            handle,
            std::ptr::null_mut(),
            0,
            std::ptr::null_mut(),
            &mut bytes,
            std::ptr::null_mut(),
        )
    } == 0
    {
        return Err(io::Error::last_os_error());
    }
    Ok(bytes as usize)
}

/// Both ends are opened before spawning. No name or listening endpoint remains
/// available to another process, and only explicitly marked handles inherit.
pub(crate) fn pipe(read_async: bool, write_async: bool) -> io::Result<(OwnedHandle, OwnedHandle)> {
    // The process default DACL also covers objects created inside MXC.
    static SEQUENCE: AtomicU64 = AtomicU64::new(0);
    let name: Vec<u16> = format!(
        r"\\.\pipe\mcp-console-{}-{}",
        std::process::id(),
        SEQUENCE.fetch_add(1, Ordering::Relaxed)
    )
    .encode_utf16()
    .chain(Some(0))
    .collect();
    let server = unsafe {
        CreateNamedPipeW(
            name.as_ptr(),
            PIPE_ACCESS_INBOUND
                | FILE_FLAG_FIRST_PIPE_INSTANCE
                | if read_async { FILE_FLAG_OVERLAPPED } else { 0 },
            PIPE_TYPE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
            1,
            65536,
            65536,
            0,
            std::ptr::null(),
        )
    };
    if server == INVALID_HANDLE_VALUE {
        return Err(io::Error::last_os_error());
    }
    let reader = unsafe { OwnedHandle::from_raw_handle(server) };
    let client = unsafe {
        CreateFileW(
            name.as_ptr(),
            GENERIC_WRITE,
            0,
            std::ptr::null(),
            OPEN_EXISTING,
            if write_async { FILE_FLAG_OVERLAPPED } else { 0 },
            std::ptr::null_mut(),
        )
    };
    if client == INVALID_HANDLE_VALUE {
        return Err(io::Error::last_os_error());
    }
    Ok((reader, unsafe { OwnedHandle::from_raw_handle(client) }))
}

pub(crate) fn inherit(handle: RawHandle, inherit: bool) -> io::Result<()> {
    if unsafe {
        SetHandleInformation(
            handle,
            HANDLE_FLAG_INHERIT,
            if inherit { HANDLE_FLAG_INHERIT } else { 0 },
        )
    } == 0
    {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}
