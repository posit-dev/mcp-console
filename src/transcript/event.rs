use rmcp::{
    ErrorData,
    model::{Annotations, CallToolRequestParams, ContentBlock, MetaObject, RequestId, TextContent},
};
use serde::Serialize;
use serde_json::Value;

#[derive(Serialize)]
pub(super) struct Envelope<'a> {
    #[serde(flatten)]
    pub event: Event<'a>,
    pub run_id: &'a str,
    pub sequence: u64,
    pub at: String,
}

#[derive(Serialize)]
#[serde(tag = "event", rename_all = "snake_case")]
pub(super) enum Event<'a> {
    RecordingStarted {
        console_version: &'a str,
        working_directory: &'a str,
        dynamic_resolution: Option<bool>,
        python_preparation: Option<bool>,
        startup_requirements: Option<&'a crate::worker_client::Declaration>,
    },
    RecordingActivated,
    RecordingEnded,
    Protocol {
        direction: &'a str,
        message: Value,
    },
    Delivery {
        request_id: Value,
        call_id: Option<u64>,
        delivered: bool,
    },
    WorkerStarted {
        #[serde(skip_serializing_if = "default_session")]
        session: &'a str,
        session_id: u64,
        pid: u32,
        requirements: &'a Value,
    },
    WorkerReady {
        session_id: u64,
    },
    WorkerEnded {
        session_id: u64,
        outcome: &'a str,
    },
    /// Server admission can precede worker acquisition and an initial response.
    /// Keep the submitted source pending until association or settlement.
    SourceAdmitted {
        call_id: u64,
    },
    /// Server admission settled, including source withheld before cell entry.
    /// Unlike CellEnded, this does not require execution to have started.
    SourceFinished {
        call_id: u64,
    },
    /// The actual worker lifetime owning this submission, even if it never runs.
    SourceAssociated {
        call_id: u64,
        session_id: u64,
    },
    CellStarted {
        call_id: u64,
        owner: super::Owner,
    },
    CellEnded {
        call_id: u64,
        owner: super::Owner,
        completion_confirmed: bool,
    },
    OutputText {
        owner: super::Owner,
        path: &'a str,
        offset: u64,
        bytes: usize,
        discarded_bytes: usize,
    },
    /// Rejected by raw image retention limits: no image file was created.
    /// Retained images have an ImageRetained event with their file path.
    ImageOmitted {
        owner: Option<super::ImageOwner>,
        encoded_bytes: usize,
        mime_type: &'a str,
    },
    ProcessOutput {
        data: &'a str,
        discarded_bytes: usize,
    },
    EnvironmentDiscovered {
        #[serde(skip_serializing_if = "default_session")]
        session: &'a str,
        dynamic_resolution: bool,
        python_preparation: bool,
        r_available: bool,
        startup_requirements: &'a crate::worker_client::Declaration,
    },
    StartupFailed {
        session_id: Option<u64>,
        message: &'a str,
    },
    PythonEnvironmentAccepted {
        packages: &'a [String],
    },
    RequirementsAccepted {
        #[serde(skip_serializing_if = "default_session")]
        session: &'a str,
        requirements: &'a Value,
    },
    RequirementsSelected {
        call_id: Option<u64>,
        action: &'a str,
        snapshot: &'a Value,
    },
    ToolCall {
        call_id: u64,
        request_id: &'a RequestId,
        request: &'a CallToolRequestParams,
        #[serde(skip_serializing_if = "Option::is_none")]
        origin: Option<&'static str>,
        #[serde(skip_serializing_if = "std::ops::Not::not")]
        omit_from_projections: bool,
    },
    ImageRetained {
        owner: super::ImageOwner,
        artifact_id: u64,
        call_id: Option<u64>,
        path: &'a str,
        mime_type: &'a str,
        bytes: usize,
    },
    CellOutput {
        owner: super::Owner,
        call_id: u64,
        path: &'a str,
        retained_bytes: u64,
        inline_omitted_bytes: u64,
        discarded_bytes: u64,
        retention_limit_bytes: u64,
    },
    SessionOutput {
        owner: super::Owner,
        path: &'a str,
        retained_bytes: u64,
        inline_omitted_bytes: u64,
        discarded_bytes: u64,
        retention_limit_bytes: u64,
    },
    ToolResult {
        call_id: u64,
        parts: &'a [super::ResultPart],
        #[serde(flatten)]
        outcome: Outcome<'a>,
        #[serde(skip_serializing_if = "std::ops::Not::not")]
        omit_from_projections: bool,
    },
}

fn default_session(session: &&str) -> bool {
    *session == "default"
}

#[derive(Serialize)]
#[serde(untagged)]
pub(super) enum Outcome<'a> {
    Result { result: RecordedResult<'a> },
    Error { error: &'a ErrorData },
}

// Record the stable tool payload, without protocol-version-specific resultType.
#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub(super) struct RecordedResult<'a> {
    pub content: Vec<RecordedContent<'a>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub structured_content: Option<&'a Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub is_error: Option<bool>,
    #[serde(rename = "_meta", skip_serializing_if = "Option::is_none")]
    pub meta: Option<&'a MetaObject>,
}

#[derive(Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub(super) enum RecordedContent<'a> {
    Text(&'a TextContent),
    Image {
        #[serde(rename = "mimeType")]
        mime_type: &'a str,
        #[serde(rename = "_meta", skip_serializing_if = "Option::is_none")]
        meta: Option<&'a MetaObject>,
        #[serde(skip_serializing_if = "Option::is_none")]
        annotations: Option<&'a Annotations>,
        #[serde(rename = "artifactId")]
        #[serde(skip_serializing_if = "Option::is_none")]
        artifact_id: Option<u64>,
        #[serde(skip_serializing_if = "Option::is_none")]
        path: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        retained: Option<bool>,
    },
    #[serde(untagged)]
    Other(&'a ContentBlock),
}
