//! Annotate the saved layer; examples never supply active configuration values.

use serde_json::{Map, Value, json};

use crate::cli::ConfigFormat;
use crate::settings::{Profile, ProfileSelection};

const HEADER: &str = "\
## MCP Console configuration, shared across clients.
## Full reference:
## https://posit-dev.github.io/mcp-console/reference/#configuration
##
## Reconnect your client after editing.
## Relative paths use Console's launch directory, not this file's directory.
## Omitted settings inherit global configuration, then Console defaults.
## Lines beginning with ## explain settings; lines beginning with # can be uncommented.
\n";

const LOCAL_BINDING: &str = "\
## Allow code to start local servers, e.g. http://localhost:8000,
## and use loopback network connections.
## auto (default): false where possible on Unix, true on Windows.
## false is unsupported on Windows when a proxy is enabled.
";

// The layout owns only guidance and ordering. Serialize every active value
// through the ordinary YAML renderer, including unknown-to-the-layout keys,
// empty mappings/lists, null, and strings requiring quoting.
struct Field {
    key: &'static str,
    help: &'static str,
    example: String,
    children: Vec<Field>,
    list_example: &'static str,
}

impl Field {
    fn new(key: &'static str, help: &'static str, example: impl Into<String>) -> Self {
        Self {
            key,
            help,
            example: example.into(),
            children: Vec::new(),
            list_example: "",
        }
    }

    fn children(mut self, children: Vec<Self>) -> Self {
        self.children = children;
        self
    }

    fn list_example(mut self, example: &'static str) -> Self {
        self.list_example = example;
        self
    }
}

pub(super) fn render(value: Value, profile: Option<&ProfileSelection>) -> Result<String, String> {
    let profile_help = match profile {
        Some(ProfileSelection::Named(Profile::Workspace)) => {
            "\
## The workspace profile allows creating, editing, and deleting project files.
## The workspace is Console's launch directory.
## Changing the working directory later does not change the workspace.
## .git, .agents, .codex, .aws, and .claude stay read-only, even when absent.
## .agents includes this configuration file; referenced Git metadata is protected too.
## Other host files remain readable when permitted by the OS.
## Network access and temporary storage are configured separately below.
## Use read_only instead to omit the profile's workspace write grants.
"
        }
        Some(ProfileSelection::Named(Profile::ReadOnly)) => {
            "\
## The read_only profile does not grant writes to project files.
## Host files remain readable when permitted by the OS.
## Temporary storage and explicit filesystem settings can grant writes separately.
"
        }
        Some(
            ProfileSelection::Named(Profile::InheritCodex) | ProfileSelection::InheritCodex { .. },
        ) => {
            "\
## The inherit_codex profile uses permissions supplied by the client.
## Without usable metadata, it uses the configured fallback (default: read_only).
## Explicit filesystem and network settings modify the selected policy.
## Inherited full access disables sandboxing, including local restrictions.
"
        }
        None => {
            "\
## Without a named profile, the base policy grants no project writes.
## Host files remain readable when permitted by the OS.
## Temporary storage and explicit filesystem settings can grant writes separately.
"
        }
    };
    let python_help = match value.get("python") {
        Some(python) if python.is_string() || python.get("existing").is_some() => {
            "\
## Use an existing Python environment without managing its packages.
## The environment and its required packages must already be installed.
"
        }
        Some(python) if python.get("first_available").is_some() => {
            "\
## Select the first available Python environment from these candidates.
## Existing selections use their installed packages; managed selections prepare packages.
"
        }
        _ => {
            "\
## Let Console manage Python and prepare these initial packages.
## version accepts a quoted version or constraint; omit it for no constraint.
"
        }
    };
    let fields = vec![
        Field::new("sandbox", "", "").children(vec![
            Field::new("profile", profile_help, "# profile: workspace\n"),
            Field::new(
                "filesystem",
                "\
## Add exceptions to the selected profile's filesystem permissions.
## To grant writes only to selected directories, use profile: read_only first.
",
                "\
# filesystem:
#   read_write: [/absolute/path/to/shared-output]
#   read_only: [./reference-data]
#   deny: [./.env, ./secrets]
",
            )
            .children(vec![
                Field::new(
                    "read_write",
                    "\
## Allow creating, editing, and deleting files in these directories.
## Add existing directories to allow writes outside the project.
",
                    "# read_write: [.]\n",
                )
                .list_example("# - /absolute/path/to/shared-output\n"),
                Field::new(
                    "read_only",
                    "\
## Prevent changes to these project paths while allowing reads.
## .agents includes this configuration file.
",
                    "# read_only: [.git, .agents, .codex, .aws, .claude]\n",
                )
                .list_example("# - ./reference-data\n"),
                Field::new(
                    "deny",
                    "\
## Block both reading and writing selected files or directories.
## A leading ~/ uses your home directory; variables and globs are not expanded.
",
                    "# deny: [./.env, ./secrets]\n",
                ),
            ]),
            Field::new(
                "tempdir",
                "\
## Temporary storage options:
## - managed (default): private writable storage with Console cleanup.
## - read_write: writable host temp directories, without Console cleanup.
## - read_only: no private storage or automatic temporary write grants;
##   other filesystem permissions still apply.
##   Note: R will not work without a writeable temporary directory.
",
                "# tempdir: managed\n",
            ),
            Field::new(
                "network",
                "\
## Network options: restricted (default), enabled, or a proxy mapping.
## enabled allows network access.
## Use a proxy to allow selected hosts.
## Use hostnames, not URLs; quote wildcard patterns.
## Package downloads use separate permissions.
",
                format!(
                    "\
# network:
#   proxy:
#     domains:
#       allow: [api.example.com, \"*.example.org\"]

{LOCAL_BINDING}\
#   allow_local_binding: true
"
                ),
            )
            .children(vec![
                Field::new("proxy", "", "# proxy: {}\n"),
                Field::new(
                    "allow_local_binding",
                    LOCAL_BINDING,
                    "# allow_local_binding: true\n",
                ),
            ]),
        ]),
        Field::new(
            "r",
            "\
## Package preparation policies for R and managed Python:
## R defaults to implicit; managed Python defaults to explicit.
## implicit: startup packages, explicit requests, and missing loads/imports.
## explicit: startup packages and explicit requests only.
## fixed: prepare startup packages; reject later package changes.
##
## Supplied package lists replace the default startup lists.
## [] requests no optional startup packages; it does not disable preparation.

## R packages to prepare at startup.
## To disable R preparation, set resolution: disabled and packages: [].
## Required R infrastructure must then already be installed.
",
            "\
# r:
#   resolution: implicit
#   packages: [dplyr, ggplot2]
",
        ),
        Field::new(
            "python",
            python_help,
            "\
# python:
#   version: \"3.13\"
#   resolution: explicit
#   packages: [numpy, pandas, matplotlib]

## Alternatively, use an existing Python environment without managing its packages.
## Choose this instead of the managed Python settings above.
## The environment and its required packages must already be installed.
# python: .venv
",
        ),
        Field::new(
            "environment",
            "\
## Environment variables for evaluated code and package preparation.
## Values must be strings.
",
            "\
# environment:
#   OMP_NUM_THREADS: \"4\"
",
        ),
        Field::new(
            "cache",
            "\
## Cache options: managed (default when sandboxed), host, or a directory mapping.
## Use host to reuse ordinary host package caches.
## directory places managed dependency caches under <directory>/dependencies.
## Relative directories use the launch directory; a leading ~/ uses your home.
## It applies to package preparation and workers.
## Configuration and recordings stay in their existing locations.
",
            "# cache:\n#   directory: /absolute/path/to/console-cache\n",
        ),
    ];
    let mut output = HEADER.to_owned();
    write_fields(
        &mut output,
        value.as_object().expect("validated configuration mapping"),
        &fields,
        0,
    )?;
    Ok(output)
}

fn write_fields(
    output: &mut String,
    values: &Map<String, Value>,
    fields: &[Field],
    indent: usize,
) -> Result<(), String> {
    for (index, field) in fields.iter().enumerate() {
        if index != 0 {
            output.push('\n');
        }
        write_block(output, field.help, indent);
        let Some(value) = values.get(field.key) else {
            write_block(output, &field.example, indent);
            continue;
        };
        if !field.children.is_empty()
            && let Some(values) = value.as_object().filter(|values| !values.is_empty())
        {
            write_block(output, &format!("{}:\n", field.key), indent);
            write_fields(output, values, &field.children, indent + 2)?;
        } else {
            write_value(output, field.key, value, indent)?;
            if value.as_array().is_some_and(|values| !values.is_empty()) {
                write_block(output, field.list_example, indent + 2);
            }
        }
    }
    for (key, value) in values {
        if !fields.iter().any(|field| field.key == key) {
            output.push('\n');
            write_value(output, key, value, indent)?;
        }
    }
    Ok(())
}

fn write_value(output: &mut String, key: &str, value: &Value, indent: usize) -> Result<(), String> {
    let rendered =
        crate::settings::materialize::render(json!({key: value}), None, ConfigFormat::Yaml)?;
    write_block(output, rendered.trim_start_matches("---\n"), indent);
    Ok(())
}

fn write_block(output: &mut String, block: &str, indent: usize) {
    for line in block.lines() {
        if !line.is_empty() {
            output.push_str(&" ".repeat(indent));
            output.push_str(line);
        }
        output.push('\n');
    }
}
