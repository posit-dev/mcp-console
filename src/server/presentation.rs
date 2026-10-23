//! Tool prose derived only from captured launch configuration.
//! Client discovery and truncation rules: docs/TOOL_DESCRIPTIONS.md#deferred-tool-discovery.
mod requirements;
mod sections;

use std::sync::Arc;

use rmcp::handler::server::router::tool::ToolRouter;
use serde_json::{Map, Value};

use super::ConsoleServer;
use crate::cell::Languages;
use crate::settings::{PythonChoice, Resolution, SandboxSettings};

/// Requested interface and preparation mode, never discovered runtime availability.
struct Profile {
    languages: Languages,
    builtin: bool,
    timeout: bool,
    uses_client_profile: bool,
    python: PythonSelection,
    python_requirements: bool,
}

enum PythonSelection {
    Existing,
    Managed(Resolution),
}

impl PythonSelection {
    fn capture(python: Option<&PythonChoice>) -> Self {
        match python {
            Some(python) if python.executable.is_some() => Self::Existing,
            Some(python) => Self::Managed(python.managed.resolution),
            None => {
                let existing = std::env::var_os("RETICULATE_PYTHON")
                    .is_some_and(|python| !python.is_empty() && python != "managed");
                if existing {
                    Self::Existing
                } else {
                    Self::Managed(crate::settings::ManagedPythonSettings::default().resolution)
                }
            }
        }
    }

    fn guidance(&self, requirements_visible: bool) -> &'static str {
        match self {
            Self::Existing => sections::PYTHON_INSTALLED,
            Self::Managed(Resolution::Explicit) => {
                if requirements_visible {
                    sections::PYTHON_EXPLICIT
                } else {
                    sections::PYTHON_INSTALLED
                }
            }
            Self::Managed(Resolution::Implicit) => sections::PYTHON_IMPLICIT,
            Self::Managed(Resolution::Fixed) => sections::PYTHON_FIXED,
            Self::Managed(Resolution::Disabled) => {
                unreachable!("managed Python resolution cannot be disabled")
            }
        }
    }
}

/// Render editable configuration without creating a server or starting runtimes.
pub(crate) fn materialized_tool(
    languages: Languages,
    python: Option<&PythonChoice>,
    policy: &SandboxSettings,
    no_sandbox: bool,
    uses_client_profile: bool,
    tool: &crate::settings::Tool,
    apply_overrides: bool,
) -> crate::settings::Tool {
    let profile = Profile {
        languages,
        builtin: true,
        timeout: tool.enabled("timeout_ms"),
        uses_client_profile,
        python: PythonSelection::capture(python),
        python_requirements: tool.enabled("requirements.python"),
    };
    let mut tool = if apply_overrides {
        tool.clone()
    } else {
        crate::settings::Tool::default()
    };
    tool.instructions.get_or_insert_with(|| profile.summary());
    tool.description
        .get_or_insert_with(|| profile.description(&profile.summary(), policy, no_sandbox));
    let mut schema = parameter_schema(languages);
    let fields = schema
        .get_mut("properties")
        .and_then(Value::as_object_mut)
        .expect("schema properties");
    profile.configure_fields(fields);
    // Saved selections and policies can change on the next launch. Preserve
    // explicit overrides, but leave generated Python guidance regenerable.
    if let Some(python) = fields.get_mut("python") {
        python
            .as_object_mut()
            .expect("Python parameter schema")
            .remove("description");
    }
    tool.materialize(&schema);
    tool
}

impl ConsoleServer {
    pub(super) fn configured_presentation(
        languages: Languages,
        builtin: bool,
        python: Option<&PythonChoice>,
        policy: &SandboxSettings,
        no_sandbox: bool,
        uses_client_profile: bool,
        tool: &crate::settings::Tool,
    ) -> Result<(ToolRouter<Self>, String), String> {
        let profile = Profile {
            languages,
            builtin,
            timeout: tool.enabled("timeout_ms"),
            uses_client_profile,
            python: PythonSelection::capture(python),
            python_requirements: tool.enabled("requirements.python"),
        };
        let instructions = tool
            .instructions
            .clone()
            .unwrap_or_else(|| profile.summary());
        let mut router = Self::tool_router();
        let send = router
            .map
            .get_mut("send")
            .expect("send tool must be registered");
        send.attr.description = Some(
            tool.description
                .clone()
                .unwrap_or_else(|| profile.description(&profile.summary(), policy, no_sandbox))
                .into(),
        );
        let mut schema = parameter_schema(languages);
        let properties = schema
            .get_mut("properties")
            .and_then(Value::as_object_mut)
            .expect("send schema must have object properties");
        profile.configure_fields(properties);
        tool.configure_schema(&mut schema)?;
        send.attr.input_schema = Arc::new(schema);
        Ok((router, instructions))
    }
}

/// Shared by configuration discovery and MCP presentation so both validate the same fields.
pub(crate) fn parameter_schema(languages: Languages) -> Map<String, Value> {
    let mut schema =
        (*rmcp::handler::server::common::schema_for_input::<super::arguments::SendArguments>()
            .expect("send arguments must have a valid input schema"))
        .clone();
    let properties = schema
        .get_mut("properties")
        .and_then(Value::as_object_mut)
        .expect("send schema must have object properties");
    let control = properties
        .get_mut("control")
        .and_then(Value::as_object_mut)
        .expect("send control schema must be an object");
    control.insert("type".to_string(), Value::String("string".to_string()));
    if let Some(values) = control.get_mut("enum").and_then(Value::as_array_mut) {
        values.retain(|value| !value.is_null());
    }
    // Omission carries meaning for get/reset. Do not advertise payload defaults.
    for property in properties
        .get_mut("requirements")
        .and_then(|requirements| requirements.get_mut("properties"))
        .and_then(Value::as_object_mut)
        .expect("requirements schema properties")
        .values_mut()
    {
        property
            .as_object_mut()
            .expect("requirement property schema")
            .remove("default");
    }
    // Configured fields stay visible even when an interpreter is unavailable.
    for (field, enabled) in [
        ("r", languages.r),
        ("python", languages.python),
        ("sql", languages.sql),
    ] {
        if !enabled {
            properties.shift_remove(field);
        }
    }
    schema
}

impl Profile {
    fn restricted_guidance(&self) -> bool {
        !(self.languages.r && self.languages.python && self.languages.sql)
    }

    fn multiple_languages(&self) -> bool {
        [self.languages.r, self.languages.python, self.languages.sql]
            .into_iter()
            .filter(|enabled| *enabled)
            .count()
            > 1
    }

    fn summary(&self) -> String {
        let names = if self.languages.r && self.languages.python && self.languages.sql {
            "R, Python, and SQL".to_string()
        } else {
            self.languages
                .fields()
                .iter()
                .map(|field| match *field {
                    "r" => "R",
                    "python" => "Python",
                    "sql" => "SQL",
                    _ => unreachable!(),
                })
                .collect::<Vec<_>>()
                .join(" and ")
        };
        if !self.builtin {
            return format!("Persistent custom-worker {names} REPL. {}", sections::REUSE);
        }
        format!(
            "Persistent {names} REPL {} {}",
            sections::BUILTIN_SCOPE,
            sections::REUSE
        )
    }

    fn description(&self, summary: &str, policy: &SandboxSettings, no_sandbox: bool) -> String {
        let mut description = summary.to_string();
        description.push_str("\n\nSend one complete ");
        description.push_str(&self.languages.cell_fields());
        description.push_str(sections::SEND_WORKFLOW);
        if self.builtin {
            description.push_str(sections::DISPLAY);
            description.push_str("\n\n");
            description.push_str(&self.language_guidance());
        } else {
            description.push_str("\n\n");
            description.push_str(if self.restricted_guidance() {
                sections::CUSTOM_SELECTED_CAPABILITIES
            } else {
                sections::CUSTOM_CAPABILITIES
            });
            if self.multiple_languages() {
                description.push_str(sections::CUSTOM_SWITCHING);
            }
        }
        description.push_str("\n\n");
        description.push_str(sections::SEND_ORDERING);
        description.push(' ');
        if self.timeout {
            description.push_str(sections::POLLING);
            description.push_str(sections::DEFAULTS);
        } else {
            description.push_str(sections::POLLING_DEFAULT_TIMEOUT);
        }
        description.push_str("\n\n");
        description.push_str(sections::OUTPUT);
        description.push_str("\n\n");
        description.push_str(&description_for_launch(
            policy,
            no_sandbox,
            self.uses_client_profile,
        ));
        description
    }

    fn language_guidance(&self) -> String {
        let mut guidance = String::new();
        if self.multiple_languages() {
            guidance.push_str(sections::SWITCHING);
            if self.languages.r && self.languages.python {
                guidance.push_str(if self.languages.sql {
                    sections::R_PYTHON_SQL_CELLS
                } else {
                    sections::R_PYTHON_CELLS
                });
                guidance.push_str(sections::R_PYTHON_WORKFLOW);
            }
            guidance.push(' ');
        }
        if !(self.languages.r && self.languages.python) {
            for (enabled, section) in [
                (self.languages.sql, sections::SQL_SELECTION),
                (self.languages.r, sections::R_SELECTION),
                (self.languages.python, sections::PYTHON_SELECTION),
            ] {
                if enabled {
                    guidance.push_str(section);
                }
            }
        }
        guidance.truncate(guidance.trim_end().len());
        if self.languages.sql {
            guidance.push_str("\n\n");
            guidance.push_str(sections::SQL_FILES);
            guidance.truncate(guidance.trim_end().len());
        }
        guidance
    }

    fn configure_fields(&self, properties: &mut Map<String, Value>) {
        if self.builtin {
            for (field, description) in [
                ("r", r_description_for(self.languages)),
                (
                    "python",
                    python_description_for(self.languages, &self.python, self.python_requirements),
                ),
                ("sql", sql_description_for(self.languages)),
            ] {
                if let Some(property) = properties.get_mut(field) {
                    property["description"] = description.into();
                }
            }
        } else {
            for (field, section) in [
                ("r", sections::CUSTOM_R),
                ("python", sections::CUSTOM_PYTHON),
                ("sql", sections::CUSTOM_SQL),
            ] {
                if let Some(property) = properties.get_mut(field) {
                    property["description"] = section.into();
                }
            }
        }
        if self.restricted_guidance() {
            properties["stdin"]["description"] =
                format!("{}{}", sections::STDIN_SELECTED, sections::STDIN_ORDERING).into();
            properties["control"]["description"] = control_description_for(false).into();
        }
        requirements::configure(properties, self.languages, self.builtin);
    }
}

// Schemars uses the same named sections for the ordinary field metadata.
pub(super) fn timeout_description() -> String {
    sections::TIMEOUT.to_string()
}

pub(super) fn r_description() -> String {
    r_description_for(Languages::all())
}

fn r_description_for(languages: Languages) -> String {
    let mut description = sections::R_RUNTIME.to_string();
    if languages.python {
        description.push_str(sections::R_BRIDGE);
    }
    if languages.sql {
        description.push_str(sections::R_SQL);
    }
    description.push_str(sections::R_PLOTS);
    description.push(' ');
    description.push_str(sections::R_SCRIPT.trim_end());
    description
}

pub(super) fn python_description() -> String {
    python_description_for(
        Languages::all(),
        &PythonSelection::Managed(crate::settings::ManagedPythonSettings::default().resolution),
        true,
    )
}

fn python_description_for(
    languages: Languages,
    python: &PythonSelection,
    requirements_visible: bool,
) -> String {
    let mut description = sections::PYTHON_RUNTIME.to_string();
    description.push_str(python.guidance(requirements_visible));
    if languages.r {
        description.push_str(sections::PYTHON_BRIDGE);
    }
    if languages.sql {
        description.push_str(sections::PYTHON_SQL);
        if languages.r {
            description.push_str(sections::PYTHON_SQL_R);
            description.push_str(sections::PYTHON_SQL_CONNECTION);
        } else {
            description.push_str(sections::PYTHON_SQL_CONNECTION_SELECTED);
        }
    }
    description.push_str(sections::PYTHON_PLOTS);
    if languages.r {
        description.push_str(sections::PYTHON_R_PLOTS);
    }
    description
}

pub(super) fn sql_description() -> String {
    sql_description_for(Languages::all())
}

fn sql_description_for(languages: Languages) -> String {
    let mut description = sections::SQL_RUNTIME.to_string();
    if languages.r {
        description.push_str(sections::SQL_R_FRAMES);
    }
    if languages.r {
        description.push_str(sections::SQL_R_STATEMENTS);
    }
    description.push_str(sections::SQL_OPERATIONS);
    description
}

pub(super) fn control_description() -> String {
    control_description_for(true)
}

fn control_description_for(full: bool) -> String {
    format!(
        "{}{}{}",
        sections::CONTROL_START,
        sections::INTERRUPT,
        if full {
            sections::CONTROL_END
        } else {
            sections::CONTROL_END_SELECTED
        }
    )
}

fn description_for_launch(
    policy: &SandboxSettings,
    no_sandbox: bool,
    uses_client_profile: bool,
) -> String {
    if uses_client_profile {
        return "The sandbox base comes from Codex metadata on the first send, or the configured fallback (default: read_only). Missing metadata is silent; malformed metadata warns on server stderr. Explicit Console permissions apply to the selected base. Full access disables Console's sandbox. Reconnect to change client permissions. Package preparation has separate permissions unless sandboxing is disabled; use only trusted dependencies.".into();
    }
    if no_sandbox {
        return "Evaluated code runs without a sandbox, with the server's permissions, including filesystem and network access. Package preparation may execute installation or build code; use only trusted dependencies.".into();
    }
    let network_access = if policy.contains_key("proxy") {
        "can access the network subject to the launcher's proxy settings"
    } else {
        match policy.get("network").and_then(serde_json::Value::as_str) {
            Some("enabled") => "can directly access the network",
            Some("restricted") => "cannot directly access the network",
            _ => unreachable!("normalized worker network choice"),
        }
    };
    let mut writable = match crate::settings::Tempdir::from_settings(policy) {
        crate::settings::Tempdir::Managed => vec![if cfg!(windows) {
            "private `TMPDIR` (also `TEMP` and `TMP`)".to_string()
        } else {
            "private `TMPDIR`".to_string()
        }],
        crate::settings::Tempdir::ReadWrite => vec!["host temporary directories".to_string()],
        crate::settings::Tempdir::ReadOnly => Vec::new(),
    };
    if policy
        .get("extends")
        .is_some_and(|profile| profile == ":workspace")
    {
        writable.push("workspace".to_string());
    }
    if let Some(entries) = policy
        .get("filesystem")
        .and_then(|filesystem| filesystem.get("entries"))
        .and_then(Value::as_array)
    {
        for entry in entries {
            if entry.get("access").and_then(Value::as_str) == Some("write")
                && let Some(path) = entry.get("path").and_then(|path| path.get("path"))
            {
                writable.push(format!("`{path}`"));
            }
        }
    }
    let writable = format!(
        "Writable locations: {}; subject to more specific read/deny rules",
        if writable.is_empty() {
            "none".to_string()
        } else {
            writable.join(", ")
        }
    );
    format!(
        "Evaluated code can read host files subject to configured restrictions and {network_access}. {writable}. Package preparation may execute installation or build code with separate filesystem and network permissions; use only trusted dependencies."
    )
}

pub(super) fn stdin_description() -> String {
    format!("{}{}", sections::STDIN_START, sections::STDIN_ORDERING)
}
