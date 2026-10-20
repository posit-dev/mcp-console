use std::ffi::OsString;
use std::path::PathBuf;
use std::sync::Mutex;
use std::sync::atomic::AtomicBool;

use super::{Environment, PythonEnvironment, WorkerState, platform};

pub(crate) struct ClientConfiguration {
    pub(super) runtime: platform::WorkerRuntime,
    pub(super) program: PathBuf,
    pub(super) arguments: Vec<OsString>,
    pub(super) relay: Option<PathBuf>,
    pub(super) no_sandbox: bool,
    pub(super) sandbox_settings: crate::settings::SandboxSettings,
    pub(super) startup_source: Option<crate::settings::startup::Startup>,
    pub(super) startup_permitted: AtomicBool,
    pub(super) resolver_settings: crate::settings::SandboxSettings,
    pub(super) duckdb_extension_directory: Option<PathBuf>,
    pub(super) worker: Mutex<WorkerState>,
    pub(super) environment: Option<Mutex<Environment>>,
    pub(super) requirements_snapshot: Mutex<serde_json::Value>,
    pub(super) runtime_r_requirements: Vec<String>,
    pub(super) dynamic_resolution: bool,
    /// Immutable admission snapshot; preparation may hold the environment lock.
    pub(super) r_resolution: crate::settings::Resolution,
    pub(super) python_only: bool,
    pub(super) python_preparation: bool,
    pub(super) resolver_preparation: Mutex<Option<crate::resolver::preparation::Preparation>>,
    /// Local built-in workers retain the controller selection across generations.
    pub(super) languages: Option<crate::cell::Languages>,
    /// A default worker may be replaced without discarding user runtime state.
    pub(super) unused_default: AtomicBool,
}

#[derive(Clone)]
pub(super) enum RResolver {
    Discover,
    Pending(BuiltinSetup),
    Configured(crate::resolver::preparation::Preparation),
    Disabled,
}

#[derive(Clone)]
pub(super) struct BuiltinSetup {
    pub(super) bootstrap: crate::resolver::preparation::Preparation,
    pub(super) python_resolver: crate::resolver::execution::PythonConfiguration,
    pub(super) configured_python: Option<OsString>,
}

impl ClientConfiguration {
    pub(crate) fn with_startup(
        mut self,
        startup: Option<crate::settings::startup::Startup>,
    ) -> Self {
        self.startup_source = startup;
        self
    }

    pub(crate) fn with_resolver_settings(
        mut self,
        settings: crate::settings::SandboxSettings,
    ) -> Result<Self, String> {
        self.duckdb_extension_directory =
            crate::resolver::cache::duckdb_extension_directory(&settings)?;
        self.resolver_settings = settings;
        Ok(self)
    }

    pub(crate) fn new(
        program: PathBuf,
        relay: Option<PathBuf>,
        no_sandbox: bool,
        sandbox_settings: crate::settings::SandboxSettings,
    ) -> Self {
        Self::with_arguments(
            program,
            Vec::new(),
            relay,
            no_sandbox,
            sandbox_settings,
            Environment {
                startup: Default::default(),
                local_runtime: None,
                custom_worker: true,
                duckdb_extensions: Default::default(),
                duckdb_r_targets: Vec::new(),
                python: None,
                r: None,
                r_resolver: RResolver::Discover,
            },
        )
    }

    pub(crate) fn builtin(
        no_sandbox: bool,
        sandbox_settings: crate::settings::SandboxSettings,
        python: Option<crate::settings::PythonChoice>,
        r_settings: crate::settings::R,
        resolver_settings: crate::settings::SandboxSettings,
        diagnostics: crate::process_output::Diagnostics,
        on_started: &dyn Fn(crate::resolver::ResolverStopHandle) -> Result<(), String>,
    ) -> Result<Self, String> {
        #[cfg(windows)]
        let _ = &diagnostics;
        let duckdb_extension_directory =
            crate::resolver::cache::duckdb_extension_directory(&resolver_settings)?;
        let languages = crate::cell::Languages::from_environment()?;
        let legacy_python = python.is_none();
        let mut startup = super::environment::StartupRequirements {
            r: r_settings.packages.clone(),
            r_resolution: r_settings.resolution,
            python: python
                .as_ref()
                .map(|python| python.managed.clone())
                .unwrap_or_default(),
            ..Default::default()
        };
        let explicit_managed = python
            .as_ref()
            .is_some_and(|python| python.executable.is_none());
        let inspect_explicit = python.is_some() || cfg!(windows);
        let configured_python = python
            .map(|python| {
                python
                    .executable
                    .map(PathBuf::into_os_string)
                    .unwrap_or_else(|| "managed".into())
            })
            .or_else(|| std::env::var_os("RETICULATE_PYTHON"));
        let program = std::env::current_exe()
            .map_err(|error| format!("failed to locate the R worker executable: {error}"))?;
        // Windows discovery executes inside the same boundary as preparation;
        // runtime write/network grants must not widen resolver permissions.
        let inspection_settings = if cfg!(windows) {
            &resolver_settings
        } else {
            &sandbox_settings
        };
        let installation = r_settings
            .executable
            .as_ref()
            .map(|executable| {
                crate::resolver::preparation::Preparation::inspect::<
                    crate::local_runtime::RInstallation,
                >(
                    crate::resolver::preparation::Operation::InspectR {
                        executable: executable.clone(),
                    },
                    inspection_settings.clone(),
                    no_sandbox,
                    diagnostics.clone(),
                    on_started,
                )
            })
            .transpose()?;
        let local_runtime;
        #[cfg(any(unix, windows))]
        let resolver_preparation;
        #[cfg(any(unix, windows))]
        let (preparation, discovery) = crate::resolver::preparation::Preparation::open_local(
            crate::resolver::preparation::Mode::Auto,
            resolver_settings.clone(),
            no_sandbox,
            configured_python.as_deref(),
            installation.clone(),
            diagnostics.clone(),
            on_started,
        )?;
        if discovery.selections.r_home.is_some()
            && !discovery.managed
            && matches!(
                r_settings.resolution,
                crate::settings::Resolution::Explicit | crate::settings::Resolution::Fixed
            )
        {
            let error = format!(
                "r.resolution={} requires R startup preparation support",
                r_settings.resolution.name()
            );
            preparation
                .close()
                .map_err(|cleanup| format!("{error}; {cleanup}"))?;
            return Err(error);
        }
        if startup
            .r
            .as_ref()
            .is_some_and(|packages| !packages.is_empty())
            && (discovery.selections.r_home.is_none() || !discovery.managed)
        {
            let error =
                "r.packages: requested startup packages require R and R preparation support";
            preparation
                .close()
                .map_err(|cleanup| format!("{error}; {cleanup}"))?;
            return Err(error.into());
        }
        #[cfg(any(unix, windows))]
        let r_home = {
            #[cfg(unix)]
            {
                use std::os::unix::ffi::OsStringExt;
                if discovery.selections.r_home.is_some() {
                    Some(PathBuf::from(OsString::from_vec(
                        discovery
                            .local_r_home_bytes
                            .ok_or("local R discovery has no R home")?,
                    )))
                } else {
                    None
                }
            }
            #[cfg(windows)]
            {
                discovery.selections.r_home.clone().map(PathBuf::from)
            }
        };
        let python_only = r_home.is_none();
        // Disabled R keeps native startup and SQL routing, while managed Python
        // uses the existing standalone uv path without R bootstrap.
        #[cfg(any(unix, windows))]
        let (r, duckdb_extensions, python, r_resolver) = if python_only
            || (!r_settings.resolution.prepares_startup()
                && PythonEnvironment::uses_managed(configured_python.as_deref())
                && (explicit_managed || discovery.local_has_uv == Some(true)))
        {
            let resolver = crate::resolver::execution::PythonConfiguration {
                preparation: preparation.clone(),
                has_uv: discovery
                    .local_has_uv
                    .ok_or("local Python discovery has no uv result")?,
            };
            let selected = crate::local_runtime::Selection::python(
                configured_python.clone(),
                startup.python.manifest(python_only),
                if startup.python.packages.is_none() {
                    startup.python.manifest(python_only).packages
                } else {
                    Vec::new()
                },
                &resolver,
                duckdb_extension_directory.clone(),
                |executable, started| {
                    crate::resolver::preparation::Preparation::inspect(
                        crate::resolver::preparation::Operation::InspectPython {
                            executable: executable.to_owned(),
                        },
                        inspection_settings.clone(),
                        no_sandbox,
                        diagnostics.clone(),
                        started,
                    )
                },
                on_started,
            )
            .and_then(|(selection, managed)| {
                let extensions = if python_only {
                    selection.prepare_default_duckdb_extensions(
                        managed.as_ref(),
                        &resolver,
                        on_started,
                    )?
                } else {
                    Default::default()
                };
                Ok((selection, managed, extensions))
            });
            let (mut selection, managed, extensions) = match selected {
                Ok(selection) => selection,
                Err(error) => {
                    preparation
                        .close()
                        .map_err(|cleanup| format!("{error}; {cleanup}"))?;
                    return Err(error);
                }
            };
            selection.r_settings = r_settings;
            selection.r_home = r_home;
            selection.installation = installation;
            // Reset retries built-in defaults after a skipped DuckDB package.
            // Configured package lists without a provider keep extensions absent.
            if extensions.is_some()
                || (python_only && managed.is_some() && startup.python.packages.is_none())
            {
                startup.native_duckdb = crate::local_runtime::DEFAULT_DUCKDB_EXTENSIONS
                    .iter()
                    .map(|extension| (*extension).into())
                    .collect();
            }
            let extensions = extensions.unwrap_or_default();
            local_runtime = Some(selection);
            resolver_preparation = Some(preparation);
            let python = Some(match managed {
                Some(selected) => PythonEnvironment::Managed { selected, resolver },
                None => PythonEnvironment::bare(configured_python.clone()),
            });
            (None, extensions, python, RResolver::Disabled)
        } else {
            if explicit_managed && !discovery.managed {
                let error = crate::local_runtime::RESOLUTION_UNAVAILABLE;
                preparation
                    .close()
                    .map_err(|cleanup| format!("{error}; {cleanup}"))?;
                return Err(error.into());
            }
            let prepare_r = r_settings.resolution.prepares_startup();
            local_runtime = Some(crate::local_runtime::Selection {
                r_home,
                installation,
                r_settings,
                automatic_python_resolution: false,
                python: None,
            });
            resolver_preparation = Some(preparation.clone());
            if discovery.managed && prepare_r {
                (
                    None,
                    Default::default(),
                    None,
                    RResolver::Pending(BuiltinSetup {
                        bootstrap: preparation.clone(),
                        python_resolver: crate::resolver::execution::PythonConfiguration {
                            preparation,
                            has_uv: discovery
                                .local_has_uv
                                .ok_or("local R discovery has no uv result")?,
                        },
                        configured_python: configured_python.clone(),
                    }),
                )
            } else {
                (
                    None,
                    Default::default(),
                    Some(PythonEnvironment::bare(configured_python.clone())),
                    RResolver::Disabled,
                )
            }
        };
        // Configured environments are captured before either interpreter starts.
        // Windows also retains its independent legacy peer-runtime selection.
        let mut local_runtime = local_runtime;
        if let Some(runtime) = &mut local_runtime {
            runtime.automatic_python_resolution =
                startup.python.resolution.permits_implicit_additions();
        }
        if let Some(runtime) = &mut local_runtime
            && runtime.python.is_none()
            && inspect_explicit
        {
            let explicit = configured_python
                .filter(|value| !value.is_empty() && value != "managed")
                .or_else(|| {
                    (legacy_python && cfg!(windows) && matches!(r_resolver, RResolver::Disabled))
                        .then(|| {
                            crate::resolver::find_path_entry("python").map(PathBuf::into_os_string)
                        })
                        .flatten()
                });
            if let Some(explicit) = explicit {
                let preparation = resolver_preparation.as_ref().expect("local preparation");
                let selected = crate::python::explicit_executable(&explicit)
                    .and_then(|executable| {
                        crate::resolver::preparation::Preparation::inspect(
                            crate::resolver::preparation::Operation::InspectPython { executable },
                            inspection_settings.clone(),
                            no_sandbox,
                            diagnostics.clone(),
                            on_started,
                        )
                    })
                    .map_err(|error| match preparation.close() {
                        Ok(()) => error,
                        Err(cleanup) => format!("{error}; {cleanup}"),
                    })?;
                runtime.python = Some(crate::local_runtime::Python {
                    selected: Box::new(selected),
                    explicit: Some(explicit),
                    managed: false,
                    duckdb_extension_directory: None,
                });
            }
        }
        let mut configuration = Self::with_arguments(
            program,
            vec![
                OsString::from("worker"),
                OsString::from("--bootstrap-runtimes"),
            ],
            None,
            no_sandbox,
            sandbox_settings,
            Environment {
                startup,
                local_runtime,
                custom_worker: false,
                duckdb_extensions,
                duckdb_r_targets: Vec::new(),
                python,
                r,
                r_resolver,
            },
        );
        configuration.duckdb_extension_directory = duckdb_extension_directory;
        configuration.resolver_settings = resolver_settings;
        configuration.resolver_preparation = Mutex::new(resolver_preparation);
        configuration.languages = Some(languages);
        Ok(configuration)
    }

    fn with_arguments(
        program: PathBuf,
        arguments: Vec<OsString>,
        relay: Option<PathBuf>,
        no_sandbox: bool,
        sandbox_settings: crate::settings::SandboxSettings,
        environment: Environment,
    ) -> Self {
        let dynamic_resolution = !matches!(environment.r_resolver, RResolver::Disabled);
        let python_only = environment
            .local_runtime
            .as_ref()
            .is_some_and(crate::local_runtime::Selection::python_only);
        let python_preparation = matches!(environment.r_resolver, RResolver::Disabled)
            && environment
                .python
                .as_ref()
                .and_then(PythonEnvironment::managed)
                .is_some();
        Self {
            runtime: platform::WorkerRuntime,
            program,
            arguments,
            relay,
            no_sandbox,
            sandbox_settings,
            startup_source: None,
            startup_permitted: AtomicBool::new(true),
            resolver_settings: Default::default(),
            duckdb_extension_directory: None,
            worker: Mutex::new(WorkerState::Initial),
            requirements_snapshot: Mutex::new(environment.inspection()),
            runtime_r_requirements: environment
                .runtime_r_requirements()
                .iter()
                .map(|s| (*s).into())
                .collect(),
            r_resolution: environment.startup.r_resolution,
            environment: Some(Mutex::new(environment)),
            dynamic_resolution,
            python_only,
            python_preparation,
            resolver_preparation: Mutex::new(None),
            languages: None,
            unused_default: AtomicBool::new(false),
        }
    }

    pub(crate) fn python_only(&self) -> bool {
        self.python_only
    }

    pub(crate) fn python_preparation(&self) -> bool {
        self.python_preparation
    }

    pub(crate) fn startup_declaration(&self) -> super::Declaration {
        self.environment
            .as_ref()
            .expect("captured environment")
            .lock()
            .expect("environment lock")
            .startup_declaration()
    }

    pub(crate) fn dynamic_resolution(&self) -> bool {
        self.dynamic_resolution
    }
}
