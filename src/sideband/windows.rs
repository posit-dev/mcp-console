use crate::jsonl::JsonlBuffer;
use crate::windows::{Event, Pipe};
use serde::{Serialize, de::DeserializeOwned};
use std::io::{self, Read, Write};
use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
use std::sync::{Arc, Mutex};

const READ_HANDLE: &str = "MCP_CONSOLE_SIDEBAND_READ_HANDLE";
const WRITE_HANDLE: &str = "MCP_CONSOLE_SIDEBAND_WRITE_HANDLE";
const READ_CHUNK_SIZE: usize = 8 * 1024;

pub(crate) struct Reader {
    input: Pipe,
    buffer: JsonlBuffer,
}
#[derive(Clone)]
pub(crate) struct Writer(Arc<Mutex<Pipe>>);
pub(crate) struct ChildEndpoints(OwnedHandle, OwnedHandle);

pub(crate) fn bind(cancel: Event) -> io::Result<(Reader, Writer, ChildEndpoints)> {
    let (worker_reader, relay_writer) = crate::windows::pipe(true, true)?;
    let (relay_reader, worker_writer) = crate::windows::pipe(true, true)?;
    crate::windows::inherit(worker_reader.as_raw_handle(), true)?;
    crate::windows::inherit(worker_writer.as_raw_handle(), true)?;
    let reader = Pipe::from(relay_reader).with_cancel(cancel.clone());
    let writer = Pipe::from(relay_writer).with_cancel(cancel);
    Ok((
        Reader::new(reader),
        Writer(Arc::new(Mutex::new(writer))),
        ChildEndpoints(worker_reader, worker_writer),
    ))
}

pub(crate) fn connect_from_env() -> io::Result<(Reader, Writer)> {
    fn adopt(name: &str) -> io::Result<OwnedHandle> {
        let value = std::env::var(name).map_err(io::Error::other)?;
        let handle = value.parse::<usize>().map_err(io::Error::other)? as *mut std::ffi::c_void;
        if handle.is_null() || handle as isize == -1 {
            return Err(io::Error::other("invalid sideband handle"));
        }
        crate::windows::inherit(handle, false)?;
        unsafe {
            std::env::remove_var(name);
        }
        Ok(unsafe { OwnedHandle::from_raw_handle(handle) })
    }
    let read = std::env::var(READ_HANDLE).map_err(io::Error::other)?;
    if std::env::var(WRITE_HANDLE).as_deref() == Ok(&read) {
        return Err(io::Error::other("sideband handles must be distinct"));
    }
    Ok((
        Reader::new(Pipe::from(adopt(READ_HANDLE)?)),
        Writer(Arc::new(Mutex::new(Pipe::from(adopt(WRITE_HANDLE)?)))),
    ))
}

impl Reader {
    fn new(pipe: Pipe) -> Self {
        Self {
            input: pipe,
            buffer: JsonlBuffer::default(),
        }
    }

    pub(crate) fn receive<T: DeserializeOwned>(&mut self) -> io::Result<T> {
        self.receive_with_messages(false)
    }

    fn receive_with_messages<T: DeserializeOwned>(&mut self, messages: bool) -> io::Result<T> {
        loop {
            if let Some(message) = self
                .buffer
                .next_line()
                .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?
            {
                return Ok(message);
            }
            // Cancellation leaves the accumulated prefix in the shared buffer
            // for the next receive or the bounded retirement drain.
            let mut chunk = [0; READ_CHUNK_SIZE];
            let read = if messages {
                self.input.read_or_message(&mut chunk)
            } else {
                self.input.read(&mut chunk)
            };
            match read {
                Ok(0) if self.buffer.has_buffered_data() => {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "worker sideband closed midway through a frame",
                    ));
                }
                Ok(0) => {
                    return Err(io::Error::new(
                        io::ErrorKind::UnexpectedEof,
                        "worker sideband closed",
                    ));
                }
                Ok(length) => self.buffer.append(&chunk[..length]),
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(error) => return Err(error),
            }
        }
    }

    pub(crate) fn receive_or_wake<T: DeserializeOwned>(
        &mut self,
        wakeup: Event,
        messages: bool,
    ) -> io::Result<Option<T>> {
        // Reuse the overlapped read's cancellation wait. A wakeup preserves
        // the partial frame, and receive resumes it after idle processing.
        self.input.set_cancel(wakeup);
        let result = self.receive_with_messages(messages);
        self.input.clear_cancel();
        match result {
            Err(error) if error.kind() == io::ErrorKind::ConnectionAborted => Ok(None),
            result => result.map(Some),
        }
    }

    pub(crate) fn drain_available<T: DeserializeOwned>(
        &mut self,
        mut forward: impl FnMut(T) -> bool,
    ) -> io::Result<()> {
        let mut remaining = match self.input.available() {
            Ok(bytes) => bytes,
            Err(error) if error.kind() == io::ErrorKind::BrokenPipe => 0,
            Err(error) => return Err(error),
        };
        self.input.clear_cancel();
        // Snapshot the readable bytes so an inherited writer cannot extend
        // retirement. Already-buffered frames and prefixes are owned separately.
        loop {
            while let Some(message) = self
                .buffer
                .next_line()
                .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?
            {
                if !forward(message) {
                    return Ok(());
                }
            }
            if remaining == 0 {
                break; // An incomplete retiring tail is deliberately abandoned.
            }
            let mut chunk = [0; READ_CHUNK_SIZE];
            let length = remaining.min(chunk.len());
            match self.input.read(&mut chunk[..length]) {
                Ok(0) => break,
                Ok(length) => {
                    remaining -= length;
                    self.buffer.append(&chunk[..length]);
                }
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(error) => return Err(error),
            }
        }
        Ok(())
    }
}
impl Writer {
    pub(crate) fn send<T: Serialize>(&self, message: &T) -> io::Result<()> {
        let mut bytes = serde_json::to_vec(message)?;
        bytes.push(b'\n');
        self.0
            .lock()
            .map_err(|_| io::Error::other("sideband writer lock poisoned"))?
            .write_all(&bytes)
    }
}
impl ChildEndpoints {
    pub(crate) fn configure_process(&self, command: &mut std::process::Command) {
        command
            .env(READ_HANDLE, (self.0.as_raw_handle() as usize).to_string())
            .env(WRITE_HANDLE, (self.1.as_raw_handle() as usize).to_string());
    }
}

pub(crate) fn available_in_process() -> bool {
    true
}
