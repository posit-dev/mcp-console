//! Requirement keys retain hidden-provider capability; their prose follows visibility.
use serde_json::{Map, Value};

use crate::cell::Languages;

pub(super) fn configure(properties: &mut Map<String, Value>, languages: Languages, builtin: bool) {
    let description = String::from(
        "Inspect declarations or prepare dependencies on the execution host without importing, \
attaching, or loading them. Requires host preparation support; bare runtimes use installed packages. Alone, performs standalone preparation. With a cell, preparation \
precedes code; without control it also precedes bundled stdin. Failure or a required restart \
withholds the cell. \
Compatible additions preserve live state on an idle worker; other changes need restart. \
With restart, candidate resolution precedes replacement; resolution failure preserves the \
current worker and sends no stdin/code. Standalone preparation rejects nonempty stdin. \
Only add can accompany interrupt, with a following cell; providers without this preparation \
support reject the combination before signaling or input. Otherwise interrupt and stdin \
precede deferred validation/preparation and are not rolled back on failure.",
    );
    let requirements = &mut properties["requirements"];
    requirements["description"] = description.into();
    let fields = requirements["properties"]
        .as_object_mut()
        .expect("requirements schema properties");
    if !languages.r || !builtin {
        fields["r"]["description"] = HIDDEN_HOST_PACKAGES.into();
    }
    if !languages.python || !builtin {
        fields["python"]["description"] = HIDDEN_REGISTRY_PACKAGES.into();
    }
    if !languages.python {
        fields["python_version"]["description"] = HIDDEN_VERSION.into();
        fields["exclude_newer"]["description"] = HIDDEN_CUTOFF.into();
        fields["action"]["description"] = ACTION.into();
    }
}

const HIDDEN_HOST_PACKAGES: &str = "Single-line ir package references for dependency preparation, \
e.g. DBI or duckdb, including supported remote references; local sources and NUL/line breaks \
are rejected.";

const HIDDEN_REGISTRY_PACKAGES: &str = "Named PEP 508 registry requirements for dependency \
preparation, e.g. duckdb>=1, with versions, extras, or markers. Paths, URLs, editable requirements, \
direct references, and local sources are rejected. Bare or user-selected environments and \
custom workers disable managed registry requirements.";

const HIDDEN_VERSION: &str = "Runtime version numbers or ==, !=, <, <=, >, >= constraints \
(e.g. >=3.11). add appends; changes with a live worker require control=\"restart\".";

const HIDDEN_CUTOFF: &str = "Package publication cutoff accepted by uv, e.g. \"2026-01-01\". \
set clears an omitted/null cutoff; add preserves omission and cannot replace an existing one.";

const ACTION: &str = "get reads the retained declaration, not an installed-package inventory, \
without starting a worker or consuming output; use it alone, without code, stdin, control, \
or payload fields. The complete declaration is in structuredContent.requirements. \
add (default) accumulates up to 64 entries per language per call; bare {} is invalid. \
set replaces the whole declaration without defaults; omitted lists/constraints are empty, \
even with only action supplied. reset restores startup defaults and rejects payload fields. \
Changed set/reset with a live worker require control=\"restart\"; unchanged declarations are \
no-ops. set accepts the complete accumulated manifest without add's per-list limit.";
