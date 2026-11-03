"""Equivalent user policies shared by Unix and native Windows acceptance."""

import os

MANAGED_TMPDIR_DESCRIPTION = (
    "private `TMPDIR` (also `TEMP` and `TMP`)"
    if os.name == "nt"
    else "private `TMPDIR`"
)

CONFIGURATION_EXAMPLES = {
    "common": {
        "sandbox": {
            "filesystem": {"read_write": ["."]},
            "network": {"allow": ["api.example.com"]},
        }
    },
    "limited": {
        "sandbox": {
            "network": {
                "allow": ["*.example.org", "**.example.net"],
                "deny": ["blocked.example.org"],
                "proxy": {"mode": "limited"},
            }
        }
    },
    "empty": {
        "sandbox": {
            "filesystem": {},
            "network": {"sockets": {"unix_sockets": []}},
        }
    },
    "enabled": {"sandbox": {"network": "enabled"}},
    "restricted": {"sandbox": {"network": "restricted"}},
    "read_only": {"sandbox": {"profile": "read_only"}},
    "client_default": {"sandbox": {"profile": "inherit_codex"}},
    "workspace": {"sandbox": {"profile": "workspace"}},
    "workspace_fallback": {
        "sandbox": {"profile": {"inherit_codex": {"fallback": "workspace"}}}
    },
    "workspace_read_write_temp": {
        "sandbox": {
            "profile": "workspace",
            "tempdir": "read_write",
            "filesystem": {"read_only": ["./data"], "deny": ["./secrets"]},
            "network": "enabled",
        }
    },
    "read_only_temp": {"sandbox": {"tempdir": "read_only"}},
    "groups": {
        "sandbox": {
            "filesystem": {
                "read_only": ["./data"],
                "read_write": ["."],
                "deny": ["./secrets"],
            }
        }
    },
}

INVALID_CONFIGURATIONS = [
    ("sandbox.profile", "workspace_write"),
    ("sandbox.profile", "workspace-write"),
    ("sandbox.profile", ":workspace"),
    ("sandbox.workspace_options", {}),
    ("sandbox.tempdir", True),
    ("sandbox.tempdir", None),
    ("sandbox.tempdir", "unmanaged"),
    ("sandbox.network", "full"),
    ("sandbox.network", True),
    ("sandbox", []),
    ("sandbox.filesystem", []),
    ("resolver", []),
    ("sandbox.network.proxy", []),
    ("sandbox.network.sockets", []),
    ("sandbox.network.proxy.domains", []),
    ("sandbox.network.proxy", None),
    ("sandbox.network.proxy.enabled", True),
    ("sandbox.network.proxy.enableSocks5", True),
    ("sandbox.network.proxy.enable_socks5", True),
    ("sandbox.network.proxy.domains", {"example.org": "allow"}),
    ("sandbox.network.allow", "example.org"),
    ("sandbox.network.allow", None),
    ("sandbox.network.deny", {"example.org": True}),
    ("sandbox.network.deny", None),
    ("sandbox.network.allow", ["example.org:443"]),
    ("sandbox.network.allow", ["[::1]:443"]),
    ("sandbox.network.allow", ["fe80::1%eth0:443"]),
    ("sandbox.network.allow", ["[fe80::1%eth0:443]"]),
    ("sandbox.network.allow", ["127.0.0.1%x:443"]),
    ("sandbox.network.allow", ["127.0.0.1%eth0"]),
    ("sandbox.network.allow", ["fe80::1%"]),
    ("sandbox.network.allow", ["fe80::1%eth0%other"]),
    ("sandbox.network.allow", ["fe80::1%eth 0"]),
    ("sandbox.network.allow", ["[example.org]suffix"]),
    ("sandbox.network.sockets.unix_sockets", True),
    ("sandbox.network.sockets.unix_sockets", "all"),
    ("sandbox.network.sockets.unix_sockets", "*"),
    ("sandbox.network.sockets.unix_sockets", {"deny": ["/tmp/a.sock"]}),
    ("sandbox.network.sockets.deny", ["/tmp/a.sock"]),
    ("sandbox.network.sockets.dangerously_allow_all_unix_sockets", True),
    ("sandbox.filesystem", {"entries": []}),
    ("sandbox.filesystem.read_write", "."),
    ("sandbox.environment", {"TOKEN": "secret sentinel"}),
    ("environment", {"TOKEN": 654987123}),
    ("environment", "secret sentinel"),
    ("resolver.environment", "secret sentinel"),
    ("resolver.environment", {"TOKEN": False}),
    ("extends", ":workspace"),
    ("sandbox.unsupported", True),
]
