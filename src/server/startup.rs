//! Share initial preparation attempts while the MCP connection owns cancellation.
#[cfg(unix)]
use std::mem::MaybeUninit;
#[cfg(unix)]
use std::os::fd::AsRawFd;
#[cfg(unix)]
use std::os::unix::net::UnixStream;
use std::sync::{Arc, Mutex};

const REGISTRATION_CLOSED: &str = "MCP connection closed during runtime preparation";

pub(super) struct Runtime {
    pub worker: crate::worker_client::Client,
    pub transcript: crate::transcript::Transcript,
}

pub(super) struct PreparedRuntime {
    pub configuration: crate::worker_client::ClientConfiguration,
    pub transcript: crate::transcript::Transcript,
}

#[derive(Clone)]
pub(super) struct Startup {
    runtime: Arc<Runtime>,
    cancellation: Arc<Mutex<Cancellation>>,
    input_closed: super::InputClosed,
    prelaunch: bool,
    initialize: Arc<Initializer>,
}

type Initializer = dyn Fn(
        &dyn Fn(crate::resolver::ResolverStopHandle) -> Result<(), String>,
        crate::process_output::Diagnostics,
    ) -> Result<PreparedRuntime, String>
    + Send
    + Sync;

#[derive(Default)]
struct Cancellation {
    started: bool,
    closed: bool,
    admission_cancelled: bool,
    registration_closed: bool,
    resolvers: Vec<crate::resolver::ResolverStopHandle>,
    retrying: bool,
}

impl Startup {
    pub fn new(
        input_closed: super::InputClosed,
        runtime: Arc<Runtime>,
        prelaunch: bool,
        initialize: impl Fn(
            &dyn Fn(crate::resolver::ResolverStopHandle) -> Result<(), String>,
            crate::process_output::Diagnostics,
        ) -> Result<PreparedRuntime, String>
        + Send
        + Sync
        + 'static,
    ) -> Self {
        runtime.worker.record_with(runtime.transcript.clone());
        Self {
            runtime,
            cancellation: Arc::new(Mutex::new(Cancellation::default())),
            input_closed,
            prelaunch,
            initialize: Arc::new(initialize),
        }
    }

    pub fn start(&self) {
        {
            let mut control = self.cancellation.lock().expect("startup cancellation lock");
            if control.started || control.closed {
                return;
            }
            control.started = true;
        }
        let control = self.cancellation.clone();
        let completion = control.clone();
        let worker = self.runtime.worker.clone();
        let recording = self.runtime.transcript.clone();
        let diagnostics = worker.diagnostics();
        let task_recording = recording.clone();
        let initialize_worker = worker.clone();
        let initialize = self.initialize.clone();
        let input_closed = self.input_closed.clone();
        let prelaunch = self.prelaunch;
        tokio::spawn(async move {
            let result = tokio::task::spawn_blocking(move || {
                observe_input(input_closed, || {
                    let (generation, startup) = {
                        let mut control = control.lock().expect("startup cancellation lock");
                        // Admission and connection closure share this owner. A refused
                        // attempt has no resolver to carry cancellation evidence.
                        if control.closed {
                            control.admission_cancelled = true;
                            return Err(REGISTRATION_CLOSED.into());
                        }
                        let generation = initialize_worker.admit()?;
                        let startup = initialize_worker.reserve_worker_startup(&generation)?;
                        (generation, startup)
                    };
                    let prepared = initialize(
                        &|resolver| {
                            let mut control = control.lock().expect("startup cancellation lock");
                            // Each preparation peer owns its retirement. A later
                            // inspection must not replace an earlier peer's receipt.
                            // Refused stages also retain their cleanup evidence.
                            control.resolvers.push(resolver.clone());
                            if control.closed {
                                control.registration_closed = true;
                                return Err(REGISTRATION_CLOSED.into());
                            }
                            initialize_worker.register_resolver_stop_handle(&generation, resolver)
                        },
                        diagnostics,
                    )?;
                    // Completed discovery no longer owns interrupt delivery.
                    initialize_worker.clear_resolver_stop_handle(&generation)?;
                    // Early control calls can launch once worker configuration
                    // is published. Record its metadata before that boundary.
                    task_recording.configure(prepared.transcript);
                    initialize_worker.configure(prepared.configuration);
                    if prelaunch {
                        initialize_worker.prelaunch(&generation);
                    }
                    drop(startup);
                    Ok(())
                })
            })
            .await
            .map_err(|error| format!("runtime preparation task failed: {error}"))
            .and_then(|result| result);
            // Recording belongs to the Console process, including later setup retries.
            if let Err(error) = &result {
                let recorder = recording.clone();
                let message = error.clone();
                if let Err(error) = tokio::task::spawn_blocking(move || {
                    recorder.startup_failed(&message);
                })
                .await
                {
                    recording.disable(format!("startup recording task failed: {error}"));
                }
            }
            let mut control = completion.lock().expect("startup cancellation lock");
            worker.finish_startup(result);
            control.retrying = false;
        });
    }

    /// Failed initial setup is retried under the connection's existing owner.
    /// Callers joining it must not restart its subsequently accepted worker.
    pub fn retry_failed(&self) -> Result<bool, String> {
        let mut control = self.cancellation.lock().expect("startup cancellation lock");
        if control.closed {
            return Err(REGISTRATION_CLOSED.into());
        }
        if control.retrying {
            return Ok(true);
        }
        if self.runtime.worker.is_configured() || !self.runtime.worker.startup_finished() {
            return Ok(false);
        }
        if control
            .resolvers
            .iter()
            .any(|resolver| !resolver.cleanup_confirmed() || !resolver.retirement_confirmed())
        {
            return Err("runtime discovery retry requires confirmed preparation cleanup".into());
        }
        if !self.runtime.worker.retry_failed_startup() {
            return Ok(false);
        }
        control.resolvers.clear();
        control.admission_cancelled = false;
        control.registration_closed = false;
        control.retrying = true;
        control.started = false;
        drop(control);
        self.start();
        Ok(true)
    }

    pub async fn ready(&self) -> Result<Arc<Runtime>, String> {
        self.runtime.worker.ready().await?;
        Ok(self.runtime.clone())
    }

    pub fn runtime(&self) -> Arc<Runtime> {
        self.runtime.clone()
    }

    pub async fn cancel(&self, deadline: std::time::Instant) -> Result<(), String> {
        {
            let mut control = self.cancellation.lock().expect("startup cancellation lock");
            control.closed = true;
            if !control.started {
                control.admission_cancelled = true;
                self.runtime
                    .worker
                    .finish_startup(Err(REGISTRATION_CLOSED.into()));
                return Ok(());
            }
            if self.runtime.worker.startup_finished() {
                return Ok(());
            }
        }
        self.runtime.worker.cancel_startup(deadline).await
    }

    pub fn finish_failed_preparation(&self, error: String) -> Result<(), String> {
        // A failed initializer has no installed configuration to own shutdown. Suppress only
        // connection refusal before admission or resolver cancellation with confirmed
        // cleanup. Completed setup errors must survive even without a resolver handle.
        let control = self.cancellation.lock().expect("startup cancellation lock");
        if control.closed
            && ((control.admission_cancelled && error == REGISTRATION_CLOSED)
                || (control.resolvers.iter().all(|resolver| {
                    resolver.cleanup_confirmed() && resolver.retirement_confirmed()
                }) && control.resolvers.last().is_some_and(|resolver| {
                    // A close/retirement failure appended by the initializer is
                    // independent of the registration refusal and must survive.
                    if control.registration_closed {
                        error == REGISTRATION_CLOSED
                    } else {
                        resolver.terminal_report().is_some_and(|report| {
                            report.confirmed
                                && report.control
                                    == Some(crate::resolver::ResolverControlOutcome::Cancelled)
                                && report.result == Err(error.clone())
                        })
                    }
                })))
        {
            Ok(())
        } else {
            Err(error)
        }
    }
}

/// Observe peer closure without consuming queued MCP messages, even when the
/// protocol task is blocked writing its initialization response.
#[cfg(unix)]
fn observe_input<T>(
    input_closed: super::InputClosed,
    initialize: impl FnOnce() -> Result<T, String>,
) -> Result<T, String> {
    let mut input = MaybeUninit::<libc::stat>::uninit();
    // SAFETY: `input` points to writable storage for the descriptor's status.
    if unsafe { libc::fstat(libc::STDIN_FILENO, input.as_mut_ptr()) } != 0 {
        return Err(format!(
            "failed to inspect MCP input: {}",
            std::io::Error::last_os_error()
        ));
    }
    // SAFETY: successful fstat initialized the status.
    let kind = unsafe { input.assume_init() }.st_mode & libc::S_IFMT;
    if !matches!(kind, libc::S_IFIFO | libc::S_IFSOCK) {
        return initialize();
    }
    std::thread::scope(|scope| {
        let (completed, completion) = UnixStream::pair()
            .map_err(|error| format!("failed to create MCP startup completion pipe: {error}"))?;
        let queue = crate::input_watch::InputWatch::new(completion.as_raw_fd())?;
        let watcher = scope.spawn(|| {
            let result = queue.wait(completion);
            if result.is_err() {
                input_closed.close();
            }
        });
        let result = initialize();
        drop(completed);
        watcher.join().expect("MCP startup watcher did not panic");
        result
    })
}

#[cfg(not(unix))]
fn observe_input<T>(
    _input_closed: super::InputClosed,
    initialize: impl FnOnce() -> Result<T, String>,
) -> Result<T, String> {
    initialize()
}
