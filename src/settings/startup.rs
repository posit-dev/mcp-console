use serde::{Deserialize, Serialize};

pub(crate) const ENVIRONMENT: &str = "MCP_CONSOLE_STARTUP_FILE";
const MAX_ENCODED_BYTES: usize = 32 * 1024;

/// Consume process-handoff data before native or interpreter setup.
///
/// # Safety
/// The process must still be single-threaded, with no concurrent environment readers.
pub(crate) unsafe fn take_environment() -> Option<std::ffi::OsString> {
    let value = std::env::var_os(ENVIRONMENT);
    // SAFETY: the caller owns the single-threaded process bootstrap.
    unsafe { std::env::remove_var(ENVIRONMENT) };
    value
}

/// The launch owner removes private storage even if the worker never consumes it.
pub(crate) struct Transport {
    _file: tempfile::NamedTempFile,
    directory: tempfile::TempDir,
}

impl Transport {
    /// Preserve access to private handoff storage in the captured native policy.
    pub fn preserve_access(&self, policy: &mut super::SandboxSettings) -> Result<(), String> {
        let directory = std::path::absolute(self.directory.path())
            .map_err(|error| format!("cannot resolve startup transport directory: {error}"))?;
        let directory = directory
            .to_str()
            .ok_or("startup transport directory must be UTF-8")?;
        // Leave malformed native policy values intact for launcher validation.
        if let serde_json::Value::Object(filesystem) = policy
            .entry("filesystem")
            .or_insert_with(|| serde_json::json!({"kind": "restricted"}))
            && let serde_json::Value::Array(entries) = filesystem
                .entry("entries")
                .or_insert_with(|| serde_json::json!([]))
        {
            entries.push(serde_json::json!({
                "path": {"type": "path", "path": directory},
                "access": "write",
            }));
        }
        Ok(())
    }
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub(crate) enum Language {
    R,
    Python,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Startup {
    pub language: Language,
    pub code: String,
}

impl Startup {
    pub fn validate(&self) -> Result<(), String> {
        if self.code.trim().is_empty() || self.code.contains('\0') {
            return Err("startup.code must be nonempty source without NUL".into());
        }
        let encoded = serde_json::to_string(self).map_err(|error| error.to_string())?;
        let bytes = encoded.len();
        if bytes > MAX_ENCODED_BYTES {
            return Err(format!(
                "startup encoded source is {bytes} bytes; maximum is {MAX_ENCODED_BYTES} bytes"
            ));
        }
        Ok(())
    }

    pub fn configure(&self, command: &mut std::process::Command) -> Result<Transport, String> {
        let mut builder = tempfile::Builder::new();
        builder.prefix("mcp-console-startup-");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            builder.permissions(std::fs::Permissions::from_mode(0o700));
        }
        let directory = builder
            .tempdir()
            .map_err(|error| format!("cannot create startup transport directory: {error}"))?;
        let mut file = tempfile::NamedTempFile::new_in(directory.path())
            .map_err(|error| format!("cannot create startup transport file: {error}"))?;
        serde_json::to_writer(file.as_file_mut(), self)
            .map_err(|error| format!("cannot write startup transport: {error}"))?;
        command.env(ENVIRONMENT, file.path());
        Ok(Transport {
            _file: file,
            directory,
        })
    }

    pub fn from_file(path: Option<&std::ffi::OsStr>) -> Result<Option<Self>, String> {
        let Some(path) = path else {
            return Ok(None);
        };
        let source = std::fs::read(path)
            .map_err(|error| format!("cannot read startup transport: {error}"))?;
        // Remove before decoding or interpreter setup, including invalid source.
        std::fs::remove_file(path)
            .map_err(|error| format!("cannot consume startup transport: {error}"))?;
        let startup: Self = serde_json::from_slice(&source)
            .map_err(|error| format!("invalid startup configuration: {error}"))?;
        startup.validate()?;
        Ok(Some(startup))
    }
}
