//! Sample the owned launcher's process tree from outside the worker sandbox.
//! The callback requests retirement through the existing generation owner; the
//! sampler never signals discovered PIDs or claims descendant cleanup.

use std::collections::{HashMap, HashSet};
use std::sync::mpsc;
use std::thread;
use std::time::Duration;

#[cfg(not(target_os = "macos"))]
use sysinfo::{MemoryRefreshKind, System};
#[cfg(not(any(target_os = "linux", target_os = "macos")))]
use sysinfo::{ProcessRefreshKind, ProcessesToUpdate};

#[cfg(target_os = "linux")]
mod linux;
#[cfg(target_os = "macos")]
mod macos;

const CHECK_INTERVAL: Duration = Duration::from_secs(5);
const MIB: u64 = 1024 * 1024;

struct ProcessMemory {
    parent: Option<u32>,
    resident_bytes: u64,
}

pub(super) struct MemoryMonitor {
    cancellation: mpsc::Sender<()>,
    thread: Option<thread::JoinHandle<()>>,
}

impl MemoryMonitor {
    pub(super) fn start(root_pid: u32, on_limit: impl FnOnce(String) + Send + 'static) -> Self {
        let (cancellation, cancelled) = mpsc::channel();
        let thread = thread::spawn(move || {
            #[cfg(not(target_os = "macos"))]
            let mut system = System::new();
            loop {
                match cancelled.recv_timeout(CHECK_INTERVAL) {
                    Err(mpsc::RecvTimeoutError::Timeout) => {}
                    Ok(()) | Err(mpsc::RecvTimeoutError::Disconnected) => return,
                }
                #[cfg(not(target_os = "macos"))]
                let total = {
                    system.refresh_memory_specifics(MemoryRefreshKind::nothing().with_ram());
                    system.total_memory()
                };
                #[cfg(target_os = "macos")]
                let total = macos::total_memory();
                #[cfg(target_os = "linux")]
                let snapshot = linux::snapshot(root_pid, &cancelled);
                #[cfg(target_os = "macos")]
                let snapshot = macos::snapshot(root_pid, &cancelled);
                #[cfg(any(target_os = "linux", target_os = "macos"))]
                let processes = match snapshot {
                    Ok(Some(processes)) => processes,
                    Ok(None) => return,
                    Err(error) => {
                        on_limit(format!(
                            "Session stopped because the memory guard could not read process memory: {error}."
                        ));
                        return;
                    }
                };
                #[cfg(not(any(target_os = "linux", target_os = "macos")))]
                let processes: HashMap<u32, ProcessMemory> = {
                    system.refresh_processes_specifics(
                        ProcessesToUpdate::All,
                        true,
                        ProcessRefreshKind::nothing().with_memory().without_tasks(),
                    );
                    system
                        .processes()
                        .iter()
                        .map(|(pid, process)| {
                            (
                                pid.as_u32(),
                                ProcessMemory {
                                    parent: process.parent().map(|parent| parent.as_u32()),
                                    resident_bytes: process.memory(),
                                },
                            )
                        })
                        .collect()
                };
                if !processes.contains_key(&root_pid) {
                    return;
                }

                if total == 0 {
                    on_limit(
                        "Session stopped because the memory guard could not read host RAM."
                            .to_string(),
                    );
                    return;
                }
                let mut children: HashMap<u32, Vec<u32>> = HashMap::new();
                for (pid, process) in &processes {
                    if let Some(parent) = process.parent {
                        children.entry(parent).or_default().push(*pid);
                    }
                }
                let mut pending = vec![root_pid];
                let mut seen = HashSet::new();
                let mut used = 0_u64;
                while let Some(pid) = pending.pop() {
                    if !seen.insert(pid) {
                        continue;
                    }
                    if let Some(process) = processes.get(&pid) {
                        used = used.saturating_add(process.resident_bytes);
                    }
                    if let Some(descendants) = children.get(&pid) {
                        pending.extend(descendants);
                    }
                }
                if u128::from(used) * 10 <= u128::from(total) * 9 {
                    continue;
                }
                // Cancellation can arrive during the process snapshot.
                if cancelled.try_recv() != Err(mpsc::TryRecvError::Empty) {
                    return;
                }
                on_limit(format!(
                    "Session stopped by the memory guard: worker process tree used {} MiB, exceeding 90% of host RAM ({} MiB). Session objects were lost. Use a smaller dataset or process it in chunks before trying again.",
                    used / MIB,
                    total / MIB,
                ));
                return;
            }
        });
        Self {
            cancellation,
            thread: Some(thread),
        }
    }

    pub(super) fn cancel_and_join(&mut self) -> Result<(), String> {
        let _ = self.cancellation.send(());
        self.thread.take().map_or(Ok(()), |thread| {
            thread
                .join()
                .map_err(|_| "worker memory monitor thread panicked".to_string())
        })
    }
}

impl Drop for MemoryMonitor {
    fn drop(&mut self) {
        let _ = self.cancel_and_join();
    }
}
