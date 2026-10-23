#!/usr/bin/env -S uv run --script

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient
from support.execution import SANDBOXED
from support.native import LOADER_VARIABLE, build_interposer
from support.normalization import code
from support.records import TranscriptWithCompanions
from support.requirements import LINUX_SANDBOX, MACOS_SANDBOX, NATIVE_FIXTURES, requires
from support.resolvers import send_and_collect_runtime_python_resolution
from support.suites import run_this_suite


def _writable_roots_reach_every_runner_launch(
    binary: Path, *, macos: bool
) -> TranscriptWithCompanions:
    # fmt: python
    exercise = code(r"""
        import errno
        import os
        from pathlib import Path
        import subprocess

        host = Path(os.environ["MCP_CONSOLE_TEST_HOST"])
        temporary = Path(os.environ["TMPDIR"])
        assert not host.is_relative_to(temporary)
        assert not temporary.is_relative_to(host)
        assert "MCP_CONSOLE_SANDBOX_CONFIG" not in os.environ
        assert "DYLD_INSERT_LIBRARIES" not in os.environ
        assert "LD_PRELOAD" not in os.environ
        _ = (temporary / "private").write_text("private storage")
        for name in ("output café 雪", "cache"):
            root = host / name
            _ = (root / "persistent").write_text("user data")
            subprocess.run(["touch", str(root / "child")], check=True)
        _ = (host / "cache" / "temporary-path").write_text(str(temporary))
        for denied in (
            host / "parent-write",
            host / "neighbor" / "created",
            host / "output café 雪" / "escape" / "created",
        ):
            try:
                _ = denied.write_text("must not be written")
            except OSError as error:
                assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS)
            else:
                raise AssertionError(f"unexpected write: {denied}")
        os.chdir(host / "output café 雪")
        print(
            "both grants and subprocess writes verified; neighboring and symlink writes denied"
        )
        """)
    # fmt: python
    write_future = code(r"""
        _ = (host / "future output" / "persistent").write_text("user data")
        print("future root is writable after restart")
        """)
    with TemporaryDirectory() as directory:
        host = Path(directory).resolve()
        roots = [host / "output café 雪", host / "cache"]
        for root in [*roots, host / "neighbor"]:
            root.mkdir()
        (roots[0] / "escape").symlink_to(host / "neighbor", target_is_directory=True)
        future = host / "future output"
        capture = host / "runner-configurations.jsonl"
        environment = {
            **os.environ,
            LOADER_VARIABLE: str(build_interposer(host, "runner_configuration")),
            "MCP_CONSOLE_TEST_RUNNER_CONFIGURATION": str(capture),
            "MCP_CONSOLE_TEST_HOST": str(host),
        }
        options = (
            "--writable-root",
            roots[0].name,
            "--writable-root",
            str(roots[1]),
            "--writable-root",
            future.name,
        )
        for arguments in ((), options):
            result = subprocess.run(
                [binary, "sandbox", *arguments, "--", sys.executable, "-c", "pass"],
                cwd=host,
                env=environment,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, result.stderr
            assert result.stdout == result.stderr == ""
            assert not future.exists()

        with McpClient(
            binary, SANDBOXED.serve("-c", "cache=host", *options), environment, host
        ) as client:
            client.initialize_and_list_tools()
            send_and_collect_runtime_python_resolution(
                client, python=exercise, timeout_ms=600_000
            )
            assert last_tool_text(client).endswith(
                "both grants and subprocess writes verified; neighboring and symlink writes denied\n"
            ), last_tool_text(client)
            assert not future.exists(), "launcher created a writable root"
            client.send(
                # fmt: python
                python=code(r"""
                    try:
                        Path("../future output").mkdir()
                    except OSError as error:
                        assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS)
                        print(error)
                    else:
                        print("runtime created the writable root")
                    """)
            )
            assert future.is_dir() == macos, last_tool_text(client)
            future.mkdir(exist_ok=True)
            temporary = Path((roots[1] / "temporary-path").read_text())
            client.send(control="restart")
            assert not temporary.exists(), "restart retained private storage"
            assert (roots[0] / "persistent").read_text() == "user data"
            send_and_collect_runtime_python_resolution(
                client, python=exercise, timeout_ms=600_000
            )
            assert last_tool_text(client).endswith(
                "both grants and subprocess writes verified; neighboring and symlink writes denied\n"
            ), last_tool_text(client)
            client.send(python=write_future)
            assert last_tool_text(client) == "future root is writable after restart\n"
            temporary = Path((roots[1] / "temporary-path").read_text())
            client.send(python="os._exit(17)")
            assert not temporary.exists(), "replacement retained private storage"
            for root in roots:
                assert (root / "persistent").read_text() == "user data"
            send_and_collect_runtime_python_resolution(
                client, python=exercise, timeout_ms=600_000
            )
            assert last_tool_text(client).endswith(
                "both grants and subprocess writes verified; neighboring and symlink writes denied\n"
            ), last_tool_text(client)
            assert (future / "persistent").read_text() == "user data"
            client.send(python=write_future)
            assert last_tool_text(client) == "future root is writable after restart\n"
            temporary = Path((roots[1] / "temporary-path").read_text())
            transcript = client.finish()
        assert not temporary.exists(), "shutdown retained private storage"
        for root in roots:
            assert (root / "persistent").read_text() == "user data"
            assert (root / "child").is_file()
        assert not (host / "neighbor" / "created").exists()
        assert not (host / "parent-write").exists()
        assert (future / "persistent").read_text() == "user data"

        payloads = [json.loads(line) for line in capture.read_text().splitlines()]
        assert len(payloads) == 5, payloads
        records = []
        for scenario, payload in zip(
            (
                "sandbox defaults",
                "sandbox with roots",
                "serve initial worker",
                "serve restart",
                "serve failure replacement",
            ),
            payloads,
            strict=True,
        ):
            entries = payload["filesystem"]["entries"]
            assert entries[0] == {
                "path": {"type": "special", "value": {"kind": "root"}},
                "access": "read",
            }
            assert entries[1:] == (
                [
                    {"path": {"type": "path", "path": str(root)}, "access": "write"}
                    for root in [*roots, future]
                ]
                if scenario != "sandbox defaults"
                else []
            )
            assert ("macos_seatbelt_profile_extension" in payload) == macos
            parent = payload["lifecycle"].get("parent_pid")
            assert parent == (
                client.process.pid if scenario.startswith("serve") else None
            )
            if parent is not None:
                payload["lifecycle"]["parent_pid"] = "<server pid>"
            for entry in entries[1:]:
                entry["path"]["path"] = entry["path"]["path"].replace(
                    str(host), "<host directory>"
                )
            records.append(
                {
                    "scenario": scenario,
                    "runner_configuration": payload,
                    "transcript_normalization": {
                        "host_directory": "omitted",
                        "server_pid": "omitted",
                    },
                }
            )
        return TranscriptWithCompanions(transcript, {"runner.yaml": records})


# The payload itself differs: macOS includes the complete Seatbelt extension;
# Linux omits it and applies missing roots only on a later sandbox launch.
@requires(MACOS_SANDBOX, NATIVE_FIXTURES)
def test_writable_roots_reach_every_runner_launch_on_macos(
    binary: Path,
) -> TranscriptWithCompanions:
    return _writable_roots_reach_every_runner_launch(binary, macos=True)


@requires(LINUX_SANDBOX, NATIVE_FIXTURES)
def test_writable_roots_reach_every_runner_launch_on_linux(
    binary: Path,
) -> TranscriptWithCompanions:
    return _writable_roots_reach_every_runner_launch(binary, macos=False)


if __name__ == "__main__":
    run_this_suite(__file__)
