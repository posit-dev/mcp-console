#!/usr/bin/env -S uv run --script

import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient
from support.native import LOADER_VARIABLE, build_interposer
from support.normalization import code
from support.records import TranscriptWithCompanions
from support.requirements import NATIVE_FIXTURES, SANDBOX, requires
from support.resolvers import send_and_collect_runtime_python_resolution
from support.suites import run_this_suite


@requires(SANDBOX, NATIVE_FIXTURES)
def test_workspace_permissions_and_description_survive_worker_replacement(
    binary: Path,
) -> TranscriptWithCompanions:
    # fmt: python
    exercise = code(r"""
        import errno
        import os
        from pathlib import Path
        import subprocess
        import sys

        workspace = Path(os.environ["TEST_WORKSPACE"])
        outside = Path(os.environ["TEST_OUTSIDE"])
        os.chdir(outside)
        ordinary = workspace / "ordinary"
        _ = ordinary.write_text("created")
        _ = ordinary.write_text("edited")
        assert ordinary.read_text() == "edited"
        ordinary.unlink()
        for path in [
            outside / "blocked",
            *[workspace / name / "keep" for name in (".git", ".agents", ".codex", ".claude")],
        ]:
            try:
                _ = path.write_text("blocked")
            except OSError as error:
                assert error.errno in (errno.EACCES, errno.EPERM, errno.EROFS), error
            else:
                raise AssertionError(path)
        assert (workspace / ".claude/keep").read_text() == "readable"
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('.claude/new').write_text('blocked')",
            ],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        assert child.returncode != 0 and (
            "PermissionError" in child.stderr or "Read-only file system" in child.stderr
        ), child
        _ = (Path(os.environ["TMPDIR"]) / "private").write_text("private")
        print("fixed workspace permits edits and protects metadata after cwd changes")
        """)
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        host = root / "workspace"
        outside = root / "outside"
        outside.mkdir()
        for name in (".git", ".agents", ".codex", ".claude"):
            (host / name).mkdir(parents=True)
            (host / name / "keep").write_text("readable")
        config = host / ".agents/console/config.yaml"
        config.parent.mkdir()
        config.write_text(
            "sandbox: {filesystem: {read_write: [.], read_only: [.git, .agents, .codex, .claude]}}\n"
        )
        capture = root / "launches.jsonl"
        environment = os.environ | {
            LOADER_VARIABLE: str(build_interposer(root, "runner_configuration")),
            "MCP_CONSOLE_TEST_RUNNER_CONFIGURATION": str(capture),
            "TEST_WORKSPACE": str(host),
            "TEST_OUTSIDE": str(outside),
        }
        environment.pop("RETICULATE_PYTHON", None)
        with McpClient(
            binary, ("serve", "-c", "cache=host"), environment, host
        ) as client:
            client.initialize_and_list_tools()
            description = client.transcript[-1]["result"]["tools"][0]["description"]
            assert "Writable locations: private `TMPDIR`" in description, description
            assert json.dumps(str(host)) in description, description
            # The trusted launch snapshot precedes even the first worker.
            config.write_text("sandbox: {}\n")
            for generation in range(4):
                send_and_collect_runtime_python_resolution(client, python=exercise)
                assert (
                    last_tool_text(client)
                    == "fixed workspace permits edits and protects metadata after cwd changes\n"
                ), last_tool_text(client)
                if generation == 0:
                    config.write_text("invalid: [")
                    client.send(control="restart")
                elif generation == 1:
                    config.unlink()
                    client.send(python="os._exit(23)")
                elif generation == 2:
                    client.send(
                        control="restart", requirements={"python": ["py-yaml12"]}
                    )
            send_and_collect_runtime_python_resolution(
                client, python="import yaml12; print(yaml12.__name__)"
            )
            assert last_tool_text(client) == "yaml12\n", last_tool_text(client)
            transcript = client.finish()
        launches = [json.loads(line) for line in capture.read_text().splitlines()]
        assert len(launches) == 4, launches
        assert all(policy == launches[0] for policy in launches), launches
        policy = launches[0]
        assert policy["network"] == "restricted"
        assert policy["filesystem"]["kind"] == "restricted"
        assert policy["filesystem"]["entries"][1:] == [
            *[
                {"path": {"type": "path", "path": str(host / name)}, "access": "read"}
                for name in (".git", ".agents", ".codex", ".claude")
            ],
            {"path": {"type": "path", "path": str(host)}, "access": "write"},
        ], policy
        for entry in transcript:
            if isinstance(result := entry.get("result"), dict):
                for tool in result.get("tools", []):
                    tool["description"] = tool["description"].replace(
                        json.dumps(str(host)), '"<workspace>"'
                    )
        return TranscriptWithCompanions(
            transcript,
            {
                "profile.yaml": [
                    {
                        "read_write": ["."],
                        "fixed_workspace": True,
                        "identical_launches": len(launches),
                        "network": "restricted",
                        "read_only": [".git", ".agents", ".codex", ".claude"],
                    }
                ]
            },
        )


if __name__ == "__main__":
    run_this_suite(__file__)
