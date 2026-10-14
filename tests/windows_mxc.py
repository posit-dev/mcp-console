"""Public proxy backend selection before legacy sandbox account provisioning."""

import json
import os
import subprocess
import sys
import unittest

from support.normalization import code
from windows_sandbox import BINARY, workspace


@unittest.skipUnless(os.name == "nt", "native Windows MXC")
class WindowsMxc(unittest.TestCase):
    def test_proxy_backend_selection(self):
        with workspace() as root:
            environment = dict(os.environ, MCP_CONSOLE_HOME=str(root / "home"))
            # A Windows build number does not prove that native PSEC is enabled.
            # Explicit MXC selection probes the same public launch path and must
            # never provision or fall back to the legacy backend.
            probe = subprocess.run(
                [
                    str(BINARY),
                    "sandbox",
                    "--config-env",
                    "TEST_MXC_POLICY",
                    "--",
                    sys.executable,
                    "-c",
                    'print("MXC backend ready")',
                ],
                cwd=root,
                env=dict(
                    environment,
                    TEST_MXC_POLICY=json.dumps(
                        {"extends": ":read-only", "windows_backend": "mxc"}
                    ),
                ),
                capture_output=True,
                text=True,
                timeout=30,
            )
            if probe.returncode == 0:
                self.assertEqual(probe.stdout.strip(), "MXC backend ready")
            else:
                self.assertEqual(probe.returncode, 1, probe.stderr)
                self.assertEqual(probe.stdout, "")
                self.assertEqual(
                    probe.stderr.strip(),
                    "mcp-console-sandbox: MXC is unavailable for this filesystem policy on this executor; "
                    "update Windows to a build supporting native MXC and the requested filesystem permissions; "
                    "see https://t-kalinowski.github.io/mcp-console/WINDOWS.html#native-sandbox",
                )
            result = subprocess.run(
                [
                    str(BINARY),
                    "sandbox",
                    "--no-config",
                    "-c",
                    "sandbox.network={proxy: {}, allow_local_binding: true}",
                    "--",
                    sys.executable,
                    "-c",
                    # fmt: python
                    code("""
                        import os
                        from pathlib import Path

                        assert os.environ["CODEX_NETWORK_PROXY_ACTIVE"] == "1"
                        assert os.environ["CODEX_NETWORK_ALLOW_LOCAL_BINDING"] == "1"
                        assert os.environ["HTTP_PROXY"].startswith("http://")
                        assert os.environ["ALL_PROXY"].startswith("socks5h://")
                        assert "MCP_CONSOLE_SANDBOX_CONFIG" not in os.environ
                        (Path(os.environ["TMP"]) / "probe.txt").write_text("private write")
                        print("MXC proxy ready")
                        """),
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if probe.returncode == 0:
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "MXC proxy ready")
            else:
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(
                    result.stderr.strip(),
                    "configuration with CLI overrides: sandbox preflight failed (exit code: 1): "
                    "mcp-console-sandbox: managed proxy configuration is not supported by the legacy Windows sandbox",
                )


if __name__ == "__main__":
    unittest.main()
