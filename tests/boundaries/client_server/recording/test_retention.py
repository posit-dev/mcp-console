"""Recording retention runs through the public server publication boundary."""

import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.records import McpTranscript, TranscriptWithCompanions
from support.requirements import POSIX, requires


@contextmanager
def recording_client(binary: Path, root: Path, *settings: str):
    with McpClient(
        binary,
        (
            "serve",
            "--no-config",
            "--no-sandbox",
            "-c",
            "languages=[python]",
            "-c",
            f"python={json.dumps(sys.executable)}",
            *[argument for setting in settings for argument in ("-c", setting)],
        ),
        environment=os.environ | {"MCP_CONSOLE_HOME": str(root / "console")},
        current_directory=root,
        record_in_project=False,
        use_home_configuration=True,
    ) as client:
        client.initialize_and_list_tools()
        sessions = root / "console/sessions"
        before = set(sessions.iterdir()) if sessions.exists() else set()
        client.expect(note="Retained analysis.")
        (recording,) = set(sessions.iterdir()) - before
        yield client, recording
        client.finish()


def age(recording: Path, days: int) -> None:
    timestamp = int(time.time()) - days * 24 * 60 * 60
    os.utime(recording / ".retention", (timestamp, timestamp))


def test_prunes_expired_recordings_and_preserves_active_saved_and_legacy(
    binary: Path,
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        with recording_client(binary, root) as (expired_client, expired):
            pass
        assert (expired / ".retention").is_file()
        age(expired, 15)
        with recording_client(binary, root, "retention.recordings_days=null") as (
            saved_client,
            saved,
        ):
            pass
        (saved / ".keep").touch()
        age(saved, 15)
        legacy = root / "console/sessions/user-recording"
        legacy.mkdir()
        (legacy / "notes.txt").write_text("User-managed notes.", encoding="utf-8")
        with recording_client(binary, root, "retention.recordings_days=null") as (
            active,
            live,
        ):
            age(live, 15)
            aged_mtime = (live / ".retention").stat().st_mtime
            with recording_client(binary, root) as (cleaner, recent):
                assert not expired.exists()
                assert saved.is_dir()
                assert live.is_dir()
                assert recent.is_dir()
                assert (legacy / "notes.txt").read_text(
                    encoding="utf-8"
                ) == "User-managed notes."
                active.expect(note="Recording survives another process's cleanup.")
            assert recent.is_dir()
        assert (live / ".retention").stat().st_mtime > aged_mtime
    return TranscriptWithCompanions(
        cleaner.transcript
        + [
            {
                "removed": "expired",
                "preserved": ["saved", "active", "recent", "legacy"],
                "age": "refreshed after finalization",
            }
        ],
        {
            "expired.yaml": McpTranscript(expired_client.transcript),
            "saved.yaml": McpTranscript(saved_client.transcript),
            "active.yaml": McpTranscript(active.transcript),
        },
    )


def test_configures_recording_retention_and_disables_cleanup(
    binary: Path,
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        with recording_client(binary, root) as (old_client, old):
            pass
        age(old, 3)
        with recording_client(binary, root, "retention.recordings_days=null") as (
            disabled_client,
            disabled,
        ):
            assert old.is_dir()
        with recording_client(binary, root, "retention.recordings_days=2") as (
            cleaner,
            _,
        ):
            assert not old.exists()
            assert disabled.is_dir()
    return TranscriptWithCompanions(
        cleaner.transcript
        + [{"retention_days": 2, "null": "cleanup disabled", "recent": "preserved"}],
        {
            "old.yaml": McpTranscript(old_client.transcript),
            "disabled.yaml": McpTranscript(disabled_client.transcript),
        },
    )


@requires(POSIX)
def test_cleanup_does_not_follow_recording_or_capture_symlinks(
    binary: Path,
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        with recording_client(binary, root) as (linked_client, linked):
            pass
        age(linked, 15)
        external = root / "saved-recording"
        linked.rename(external)
        linked.symlink_to(external, target_is_directory=True)
        with recording_client(binary, root) as (expired_client, expired):
            assert linked.is_symlink()
            assert (external / "transcript.md").is_file()
        (expired / "user-link").symlink_to(external, target_is_directory=True)
        age(expired, 15)
        with recording_client(binary, root) as (cleaner, _):
            assert not expired.exists()
            assert linked.is_symlink()
            assert (external / "transcript.md").is_file()
    return TranscriptWithCompanions(
        cleaner.transcript
        + [
            {
                "recording_symlink": "preserved",
                "nested_symlink": "removed without following",
                "external_target": "preserved",
            }
        ],
        {
            "linked.yaml": McpTranscript(linked_client.transcript),
            "expired.yaml": McpTranscript(expired_client.transcript),
        },
    )
