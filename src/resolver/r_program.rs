//! Keep embedded resolver source alive through Rscript's complete invocation.
use std::process::Command;

#[cfg(windows)]
const READ_ACCESS: &str = include_str!("programs/windows_read_access.R");

pub(super) struct Rscript {
    #[cfg(windows)]
    _wrapper: Option<tempfile::NamedTempFile>,
}

impl Rscript {
    /// Keep ir's Rscript adapter alive until its operation and descendants retire.
    /// It adapts R read/search queries; the existing Job and MXC own access.
    pub(super) fn append(
        command: &mut Command,
        executable: &std::path::Path,
    ) -> Result<Self, String> {
        #[cfg(windows)]
        if std::env::var_os(crate::settings::SANDBOX_MARKER).is_some() {
            use std::io::Write;
            let mut wrapper = tempfile::Builder::new()
                .prefix("mcp-console-rscript-")
                .suffix(".cmd")
                .tempfile()
                .map_err(|error| error.to_string())?;
            wrapper.write_all(b"@setlocal DisableDelayedExpansion\r\n@\"%MCP_CONSOLE_RSCRIPT_ADAPTER%\" resolver-rscript -- \"%MCP_CONSOLE_RSCRIPT_EXECUTABLE%\" %*\r\n")
                .map_err(|error| error.to_string())?;
            command
                .arg(wrapper.path())
                .env(
                    "MCP_CONSOLE_RSCRIPT_ADAPTER",
                    std::env::current_exe().map_err(|error| error.to_string())?,
                )
                .env("MCP_CONSOLE_RSCRIPT_EXECUTABLE", executable);
            return Ok(Self {
                _wrapper: Some(wrapper),
            });
        }
        command.arg(executable);
        Ok(Self {
            #[cfg(windows)]
            _wrapper: None,
        })
    }
}

pub(super) struct RProgram {
    #[cfg(windows)]
    _file: tempfile::NamedTempFile,
}

impl RProgram {
    pub(super) fn append(command: &mut Command, source: &str) -> Result<Self, String> {
        #[cfg(unix)]
        {
            command.args(["-e", source]);
            Ok(Self {})
        }
        #[cfg(windows)]
        {
            // Windows Rscript reparses -e arguments and truncates multiline
            // expressions. Materialize the compile-time source as a script;
            // neither the checkout nor an installation layout is consulted.
            let program = Self::create(source.as_bytes())?;
            command.arg(program._file.path());
            Ok(program)
        }
    }

    #[cfg(windows)]
    fn create(source: &[u8]) -> Result<Self, String> {
        use std::io::Write;
        let mut file = tempfile::Builder::new()
            .prefix("mcp-console-resolver-")
            .suffix(".R")
            .tempfile()
            .map_err(|error| error.to_string())?;
        if std::env::var_os(crate::settings::SANDBOX_MARKER).is_some() {
            file.write_all(READ_ACCESS.as_bytes())
                .map_err(|error| error.to_string())?;
            file.write_all(b"\n").map_err(|error| error.to_string())?;
        }
        file.write_all(source.strip_prefix(b"\xef\xbb\xbf").unwrap_or(source))
            .map_err(|error| error.to_string())?;
        Ok(Self { _file: file })
    }
}

/// Forward an ir-owned Rscript invocation inside its existing preparation Job.
/// No policy, discovery, session state or additional retirement owner lives here.
#[cfg(windows)]
pub(crate) fn run_rscript(command: &[std::ffi::OsString]) -> Result<i32, String> {
    let (executable, arguments) = command.split_first().ok_or("missing Rscript executable")?;
    let mut child = super::process::resolver_command(std::path::Path::new(executable));
    let mut program = None;
    if let Some(index) = arguments.iter().position(|argument| argument == "-e") {
        let source = arguments
            .get(index + 1)
            .and_then(|value| value.to_str())
            .ok_or("missing UTF-8 Rscript expression")?;
        program = Some(RProgram::create(source.as_bytes())?);
        child
            .args(&arguments[..index])
            .arg(program.as_ref().expect("R program")._file.path())
            .args(&arguments[index + 2..]);
    } else if let Some(index) = arguments
        .iter()
        .position(|argument| !argument.to_string_lossy().starts_with('-'))
    {
        let source = std::fs::read(&arguments[index])
            .map_err(|error| format!("cannot read Rscript program: {error}"))?;
        program = Some(RProgram::create(&source)?);
        child
            .args(&arguments[..index])
            .arg(program.as_ref().expect("R program")._file.path())
            .args(&arguments[index + 1..]);
    } else {
        child.args(arguments);
    }
    let status = child
        .status()
        .map_err(|error| format!("cannot start preparation Rscript: {error}"))?;
    drop(program);
    Ok(status.code().unwrap_or(1))
}
