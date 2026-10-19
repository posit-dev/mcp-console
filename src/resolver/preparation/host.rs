use std::ffi::OsStr;
use std::io::{self, BufReader};
use std::path::PathBuf;
use std::sync::{Mutex, mpsc};
use std::thread;

use super::{Discovery, Input, Mode, Operation, Output, Selections};
#[cfg(unix)]
use crate::process_io::{Io, duplicate};
use crate::resolver::{self, ResolverControlOutcome, ResolverStopHandle};

struct Context {
    mode: Mode,
    installation: Option<crate::local_runtime::RInstallation>,
    bootstrap: Option<resolver::ManagedRBootstrap>,
    r: Option<resolver::ManagedRResolverConfiguration>,
    python: resolver::ManagedPythonResolverConfiguration,
    rscript: Option<PathBuf>,
    managed_python: bool,
}

impl Context {
    fn discover(
        mode: Mode,
        installation: Option<crate::local_runtime::RInstallation>,
        on_started: &dyn Fn(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<(Self, Discovery), String> {
        if let Some(installation) = &installation {
            installation.validate()?;
            // Apply accepted native paths after the preparation launcher's
            // configured environment, before discovery captures any R choices.
            installation.configure_environment();
            unsafe { std::env::set_var("RHOME", &installation.home) };
        }
        let mode = if matches!(mode, Mode::Auto) {
            if crate::local_runtime::Selection::r_is_present() {
                Mode::R
            } else {
                Mode::PythonOnly
            }
        } else {
            mode
        };
        let configured_python = std::env::var_os("RETICULATE_PYTHON");
        let managed_python = !configured_python
            .as_deref()
            .is_some_and(|python| !python.is_empty() && python != OsStr::new("managed"));
        let configured_python = configured_python.and_then(|python| python.into_string().ok());
        if !matches!(mode, Mode::R) {
            let python =
                resolver::ManagedPythonResolverConfiguration::capture().without_r_bootstrap();
            let has_uv = python.has_uv();
            return Ok((
                Self {
                    mode,
                    installation,
                    bootstrap: None,
                    r: None,
                    python,
                    rscript: None,
                    managed_python,
                },
                Discovery {
                    managed: false,
                    selections: Selections {
                        r_home: None,
                        python: configured_python,
                    },
                    local_r_home_bytes: None,
                    local_has_uv: Some(has_uv),
                },
            ));
        }
        let python = resolver::ManagedPythonResolverConfiguration::capture();
        let (bootstrap, rscript) = resolver::discover(&python, on_started)?;
        let home = rscript
            .parent()
            .and_then(std::path::Path::parent)
            .ok_or("Rscript has no R home")?;
        #[cfg(windows)]
        let home = if home.file_name().is_some_and(|name| name == "bin") {
            home.parent().ok_or("Rscript has no R home")?
        } else {
            home
        };
        let discovery = Discovery {
            managed: bootstrap.is_some(),
            selections: Selections {
                r_home: Some(home.to_string_lossy().into_owned()),
                python: configured_python,
            },
            #[cfg(unix)]
            local_r_home_bytes: Some({
                use std::os::unix::ffi::OsStrExt;
                home.as_os_str().as_bytes().to_vec()
            }),
            #[cfg(windows)]
            local_r_home_bytes: None,
            local_has_uv: Some(python.has_uv()),
        };
        Ok((
            Self {
                mode,
                installation,
                bootstrap,
                r: None,
                python,
                rscript: Some(rscript),
                managed_python,
            },
            discovery,
        ))
    }

    fn execute(
        &mut self,
        operation: Operation,
        on_started: &dyn Fn(ResolverStopHandle) -> Result<(), String>,
        report: &dyn Fn(String) -> Result<(), String>,
    ) -> Result<serde_json::Value, String> {
        if let Some(installation) = &self.installation {
            installation.validate()?;
        }
        match operation {
            Operation::InspectR { executable } => serde_json::to_value(
                crate::local_runtime::RInstallation::inspect(&executable, on_started)?,
            )
            .map_err(|error| error.to_string()),
            Operation::Bootstrap => {
                let bootstrap = self
                    .bootstrap
                    .as_ref()
                    .ok_or("dynamic environment resolution is unavailable")?;
                // Capture choices once; a failed selected bootstrap remains an error.
                if self.r.is_none() {
                    self.r = Some(bootstrap.prepare(&mut self.python, on_started)?);
                }
                Ok(serde_json::Value::Null)
            }
            Operation::R {
                requirements,
                defaults,
            } => {
                let configuration = self.r.as_ref().ok_or("R bootstrap has not been prepared")?;
                let (r, _) = resolver::defaults::resolve(
                    "R package",
                    requirements,
                    defaults,
                    |requirements, started| {
                        resolver::resolve_r_with(configuration, requirements, started)
                    },
                    on_started,
                    report,
                )?;
                serde_json::to_value(r).map_err(|error| error.to_string())
            }
            Operation::ResolveRStandalone { requirements } => {
                let r = if let Some(configuration) = &self.r {
                    resolver::resolve_r_with(configuration, requirements, on_started)?
                } else {
                    resolver::resolve_r(requirements, on_started, |configuration| {
                        self.r = Some(configuration);
                    })?
                };
                self.rscript = Some(r.rscript().to_path_buf());
                serde_json::to_value(r).map_err(|error| error.to_string())
            }
            Operation::Python {
                requirements,
                defaults,
                r,
                selected_python,
            } => {
                let r =
                    r.map(|r| r.on_host(self.rscript.as_ref().expect("managed R has an Rscript")));
                self.prepare_uv(r.as_ref(), on_started)?;
                let (python, _) = resolver::defaults::resolve(
                    "Python package",
                    requirements.packages.clone(),
                    defaults,
                    |packages, started| {
                        let mut requirements = requirements.clone();
                        requirements.packages = packages;
                        resolver::resolve_python_manifest_for_host(
                            requirements,
                            &self.python,
                            selected_python.as_deref(),
                            started,
                        )
                    },
                    on_started,
                    report,
                )?;
                serde_json::to_value(python).map_err(|error| error.to_string())
            }
            Operation::PythonVersion { constraints, r } => {
                let r =
                    r.map(|r| r.on_host(self.rscript.as_ref().expect("managed R has an Rscript")));
                self.prepare_uv(r.as_ref(), on_started)?;
                let version =
                    resolver::resolve_python_version(constraints, &self.python, on_started)?;
                Ok(serde_json::Value::String(version))
            }
            Operation::InspectPython { executable } => {
                serde_json::to_value(crate::python::inspect_native(&executable, on_started)?)
                    .map_err(|error| error.to_string())
            }
            Operation::Uv { r } => {
                let r = r.on_host(self.rscript.as_ref().expect("managed R has an Rscript"));
                if let Some(uv) = self.python.selected_uv() {
                    return serde_json::to_value(uv).map_err(|error| error.to_string());
                }
                let configuration = self.r.as_ref().ok_or("R bootstrap has not been prepared")?;
                let uv = configuration.resolve_uv(&r, &self.python, on_started)?;
                self.python.set_resolved_uv(uv.clone());
                serde_json::to_value(uv).map_err(|error| error.to_string())
            }
            Operation::Duckdb {
                r,
                extensions,
                defaults,
            } => {
                let r = r.on_host(self.rscript.as_ref().expect("managed R has an Rscript"));
                let (_, accepted) = resolver::defaults::resolve(
                    "DuckDB extension",
                    extensions,
                    defaults,
                    |extensions, started| {
                        if extensions.is_empty() {
                            return Ok(());
                        }
                        resolver::resolve_duckdb_extensions(&r, &extensions, started)
                    },
                    on_started,
                    report,
                )?;
                serde_json::to_value(accepted).map_err(|error| error.to_string())
            }
            Operation::PythonDuckdbAvailable { python } => {
                if !matches!(self.mode, Mode::PythonOnly) || !self.managed_python {
                    return Err("Python-backed DuckDB preparation requires managed Python".into());
                }
                resolver::python_duckdb_available(&python, on_started).map(serde_json::Value::Bool)
            }
            Operation::DuckdbPython {
                python,
                extensions,
                defaults,
                extension_directory,
            } => {
                if !matches!(self.mode, Mode::PythonOnly) || !self.managed_python {
                    return Err("Python-backed DuckDB preparation requires managed Python".into());
                }
                let (_, accepted) = resolver::defaults::resolve(
                    "DuckDB extension",
                    extensions,
                    defaults,
                    |extensions, started| {
                        if extensions.is_empty() {
                            return Ok(());
                        }
                        resolver::resolve_python_duckdb_extensions(
                            &python,
                            &extensions,
                            &extension_directory,
                            started,
                        )
                    },
                    on_started,
                    report,
                )?;
                serde_json::to_value(accepted).map_err(|error| error.to_string())
            }
        }
    }

    fn prepare_uv(
        &mut self,
        r: Option<&resolver::ManagedR>,
        on_started: &dyn Fn(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<(), String> {
        if !self.managed_python {
            return Err("managed Python requirements are disabled because the session uses a user-selected Python environment".into());
        }
        if !self.python.has_uv() {
            let r = r.ok_or("Python sessions without R require `uv` on PATH; set python in .agents/console/config.yaml to use an existing environment")?;
            let configuration = self.r.as_ref().ok_or("R bootstrap has not been prepared")?;
            let uv = configuration.resolve_uv(r, &self.python, on_started)?;
            self.python.set_resolved_uv(uv);
        }
        Ok(())
    }
}

enum Event {
    Input(Result<Input, String>),
    Diagnostic { id: u64, text: String },
    Started(ResolverStopHandle, mpsc::Sender<()>),
    Completed(Output),
    OutputFailed(String),
}

fn perform<T>(
    id: u64,
    events: &mpsc::Sender<Event>,
    run: impl FnOnce(&dyn Fn(ResolverStopHandle) -> Result<(), String>) -> Result<T, String>,
    value: impl FnOnce(&T) -> serde_json::Value,
) -> Result<T, String> {
    let handles = Mutex::new(Vec::new());
    let result = run(&|handle| {
        handles
            .lock()
            .expect("preparation handles lock")
            .push(handle.clone());
        let (registered, response) = mpsc::channel();
        events
            .send(Event::Started(handle, registered))
            .map_err(|_| "preparation owner stopped")?;
        response
            .recv()
            .map_err(|_| "preparation owner stopped".to_string())
    });
    let handles = handles.into_inner().expect("preparation handles lock");
    let confirmed = handles.iter().all(ResolverStopHandle::cleanup_confirmed);
    // Only a stage with the operation's final result can supply its cause.
    // Earlier controlled stages and later acknowledgments cannot relabel an
    // independent failure, including one constructed after collection.
    let outcome = result.as_ref().map(|_| ());
    let control = handles.iter().find_map(|handle| {
        handle
            .terminal_report()
            .filter(|report| report.result.as_ref().map(|_| ()) == outcome)
            .and_then(|report| report.control)
    });
    events
        .send(Event::Completed(Output::Completed {
            id,
            result: Some(result.as_ref().map(value).map_err(Clone::clone)),
            control,
            confirmed,
        }))
        .map_err(|_| "preparation owner stopped")?;
    result
}

pub(super) fn run() -> Result<(), String> {
    #[cfg(unix)]
    let (input_cancelled, input_cancel) = io::pipe().map_err(|e| e.to_string())?;
    #[cfg(unix)]
    let mut input = BufReader::new(Io::new(duplicate(0)?, Some(input_cancelled))?);
    #[cfg(windows)]
    let mut input = BufReader::new(io::stdin());
    let first: Input = super::read_jsonl(&mut input)?;
    let Input::Open { mode, installation } = first else {
        return Err("expected resolver open".into());
    };
    let (events, received) = mpsc::channel();
    let (outgoing, output) = mpsc::channel::<Output>();
    #[cfg(unix)]
    let (output_cancelled, output_cancel) = io::pipe().map_err(|e| e.to_string())?;
    let input_events = events.clone();
    let input_task = thread::spawn(move || {
        // Preserve frames prefetched alongside Open when transferring input ownership.
        let mut input = input;
        let result = (|| {
            loop {
                input_events
                    .send(Event::Input(Ok(super::read_jsonl(&mut input)?)))
                    .map_err(|_| "preparation owner stopped")?;
            }
            #[allow(unreachable_code)]
            Ok::<(), String>(())
        })();
        if let Err(error) = result {
            let _ = input_events.send(Event::Input(Err(error)));
        }
    });
    let output_events = events.clone();
    let (output_drained, drained) = mpsc::channel();
    let output_task = thread::spawn(move || {
        let result = (|| {
            #[cfg(unix)]
            let mut writer = Io::new(duplicate(1)?, Some(output_cancelled))?;
            #[cfg(windows)]
            let mut writer = io::stdout();
            for message in output {
                message.write(&mut writer)?;
            }
            Ok::<(), String>(())
        })();
        if let Err(error) = result {
            let _ = output_events.send(Event::OutputFailed(error));
        }
        let _ = output_drained.send(());
    });
    outgoing
        .send(Output::Hello)
        .map_err(|_| "preparation output stopped")?;
    let (jobs, work) = mpsc::channel();
    let job_events = events.clone();
    let worker = thread::spawn(move || {
        let mut context = perform(
            0,
            &job_events,
            |started| Context::discover(mode, installation, started),
            |(_, discovery)| serde_json::to_value(discovery).expect("discovery serializes"),
        )?
        .0;
        for (id, operation) in work {
            let _ = perform(
                id,
                &job_events,
                |started| {
                    context.execute(operation, started, &|text| {
                        job_events
                            .send(Event::Diagnostic { id, text })
                            .map_err(|_| "preparation owner stopped".into())
                    })
                },
                Clone::clone,
            );
        }
        Ok::<(), String>(())
    });
    let mut active = Some(0);
    let mut last_id = 0;
    let mut handle: Option<ResolverStopHandle> = None;
    let mut pending: Option<ResolverControlOutcome> = None;
    let mut closing = false;
    let mut failure = None;
    let mut confirmed = true;
    while active.is_some() || !closing {
        let event = received
            .recv()
            .map_err(|_| "preparation owner events stopped")?;
        match event {
            Event::Input(Ok(Input::Run { id, operation }))
                if !closing && active.is_none() && id > last_id =>
            {
                active = Some(id);
                last_id = id;
                handle = None;
                pending = None;
                if jobs.send((id, *operation)).is_err() {
                    failure = Some("preparation executor stopped".into());
                    closing = true;
                    active = None;
                }
            }
            Event::Input(Ok(Input::Control { id, control })) if active == Some(id) => {
                pending.get_or_insert(control);
                let result = if let Some(handle) = &handle {
                    // The operation still owns this control between resolver
                    // stages, even after the previous handle has completed.
                    apply(handle, control).map(|_| true)
                } else {
                    Ok(true)
                };
                let _ = outgoing.send(Output::Controlled { id, result });
            }
            Event::Input(Ok(Input::Control { id, .. })) => {
                let _ = outgoing.send(Output::Controlled {
                    id,
                    result: Ok(false),
                });
            }
            Event::Started(started, registered) => {
                let _ = registered.send(());
                if closing {
                    let _ = started.stop();
                } else if let Some(control) = pending {
                    let _ = apply(&started, control);
                }
                handle = Some(started);
            }
            Event::Completed(message) => {
                if let Output::Completed {
                    confirmed: retired, ..
                } = &message
                {
                    confirmed &= retired;
                }
                active = None;
                handle = None;
                let _ = outgoing.send(message);
                if !confirmed {
                    failure = Some("preparation retirement is unconfirmed".into());
                    closing = true;
                }
            }
            Event::Diagnostic { id, text } => {
                let _ = outgoing.send(Output::Diagnostic { id, text });
            }
            Event::Input(Ok(Input::Close)) => closing = true,
            Event::Input(Err(error)) | Event::OutputFailed(error) => {
                failure = Some(error);
                closing = true;
            }
            _ => {
                failure = Some("unexpected resolver request".into());
                closing = true;
            }
        }
        if closing && let Some(handle) = &handle {
            let _ = handle.stop();
        }
    }
    drop(jobs);
    let _ = worker.join().map_err(|_| "preparation executor panicked")?;
    #[cfg(unix)]
    {
        drop(input_cancel);
        let _ = input_task.join();
    }
    // The Windows inherited standard handles are synchronous. These I/O
    // threads belong to this resolve process, which exits after all resolver
    // Jobs have retired. A caller retaining stdin cannot delay that exit.
    #[cfg(windows)]
    drop(input_task);
    if failure.is_none() && confirmed {
        let _ = outgoing.send(Output::Closed);
    }
    drop(outgoing);
    if failure.is_some() || !confirmed {
        if !confirmed {
            // Preserve the queued failure and control receipts before retiring I/O.
            // A peer that stops reading must still not hold an unconfirmed owner.
            let _ = drained.recv_timeout(std::time::Duration::from_secs(1));
        }
        #[cfg(unix)]
        drop(output_cancel);
    }
    #[cfg(unix)]
    let _ = output_task.join();
    #[cfg(windows)]
    if failure.is_none() {
        let _ = output_task.join();
    }
    failure.map_or(Ok(()), Err)
}

fn apply(handle: &ResolverStopHandle, control: ResolverControlOutcome) -> Result<bool, String> {
    match control {
        ResolverControlOutcome::Interrupted => handle.interrupt(),
        ResolverControlOutcome::Cancelled => handle.stop().map(|()| true),
    }
}
