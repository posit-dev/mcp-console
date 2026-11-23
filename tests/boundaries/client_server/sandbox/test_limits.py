"""Mandatory filesystem limits survive ordinary worker replacement."""

import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.python.test_without_r import (
    environment as python_only_environment,
)
from boundaries.client_server.sandbox.test_codex_inheritance import send_with_state
from support.client import McpClient
from support.execution import SANDBOXED
from support.normalization import code, normalize_recording
from support.records import Transcript
from support.requirements import PROCESS_EVENTS, SANDBOX, requires
from support.suites import run_this_suite


def test_describes_mandatory_limits_at_discovery(binary: Path) -> Transcript:
    sensitive = {
        "filesystem": {"deny": ["private-project"]},
        "network": {"domains": {"allow": ["private.example.invalid"]}},
    }
    records = []
    with TemporaryDirectory() as directory:
        workspace = Path(directory)
        for profile, network, limits in [
            ("workspace", "enabled", sensitive),
            ("inherit_codex", "enabled", sensitive),
            ("workspace", "enabled", {"network": {"allow_local_binding": True}}),
            ("workspace", "enabled", {"network": {"domains": {"allow": []}}}),
            ("workspace", "{proxy: {}}", {"network": {"domains": {"allow": []}}}),
        ]:
            # Discovery of a fixed profile needs no interpreter. The inherited
            # profile checks admission of a real cell on its first send.
            worker = (
                [
                    "-c",
                    "languages=[python]",
                    "-c",
                    "python=" + json.dumps(sys.executable),
                ]
                if profile == "inherit_codex"
                else ["--worker", "unused-worker"]
            )
            with McpClient(
                binary,
                (
                    "serve",
                    "--no-config",
                    "--limits-env",
                    "PRIVATE_POLICY",
                    *worker,
                    "-c",
                    "cache=host",
                    "-c",
                    f"sandbox.profile={profile}",
                    "-c",
                    f"sandbox.network={network}",
                    "-c",
                    "sandbox.tempdir=read_only",
                ),
                os.environ | {"PRIVATE_POLICY": json.dumps(limits)},
                workspace,
                recording_receipts=False,
            ) as client:
                client.initialize_and_list_tools()
                description = client.transcript[-1]["result"]["tools"][0]["description"]
                empty_allowlist = (
                    limits["network"].get("domains", {}).get("allow") == []
                )
                active = limits == sensitive or empty_allowlist
                assert ("mandatory limits" in description) == active, description
                assert ("can directly access the network" in description) == (
                    not active
                )
                assert "Full access disables" not in description
                if profile == "workspace":
                    assert "workspace" in description
                if empty_allowlist:
                    assert "cannot reach any network destinations" in description, (
                        description
                    )
                for private in (
                    "private-project",
                    "private.example.invalid",
                    "PRIVATE_POLICY",
                ):
                    assert private not in description
                records.append(
                    {
                        "profile": profile,
                        "network": network,
                        "active": active,
                        "permissions": description.rsplit("\n\n", 1)[-1],
                    }
                )
                if profile == "inherit_codex":
                    result = send_with_state(
                        client,
                        {"permissionProfile": {"type": "disabled"}},
                        python="from pathlib import Path; Path('ran').touch()",
                    )["result"]
                    assert result["isError"], result
                    error = result["content"][0]["text"]
                    assert "sandbox limits require sandboxing" in error, error
                    assert not (workspace / "ran").exists()
                    records[-1]["full_access_error"] = error
                    _, stderr = client.finish_with_standard_error(
                        expected_exit_status=1
                    )
                    assert stderr == error + "\n", (error, stderr)
                    records[-1]["stderr"] = stderr
                else:
                    client.finish()
    return records


# MCP recording receipts and lifecycle observation require the shared process
# event facilities; hosts without them cannot establish a completed replacement.
@requires(SANDBOX, PROCESS_EVENTS)
def test_retains_limits_after_user_config_edits_and_restart(binary: Path) -> Transcript:
    # fmt: python
    exercise = code(r"""
        import os
        from pathlib import Path

        assert "generation_marker" not in globals()
        assert "TEST_LIMITS" not in os.environ
        protected = Path("protected/child/secret")
        for operation in (protected.read_text, lambda: protected.write_text("escaped")):
            try:
                operation()
            except OSError:
                pass
            else:
                raise AssertionError("ordinary grants reopened the protected file")
        _ = Path("public/probe").write_text("permitted")
        generation_marker = True
        os.environ["TEST_LIMITS"] = "{}"
        print("fresh worker retains filesystem limits and consumes the limits environment")
        """)
    with TemporaryDirectory() as directory:
        workspace = Path(directory).resolve()
        tools = workspace / "tools"
        tools.mkdir()
        environment = python_only_environment(tools)
        environment["MCP_CONSOLE_LANGUAGES"] = "python"
        environment["TEST_LIMITS"] = "filesystem:\n  deny: [./protected]\n"
        protected = workspace / "protected/child/secret"
        protected.parent.mkdir(parents=True)
        protected.write_text("protected")
        (workspace / "public").mkdir()
        config = workspace / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps(
                {
                    "cache": "host",
                    "python": sys.executable,
                    "languages": ["python"],
                    "environment": {"TEST_LIMITS": "{}"},
                    "sandbox": {
                        "profile": "workspace",
                        "filesystem": {"read_write": ["protected/child"], "deny": []},
                        "network": "enabled",
                    },
                    "resolver": {"sandbox": {"network": "enabled"}},
                }
            ),
            encoding="utf-8",
        )
        # Network policy is unrelated to this filesystem regression. Keep both
        # launch paths proxy-free, including on hosts with restricted netlink.
        with McpClient(
            binary,
            SANDBOXED.serve(
                "--limits-env",
                "TEST_LIMITS",
                "--writable-root",
                "protected/child",
                "--writable-root",
                "public",
                "-c",
                "sandbox.filesystem.deny=[]",
            ),
            environment,
            workspace,
        ) as client:
            client.initialize_and_list_tools()
            expected = "fresh worker retains filesystem limits and consumes the limits environment\n"
            client.expect(expected, python=exercise)
            assert protected.read_text() == "protected"
            assert (workspace / "public/probe").read_text() == "permitted"

            # Editing user configuration cannot remove the captured mandatory
            # policy when the server creates its replacement worker.
            config.write_text(
                "sandbox: null\nresolver: {sandbox: null}\n", encoding="utf-8"
            )
            client.expect(
                "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]",
                control="restart",
            )
            client.expect(expected, python=exercise)
            assert protected.read_text() == "protected"
            assert (workspace / "public/probe").read_text() == "permitted"
            return normalize_recording(client.finish(), workspace)


if __name__ == "__main__":
    run_this_suite(__file__)
