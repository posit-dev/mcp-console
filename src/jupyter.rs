//! Captured launch selection for the Jupyter protocol adapter.

use std::path::PathBuf;

#[cfg(target_os = "linux")]
mod linux;

#[derive(Clone)]
pub(crate) struct Configuration {
    pub(crate) kernel: String,
    pub(crate) python: PathBuf,
    pub(crate) transport: Transport,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, clap::ValueEnum)]
pub enum Transport {
    #[default]
    Auto,
    Tcp,
    Ipc,
}

impl Transport {
    pub(crate) fn name(self) -> &'static str {
        match self {
            Self::Auto => "auto",
            Self::Tcp => "tcp",
            Self::Ipc => "ipc",
        }
    }
}

impl Configuration {
    /// Grant only the private transport needed by the selected local kernel.
    /// This never authorizes access to host services or external networks.
    pub(crate) fn launch(
        &self,
        no_sandbox: bool,
        settings: &mut crate::settings::SandboxSettings,
    ) -> Result<Transport, String> {
        let restricted =
            settings.get("network").and_then(serde_json::Value::as_str) == Some("restricted");
        let transport = match self.transport {
            Transport::Auto if !no_sandbox && restricted && cfg!(target_os = "macos") => {
                Transport::Ipc
            }
            Transport::Auto => Transport::Tcp,
            transport => transport,
        };
        if no_sandbox {
            return Ok(transport);
        }
        #[cfg(target_os = "macos")]
        if transport == Transport::Ipc {
            if crate::settings::Tempdir::from_settings(settings)
                != crate::settings::Tempdir::Managed
            {
                return Err("Jupyter private IPC requires sandbox.tempdir: managed".into());
            }
            if settings
                .get("filesystem")
                .is_some_and(|filesystem| filesystem["kind"] == "unrestricted")
            {
                // Preserve inherited host access using the existing root-write
                // representation. The runner can then protect its private root
                // and supply RUNNER_PRIVATE_DATA for the IPC path rules.
                settings.insert(
                    "filesystem".into(),
                    serde_json::json!({
                        "kind": "restricted",
                        "entries": [{
                            "path": {"type": "special", "value": {"kind": "root"}},
                            "access": "write",
                        }],
                    }),
                );
            }
            let extension = settings
                .entry("macos_seatbelt_profile_extension")
                .or_insert_with(|| "".into());
            let mut policy = extension
                .as_str()
                .expect("captured Seatbelt extension")
                .to_owned();
            policy.push_str(include_str!("jupyter/private_ipc.sbpl"));
            *extension = policy.into();
        }
        #[cfg(target_os = "linux")]
        linux::configure(settings, transport)?;
        Ok(transport)
    }

    pub(crate) fn languages(&self) -> crate::cell::Languages {
        crate::cell::Languages {
            kernel: true,
            r: false,
            python: false,
            sql: false,
        }
    }
}
