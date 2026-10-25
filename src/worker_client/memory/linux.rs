//! Read process memory without retaining procfs descriptors or changing limits.
//! sysinfo's Linux process cache raises RLIMIT_NOFILE and holds stat files open;
//! the server must preserve its caller's limit and keep sampling ownership local.

use std::collections::HashMap;
use std::fs;
use std::io;
use std::path::Path;

use super::ProcessMemory;

pub(super) fn snapshot() -> io::Result<HashMap<u32, ProcessMemory>> {
    // SAFETY: sysconf has no pointer arguments or process mutation.
    let page_size = unsafe { libc::sysconf(libc::_SC_PAGESIZE) };
    if page_size <= 0 {
        return Err(io::Error::other("system page size is unavailable"));
    }
    let mut processes = HashMap::new();
    // Top-level entries are processes. Enumerating task directories would count
    // each thread's shared address space again.
    for entry in fs::read_dir("/proc")?.flatten() {
        let Some(pid) = entry
            .file_name()
            .to_str()
            .and_then(|name| name.parse::<u32>().ok())
        else {
            continue;
        };
        // Processes can exit or become unreadable between enumeration and reads.
        if let Some(memory) = read_process(&entry.path(), page_size as u64) {
            processes.insert(pid, memory);
        }
    }
    Ok(processes)
}

fn read_process(directory: &Path, page_size: u64) -> Option<ProcessMemory> {
    let stat = fs::read(directory.join("stat")).ok()?;
    // comm can contain spaces, parentheses, or non-UTF-8 bytes. Only the tail
    // after its final ')' contains the ASCII state and parent-PID fields.
    let tail = &stat[stat.iter().rposition(|byte| *byte == b')')? + 1..];
    let parent = std::str::from_utf8(tail)
        .ok()?
        .split_whitespace()
        .nth(1)?
        .parse()
        .ok()?;
    let statm = fs::read_to_string(directory.join("statm")).ok()?;
    let resident_pages: u64 = statm.split_whitespace().nth(1)?.parse().ok()?;
    Some(ProcessMemory {
        parent: Some(parent),
        resident_bytes: resident_pages.saturating_mul(page_size),
    })
}
