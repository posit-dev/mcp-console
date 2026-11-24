use super::Operation;
use crate::resolver::ResolverStopHandle;

#[derive(Clone)]
pub(crate) struct Preparation {
    pub(crate) discovery: std::sync::Arc<super::Discovery>,
    pub(crate) caches: super::PreparationCaches,
}

impl Preparation {
    pub(crate) fn open_local(
        _: super::Worker<'_>,
        _: crate::settings::SandboxSettings,
        _: bool,
        _: Option<&std::ffi::OsStr>,
        _: crate::process_output::Diagnostics,
        _: &dyn Fn(crate::resolver::ResolverStopHandle) -> Result<(), String>,
    ) -> Result<Self, String> {
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
