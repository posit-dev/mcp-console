//! Read only the launcher's descendants and their resident memory.
//! sysinfo retains full records for every host process, even for memory-only
//! refreshes. Those unrelated records dwarf the session's bounded output state.

use std::collections::HashMap;
use std::io;
use std::mem::{MaybeUninit, size_of, size_of_val};

use super::ProcessMemory;

pub(super) fn snapshot(root_pid: u32) -> io::Result<HashMap<u32, ProcessMemory>> {
    let mut processes = HashMap::new();
    let mut pending = vec![(root_pid, None)];
    let mut children = Vec::<libc::pid_t>::new();
    while let Some((pid, parent)) = pending.pop() {
        if processes.contains_key(&pid) {
            continue;
        }
        let mut information = MaybeUninit::<libc::proc_taskinfo>::uninit();
        // SAFETY: proc_pidinfo writes at most the supplied structure size.
        let bytes = unsafe {
            libc::proc_pidinfo(
                pid as libc::pid_t,
                libc::PROC_PIDTASKINFO,
                0,
                information.as_mut_ptr().cast(),
                size_of::<libc::proc_taskinfo>() as libc::c_int,
            )
        };
        if bytes as usize != size_of::<libc::proc_taskinfo>() {
            // Processes can exit or become unreadable during enumeration.
            continue;
        }
        // SAFETY: the successful call initialized the complete structure.
        let information = unsafe { information.assume_init() };
        processes.insert(
            pid,
            ProcessMemory {
                parent,
                resident_bytes: information.pti_resident_size,
            },
        );
        // A null buffer asks for a PID count, not a byte count. Keep one spare
        // slot so a filled buffer is distinguishable from a complete list.
        let count =
            unsafe { libc::proc_listchildpids(pid as libc::pid_t, std::ptr::null_mut(), 0) };
        if count < 0 {
            return Err(io::Error::last_os_error());
        }
        children.resize(count as usize + 1, 0);
        loop {
            let bytes = libc::c_int::try_from(size_of_val(children.as_slice()))
                .map_err(|_| io::Error::other("child process list is too large"))?;
            // SAFETY: the buffer holds exactly the supplied byte count.
            let count = unsafe {
                libc::proc_listchildpids(pid as libc::pid_t, children.as_mut_ptr().cast(), bytes)
            };
            if count < 0 {
                return Err(io::Error::last_os_error());
            }
            if (count as usize) < children.len() {
                pending.extend(
                    children[..count as usize]
                        .iter()
                        .map(|child| (*child as u32, Some(pid))),
                );
                break;
            }
            // The process forked while it was being sampled; enumerate again
            // with room for the new children rather than truncating the tree.
            children.resize(children.len() * 2, 0);
        }
    }
    Ok(processes)
}
