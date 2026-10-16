"""Equivalent user policies shared by Unix and native Windows acceptance."""

CONFIGURATION_EXAMPLES = {
    "common": {
        "sandbox": {
            "filesystem": {"read_write": ["."]},
            "network": {"proxy": {"domains": {"allow": ["api.example.com"]}}},
        }
    },
    "limited": {
        "sandbox": {
            "network": {
                "proxy": {
                    "mode": "limited",
                    "domains": {
                        "allow": ["*.example.org", "**.example.net"],
                        "deny": ["blocked.example.org"],
                    },
                }
            }
        }
    },
    "empty": {
        "sandbox": {
            "filesystem": {},
            "network": {"proxy": {}, "sockets": {"unix_sockets": []}},
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
    ("sandbox.network", {}),
    ("sandbox", []),
    ("sandbox.filesystem", []),
    ("resolver", []),
    # Windows rejects explicit resolver policies before field validation.
    ("resolver.sandbox.profile", "inherit_codex"),
    ("resolver.sandbox.profile", {"inherit_codex": {"fallback": "workspace"}}),
    ("sandbox.network.proxy", []),
    ("sandbox.network.sockets", []),
    ("sandbox.network.proxy.domains", []),
    ("sandbox.network.proxy", None),
    ("sandbox.network.proxy.enabled", True),
    ("sandbox.network.proxy.enableSocks5", True),
    ("sandbox.network.proxy.enable_socks5", True),
    ("sandbox.network.proxy.domains", {"example.org": "allow"}),
    ("sandbox.network.proxy.domains.allow", ["example.org:443"]),
    ("sandbox.network.proxy.domains.allow", ["[::1]:443"]),
    ("sandbox.network.proxy.domains.allow", ["fe80::1%eth0:443"]),
    ("sandbox.network.proxy.domains.allow", ["[fe80::1%eth0:443]"]),
    ("sandbox.network.proxy.domains.allow", ["127.0.0.1%x:443"]),
    ("sandbox.network.proxy.domains.allow", ["127.0.0.1%eth0"]),
    ("sandbox.network.proxy.domains.allow", ["fe80::1%"]),
    ("sandbox.network.proxy.domains.allow", ["fe80::1%eth0%other"]),
    ("sandbox.network.proxy.domains.allow", ["fe80::1%eth 0"]),
    ("sandbox.network.proxy.domains.allow", ["[example.org]suffix"]),
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
