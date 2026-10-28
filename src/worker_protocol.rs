use std::collections::BTreeSet;

use serde::{Deserialize, Serialize};

use crate::cell::Language;

pub(crate) const DEFAULT_PYTHON_PACKAGES: &[&str] = &["numpy", "pandas", "matplotlib", "plotnine"];
pub(crate) const DEFAULT_NATIVE_PYTHON_PACKAGES: &[&str] =
    &["numpy", "pandas", "matplotlib", "plotnine", "duckdb"];

pub(crate) fn deserialize_payload_free<'de, D>(deserializer: D) -> Result<(), D::Error>
where
    D: serde::Deserializer<'de>,
{
    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    struct PayloadFree {}

    PayloadFree::deserialize(deserializer).map(drop)
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum RResolutionFailureKind {
    Host,
    Interrupted,
    Operation,
}

#[derive(Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum ServerMessage {
    Evaluate {
        language: Language,
        source: String,
    },
    PrepareR {
        library: String,
    },
    RResolved {
        library: String,
    },
    RResolutionFailed {
        failure: RResolutionFailureKind,
        message: String,
    },
    PreparePython {
        packages: Vec<String>,
    },
    PythonResolved {
        python: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        native: Option<Box<NativePythonActivation>>,
    },
    PythonResolutionFailed {
        message: String,
    },
    PythonVersionResolved {
        version: String,
    },
    PythonVersionResolutionFailed {
        message: String,
    },
    #[serde(deserialize_with = "deserialize_payload_free")]
    Shutdown,
}

#[derive(Clone, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct PythonRequirementManifest {
    pub(crate) packages: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub(crate) python_version: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) exclude_newer: Option<String>,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct NativePythonActivation {
    pub(crate) selected: crate::python::NativePython,
    pub(crate) requirements: PythonRequirementManifest,
}

impl PythonRequirementManifest {
    pub(crate) fn normalized(mut self) -> Self {
        let mut packages = self.packages.into_iter().collect::<BTreeSet<_>>();
        self.packages = DEFAULT_PYTHON_PACKAGES
            .iter()
            .filter(|package| packages.remove(**package))
            .map(|package| (*package).to_string())
            .collect();
        self.packages.extend(packages);
        self.python_version = self
            .python_version
            .into_iter()
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect();
        self
    }
}

pub(crate) fn default_python_requirement_manifest() -> PythonRequirementManifest {
    PythonRequirementManifest {
        packages: DEFAULT_PYTHON_PACKAGES
            .iter()
            .map(|package| (*package).to_string())
            .collect(),
        ..Default::default()
    }
}

pub(crate) fn default_native_python_requirement_manifest() -> PythonRequirementManifest {
    PythonRequirementManifest {
        packages: DEFAULT_NATIVE_PYTHON_PACKAGES
            .iter()
            .map(|package| (*package).to_string())
            .collect(),
        ..Default::default()
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct PythonImportResolution {
    pub(crate) module: String,
    pub(crate) distribution: String,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct PythonResolveRequest {
    pub(crate) requirements: PythonRequirementManifest,
    pub(crate) retained_requirements: PythonRequirementManifest,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub(crate) explicit_declaration: bool,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub(crate) initialized: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) import_resolution: Option<PythonImportResolution>,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct PythonVersionResolveRequest {
    pub(crate) constraints: Vec<String>,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub(crate) explicit_declaration: bool,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum ConsoleChannel {
    Output,
    Diagnostic,
    Result,
}

/// A language failure, distinct from diagnostic output and worker failure.
#[derive(Clone, Debug, serde::Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ExecutionError {
    pub(crate) ename: String,
    pub(crate) evalue: String,
    pub(crate) traceback: Vec<String>,
}

impl ExecutionError {
    pub(crate) fn new(name: &str, message: impl Into<String>) -> Self {
        Self {
            ename: name.into(),
            evalue: message.into(),
            traceback: Vec::new(),
        }
    }
}

#[derive(Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum WorkerMessage {
    #[serde(deserialize_with = "deserialize_payload_free")]
    CellStarted,
    #[serde(deserialize_with = "deserialize_payload_free")]
    Ready,
    RuntimeInitialized {
        interrupted: bool,
    },
    RInitialization {
        complete: bool,
    },
    ConsoleOutput {
        data: String,
    },
    ConsoleDiagnostic {
        data: String,
    },
    Image {
        data: String,
        mime_type: String,
    },
    InputRequested {
        prompt: String,
    },
    #[serde(deserialize_with = "deserialize_payload_free")]
    InputReceived,
    #[serde(deserialize_with = "deserialize_payload_free")]
    InputCancelled,
    RPrepared {
        library: String,
    },
    RPreparationFailed {
        message: String,
    },
    ResolveR {
        packages: Vec<String>,
    },
    RActivated {
        library: String,
    },
    RActivationFailed {
        library: String,
        message: String,
    },
    ResolvePython {
        request: PythonResolveRequest,
    },
    ResolvePythonVersion {
        request: PythonVersionResolveRequest,
    },
    PythonActivated {
        requirements: PythonRequirementManifest,
    },
    PythonActivationFailed {
        requirements: PythonRequirementManifest,
    },
    #[serde(deserialize_with = "deserialize_payload_free")]
    PythonPrepared,
    PythonPreparationFailed {
        message: String,
    },
    PythonPreparationRejected {
        message: String,
    },
    #[serde(deserialize_with = "deserialize_payload_free")]
    Completed,
    ExecutionError {
        error: ExecutionError,
    },
    ExecutionResult {
        text: String,
    },
}

#[cfg(test)]
mod tests {
    use super::{
        PythonImportResolution, PythonResolveRequest, RResolutionFailureKind, ServerMessage,
        WorkerMessage, default_python_requirement_manifest,
    };

    fn assert_encoding(message: &impl serde::Serialize, expected: &str) {
        assert_eq!(serde_json::to_string(message).unwrap(), expected);
    }

    fn accepted_unknown_fields<T: serde::de::DeserializeOwned>(
        messages: &[&'static str],
    ) -> Vec<&'static str> {
        messages
            .iter()
            .copied()
            .filter(|message| serde_json::from_str::<T>(message).is_ok())
            .collect()
    }

    #[test]
    fn payload_free_server_messages_reject_unknown_fields() {
        let accepted =
            accepted_unknown_fields::<ServerMessage>(&[r#"{"kind":"shutdown","obsolete":true}"#]);
        assert!(accepted.is_empty(), "accepted unknown fields: {accepted:?}");
    }

    #[test]
    fn payload_free_server_messages_retain_their_encoding() {
        assert_encoding(&ServerMessage::Shutdown, r#"{"kind":"shutdown"}"#);
    }

    #[test]
    fn runtime_r_server_messages_retain_their_encoding() {
        assert_encoding(
            &ServerMessage::RResolved {
                library: "/managed/r".to_string(),
            },
            r#"{"kind":"r_resolved","library":"/managed/r"}"#,
        );
        for (failure, encoded) in [
            (RResolutionFailureKind::Host, "host"),
            (RResolutionFailureKind::Interrupted, "interrupted"),
            (RResolutionFailureKind::Operation, "operation"),
        ] {
            assert_encoding(
                &ServerMessage::RResolutionFailed {
                    failure,
                    message: "resolution failed".to_string(),
                },
                &format!(
                    r#"{{"kind":"r_resolution_failed","failure":"{encoded}","message":"resolution failed"}}"#
                ),
            );
        }
    }

    #[test]
    fn payload_free_worker_messages_reject_unknown_fields() {
        let accepted = accepted_unknown_fields::<WorkerMessage>(&[
            r#"{"kind":"ready","obsolete":true}"#,
            r#"{"kind":"input_received","obsolete":true}"#,
            r#"{"kind":"input_cancelled","obsolete":true}"#,
            r#"{"kind":"python_prepared","obsolete":true}"#,
            r#"{"kind":"completed","obsolete":true}"#,
            r#"{"kind":"python_prepared","python_checkpoint":{"packages":[]}}"#,
            r#"{"kind":"completed","python_checkpoint":{"packages":[]}}"#,
        ]);
        assert!(accepted.is_empty(), "accepted unknown fields: {accepted:?}");
    }

    #[test]
    fn payload_free_worker_messages_retain_their_encoding() {
        for (message, expected) in [
            (WorkerMessage::Ready, r#"{"kind":"ready"}"#),
            (WorkerMessage::InputReceived, r#"{"kind":"input_received"}"#),
            (
                WorkerMessage::InputCancelled,
                r#"{"kind":"input_cancelled"}"#,
            ),
            (
                WorkerMessage::PythonPrepared,
                r#"{"kind":"python_prepared"}"#,
            ),
            (WorkerMessage::Completed, r#"{"kind":"completed"}"#),
        ] {
            assert_encoding(&message, expected);
        }
    }

    #[test]
    fn runtime_r_worker_messages_retain_their_encoding() {
        for (message, expected) in [
            (
                WorkerMessage::ResolveR {
                    packages: vec!["cli".to_string(), "glue".to_string()],
                },
                r#"{"kind":"resolve_r","packages":["cli","glue"]}"#,
            ),
            (
                WorkerMessage::RActivated {
                    library: "/managed/r".to_string(),
                },
                r#"{"kind":"r_activated","library":"/managed/r"}"#,
            ),
            (
                WorkerMessage::RActivationFailed {
                    library: "/managed/r".to_string(),
                    message: "activation failed".to_string(),
                },
                r#"{"kind":"r_activation_failed","library":"/managed/r","message":"activation failed"}"#,
            ),
        ] {
            assert_encoding(&message, expected);
        }
    }

    #[test]
    fn python_import_resolution_metadata_rejects_unknown_fields_and_retains_its_encoding() {
        assert!(
            serde_json::from_str::<PythonResolveRequest>(
                r#"{"requirements":{"packages":["numpy","pandas","py-yaml12"]},"retained_requirements":{"packages":["numpy","pandas","py-yaml12"]},"import_resolution":{"module":"yaml12","distribution":"py-yaml12","obsolete":true}}"#,
            )
            .is_err()
        );
        let mut requirements = default_python_requirement_manifest();
        requirements.packages.push("py-yaml12".to_string());
        assert_encoding(
            &PythonResolveRequest {
                requirements: requirements.clone(),
                retained_requirements: requirements,
                explicit_declaration: false,
                initialized: false,
                import_resolution: Some(PythonImportResolution {
                    module: "yaml12".to_string(),
                    distribution: "py-yaml12".to_string(),
                }),
            },
            r#"{"requirements":{"packages":["numpy","pandas","matplotlib","plotnine","py-yaml12"]},"retained_requirements":{"packages":["numpy","pandas","matplotlib","plotnine","py-yaml12"]},"import_resolution":{"module":"yaml12","distribution":"py-yaml12"}}"#,
        );
    }
}
