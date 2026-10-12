use std::process::ExitCode;

use clap::Parser;

#[cfg(windows)]
mod windows;

mod cell;
mod cli;
mod config;
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
        #[cfg(windows)]
        cli::Command::SandboxSetup { status, state_dir } => {
            match sandbox::windows_setup(status, state_dir) {
                Ok(exit_code) => {
                    if !status && exit_code == ExitCode::SUCCESS {
                        println!("\n{}", cli::SANDBOX_SETUP_DETAILS);
                    }
                    exit_code
                }
                Err(error) => {
                    eprintln!("{error}");
                    ExitCode::FAILURE
                }
            }
        }
        cli::Command::Serve {
            worker,
            relay,
            no_sandbox,
            writable_root,
            overrides: command_overrides,
        } => {
            if let Err(error) = overrides.extend(command_overrides) {
                return exit_with_error(error);
            }
            match run_server(worker, relay, no_sandbox, writable_root, &overrides) {
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
        cli::Command::WorkerRelay { command } => match worker_relay::run(&command) {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => exit_with_error(error),
        },
        cli::Command::Sandbox {
            exit_with_parent,
            command,
            config_env,
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
        sandbox::materialize_settings(captured.policy.clone(), Vec::new(), &directory)?
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
    no_sandbox: bool,
    writable_roots: Vec<std::path::PathBuf>,
    overrides: &cli::ConfigOverrides,
) -> Result<(), Box<dyn std::error::Error>> {
    let directory = std::env::current_dir()?;
    let settings::Captured {
        tool,
        startup,
        cache,
        python,
        r,
        languages,
        source: _,
        mut policy,
        mut resolver,
        sandbox_requested,
        resolver_sandbox_requested,
    } = settings::discover(&directory, overrides)?;
    if no_sandbox && (sandbox_requested || resolver_sandbox_requested) {
        return Err("explicit sandbox or resolver.sandbox permissions require sandboxing; remove them when using --no-sandbox".into());
    }
    resolver::cache::configure(
        cache,
        no_sandbox,
        python.as_ref(),
        &mut resolver,
        &mut policy,
    )?;
    if startup.is_some() && (worker.is_some() || relay.is_some()) {
        return Err("startup requires the built-in worker and relay".into());
    }
    if python.is_some() && (worker.is_some() || relay.is_some()) {
        return Err("python selection requires the built-in worker and relay".into());
    }
    if r.is_some() && (worker.is_some() || relay.is_some()) {
        return Err("r settings require the built-in worker and relay".into());
    }
    let settings = if no_sandbox {
        policy
    } else {
        // Native validation belongs to the owned background launch. Running a
        // preflight child here would precede MCP serving and EOF ownership.
        sandbox::materialize_settings(policy, writable_roots, &directory)?
    };
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?;
    let result = runtime.block_on(server::run(
        worker,
        relay,
        no_sandbox,
        settings,
        python,
        r.unwrap_or_default(),
        resolver,
        startup,
        languages,
        tool,
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
