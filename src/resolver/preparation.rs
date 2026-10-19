#![cfg_attr(not(any(unix, windows)), allow(dead_code))]
//! Private preparation traffic, separate from the relay stream. The owner keeps
//! trusted startup choices; each operation completes and retires its own resolver
//! groups before returning a result. Session manifests and activation stay local.

use crate::resolver::{ManagedPython, ManagedR, ResolverControlOutcome};
use crate::worker_protocol::PythonRequirementManifest;
use serde::{Deserialize, Serialize};
use std::io::{Read, Write};

#[cfg(any(unix, windows))]
mod client;
#[cfg(any(unix, windows))]
mod host;
#[cfg(not(any(unix, windows)))]
mod unsupported;
#[cfg(any(unix, windows))]
pub(crate) use client::Preparation;
#[cfg(not(any(unix, windows)))]
pub(crate) use unsupported::Preparation;

const LIMIT: usize = 1024 * 1024;

#[derive(Default, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Selections {
    pub r_home: Option<String>,
    pub python: Option<String>,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Discovery {
    pub managed: bool,
    pub selections: Selections,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub local_r_home_bytes: Option<Vec<u8>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub local_has_uv: Option<bool>,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) enum Operation {
    InspectR {
        #[serde(with = "crate::local_runtime::native_path")]
        executable: std::path::PathBuf,
    },
    Bootstrap,
    ResolveRStandalone {
        requirements: Vec<String>,
    },
    R {
        requirements: Vec<String>,
        #[serde(default, skip_serializing_if = "Vec::is_empty")]
        defaults: Vec<String>,
    },
    Python {
        requirements: PythonRequirementManifest,
        #[serde(default, skip_serializing_if = "Vec::is_empty")]
        defaults: Vec<String>,
        r: Option<ManagedR>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        selected_python: Option<std::path::PathBuf>,
    },
    PythonVersion {
        constraints: Vec<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        r: Option<ManagedR>,
    },
    InspectPython {
        executable: std::path::PathBuf,
    },
    Uv {
        r: ManagedR,
    },
    Duckdb {
        r: ManagedR,
        extensions: Vec<String>,
        #[serde(default, skip_serializing_if = "Vec::is_empty")]
        defaults: Vec<String>,
    },
    DuckdbPython {
        python: ManagedPython,
        extensions: Vec<String>,
        #[serde(default, skip_serializing_if = "Vec::is_empty")]
        defaults: Vec<String>,
        extension_directory: std::path::PathBuf,
    },
    PythonDuckdbAvailable {
        python: ManagedPython,
    },
}

#[derive(Clone, Copy, Default, Deserialize, Serialize)]
pub(crate) enum Mode {
    #[default]
    R,
    PythonOnly,
    Custom,
    Auto,
}

impl Mode {
    fn is_r(&self) -> bool {
        matches!(self, Self::R)
    }
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
enum Input {
    Open {
        #[serde(default, skip_serializing_if = "Mode::is_r")]
        mode: Mode,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        installation: Option<crate::local_runtime::RInstallation>,
    },
    Run {
        id: u64,
        operation: Box<Operation>,
    },
    Control {
        id: u64,
        control: ResolverControlOutcome,
    },
    Close,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
enum Output {
    Hello,
    Diagnostic {
        id: u64,
        text: String,
    },
    ResultChunk {
        id: u64,
        text: String,
    },
    Completed {
        id: u64,
        result: Option<Result<serde_json::Value, String>>,
        control: Option<ResolverControlOutcome>,
        confirmed: bool,
    },
    Controlled {
        id: u64,
        result: Result<bool, String>,
    },
    Closed,
}

impl Output {
    fn write(&self, writer: &mut impl Write) -> Result<(), String> {
        if let Self::Diagnostic { id, text } = self {
            let mut tail = text.as_str();
            while !tail.is_empty() {
                let end = tail.floor_char_boundary((LIMIT / 8).min(tail.len()));
                write_jsonl(
                    writer,
                    &Self::Diagnostic {
                        id: *id,
                        text: tail[..end].into(),
                    },
                )?;
                tail = &tail[end..];
            }
            return Ok(());
        }
        let Self::Completed {
            id,
            result: Some(result),
            control,
            confirmed,
        } = self
        else {
            return write_jsonl(writer, self);
        };
        let result = serde_json::to_string(result).map_err(|error| error.to_string())?;
        if result.len() <= LIMIT / 8 {
            return write_jsonl(writer, self);
        }
        // JSON can expand each text byte to six bytes. Leave room for the
        // envelope; only the terminal receipt completes the assembled result.
        let mut tail = result.as_str();
        while !tail.is_empty() {
            let end = tail.floor_char_boundary((LIMIT / 8).min(tail.len()));
            write_jsonl(
                writer,
                &Self::ResultChunk {
                    id: *id,
                    text: tail[..end].to_owned(),
                },
            )?;
            tail = &tail[end..];
        }
        write_jsonl(
            writer,
            &Self::Completed {
                id: *id,
                result: None,
                control: *control,
                confirmed: *confirmed,
            },
        )
    }
}

fn encode(message: &impl Serialize) -> Result<Vec<u8>, String> {
    let bytes = serde_json::to_vec(message).map_err(|error| error.to_string())?;
    if bytes.len() > LIMIT {
        return Err("resolver message exceeds 1 MiB".into());
    }
    Ok(bytes)
}

fn write_jsonl(writer: &mut impl Write, message: &impl Serialize) -> Result<(), String> {
    let bytes = encode(message)?;
    writer
        .write_all(&bytes)
        .and_then(|()| writer.write_all(b"\n"))
        .and_then(|()| writer.flush())
        .map_err(|error| error.to_string())
}

fn read_jsonl<T: serde::de::DeserializeOwned>(reader: &mut impl Read) -> Result<T, String> {
    let mut bytes = Vec::new();
    loop {
        let mut byte = [0];
        let count = reader.read(&mut byte).map_err(|error| error.to_string())?;
        if count == 0 {
            return Err("resolver input closed".into());
        }
        if byte[0] == b'\n' {
            break;
        }
        bytes.push(byte[0]);
        if bytes.len() > LIMIT {
            return Err("resolver JSON line exceeds 1 MiB".into());
        }
    }
    serde_json::from_slice(&bytes).map_err(|error| format!("invalid resolver JSON: {error}"))
}

pub(crate) fn run_local() -> Result<(), String> {
    #[cfg(any(unix, windows))]
    return host::run();
    #[cfg(not(any(unix, windows)))]
    Err("host resolution requires macOS, Linux, or Windows".into())
}
