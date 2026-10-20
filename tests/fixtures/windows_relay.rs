//! Native relay fixtures exercised through the public MCP server.
use std::ffi::c_void;
use std::io::{self, BufRead, Write};
use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
use std::os::windows::process::CommandExt;
use std::process::{Child, Command};
use std::time::Duration;

#[repr(C)]
struct ThreadEntry {
    size: u32,
    usage: u32,
    id: u32,
    process: u32,
    base_priority: i32,
    delta_priority: i32,
    flags: u32,
}

#[link(name = "kernel32")]
unsafe extern "system" {
    fn CreateToolhelp32Snapshot(flags: u32, pid: u32) -> *mut c_void;
    fn Thread32First(snapshot: *mut c_void, entry: *mut ThreadEntry) -> i32;
    fn Thread32Next(snapshot: *mut c_void, entry: *mut ThreadEntry) -> i32;
    fn OpenThread(access: u32, inherit: i32, id: u32) -> *mut c_void;
    fn ResumeThread(thread: *mut c_void) -> u32;
    fn CreateNamedPipeW(
        name: *const u16,
        mode: u32,
        pipe_mode: u32,
        instances: u32,
        output_size: u32,
        input_size: u32,
        timeout: u32,
        security: *const c_void,
    ) -> *mut c_void;
}

fn main() -> io::Result<()> {
    match std::env::var("TEST_RELAY_SCENARIO").unwrap().as_str() {
        "stall_shutdown" | "exit_shutdown" => {
            println!(r#"{{"kind":"worker_started","pid":{}}}"#, std::process::id());
            println!(r#"{{"kind":"ready"}}"#);
            io::stdout().flush()?;
            for line in io::stdin().lock().lines() {
                let line = line?;
                if line.contains(r#""kind":"shutdown""#) {
                    println!(r#"{{"kind":"shutdown_started"}}"#);
                    io::stdout().flush()?;
                    if std::env::var("TEST_RELAY_SCENARIO").unwrap() == "exit_shutdown" {
                        return Ok(());
                    }
                    std::thread::sleep(Duration::from_secs(60));
                } else if line.contains(r#""kind":"evaluate""#) {
                    println!(r#"{{"kind":"console_output","data":"42\n"}}"#);
                    println!(r#"{{"kind":"completed"}}"#);
                    io::stdout().flush()?;
                }
            }
            Ok(())
        }
        "block_sideband" => block_sideband(),
        scenario => panic!("unknown relay scenario: {scenario}"),
    }
}

fn block_sideband() -> io::Result<()> {
    // Reserve the relay's first private pipe name before it can execute. Its
    // FILE_FLAG_FIRST_PIPE_INSTANCE then fails with a complete native error.
    let mut child = ChildOwner(
        Command::new(std::env::var_os("TEST_CONSOLE_BINARY").unwrap())
            .arg("worker-relay")
            .args(std::env::args_os().skip(1))
            .creation_flags(0x08000004) // CREATE_NO_WINDOW | CREATE_SUSPENDED
            .spawn()?,
    );
    let name: Vec<u16> = format!(r"\\.\pipe\mcp-console-{}-0", child.0.id())
        .encode_utf16()
        .chain(Some(0))
        .collect();
    let pipe = unsafe { CreateNamedPipeW(name.as_ptr(), 1, 0, 1, 4096, 4096, 0, std::ptr::null()) };
    if pipe as isize == -1 {
        return Err(io::Error::last_os_error());
    }
    let _pipe = unsafe { OwnedHandle::from_raw_handle(pipe) };
    resume(child.0.id())?;
    child.0.wait()?;
    Ok(())
}

fn resume(pid: u32) -> io::Result<()> {
    let snapshot = unsafe { CreateToolhelp32Snapshot(4, 0) }; // TH32CS_SNAPTHREAD
    if snapshot as isize == -1 {
        return Err(io::Error::last_os_error());
    }
    let snapshot = unsafe { OwnedHandle::from_raw_handle(snapshot) };
    let mut entry: ThreadEntry = unsafe { std::mem::zeroed() };
    entry.size = std::mem::size_of::<ThreadEntry>() as u32;
    let mut found = unsafe { Thread32First(snapshot.as_raw_handle(), &mut entry) };
    while found != 0 {
        if entry.process == pid {
            let handle = unsafe { OpenThread(2, 0, entry.id) }; // THREAD_SUSPEND_RESUME
            if handle.is_null() {
                return Err(io::Error::last_os_error());
            }
            let thread = unsafe { OwnedHandle::from_raw_handle(handle) };
            if unsafe { ResumeThread(thread.as_raw_handle()) } == u32::MAX {
                return Err(io::Error::last_os_error());
            }
            return Ok(());
        }
        found = unsafe { Thread32Next(snapshot.as_raw_handle(), &mut entry) };
    }
    Err(io::Error::other("suspended fixture thread was not found"))
}

struct ChildOwner(Child);
impl Drop for ChildOwner {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
