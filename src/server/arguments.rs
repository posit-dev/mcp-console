use rmcp::schemars;
use serde::Deserialize;

const DEFAULT_TIMEOUT_MS: u64 = 60_000;

impl SendArguments {
    /// Inspect source keys before decoding nullable values or executing any same-call effects.
    pub(super) fn validate_fields(
        arguments: Option<&serde_json::Map<String, serde_json::Value>>,
        languages: crate::cell::Languages,
        setting: &str,
    ) -> Result<(), String> {
        let Some(arguments) = arguments else {
            return Ok(());
        };
        for language in [
            crate::cell::Language::R,
            crate::cell::Language::Python,
            crate::cell::Language::Sql,
        ] {
            let field = crate::cell::Languages::field(language);
            if arguments.contains_key(field) && !languages.enables(language) {
                return Err(if setting == crate::cell::LANGUAGES_ENV {
                    format!("`{field}` cells are disabled by `{setting}`")
                } else {
                    format!("`{field}` source fields are hidden by `{setting}`")
                });
            }
        }
        let mut fields = languages.fields();
        fields.extend(["control", "requirements", "stdin", "timeout_ms"]);
        if let Some(unknown) = arguments
            .keys()
            .find(|field| !fields.contains(&field.as_str()))
        {
            let expected = fields
                .iter()
                .map(|field| format!("`{field}`"))
                .collect::<Vec<_>>()
                .join(", ");
            return Err(format!(
                "failed to deserialize parameters: unknown field `{unknown}`, expected one of {expected}"
            ));
        }
        Ok(())
    }
}

#[derive(Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub(super) struct SendArguments {
    #[schemars(description = super::presentation::r_description())]
    pub(super) r: Option<String>,
    #[schemars(description = super::presentation::python_description())]
    pub(super) python: Option<String>,
    #[schemars(description = super::presentation::sql_description())]
    pub(super) sql: Option<String>,
    #[serde(default = "default_timeout_ms")]
    #[schemars(description = super::presentation::timeout_description())]
    pub(super) timeout_ms: u64,
    #[schemars(description = super::presentation::control_description())]
    pub(super) control: Option<SendControl>,
    #[schemars(description = super::presentation::stdin_description())]
    pub(super) stdin: Option<String>,
    /// Inspect declarations or prepare dependencies on the execution host without importing,
    /// attaching, or loading them. Requires host preparation support; bare runtimes use installed
    /// packages. Alone, performs standalone preparation. With a cell, preparation
    /// precedes code; without control it also precedes bundled stdin. Failure or a required restart
    /// withholds the cell.
    /// Compatible additions preserve live state on an idle worker; other changes need restart.
    /// With restart, candidate resolution precedes replacement; resolution failure preserves the
    /// current worker and sends no stdin/code. Standalone preparation rejects nonempty stdin.
    /// Only add can accompany interrupt, with a following cell; providers without this preparation
    /// support reject the combination before signaling or input. Otherwise interrupt and stdin
    /// precede deferred validation/preparation and are not rolled back on failure.
    pub(super) requirements: Option<Requirements>,
}

#[derive(Clone, Copy, Deserialize, schemars::JsonSchema)]
#[schemars(inline)]
#[serde(rename_all = "snake_case")]
pub(super) enum SendControl {
    Interrupt,
    Restart,
}

#[derive(Deserialize, schemars::JsonSchema)]
#[schemars(inline)]
#[serde(deny_unknown_fields)]
pub(super) struct Requirements {
    /// get reads the retained declaration, not an installed-package inventory, without starting a
    /// worker or consuming output; use it alone, without code, stdin, control, or payload fields.
    /// The complete declaration is in structuredContent.requirements.
    /// add (default) accumulates up to 64 entries per language per call; bare {} is invalid.
    /// set replaces the whole declaration without defaults; omitted lists/constraints are empty,
    /// even with only action supplied. reset restores startup defaults and rejects payload fields.
    /// Changed set/reset with a live worker require control="restart"; unchanged declarations are
    /// no-ops. set accepts the complete accumulated manifest without add's per-list limit.
    #[serde(default)]
    pub(super) action: crate::worker_client::RequirementsAction,
    /// Python version numbers or ==, !=, <, <=, >, >= constraints (e.g. >=3.11).
    /// add appends; changes with a live worker require control="restart".
    #[serde(default, deserialize_with = "supplied_list")]
    #[schemars(with = "Vec<String>")]
    pub(super) python_version: Option<Vec<String>>,
    /// Package publication cutoff accepted by uv, e.g. "2026-01-01".
    /// set clears an omitted/null cutoff; add preserves omission and cannot replace an existing one.
    #[serde(default, deserialize_with = "supplied_nullable")]
    pub(super) exclude_newer: Option<Option<String>>,
    /// Managed DuckDB extension names, e.g. fts or spatial: lowercase ASCII letter first, then
    /// lowercase letters, digits, or underscores (at most 64 characters).
    /// Use DuckDB's LOAD or automatic loading after preparation.
    #[serde(default, deserialize_with = "supplied_list")]
    #[schemars(with = "Vec<String>", inner(length(min = 1, max = 64)))]
    pub(super) duckdb: Option<Vec<String>>,
    /// Single-line ir package references, e.g. data.table or sf, including supported remote
    /// references; local sources and NUL/line breaks are rejected. Automatic R loads accept plain
    /// package names only; use this field for explicit references or preparation before use.
    #[serde(default, deserialize_with = "supplied_list")]
    #[schemars(with = "Vec<String>", inner(length(min = 1)))]
    pub(super) r: Option<Vec<String>>,
    /// Named PEP 508 registry requirements with versions, extras, or markers, e.g. polars>=1 or
    /// requests[socks]. Paths, URLs, editable requirements, direct references, and local sources
    /// are rejected. Bare or explicitly selected Python environments and custom workers disable
    /// managed Python requirements.
    #[serde(default, deserialize_with = "supplied_list")]
    #[schemars(with = "Vec<String>", inner(length(min = 1)))]
    pub(super) python: Option<Vec<String>>,
}

fn supplied_list<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> Result<Option<Vec<String>>, D::Error> {
    Vec::<String>::deserialize(deserializer).map(Some)
}

fn supplied_nullable<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> Result<Option<Option<String>>, D::Error> {
    Option::<String>::deserialize(deserializer).map(Some)
}

fn default_timeout_ms() -> u64 {
    DEFAULT_TIMEOUT_MS
}
