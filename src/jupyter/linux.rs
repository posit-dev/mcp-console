//! Private kernel communication inside the runner's Linux network namespace.

use super::Transport;
use crate::settings::SandboxSettings;
use serde_json::{Value, json};

pub(super) fn configure(
    settings: &mut SandboxSettings,
    transport: Transport,
) -> Result<(), String> {
    if let Some(proxy) = settings.get("proxy") {
        if transport == Transport::Ipc && proxy["dangerouslyAllowAllUnixSockets"] != true {
            return Err("Jupyter IPC requires Unix-socket access unavailable under this Linux proxy policy; use tcp for communication inside the private network namespace".into());
        }
        return Ok(());
    }
    if settings.get("network").and_then(Value::as_str) == Some("enabled") {
        return Ok(());
    }
    match transport {
        Transport::Auto => unreachable!("Jupyter transport must be resolved before sandbox setup"),
        Transport::Ipc => {
            return Err("Jupyter IPC is unavailable under the pinned Linux restricted-network policy; use auto or tcp for communication inside a private network namespace".into());
        }
        Transport::Tcp => {}
    }
    // The pinned runner's restricted seccomp denies even local bind/connect.
    // Its managed-network path permits TCP only inside a new network namespace.
    // An empty proxy allowlist supplies that path without any host destination;
    // standalone Unix sockets remain denied, so host services stay unreachable.
    settings.insert(
        "proxy".into(),
        json!({
            "enabled": true,
            "mode": "limited",
            "enableSocks5": false,
            "enableSocks5Udp": false,
            "allowUpstreamProxy": false,
            "domains": {},
            "unixSockets": {},
            "dangerouslyAllowAllUnixSockets": false,
            "allowLocalBinding": false,
        }),
    );
    Ok(())
}
