//! Deterministic worker endpoints for Windows relay protocol regressions.
use std::ffi::c_void;
use std::io::{self, Read, Write};
use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};

mod windows_framing;
mod windows_gate;
use windows_gate::Gate;

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
    fn CreateEventW(a: *const c_void, manual: i32, initial: i32, name: *const u16) -> *mut c_void;
    fn ReadFile(h: *mut c_void, b: *mut u8, n: u32, count: *mut u32, o: *mut Overlapped) -> i32;
    fn WriteFile(h: *mut c_void, b: *const u8, n: u32, count: *mut u32, o: *mut Overlapped) -> i32;
    fn GetOverlappedResult(h: *mut c_void, o: *mut Overlapped, count: *mut u32, wait: i32) -> i32;
    fn FlushFileBuffers(handle: *mut c_void) -> i32;
    fn SetEvent(handle: *mut c_void) -> i32;
    fn WaitForSingleObject(handle: *mut c_void, timeout: u32) -> u32;
    fn SetHandleInformation(handle: *mut c_void, mask: u32, flags: u32) -> i32;
    fn CreateNamedPipeW(
        name: *const u16,
        access: u32,
        mode: u32,
        instances: u32,
        output: u32,
        input: u32,
        timeout: u32,
        security: *const c_void,
    ) -> *mut c_void;
    fn CreateFileW(
        name: *const u16,
        access: u32,
        sharing: u32,
        security: *const c_void,
        creation: u32,
        flags: u32,
        template: *mut c_void,
    ) -> *mut c_void;
}
unsafe extern "C" {
    fn _close(fd: i32) -> i32;
}

fn transfer(handle: *mut c_void, bytes: &mut [u8], write: bool) -> io::Result<usize> {
    let event = unsafe { CreateEventW(std::ptr::null(), 1, 0, std::ptr::null()) };
    if event.is_null() {
        return Err(io::Error::last_os_error());
    }
    let event = unsafe { OwnedHandle::from_raw_handle(event) };
    let mut overlapped: Overlapped = unsafe { std::mem::zeroed() };
    overlapped.event = event.as_raw_handle();
    let started = unsafe {
        if write {
            WriteFile(
                handle,
                bytes.as_ptr(),
                bytes.len() as u32,
                std::ptr::null_mut(),
                &mut overlapped,
            )
        } else {
            ReadFile(
                handle,
                bytes.as_mut_ptr(),
                bytes.len() as u32,
                std::ptr::null_mut(),
                &mut overlapped,
            )
        }
    };
    if started == 0 && io::Error::last_os_error().raw_os_error() != Some(997) {
        return Err(io::Error::last_os_error());
    }
    let mut count = 0;
    if unsafe { GetOverlappedResult(handle, &mut overlapped, &mut count, 1) } == 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(count as usize)
}

fn send(handle: *mut c_void, text: &str) -> io::Result<()> {
    send_bytes(handle, text.as_bytes())
}

fn send_bytes(handle: *mut c_void, bytes: &[u8]) -> io::Result<()> {
    let mut bytes = bytes.to_vec();
    let mut offset = 0;
    while offset < bytes.len() {
        offset += transfer(handle, &mut bytes[offset..], true)?;
    }
    Ok(())
}

fn main() -> io::Result<()> {
    let handle = |name| std::env::var(name).unwrap().parse::<usize>().unwrap() as *mut c_void;
    let read = handle("MCP_CONSOLE_SIDEBAND_READ_HANDLE");
    let write = handle("MCP_CONSOLE_SIDEBAND_WRITE_HANDLE");
    let scenario = std::env::var("TEST_WORKER_SCENARIO").unwrap();
    if scenario == "framing_holder" {
        return windows_framing::hold(write);
    }
    let mut ready = Gate::connect(std::env::var("TEST_WORKER_READY").unwrap())?;
    writeln!(ready, "{}", std::process::id())?;
    // Keep the pipe connected until the host has pinned our identity. Closing
    // a client before ConnectNamedPipe can otherwise discard the rendezvous.
    let mut release = [0];
    ready.read_exact(&mut release)?;
    assert_eq!(release, [b'1']);
    if scenario.starts_with("framing_") {
        return windows_framing::run(&scenario, read, write, ready);
    }
    if scenario.starts_with("retirement_") {
        return retirement(&scenario, read, write, ready);
    }
    drop(ready);
    if scenario == "closed_stdin" {
        assert_eq!(unsafe { _close(0) }, 0);
    } else if scenario == "commands" {
        std::thread::spawn(|| {
            let mut byte = [0];
            if io::stdin().read_exact(&mut byte).is_ok() {
                std::fs::write(std::env::var("TEST_DISPATCHED").unwrap(), b"stdin").unwrap();
            }
        });
    }
    send(write, "{\"kind\":\"ready\"}\n")?;
    loop {
        let mut command = Vec::new();
        loop {
            let mut byte = [0];
            transfer(read, &mut byte, false)?;
            command.push(byte[0]);
            if byte[0] == b'\n' {
                break;
            }
        }
        let command = String::from_utf8(command).unwrap();
        if scenario == "forwarding" {
            let marker = std::path::PathBuf::from(std::env::var("TEST_DISPATCHED").unwrap());
            std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(&marker)?
                .write_all(command.as_bytes())?;
            if command.trim() == "{\"kind\":\"shutdown\"}" {
                let mut input = Vec::new();
                io::stdin().read_to_end(&mut input)?;
                std::fs::write(marker.with_extension("stdin"), input)?;
                return Ok(());
            }
            send(write, "{\"kind\":\"completed\"}\n")?;
            continue;
        }
        if command.contains("shutdown") {
            return Ok(());
        }
        match scenario.as_str() {
            "commands" => std::fs::write(std::env::var("TEST_DISPATCHED").unwrap(), command)?,
            "invalid_sideband" => send(write, "{\"kind\":\"broken\"}\n")?,
            "exit_tail" | "exit_invalid" => {
                let mut frames = String::new();
                for index in 0..1024 {
                    frames.push_str(&format!(
                        "{{\"kind\":\"console_output\",\"data\":\"{index:04}\"}}\n"
                    ));
                }
                if scenario == "exit_invalid" {
                    frames.push_str("{\"kind\":\"broken\"}\n");
                } else {
                    frames.push_str(
                        "{\"kind\":\"image\",\"data\":\"eA==\",\"mime_type\":\"image/png\"}\n",
                    );
                    frames.push_str("{\"kind\":\"completed\"}\n");
                }
                send(write, &frames)?;
                return Ok(());
            }
            _ => {}
        }
    }
}

fn retirement(
    scenario: &str,
    read: *mut c_void,
    write: *mut c_void,
    ready: Gate,
) -> io::Result<()> {
    let marker = std::path::PathBuf::from(std::env::var("TEST_DISPATCHED").unwrap());
    let sideband_eof = scenario == "retirement_sideband";
    let mut stdin_ready = ready.try_clone()?;
    let stdin_marker = marker.with_extension("stdin");
    let (stdin_closed_tx, stdin_closed_rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut bytes = Vec::new();
        io::stdin().read_to_end(&mut bytes).unwrap();
        std::fs::write(stdin_marker, bytes).unwrap();
        if sideband_eof {
            writeln!(stdin_ready, "stdin closed").unwrap();
            stdin_closed_tx.send(()).unwrap();
        }
    });
    let mut ready = ready;
    let mut writer = Some(unsafe { OwnedHandle::from_raw_handle(write) });
    send(write, "{\"kind\":\"ready\"}\n")?;
    let mut journal = std::fs::File::create(marker)?;
    loop {
        let mut command = Vec::new();
        loop {
            let mut byte = [0];
            if transfer(read, &mut byte, false)? == 0 {
                std::thread::park();
            }
            command.push(byte[0]);
            if byte[0] == b'\n' {
                break;
            }
        }
        journal.write_all(&command)?;
        let command = String::from_utf8(command).unwrap();
        if scenario == "retirement_backpressure" && command.contains("evaluate") {
            // Keep the outer writer inside one frame while the sideband reader
            // waits for ordinary event capacity. Neither endpoint closes.
            let data = "x".repeat(1024 * 1024);
            send(
                write,
                &format!("{{\"kind\":\"console_output\",\"data\":\"{data}\"}}\n"),
            )?;
            loop {
                send(
                    write,
                    "{\"kind\":\"console_output\",\"data\":\"blocked\"}\n",
                )?;
            }
        } else if sideband_eof && command.contains("evaluate") {
            writer.take();
        } else if command.trim() == "{\"kind\":\"shutdown\"}" {
            if sideband_eof {
                // Retirement closes stdin and sends shutdown concurrently.
                // Publish the EOF checkpoint before acknowledging shutdown.
                stdin_closed_rx.recv().unwrap();
            }
            writeln!(ready, "shutdown")?;
        }
    }
}
