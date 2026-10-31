use std::process::ExitCode;

use clap::Parser;

#[cfg(windows)]
mod windows;

mod cell;
mod cli;
mod config;
mod configure_client;
mod console_paths;
#[cfg(unix)]
mod input_watch;
#[cfg(any(unix, windows))]
mod jsonl;
mod local_runtime;
#[cfg(unix)]
mod process_descriptors;
#[cfg(any(unix, windows))]
mod process_exit;
#[cfg(unix)]
mod process_io;
#[cfg(any(unix, windows))]
mod process_output;
#[cfg(any(unix, windows))]
mod python;
mod python_requirement;
#[cfg(any(unix, windows))]
mod r_bridge;
#[cfg(any(unix, windows))]
mod r_environment;
#[cfg(any(unix, windows))]
mod r_graphics;
mod r_package_name;
#[cfg(unix)]
mod readiness;
mod relay_protocol;
mod resolver;
mod sandbox;
mod server;
mod server_transport;
mod settings;
#[cfg(any(unix, windows))]
mod sideband;
#[cfg(any(unix, windows))]
mod sql;
mod transcript;
mod warmup;
mod worker;
mod worker_client;
mod worker_protocol;
mod worker_relay;

fn main() -> ExitCode {
    #[cfg(all(windows, target_arch = "x86_64"))]
    if std::env::var_os("PROCESSOR_ARCHITECTURE").is_none_or(|value| value.is_empty()) {
        // Filtered MCP environments can omit this Windows runtime variable.
        // R DuckDB's native teardown can crash without it, including in the
        // preparation child. Each Console process restores its own architecture.
        // SAFETY: entry is single-threaded, before native runtime loading.
        unsafe { std::env::set_var("PROCESSOR_ARCHITECTURE", "AMD64") };
    }
    let cli = cli::Cli::parse();
    let mut overrides = cli.overrides;
    match cli.command {
        cli::Command::Warmup {
            overrides: command_overrides,
            no_sandbox,
            writable_root,
            timeout,
            worker,
            relay,
        } => {
            if let Err(error) = overrides.extend(command_overrides) {
                return exit_with_error(error);
            }
            match warmup::run(
                &overrides,
                worker,
                relay,
                no_sandbox,
                writable_root,
                timeout,
            ) {
                Ok(true) => ExitCode::SUCCESS,
                Ok(false) => ExitCode::FAILURE,
                Err(error) => exit_with_error(error),
            }
        }
        cli::Command::WarmupServer { command } => match warmup::run_server(&command) {
            Ok(true) => ExitCode::SUCCESS,
            Ok(false) => ExitCode::FAILURE,
            Err(error) => exit_with_error(error),
        },
        cli::Command::ConfigureClient(options) => match configure_client::run(&options) {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => exit_with_error(error),
        },
        cli::Command::Config {
            command: cli::ConfigCommand::Init(options),
        } => match config::init::run(&options, &overrides) {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => exit_with_error(error),
        },
        cli::Command::Config {
            command:
                cli::ConfigCommand::Resolve {
                    overrides: command_overrides,
                    no_sandbox,
                    full,
                    prune: _,
                    format,
                },
        } => {
            if let Err(error) = overrides.extend(command_overrides) {
                return exit_with_error(error);
            }
            match resolve_configuration(&overrides, no_sandbox, full, format) {
                Ok(output) => {
                    println!("{output}");
                    ExitCode::SUCCESS
                }
                Err(error) => exit_with_error(error),
            }
        }
        cli::Command::Serve {
            worker,
            relay,
            control_socket,
            no_sandbox,
            writable_root,
            overrides: command_overrides,
        } => {
            if let Err(error) = overrides.extend(command_overrides) {
                return exit_with_error(error);
            }
            match run_server(
                worker,
                relay,
                control_socket,
                no_sandbox,
                writable_root,
                &overrides,
            ) {
                Ok(()) => ExitCode::SUCCESS,
                Err(error) => exit_with_error(error),
            }
        }
        cli::Command::Worker { bootstrap_runtimes } => match worker::run(bootstrap_runtimes) {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => exit_with_error(error),
        },
        cli::Command::Resolve => match resolver::run() {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => exit_with_error(error),
        },
        #[cfg(windows)]
        cli::Command::ResolverRscript { command } => match resolver::run_rscript(&command) {
            Ok(code) => std::process::exit(code),
            Err(error) => exit_with_error(error),
        },
        cli::Command::WorkerRelay { command } => match worker_relay::run(&command) {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => exit_with_error(error),
        },
        cli::Command::Sandbox {
            exit_with_parent,
            command,
            config_env,
            runner_config_env,
            settings_env,
            writable_root,
            overrides: command_overrides,
        } => {
            if let Err(error) = overrides.extend(command_overrides) {
                return exit_with_error(error);
            }
            match sandbox::run(
                &command,
                exit_with_parent,
                config_env.as_deref(),
                runner_config_env.as_deref(),
                settings_env.as_deref(),
                writable_root,
                &overrides,
            ) {
                Ok(exit_code) => exit_code,
                Err(error) => exit_with_error(error),
            }
        }
    }
}

fn resolve_configuration(
    overrides: &cli::ConfigOverrides,
    no_sandbox: bool,
    full: bool,
    format: cli::ConfigFormat,
) -> Result<String, String> {
    let directory = std::env::current_dir().map_err(|error| error.to_string())?;
    let (captured, value) = settings::read(&directory, overrides)?;
    let policy = if no_sandbox {
        captured.policy.clone()
    } else {
        settings::materialize_sandbox(
            captured.policy.clone(),
            Vec::new(),
            &directory,
            settings::PolicyContext::Configuration,
        )?
    };
    let mut defaults = if full {
        None
    } else {
        let (mut captured, defaults) = settings::read(
            &directory,
            &cli::ConfigOverrides {
                no_config: true,
                no_global_config: false,
                no_project_config: false,
                config_file: None,
                default_values: Vec::new(),
                values: Vec::new(),
                values_file: None,
            },
        )?;
        // Compare with the binary's defaults, including when the internal
        // language environment selects a different interface for this launch.
        captured.languages = Some(cell::Languages::all());
        let policy = captured.policy.clone();
        Some(settings::materialize::configuration(
            captured, defaults, &policy, no_sandbox, None,
        )?)
    };
    let value = settings::materialize::configuration(
        captured,
        value,
        &policy,
        no_sandbox,
        defaults.as_mut(),
    )?;
    settings::materialize::render(value, defaults, format)
}

fn run_server(
    worker: Option<std::path::PathBuf>,
    relay: Option<std::path::PathBuf>,
    control_socket: Option<std::path::PathBuf>,
    no_sandbox: bool,
    writable_roots: Vec<std::path::PathBuf>,
    overrides: &cli::ConfigOverrides,
) -> Result<(), Box<dyn std::error::Error>> {
    #[cfg(not(unix))]
    if control_socket.is_some() {
        return Err("--control-socket is available only on Unix".into());
    }
    let directory = std::env::current_dir()?;
    let settings::Captured {
        profile,
        network_explicit,
        tool,
        sessions,
        startup,
        cache,
        python,
        r,
        languages,
        source: _,
        policy,
        resolver,
        sandbox_requested,
        resolver_sandbox_requested,
    } = settings::discover(&directory, overrides)?;
    if no_sandbox && (sandbox_requested || resolver_sandbox_requested) {
        return Err("explicit sandbox or resolver.sandbox permissions require sandboxing; remove them when using --no-sandbox".into());
    }
    if startup.is_some() && (worker.is_some() || relay.is_some()) {
        return Err("startup requires the built-in worker and relay".into());
    }
    if python.is_some() && (worker.is_some() || relay.is_some()) {
        return Err("python selection requires the built-in worker and relay".into());
    }
    if r.is_some() && (worker.is_some() || relay.is_some()) {
        return Err("r settings require the built-in worker and relay".into());
    }
    let sandbox = server::SandboxConfiguration {
        no_sandbox,
        profile,
        network_explicit,
        policy,
        resolver,
        cache,
        writable_roots,
        directory,
    };
    let sandbox = if sandbox
        .profile
        .as_ref()
        .is_some_and(settings::ProfileSelection::inherits_codex)
    {
        sandbox
    } else {
        sandbox.configure(None, python.as_ref())?
    };
    // Stdio uses blocking adapters and child processes have native observers.
    // Only the optional control socket needs this runtime's I/O driver.
    let mut builder = tokio::runtime::Builder::new_current_thread();
    builder.enable_time();
    if control_socket.is_some() {
        builder.enable_io();
    }
    let runtime = builder.build()?;
    let result = runtime.block_on(server::run(
        worker,
        relay,
        sandbox,
        python,
        r.unwrap_or_default(),
        startup,
        languages,
        tool,
        sessions,
        control_socket,
    ));
    // `server::run` has already finished owned runtime retirement and response settling. Tokio's
    // stdout uses a blocking task that cannot be cancelled while the client
    // leaves its output pipe full, so runtime teardown must not wait for it.
    // The process exits immediately after this function returns.
    runtime.shutdown_background();
    result
}

fn exit_with_error(error: impl std::fmt::Display) -> ExitCode {
    eprintln!("{error}");
    ExitCode::FAILURE
}
