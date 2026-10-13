"""Required MXC acceptance on a native Windows host without legacy accounts."""

import os
import subprocess
import sys
import unittest

from support.normalization import code
from windows_sandbox import BINARY, workspace


@unittest.skipUnless(os.name == "nt", "native Windows MXC")
class WindowsMxc(unittest.TestCase):
    def test_proxy_launch(self):
        with workspace() as root:
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
                env=dict(os.environ, MCP_CONSOLE_HOME=str(root / "home")),
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "MXC proxy ready")


if __name__ == "__main__":
    unittest.main()
