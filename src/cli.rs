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
  mcp-console warmup
  mcp-console sandbox -- python -c 'print(\"hello\")'
  mcp-console configure-client codex claude";

const SANDBOX_EXAMPLES: &str = "\
Examples:
  mcp-console sandbox -- Rscript analysis.R
  mcp-console sandbox -- python script.py";

const CONFIGURE_CLIENT_EXAMPLES: &str = "\
Prints each configuration file and setting changed. Unrelated settings are
preserved. Use --dry-run to preview or apply the changes manually.
Allows Console's send tool without individual approval, subject to client restrictions.
Starts cache warming in the background; use --no-warmup to skip it.
Codex launches default to the client's sandbox permissions; configuration files
can select another profile. Arguments after -- customize the serve settings.

Examples:
  uvx mcp-console configure-client codex claude
  mcp-console configure-client codex --pin-version current
  mcp-console configure-client codex --pin-version v0.0.5
  mcp-console configure-client claude --no-uvx --dry-run";

const CONFIG_INIT_EXAMPLES: &str = "\
Starts with workspace permissions and commented examples for common settings.
Apply -c settings after the starter permissions; other defaults remain implicit.
Existing files are never overwritten. Does not start interpreters or prepare
packages.

Examples:
  mcp-console config init
  mcp-console config init --global
  mcp-console config init --directory /path/to/project
  mcp-console config init -c r.resolution=disabled -c 'r.packages=[]' --dry-run";

#[derive(Debug, Parser)]
#[command(
    name = "mcp-console",
    bin_name = "mcp-console",
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

#[derive(Debug, Default, Args)]
pub struct ConfigOverrides {
    /// Skip trusted launch-directory project configuration; still load global configuration and apply -c overrides
    #[arg(long, long_help = PROJECT_CONFIG_HELP)]
    pub no_project_config: bool,

    /// Skip global configuration; still load project configuration and apply -c overrides
    #[arg(long)]
    pub no_global_config: bool,

    /// Skip global and project configuration; still apply --default-config and -c settings
    #[arg(long)]
    pub no_config: bool,

    /// Use only this YAML or JSON configuration file, then apply -c overrides
    #[arg(long, value_name = "PATH")]
    pub config_file: Option<PathBuf>,

    /// Supply defaults below configuration files; repeat for ordered dotted KEY=VALUE assignments
    #[arg(long = "default-config", value_name = "KEY=VALUE")]
    pub default_values: Vec<String>,

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
        self.default_values.extend(other.default_values);
        self.values.extend(other.values);
        self.no_project_config |= other.no_project_config;
        self.no_global_config |= other.no_global_config;
        self.no_config |= other.no_config;
        Ok(())
    }
}

#[derive(Debug, Subcommand)]
pub enum Command {
    /// Check the configured server and runtimes and warm their caches
    ///
    /// Dependency preparation runs with host permissions, using the same caches
    /// as serve. Run only trusted configuration and dependencies. Runtime checks
    /// use the configured sandbox unless --no-sandbox is selected.
    #[command(display_order = 5)]
    Warmup {
        #[command(flatten)]
        overrides: ConfigOverrides,

        /// Run evaluated code and dependency resolution with host permissions
        #[arg(long)]
        no_sandbox: bool,

        /// Allow writes to an additional path (temporary launch option)
        #[arg(long, value_name = "PATH", conflicts_with = "no_sandbox")]
        writable_root: Vec<PathBuf>,

        /// Maximum seconds for server initialization and each language check
        #[arg(long, default_value_t = 600, value_name = "SECONDS", value_parser = clap::value_parser!(u32).range(1..))]
        timeout: u32,

        /// Replace the runtime worker during development
        #[arg(long, hide = true, value_name = "PATH")]
        worker: Option<PathBuf>,

        /// Replace the worker relay during development
        #[arg(long, hide = true, value_name = "PATH", requires = "worker")]
        relay: Option<PathBuf>,
    },

    /// Warm a server selected by client configuration
    #[command(hide = true)]
    WarmupServer {
        #[arg(value_name = "COMMAND", required = true, num_args = 1.., allow_hyphen_values = true, trailing_var_arg = true)]
        command: Vec<OsString>,
    },

    /// Configure clients to launch MCP Console and expose its tools
    #[command(after_help = CONFIGURE_CLIENT_EXAMPLES, display_order = 4)]
    ConfigureClient(ConfigureClientOptions),

    /// Create or inspect configuration without starting interpreters or preparing packages
    #[command(display_order = 3)]
    Config {
        #[command(subcommand)]
        command: ConfigCommand,
    },
    /// Run the MCP server over standard input and output
    #[command(display_order = 1)]
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

    /// Run the internal Rscript preparation adapter
    #[cfg(windows)]
    #[command(hide = true)]
    ResolverRscript {
        #[arg(required = true, trailing_var_arg = true, allow_hyphen_values = true)]
        command: Vec<OsString>,
    },

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
    #[command(after_help = SANDBOX_EXAMPLES, display_order = 2)]
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
    /// Create an annotated Console configuration with workspace permissions
    #[command(after_help = CONFIG_INIT_EXAMPLES)]
    Init(ConfigInitOptions),

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

#[derive(Debug, Args)]
pub struct ConfigInitOptions {
    /// Create global configuration in Console home instead of the project directory
    #[arg(long, conflicts_with = "directory")]
    pub global: bool,

    /// Existing project directory; defaults to the current directory without searching Git parents
    #[arg(long, value_name = "PATH")]
    pub directory: Option<PathBuf>,

    /// Override starter settings; repeat for ordered dotted KEY=VALUE assignments
    #[arg(short = 'c', long = "config", value_name = "KEY=VALUE")]
    pub values: Vec<String>,

    /// Print the destination and proposed contents without creating files or directories
    #[arg(long)]
    pub dry_run: bool,
}

#[derive(Clone, Copy, Debug, clap::ValueEnum)]
pub enum ConfigFormat {
    Yaml,
    Json,
}

#[derive(Debug, Args)]
pub struct ConfigureClientOptions {
    /// One or more clients to configure
    #[arg(value_enum, required = true, num_args = 1.., value_name = "CLIENT")]
    pub clients: Vec<ConfigureClient>,

    /// Write project configuration in PATH; defaults to global registration when omitted
    #[arg(long, value_name = "PATH")]
    pub directory: Option<PathBuf>,

    /// Configure an absolute executable path; temporary uvx runs install recorded direct sources with uv. Index installs must be persistent first. Conflicts with --pin-version
    #[arg(long, conflicts_with = "pin_version")]
    pub no_uvx: bool,

    /// Console version to resolve with uvx: exact version, current, or latest; unpinned by default. Conflicts with --no-uvx
    #[arg(long, value_name = "VERSION", value_parser = parse_pin_version)]
    pub pin_version: Option<String>,

    /// Server name in the client's configuration
    #[arg(long, default_value = "console", value_parser = clap::builder::NonEmptyStringValueParser::new())]
    pub name: String,

    /// Keep the client's default tool discovery instead of loading Console tools up front
    #[arg(long)]
    pub no_auto_load: bool,

    /// Skip the background runtime checks and cache warming after configuration
    #[arg(long)]
    pub no_warmup: bool,

    /// Preview setup without writing files, installing, or starting background warmup
    #[arg(long)]
    pub dry_run: bool,

    /// Additional arguments passed to mcp-console serve, after --
    #[arg(last = true, value_name = "SERVE_ARGS")]
    pub serve_args: Vec<String>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, clap::ValueEnum)]
pub enum ConfigureClient {
    Codex,
    Claude,
}

fn parse_pin_version(input: &str) -> Result<String, String> {
    let version = if input == "current" {
        env!("CARGO_PKG_VERSION")
    } else {
        input
    };
    // Validate syntax here; uv checks package availability when launched.
    if version != "latest" {
        version.parse::<pep440_rs::Version>().map_err(|error| {
            format!("expected an exact PEP 440 version, current, or latest: {error}")
        })?;
    }
    Ok(version.to_owned())
}
