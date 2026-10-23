//! Cancellable inspection of an already selected candidate, inside the worker.

#[cfg(unix)]
use std::io::{Read, Write};
#[cfg(unix)]
use std::os::fd::AsRawFd;
#[cfg(unix)]
use std::os::unix::net::UnixStream;
use std::process::{Command, Stdio};
#[cfg(unix)]
use std::thread;
#[cfg(unix)]
use std::time::Duration;

const SOURCE: &str = include_str!("probe.py");

pub(super) enum Error {
    Interrupted,
    Message(String),
}

impl std::fmt::Display for Error {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let message = match self {
            Self::Interrupted => "KeyboardInterrupt",
            Self::Message(message) => message,
        };
        write!(formatter, "Python environment inspection failed: {message}")
    }
}

impl From<String> for Error {
    fn from(message: String) -> Self {
        Self::Message(message)
    }
}

#[cfg(unix)]
pub(super) fn inspect(executable: &str) -> Result<serde_json::Value, Error> {
    // Linux's restricted network policy denies socket sends, including notices.
    let (mut notices, notify) = std::io::pipe().map_err(|error| error.to_string())?;
    let (completion, completed) = UnixStream::pair().map_err(|error| error.to_string())?;
    let watch = crate::input_watch::InputWatch::new(completion.as_raw_fd())?;
    let (exited, exit_writer) = std::io::pipe().map_err(|error| error.to_string())?;
    let (output, output_writer) = std::io::pipe().map_err(|error| error.to_string())?;
    let mut child = Command::new(executable)
        .args(["-S", "-c", SOURCE])
        .stdin(Stdio::null())
        .stdout(
            output_writer
                .try_clone()
                .map_err(|error| error.to_string())?,
        )
        .stderr(output_writer)
        .spawn()
        .map_err(|error| error.to_string())?;
    // Every return, including failure to create an observer, reaps this child.
    struct Child<'a>(&'a mut std::process::Child);
    impl Drop for Child<'_> {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
    let child = Child(&mut child);
    let mut exit_notify = notify.try_clone().map_err(|error| error.to_string())?;
    let mut waiter =
        crate::process_exit::ChildExitWaiter::start_notifying(child.0.id(), move || {
            drop(exit_writer);
            let _ = exit_notify.write_all(b"E");
        })?;
    let input = thread::spawn(move || {
        if watch.wait(completion).is_err() {
            let mut notify = notify;
            let _ = notify.write_all(b"C");
        }
    });
    let reader = thread::spawn(move || {
        let mut bytes = Vec::new();
        crate::process_output::RelayOutput::new(output, exited)
            .read_to_end(&mut bytes)
            .map(|_| bytes)
    });
    let result = (|| {
        let mut wakeup = crate::worker::python_interrupt_wakeup()?;
        loop {
            if crate::worker::python_interrupt_pending() {
                return Err(Error::Interrupted);
            }
            let ready =
                crate::readiness::wait_for_io(notices.as_raw_fd(), libc::POLLIN, Some(&wakeup))
                    .map_err(|error| error.to_string())?;
            if ready.cancelled {
                let _ = wakeup.read(&mut [0u8; 64]);
                continue;
            }
            let mut notice = [0];
            notices
                .read_exact(&mut notice)
                .map_err(|error| error.to_string())?;
            if notice[0] == b'C' {
                crate::worker::mark_shutting_down();
                return Err(Error::Message("worker is shutting down".into()));
            }
            return Ok(());
        }
    })();
    if result.is_err()
        && let Err(error) = child.0.kill()
        && error.raw_os_error() != Some(libc::ESRCH)
    {
        return Err(super::environment::infrastructure(error.to_string()).into());
    }
    // Do not reap before the WNOWAIT observer has finished.
    if !waiter.wait(Duration::from_secs(60))? {
        return Err(super::environment::infrastructure(
            "Python probe exit observer did not finish".to_string(),
        )
        .into());
    }
    let status = child.0.wait().map_err(|error| error.to_string())?;
    // Both platform observers wake on peer closure; Linux deliberately ignores
    // queued data on this completion socket.
    drop(completed);
    input.join().map_err(|_| {
        super::environment::infrastructure("Python probe input observer panicked".to_string())
    })?;
    let output = reader
        .join()
        .map_err(|_| {
            super::environment::infrastructure("Python probe output reader panicked".to_string())
        })?
        .map_err(|error| error.to_string())?;
    result?;
    if !status.success() {
        return Err(format!("{status}: {}", String::from_utf8_lossy(&output)).into());
    }
    parse_output(&output)
}

fn parse_output(output: &[u8]) -> Result<serde_json::Value, Error> {
    let marker = b"\x1eMCP_CONSOLE_ENVIRONMENT\x1e";
    let start = output
        .windows(marker.len())
        .position(|bytes| bytes == marker)
        .ok_or_else(|| Error::Message("candidate omitted its environment record".into()))?
        + marker.len();
    let length = output[start..]
        .iter()
        .position(|byte| *byte == 0x1f)
        .ok_or_else(|| Error::Message("candidate environment record is incomplete".into()))?;
    serde_json::from_slice(&output[start..start + length])
        .map_err(|error| Error::Message(format!("invalid candidate environment: {error}")))
}

#[cfg(windows)]
pub(super) fn inspect(executable: &str) -> Result<serde_json::Value, Error> {
    use crate::resolver::process::ResolverProcess;
    let output = crate::worker::with_python_interrupt(|started| {
        let resolver = ResolverProcess::new();
        let mut command = Command::new(executable);
        command
            .args(["-S", "-c", SOURCE])
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        let invocation = resolver
            .spawn(&mut command, None)
            .map_err(|error| error.to_string())?;
        let path = std::path::Path::new(executable);
        let output = resolver.collect(invocation, path, "Python inspection", started)?;
        if !output.status.success() {
            return Err(format!(
                "{}: {}{}",
                output.status,
                String::from_utf8_lossy(&output.stdout),
                String::from_utf8_lossy(&output.stderr)
            ));
        }
        Ok(output.stdout)
    });
    if crate::worker::python_interrupt_pending() {
        return Err(Error::Interrupted);
    }
    parse_output(&output?)
}
