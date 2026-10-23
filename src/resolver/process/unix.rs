//! Unix resolvers own a process group and retain the leader until reaping.
//! Group signalling retires trusted host preparation; it is not a sandbox.

use std::io;
use std::os::unix::process::CommandExt as _;
use std::path::Path;
pub(crate) use std::process::Child;
use std::process::{Command, ExitStatus};

use super::{ResolverInterrupt, settle_observation};
use crate::process_exit::ChildExitWaiter;

pub(super) type Cancel = io::PipeWriter;

pub(super) fn prepare_io(command: &mut Command, input: bool) -> io::Result<super::Endpoints> {
    use crate::process_io::Io;
    use crate::process_output::RelayOutput;
    use std::process::Stdio;
    let (input_cancelled, input_cancel) = io::pipe()?;
    let (output_cancelled, output_cancel) = io::pipe()?;
    let (stdout, stdout_child) = io::pipe()?;
    let (stderr, stderr_child) = io::pipe()?;
    let writer = if input {
        let (stdin_child, stdin) = io::pipe()?;
        let stdin = Io::new(stdin, Some(input_cancelled)).map_err(io::Error::other)?;
        command.stdin(Stdio::from(stdin_child));
        Some(Box::new(stdin) as Box<dyn io::Write + Send>)
    } else {
        command.stdin(Stdio::null());
        None
    };
    let stdout = RelayOutput::new(stdout, output_cancelled.try_clone()?);
    let stderr = RelayOutput::new(stderr, output_cancelled);
    command
        .stdout(Stdio::from(stdout_child))
        .stderr(Stdio::from(stderr_child));
    Ok(super::Endpoints {
        input: writer,
        stdout: Box::new(stdout),
        stderr: Box::new(stderr),
        input_cancel,
        output_cancel,
    })
}

pub(crate) fn resolver_command(program: &Path) -> Command {
    let mut command = Command::new(program);
    command.process_group(0);
    // SAFETY: the closure calls only libc signal functions after fork and
    // before exec. Resolver programs must not inherit an ignored or blocked
    // SIGINT from the MCP host.
    unsafe {
        command.pre_exec(|| {
            if libc::signal(libc::SIGINT, libc::SIG_DFL) == libc::SIG_ERR {
                return Err(io::Error::last_os_error());
            }
            let mut signals = std::mem::zeroed();
            if libc::sigemptyset(&mut signals) != 0
                || libc::sigaddset(&mut signals, libc::SIGINT) != 0
                || libc::sigprocmask(libc::SIG_UNBLOCK, &signals, std::ptr::null_mut()) != 0
            {
                return Err(io::Error::last_os_error());
            }
            Ok(())
        });
    }
    command
}

pub(crate) fn spawn_resolver(command: &mut Command) -> io::Result<Child> {
    command.spawn()
}

pub(super) fn interrupt_resolver(child: &mut Child) -> io::Result<ResolverInterrupt> {
    let pid = child.id();
    // A liveness probe followed by killpg races natural exit; Linux accepts
    // signaling an unreaped zombie. Confirm suspension or exit before SIGINT
    // so a still-live leader cannot exit independently in that gap.
    if !stop_before_interrupt(pid)? {
        return Ok(ResolverInterrupt::AlreadyExited);
    }
    let interrupted = signal_interrupt(pid);
    // Resume even if signaling failed. Collection owns subsequent retirement.
    // SAFETY: the unreaped direct child retains `pid` throughout this call.
    let resumed = unsafe { libc::kill(pid as libc::pid_t, libc::SIGCONT) };
    let resume_error = (resumed < 0).then(io::Error::last_os_error);
    let result = interrupted?;
    if let Some(error) = resume_error {
        return Err(error);
    }
    Ok(result)
}

fn stop_before_interrupt(pid: u32) -> io::Result<bool> {
    // Stop only the leader. Its observed stop prevents exit until SIGCONT;
    // WNOWAIT leaves terminal status and process identity with the exit owner.
    // SAFETY: the unreaped direct child retains `pid` throughout this call.
    if unsafe { libc::kill(pid as libc::pid_t, libc::SIGSTOP) } < 0 {
        let error = io::Error::last_os_error();
        if matches!(error.raw_os_error(), Some(libc::EPERM) | Some(libc::ESRCH))
            && crate::process_exit::direct_child_has_exited(pid)?
        {
            return Ok(false);
        }
        return Err(error);
    }
    loop {
        let mut information = std::mem::MaybeUninit::<libc::siginfo_t>::zeroed();
        // SAFETY: `information` is writable and `pid` is our unreaped child.
        let result = unsafe {
            libc::waitid(
                libc::P_PID,
                pid as libc::id_t,
                information.as_mut_ptr(),
                libc::WSTOPPED | libc::WEXITED | libc::WNOWAIT,
            )
        };
        if result < 0 {
            let error = io::Error::last_os_error();
            if error.kind() == io::ErrorKind::Interrupted {
                continue;
            }
            return Err(error);
        }
        // SAFETY: successful blocking waitid initialized the status.
        let information = unsafe { information.assume_init() };
        return match information.si_code {
            libc::CLD_STOPPED => Ok(true),
            libc::CLD_EXITED | libc::CLD_KILLED | libc::CLD_DUMPED => Ok(false),
            code => Err(io::Error::other(format!(
                "unexpected resolver status before interrupt: {code}"
            ))),
        };
    }
}

fn signal_interrupt(pid: u32) -> io::Result<ResolverInterrupt> {
    // SAFETY: `process_group(0)` made the resolver PID its process-group ID.
    if unsafe { libc::killpg(pid as libc::pid_t, libc::SIGINT) } == 0 {
        return Ok(ResolverInterrupt::Signaled);
    }
    let error = io::Error::last_os_error();
    if matches!(error.raw_os_error(), Some(libc::EPERM) | Some(libc::ESRCH)) {
        // Keep an exited leader unreaped so its watcher remains authoritative
        // and this resolver PID cannot be reused before normal cleanup.
        if crate::process_exit::direct_child_has_exited(pid)? {
            return Ok(ResolverInterrupt::AlreadyExited);
        }
    }
    Err(error)
}

pub(super) fn stop_resolver(
    child: &mut Child,
    program: &Path,
    kind: &str,
    exit: Option<&mut ChildExitWaiter>,
) -> Result<ExitStatus, String> {
    // SAFETY: `process_group(0)` made the resolver PID its process-group ID.
    let result = unsafe { libc::killpg(child.id() as libc::pid_t, libc::SIGKILL) };
    if result < 0 {
        let kill_error = io::Error::last_os_error();
        match crate::process_exit::direct_child_has_exited(child.id()) {
            // macOS reports EPERM when only the unreaped group leader remains.
            // ESRCH likewise means there is no remaining group to stop.
            Ok(true)
                if matches!(
                    kill_error.raw_os_error(),
                    Some(libc::EPERM) | Some(libc::ESRCH)
                ) => {}
            Ok(_) => {
                return Err(format!(
                    "failed to stop {kind} resolver `{}`: {kill_error}",
                    program.display()
                ));
            }
            Err(wait_error) => {
                return Err(format!(
                    "failed to stop {kind} resolver `{}`: {kill_error}; additionally failed to read its status: {wait_error}",
                    program.display()
                ));
            }
        }
    }
    settle_observation(exit);
    child.wait().map_err(|error| {
        format!(
            "failed to reap {kind} resolver `{}`: {error}",
            program.display()
        )
    })
}
