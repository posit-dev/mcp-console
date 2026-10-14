use std::ffi::OsString;
use std::path::PathBuf;

use clap::{Args, Parser, Subcommand};

const PROJECT_CONFIG_HELP: &str = "\
Skip launch-directory project configuration; still load global configuration and apply -c overrides.
Automatic project configuration is trusted launcher input: it can select executables, child environments, and sandbox permissions. Trusting sandboxed code is not equivalent to trusting it to define the sandbox.
Global settings supply defaults, not a mandatory security ceiling; projects may override them. Integrations opening unfamiliar projects should use this flag until they authorize project configuration.";

const ROOT_EXAMPLES: &str = "\
Examples:
  mcp-console serve
  mcp-console sandbox -- python -c 'print(\"hello\")'";

const SANDBOX_EXAMPLES: &str = "\
Examples:
  mcp-console sandbox -- Rscript analysis.R
  mcp-console sandbox -- python script.py";

#[derive(Debug, Parser)]
#[command(
    name = "mcp-console",
    version,
    about = env!("CARGO_PKG_DESCRIPTION"),
    after_help = ROOT_EXAMPLES
)]
pub struct Cli {
    #[command(flatten)]
    pub overrides: ConfigOverrides,

    #[command(subcommand)]
    pub command: Command,
}

#[derive(Debug, Args)]
pub struct ConfigOverrides {
    /// Skip trusted launch-directory project configuration; still load global configuration and apply -c overrides
    #[arg(long, long_help = PROJECT_CONFIG_HELP)]
    pub no_project_config: bool,

    /// Skip global configuration; still load project configuration and apply -c overrides
    #[arg(long)]
    pub no_global_config: bool,

    /// Skip global and project configuration; apply only -c overrides
    #[arg(long)]
    pub no_config: bool,

    /// Use only this YAML or JSON configuration file, then apply -c overrides
    #[arg(long, value_name = "PATH")]
    pub config_file: Option<PathBuf>,

    /// Override layered global and project configuration; repeat for ordered dotted KEY=VALUE assignments
    #[arg(short = 'c', long = "config", value_name = "KEY=VALUE")]
    pub values: Vec<String>,

    /// Private R adapter transport: optional files and ordered KEY=VALUE overrides
    #[arg(long = "config-overrides-file", hide = true)]
    pub values_file: Option<PathBuf>,
}

impl ConfigOverrides {
    pub fn extend(&mut self, other: Self) -> Result<(), String> {
        if self.config_file.is_some() && other.config_file.is_some() {
            return Err("--config-file may be supplied only once".into());
        }
        if other.config_file.is_some() {
            self.config_file = other.config_file;
        }
        if self.values_file.is_some() && other.values_file.is_some() {
            return Err("--config-overrides-file may be supplied only once".into());
        }
        if other.values_file.is_some() {
            self.values_file = other.values_file;
        }
        self.values.extend(other.values);
        self.no_project_config |= other.no_project_config;
        self.no_global_config |= other.no_global_config;
        self.no_config |= other.no_config;
        Ok(())
    }
}

#[derive(Debug, Subcommand)]
pub enum Command {
    /// Inspect configuration without starting interpreters or preparing packages
    Config {
        #[command(subcommand)]
        command: ConfigCommand,
    },
    /// Run the MCP server over standard input and output
    Serve {
        #[command(flatten)]
        overrides: ConfigOverrides,

        /// Run evaluated code and dependency resolution with host permissions
        #[arg(long)]
        no_sandbox: bool,

        /// Allow writes to an additional path (temporary launch option)
        #[arg(long, value_name = "PATH", conflicts_with = "no_sandbox")]
        writable_root: Vec<PathBuf>,

        /// Replace the runtime worker during development
        #[arg(long, hide = true, value_name = "PATH")]
        worker: Option<PathBuf>,

        /// Replace the worker relay during development
        #[arg(long, hide = true, value_name = "PATH", requires = "worker")]
        relay: Option<PathBuf>,
    },

    /// Run the internal R worker
    #[command(hide = true)]
    Worker {
        /// Initialize enabled runtimes after transport readiness
        #[arg(long, hide = true)]
        bootstrap_runtimes: bool,
    },

    /// Run the internal host resolver
    #[command(hide = true)]
    Resolve,

    /// Run the internal worker relay
    #[command(hide = true)]
    WorkerRelay {
        /// Worker command to launch through the relay
        #[arg(
            value_name = "COMMAND",
            required = true,
            num_args = 1..,
            allow_hyphen_values = true,
            trailing_var_arg = true
        )]
        command: Vec<OsString>,
    },

    /// Run a command with the default or an explicit sandbox policy
    #[command(after_help = SANDBOX_EXAMPLES)]
    Sandbox {
        #[command(flatten)]
        overrides: ConfigOverrides,

        /// Read the runner configuration as JSON from this launch environment variable
        #[arg(long, value_name = "NAME", conflicts_with = "exit_with_parent")]
        config_env: Option<String>,

        /// Consume the application settings captured by the server
        #[arg(long, hide = true, value_name = "NAME", conflicts_with_all = ["config_env", "writable_root"])]
        settings_env: Option<String>,

        /// Allow writes to an additional path (temporary launch option)
        #[arg(long, value_name = "PATH", conflicts_with = "config_env")]
        writable_root: Vec<PathBuf>,

        /// Retire the sandbox when this parent process exits
        #[arg(long, hide = true, value_name = "PID")]
        exit_with_parent: Option<u32>,

        /// Command and arguments to run
        #[arg(
            value_name = "COMMAND",
            required = true,
            num_args = 1..,
            trailing_var_arg = true
        )]
        command: Vec<OsString>,
    },
}

#[derive(Debug, Subcommand)]
pub enum ConfigCommand {
    /// Print resolved configuration
    Resolve {
        #[command(flatten)]
        overrides: ConfigOverrides,
        /// Resolve defaults for a subsequent serve --no-sandbox launch
        #[arg(long)]
        no_sandbox: bool,
        /// Include all configuration defaults and the complete editable tool presentation
        #[arg(long, conflicts_with = "prune")]
        full: bool,
        /// Include only settings that differ from built-in defaults (the default)
        #[arg(long)]
        prune: bool,
        /// Output format
        #[arg(long, value_enum, default_value = "yaml")]
        format: ConfigFormat,
    },
}

#[derive(Clone, Copy, Debug, clap::ValueEnum)]
pub enum ConfigFormat {
    Yaml,
    Json,
}
