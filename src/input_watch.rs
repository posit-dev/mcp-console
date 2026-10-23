//! Observe input closure without consuming queued protocol bytes.
#[cfg(target_os = "linux")]
mod linux;
#[cfg(target_os = "macos")]
mod macos;
#[cfg(target_os = "linux")]
pub(crate) use linux::InputWatch;
#[cfg(target_os = "macos")]
pub(crate) use macos::InputWatch;
