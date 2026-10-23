use std::ffi::OsString;
use std::process::ExitCode;

pub(super) fn run(_command: &[OsString]) -> Result<ExitCode, String> {
    Err("`mcp-console sandbox` is not supported on this operating system".to_string())
}
