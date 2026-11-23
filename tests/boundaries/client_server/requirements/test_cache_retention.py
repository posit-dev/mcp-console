"""Expire managed caches only after their last live consumer retires."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.python.test_without_r import environment
from support.client import McpClient
from support.records import McpTranscript, Transcript, TranscriptWithCompanions
from support.requirements import POSIX, SANDBOX, requires


def launch(
    binary: Path,
    root: Path,
    *,
    days: int | None = 14,
    host: bool = False,
    inherited: bool = False,
):
    config = root / "config.json"
    config.write_text(
        json.dumps(
            {
                "cache": "host" if host else {"directory": str(root / "cache")},
                "retention": {"cache_days": days},
                "languages": ["python"],
                "python": str(Path(sys.executable)),
                **({"sandbox": {"profile": "inherit_codex"}} if inherited else {}),
            }
        )
    )
    tools = root / "bin"
    tools.mkdir(exist_ok=True)
    env = environment(tools)
    env.update(
        MCP_CONSOLE_HOME=str(root / "home"),
        MCP_CONSOLE_CACHE_DIR=str(root / "cache"),
    )
    return McpClient(binary, ("serve", "--config-file", str(config)), env, root)


@requires(SANDBOX)
def test_resets_only_expired_managed_dependencies(
    binary: Path,
) -> TranscriptWithCompanions:
    companions = {}
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        cache = root / "cache"
        dependencies = cache / "dependencies"
        dependencies.mkdir(parents=True)
        sentinel = dependencies / "prepared-package"
        sentinel.write_text("retained dependency")
        companion = cache / "sandbox"
        companion.mkdir()
        companion.joinpath("binary").write_text("companion cache")
        # Adopting existing storage starts its retention interval now.
        with launch(binary, root) as client:
            client.initialize_and_list_tools()
            client.expect("ready\n", python="print('ready')")
            assert sentinel.exists()
            marker = cache / ".cache-retention"
            assert marker.is_file()
            transcript = client.finish()
        # Filesystem timestamps model elapsed time without timing-dependent waits.
        aged = int(time.time()) - 3 * 24 * 60 * 60
        os.utime(marker, (aged, aged))
        for name, days, host in (
            ("unexpired", 14, False),
            ("disabled", None, False),
            ("host", 1, True),
        ):
            with launch(binary, root, days=days, host=host) as client:
                client.initialize_and_list_tools()
                client.expect("ready\n", python="print('ready')")
                assert sentinel.exists(), name
                assert marker.stat().st_mtime == aged
                companions[f"{name}.yaml"] = McpTranscript(client.finish())
        with launch(binary, root, days=1) as client:
            client.initialize_and_list_tools()
            client.expect("fresh\n", python="print('fresh')")
            assert not sentinel.exists()
            assert marker.stat().st_mtime > aged
            assert companion.joinpath("binary").read_text() == "companion cache"
            companions["expired.yaml"] = McpTranscript(client.finish())
    return TranscriptWithCompanions(transcript, companions)


@requires(SANDBOX)
def test_defers_reset_until_active_consumers_retire(
    binary: Path,
) -> TranscriptWithCompanions:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        # Client-inherited policies select caches during first-send admission,
        # rather than retaining a lease in the initially captured configuration.
        with launch(binary, root, days=None, inherited=True) as first:
            first.initialize_and_list_tools()
            first.expect("retained\n", python="value = 'retained'; print(value)")
            cache = root / "cache"
            sentinel = cache / "dependencies/prepared-package"
            sentinel.write_text("live dependency")
            marker = cache / ".cache-retention"
            aged = int(time.time()) - 3 * 24 * 60 * 60
            os.utime(marker, (aged, aged))
            with launch(binary, root, days=1) as second:
                second.initialize_and_list_tools()
                second.expect("second\n", python="print('second')")
                assert sentinel.read_text() == "live dependency"
                assert marker.stat().st_mtime == aged
                overlap = second.finish()
            first.expect("retained\n", python="print(value)")
            transcript = first.finish()
        with launch(binary, root, days=1) as client:
            client.initialize_and_list_tools()
            client.expect("fresh\n", python="print('fresh')")
            assert not sentinel.exists()
            assert marker.stat().st_mtime > aged
            reset = client.finish()
    return TranscriptWithCompanions(
        transcript,
        {"overlap.yaml": McpTranscript(overlap), "reset.yaml": McpTranscript(reset)},
    )


@requires(POSIX)
def test_refuses_linked_cache_metadata_or_dependencies(binary: Path) -> Transcript:
    transcript = []
    for linked in (".cache-retention", "dependencies"):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            cache = root / "cache"
            cache.mkdir()
            outside = root / "outside"
            outside.mkdir()
            sentinel = outside / "user-file"
            sentinel.write_text("user data")
            marker = cache / ".cache-retention"
            if linked == ".cache-retention":
                marker.symlink_to(sentinel)
            else:
                marker.touch()
                cache.joinpath("dependencies").symlink_to(outside)
                aged = int(time.time()) - 3 * 24 * 60 * 60
                os.utime(marker, (aged, aged))
            result = subprocess.run(
                [
                    binary,
                    "serve",
                    "--no-config",
                    "-c",
                    f"cache.directory={cache}",
                    "-c",
                    "retention.cache_days=1",
                ],
                env=os.environ | {"MCP_CONSOLE_HOME": str(root / "home")},
                cwd=root,
                input="",
                capture_output=True,
                text=True,
                timeout=30,
            )
            reason = (
                "cache retention marker must be a regular file"
                if linked == ".cache-retention"
                else "managed dependencies must be a directory, not a symlink or file"
            )
            assert result.returncode == 1, result
            assert not result.stdout
            assert (
                result.stderr == f"cannot maintain Console cache '{cache}': {reason}\n"
            )
            assert sentinel.read_text() == "user data"
            assert cache.joinpath(linked).is_symlink()
            transcript.append(
                {"stderr": result.stderr.replace(str(root), "<workspace>")}
            )
    return transcript
