use super::Operation;
use crate::resolver::ResolverStopHandle;

#[derive(Clone)]
pub(crate) struct Preparation;

impl Preparation {
    pub(crate) fn open_local(
        _: super::Mode,
        _: Option<crate::settings::SandboxSettings>,
        _: Option<&std::ffi::OsStr>,
        _: Option<crate::local_runtime::RInstallation>,
        _: crate::process_output::Diagnostics,
        _: &dyn Fn(crate::resolver::ResolverStopHandle) -> Result<(), String>,
    ) -> Result<(Self, super::Discovery), String> {
        Err("host resolution requires macOS, Linux, or Windows".into())
    }

    pub(crate) fn call<T>(
        &self,
        _operation: Operation,
        _on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<T, String> {
        Err("host resolution requires macOS, Linux, or Windows".into())
    }

    pub(crate) fn close(&self) -> Result<(), String> {
        Ok(())
    }
}
