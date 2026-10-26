//! Read process memory without retaining procfs descriptors or changing limits.
//! sysinfo's Linux process cache raises RLIMIT_NOFILE and holds stat files open;
//! the server must preserve its caller's limit and keep sampling ownership local.

use std::collections::HashMap;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::mpsc;

use super::ProcessMemory;

/// None means retirement cancelled the scan; an empty map means the root vanished.
pub(super) fn snapshot(
    root_pid: u32,
    cancelled: &mpsc::Receiver<()>,
) -> io::Result<Option<HashMap<u32, ProcessMemory>>> {
    // SAFETY: sysconf has no pointer arguments or process mutation.
    let page_size = unsafe { libc::sysconf(libc::_SC_PAGESIZE) };
    if page_size <= 0 {
        return Err(io::Error::other("system page size is unavailable"));
    }
    let mut processes = HashMap::new();
    let root = PathBuf::from(format!("/proc/{root_pid}"));
    let Some(parent) = read_parent(&root) else {
        return Ok(Some(processes));
    };
    let Some(resident_bytes) = read_resident_bytes(&root, page_size as u64) else {
        return Ok(Some(processes));
    };
    processes.insert(
        root_pid,
        ProcessMemory {
            parent: Some(parent),
            resident_bytes,
        },
    );
    // Parent metadata remains available without CONFIG_PROC_CHILDREN. Walk
    // each candidate's ancestry without retaining unrelated process records.
    // A single enumeration also finishes when the owned tree keeps forking.
    // Top-level entries count each process, not threads.
    for entry in fs::read_dir("/proc")?.flatten() {
        if cancelled.try_recv() != Err(mpsc::TryRecvError::Empty) {
            return Ok(None);
        }
        let Some(pid) = entry
            .file_name()
            .to_str()
            .and_then(|name| name.parse::<u32>().ok())
        else {
            continue;
        };
        let mut ancestor = pid;
        let mut ancestry = Vec::new();
        loop {
            if cancelled.try_recv() != Err(mpsc::TryRecvError::Empty) {
                return Ok(None);
            }
            if processes.contains_key(&ancestor) {
                // Read only owned RSS, adding parents before their children so
                // an unreadable or disappearing ancestor leaves no orphaned
                // records in the sampled tree.
                for (pid, parent) in ancestry.into_iter().rev() {
                    if cancelled.try_recv() != Err(mpsc::TryRecvError::Empty) {
                        return Ok(None);
                    }
                    let directory = PathBuf::from(format!("/proc/{pid}"));
                    let Some(resident_bytes) = read_resident_bytes(&directory, page_size as u64)
                    else {
                        break;
                    };
                    processes.insert(
                        pid,
                        ProcessMemory {
                            parent: Some(parent),
                            resident_bytes,
                        },
                    );
                }
                break;
            }
            // PID churn can make separately observed parent links form a cycle.
            if ancestor == 0 || ancestry.iter().any(|(pid, _)| *pid == ancestor) {
                break;
            }
            let directory = PathBuf::from(format!("/proc/{ancestor}"));
            let Some(parent) = read_parent(&directory) else {
                break;
            };
            ancestry.push((ancestor, parent));
            ancestor = parent;
        }
    }
    Ok(Some(processes))
}

fn read_parent(directory: &Path) -> Option<u32> {
    let stat = fs::read(directory.join("stat")).ok()?;
    // comm can contain spaces, parentheses, or non-UTF-8 bytes. Only the tail
    // after its final ')' contains the ASCII state and parent-PID fields.
    let tail = &stat[stat.iter().rposition(|byte| *byte == b')')? + 1..];
    std::str::from_utf8(tail)
        .ok()?
        .split_whitespace()
        .nth(1)?
        .parse()
        .ok()
}

fn read_resident_bytes(directory: &Path, page_size: u64) -> Option<u64> {
    let statm = fs::read_to_string(directory.join("statm")).ok()?;
    let resident_pages: u64 = statm.split_whitespace().nth(1)?.parse().ok()?;
    Some(resident_pages.saturating_mul(page_size))
}
