//! Process-owned Console session with an optional Python Jupyter transport.
mod controller;

use std::path::Path;
use std::process::{Command, ExitCode};
use std::sync::Arc;

use crate::cell::{Language as SourceLanguage, Languages};
use crate::server::startup::{PreparedRuntime, Runtime, Startup};

#[derive(Clone, Copy, Debug, clap::ValueEnum)]
pub(crate) enum Language {
    R,
    Python,
    Sql,
}

impl Language {
    fn source(self) -> SourceLanguage {
        match self {
            Self::R => SourceLanguage::R,
            Self::Python => SourceLanguage::Python,
            Self::Sql => SourceLanguage::Sql,
        }
    }

    fn name(self) -> &'static str {
        Languages::field(self.source())
    }
}

pub(crate) fn launch(
    language: Language,
    connection_file: &Path,
    python: Option<&Path>,
    overrides: &crate::cli::ConfigOverrides,
    no_sandbox: bool,
) -> Result<ExitCode, Box<dyn std::error::Error>> {
    let default_python = if cfg!(windows) { "python" } else { "python3" };
    let mut command = Command::new(python.unwrap_or_else(|| Path::new(default_python)));
    command
        .args([
            "-u",
            "-c",
            include_str!("jupyter.py"),
            "--language",
            language.name(),
        ])
        .arg("--connection-file")
        .arg(connection_file)
        .arg("--binary")
        .arg(std::env::current_exe()?)
        .arg("--");
    for (enabled, flag) in [
        (overrides.no_project_config, "--no-project-config"),
        (overrides.no_global_config, "--no-global-config"),
        (overrides.no_config, "--no-config"),
        (no_sandbox, "--no-sandbox"),
    ] {
        if enabled {
            command.arg(flag);
        }
    }
    for (path, flag) in [
        (&overrides.config_file, "--config-file"),
        (&overrides.values_file, "--config-overrides-file"),
    ] {
        if let Some(path) = path {
            command.arg(flag).arg(path);
        }
    }
    for value in &overrides.default_values {
        command.arg("--default-config").arg(value);
    }
    for value in &overrides.values {
        command.arg("--config").arg(value);
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        Err(command.exec().into())
    }
    #[cfg(windows)]
    {
        Ok(if command.status()?.success() {
            ExitCode::SUCCESS
        } else {
            ExitCode::FAILURE
        })
    }
}

pub(crate) fn run(
    language: Language,
    overrides: &crate::cli::ConfigOverrides,
    no_sandbox: bool,
) -> Result<(), Box<dyn std::error::Error>> {
    let directory = std::env::current_dir()?;
    let settings = crate::settings::discover(&directory, overrides)?;
    let languages = match settings.languages {
        Some(languages) => languages,
        None => Languages::from_environment()?,
    };
    if !languages.enables(language.source()) {
        return Err(format!(
            "kernel language {} is disabled by configuration",
            language.name()
        )
        .into());
    }
    if no_sandbox && (settings.sandbox_requested || settings.resolver_sandbox_requested) {
        return Err("explicit sandbox or resolver.sandbox permissions require sandboxing".into());
    }
    let sandbox = crate::server::SandboxConfiguration {
        no_sandbox,
        profile: settings.profile,
        network_explicit: settings.network_explicit,
        policy: settings.policy,
        resolver: settings.resolver,
        cache: settings.cache,
        writable_roots: Vec::new(),
        directory: directory.clone(),
    }
    .configure(None, settings.python.as_ref())?;
    let runtime = Arc::new(Runtime {
        worker: crate::worker_client::Client::pending(),
        transcript: crate::transcript::Transcript::pending(Ok(directory.clone())),
    });
    let events = runtime.worker.kernel_events();
    let startup = Startup::new(None, runtime, true, move |started, diagnostics| {
        let configuration = crate::worker_client::ClientConfiguration::builtin(
            sandbox.no_sandbox,
            sandbox.policy.clone(),
            settings.python.clone(),
            settings.r.clone().unwrap_or_default(),
            sandbox.resolver.clone(),
            diagnostics,
            started,
        )?
        .with_startup(settings.startup.clone());
        let transcript = crate::transcript::Transcript::configured(
            Ok(directory.clone()),
            configuration.dynamic_resolution(),
            configuration.python_preparation(),
            !configuration.python_only(),
            configuration.startup_declaration(),
        );
        Ok(PreparedRuntime {
            configuration,
            transcript,
        })
    });
    let executor = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()?;
    let result = executor.block_on(controller::run(language, startup, events));
    // A closed transport must not retain a process through a blocking stdio task.
    // The controller has already settled native resources and recordings.
    executor.shutdown_background();
    result.map_err(Into::into)
}
