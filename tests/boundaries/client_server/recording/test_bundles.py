"""Portable session documents and their automatically announced locations."""

import base64
import os
import re
import json
import shutil
import sys
import tempfile
import mmap
import struct
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import normalize_recording
from support.previews import live_recording_events
from support.records import Transcript
from support.suites import run_this_suite
from support.requirements import R, SQL, POSIX, NATIVE_FIXTURES, requires
from support.native import LOADER_VARIABLE, build_interposer
from support.normalization import code
from support.checkpoints import release_fixture_checkpoint
from boundaries.client_server._harness import wait_for_marker
from boundaries.client_server.python.test_without_r import selected_environment


@requires(POSIX)
@executions(SANDBOXED)
def test_bundle_symlink_cannot_redirect_host_writes(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        target = root / "denied"
        target.mkdir()
        with McpClient(
            binary,
            execution.serve("--writable-root", str(workspace)),
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "ready\n",
                # fmt: python
                python=code("""
                    from pathlib import Path

                    target = Path.cwd().parent / "denied"
                    try:
                        (target / "worker-write").write_text("forbidden")
                    except PermissionError:
                        pass
                    else:
                        raise AssertionError("target must be outside the worker write policy")
                    print("ready")
                    """),
            )
            client.expect(
                "swapped\n",
                # fmt: python
                python=code("""
                    (recording,) = Path(".agents/console/sessions").iterdir()
                    bundle = recording / "sessions/session-001"
                    bundle.rename(recording / "saved-session")
                    bundle.symlink_to(target, target_is_directory=True)
                    print("swapped")
                    """),
            )
            assert not list(target.iterdir()), list(target.iterdir())
            client.expect("still running\n", python="print('still running')")
            transcript, diagnostics = client.finish_with_standard_error()
            assert not list(target.iterdir()), list(target.iterdir())
            assert "transcript projections disabled:" in diagnostics, diagnostics
            return normalize_recording(transcript, workspace)


@requires(POSIX)
@executions(DIRECT)
def test_replacements_do_not_accumulate_report_descriptors(
    binary: Path, execution: Execution
) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        wrapper = root / "bounded-console"
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import os, resource, sys\n"
            "resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))\n"
            f"os.execv({str(binary)!r}, [{str(binary)!r}, *sys.argv[1:]])\n"
        )
        wrapper.chmod(0o700)
        with McpClient(
            wrapper,
            execution.serve("--worker", str(worker)),
            current_directory=root,
        ) as client:
            client.initialize_and_list_tools()
            for session in range(1, 49):
                if session > 1:
                    client.send(control="restart")
                client.expect(
                    f"zod: session {session:03}\n", r=f"echo session {session:03}"
                )
            transcript, diagnostics = client.finish_with_standard_error()
            assert diagnostics == "", diagnostics
            assert len(client.bundle_notices) == 48, len(client.bundle_notices)
            (recording,) = (root / ".agents/console/sessions").iterdir()
            for session in range(1, 49):
                report = recording / f"sessions/session-{session:03}/session.md"
                assert f"zod: session {session:03}\n```" in report.read_text()
            return [
                *transcript[:3],
                {"session bundles": "48 sessions under a 64-descriptor limit"},
            ]


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_bundle_notice_escapes_path_line_breaks(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / 'line\nbreak\r\ttab\u0085\u2028\u2029"\\end'
        root.mkdir()
        with McpClient(
            binary,
            execution.serve(),
            current_directory=root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect("ready\n", python="print('ready')")
            (notice,) = client.bundle_notices
            lines = notice.splitlines()
            assert len(lines) == 4, notice
            bundle = Path(json.loads(lines[0].split(": ", 1)[1]))
            assert bundle.is_relative_to(root.resolve()), bundle
            assert (
                Path(json.loads(lines[1].removeprefix("Source: ")))
                == bundle / "session.qmd"
            )
            assert (
                Path(json.loads(lines[2].removeprefix("Report: ")))
                == bundle / "session.md"
            )
            assert "ready\n```" in (bundle / "session.md").read_text()
            return normalize_recording(client.finish(), root)


@requires(NATIVE_FIXTURES)
@executions(DIRECT)
def test_report_publication_writes_only_changed_content(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        profile = root / "report-writes"
        profile.write_bytes(bytes(8))
        with profile.open("r+b") as stream, mmap.mmap(stream.fileno(), 8) as counts:
            with McpClient(
                binary,
                execution.serve(),
                {
                    **os.environ,
                    LOADER_VARIABLE: str(build_interposer(root, "report_writes")),
                    "MCP_CONSOLE_TEST_REPORT_WRITES": str(profile),
                },
                current_directory=root,
            ) as client:
                client.initialize_and_list_tools()
                for _ in range(24):
                    client.expect(
                        "result:" + "x" * 2048 + "\n",
                        python='print("result:" + "x" * 2048)',
                    )
                result = client.send(
                    # fmt: python
                    python=code("""
                        for index in range(24):
                            print("polled:" + "y" * 2048)
                            input("next> ")
                        """)
                )
                for _ in range(24):
                    assert result["content"][0]["text"].endswith(
                        "[waiting for stdin]"
                    ), result
                    result = client.send(stdin="\n")
                assert result["content"][0]["text"] == "[done]", result
                transcript, diagnostics = client.finish_with_standard_error()
                assert diagnostics == "", diagnostics
            (recording,) = (root / ".agents/console/sessions").iterdir()
            report = (recording / "sessions/session-001/session.md").read_text()
            assert report.count("result:" + "x" * 2048) == 24, report
            assert report.count("polled:" + "y" * 2048) == 24, report
            assert report.count("_Execution ended; completion confirmed._") == 25, (
                report
            )
            assert "Unexecuted submission" not in report, report
            written = struct.unpack_from("=Q", counts)[0]
            assert written < len(report.encode()) * 4, (written, len(report.encode()))
            return [
                *transcript[:3],
                {"report publication": "bounded writes across cells and polls"},
            ]


@executions(DIRECT, SANDBOXED)
def test_materializes_portable_bundle(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        with McpClient(
            binary, execution.serve(), current_directory=workspace
        ) as client:
            client.initialize_and_list_tools()
            client.request("ping")
            root = workspace / ".agents/console/sessions"
            assert not root.exists() or not list(root.iterdir())
            response = client.send(
                # fmt: python
                python=code("""
                    executions = globals().get("executions", 0) + 1
                    identity = object()
                    print("observed value")
                    """)
            )
            (recording,) = root.iterdir()
            bundle = recording / "sessions/session-001"
            assert (bundle / "session.qmd").is_file()
            assert (bundle / "session.md").is_file()
            notice = client.bundle_notices[-1]
            assert Path(json.loads(notice.splitlines()[0].split(": ", 1)[1])).samefile(
                bundle
            ), response
            assert "Console host" in notice, notice
            assert "observed value" in (bundle / "session.md").read_text()
            qmd = (bundle / "session.qmd").read_text()
            assert "identity = object()" in qmd, qmd
            assert "ir render session.qmd" in qmd, qmd
            assert qmd.count("observed value") == 1, (
                qmd
            )  # Source without captured output.
            client.send(
                python="assert identity is not None and executions == 1; raise ValueError('kept error')"
            )
            report = (bundle / "session.md").read_text()
            assert "ValueError: kept error" in report, report
            assert "Session bundle" not in report, report
            client.send(python="assert not hasattr(_console, 'export_session')")
            transcript = client.finish()
            copied = workspace / "copied"
            shutil.copytree(bundle, copied)
            assert (copied / "session.md").read_text() == (
                bundle / "session.md"
            ).read_text()
            events = [
                json.loads(line)
                for line in (recording / "capture/events.jsonl")
                .read_text()
                .splitlines()
            ]
            assert any(event["event"] == "recording_ended" for event in events)
            return normalize_recording(transcript, workspace)


@executions(DIRECT, SANDBOXED)
def test_python_bundle_without_r_and_replacement(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        if os.name != "nt":
            (root / "python3").symlink_to(sys.executable)
        with McpClient(
            binary,
            execution.serve(),
            selected_environment(root),
            current_directory=root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect("original\n", python="print('original')")
            client.send(control="restart")
            assert len(client.bundle_notices) == 1, client.bundle_notices
            client.expect("replacement\n", python="print('replacement')")
            assert len(client.bundle_notices) == 2, client.bundle_notices
            (recording,) = (root / ".agents/console/sessions").iterdir()
            for id, label in ((1, "original"), (2, "replacement")):
                bundle = recording / f"sessions/session-{id:03}"
                qmd = (bundle / "session.qmd").read_text()
                report = (bundle / "session.md").read_text()
                assert f"print('{label}')" in qmd and f"print('{label}')" in report
                assert "r-available: false" in qmd, qmd
                assert f"{label}\n```" in report, report
                assert ("replacement" if id == 1 else "original") not in report
            return normalize_recording(client.finish(), root)


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_images_are_portable_and_later_polls_keep_intervals(
    binary: Path, execution: Execution
) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with McpClient(
            binary,
            execution.serve("--worker", str(worker)),
            {**os.environ, "TMPDIR": directory, "ZOD_RECORDING_INTERVALS": "1"},
            current_directory=root,
        ) as client:
            client.initialize_and_list_tools()
            client.send(r="record intervals")
            release_fixture_checkpoint(
                wait_for_marker(root, "zod-recording-post-release", client),
                client=client,
            )
            wait_for_marker(root, "zod-recording-post-observed", client)
            client.send()
            formats = client.send(r="emit image formats")
            transcript = client.finish()
        (recording,) = (root / ".agents/console/sessions").iterdir()
        bundle = recording / "sessions/session-001"
        report = (bundle / "session.md").read_text()
        assert "Startup output" in report and "Between cells (after 001)" in report, (
            report
        )
        capture = [
            json.loads(line)
            for line in (recording / "capture/events.jsonl").read_text().splitlines()
        ]
        for result in capture:
            if result["event"] == "tool_result" and result["call_id"] in (1, 2):
                for part in result["parts"]:
                    block = result["result"]["content"][part["block"]]
                    if block["type"] == "text" and part["owner"] is not None:
                        fragment = (
                            block["text"].encode()[part["start"] : part["end"]].decode()
                        )
                        assert fragment in report, (fragment, report)
        links = re.findall(r"!\[Recorded image\]\(<(images/[^>]+)>\)", report)
        assert links and len(links) == len(set(links)), report
        formats_images = [
            base64.b64decode(block["data"])
            for block in formats["content"]
            if block["type"] == "image"
        ]
        retained = [(bundle / link).read_bytes() for link in links]
        assert all(image in retained for image in formats_images)
        assert any(link.endswith(".jpg") for link in links), links
        assert "![" not in (bundle / "session.qmd").read_text()
        copy = root / "copy"
        shutil.copytree(bundle, copy)
        transcript = normalize_recording(transcript, root)
        shutil.rmtree(recording)
        assert (copy / "session.md").read_text() == report
        assert all((copy / link).is_file() for link in links)
        return normalize_recording(transcript, root)


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_r_report_does_not_initialize_python_or_replay(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with McpClient(binary, execution.serve(), current_directory=root) as client:
            client.initialize_and_list_tools()
            client.expect(
                "recorded once\n",
                # fmt: r
                r=code("""
                    executions <- if (exists("executions", inherits = FALSE)) {
                      executions + 1L
                    } else {
                      1L
                    }
                    stopifnot(!"export_session" %in% names(.console))
                    cat("recorded once\\n")
                    """),
            )
            (recording,) = (root / ".agents/console/sessions").iterdir()
            bundle = recording / "sessions/session-001"
            assert "recorded once\n```" in (bundle / "session.md").read_text()
            client.expect(
                r="stopifnot(executions == 1L, !reticulate::py_available(initialize = FALSE))"
            )
            return normalize_recording(client.finish(), root)


@requires(SQL)
@executions(DIRECT, SANDBOXED)
def test_sql_report_preserves_preview_without_repeating_query(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with McpClient(binary, execution.serve(), current_directory=root) as client:
            client.initialize_and_list_tools()
            client.send(
                sql="CREATE SEQUENCE IF NOT EXISTS execution_count START 1; SELECT nextval('execution_count') AS value"
            )
            (recording,) = (root / ".agents/console/sessions").iterdir()
            report = (recording / "sessions/session-001/session.md").read_text()
            assert "execution_count" in report and "value" in report, report
            client.send(
                sql="SELECT currval('execution_count') AS still_one",
                requirements={"duckdb": ["fts"]},
            )
            output = client.transcript[-1]["result"]["content"][0]["text"]
            assert re.search(r"(?m)^1\s+1$", output), output
            qmd = (recording / "sessions/session-001/session.qmd").read_text()
            assert "    - fts\n" in qmd, qmd
            return normalize_recording(client.finish(), root)


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_bundle_image_failure_preserves_capture(
    binary: Path, execution: Execution
) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with McpClient(
            binary, execution.serve("--worker", str(worker)), current_directory=root
        ) as client:
            client.initialize_and_list_tools()
            client.expect("zod: ready\n", r="echo ready")
            (recording,) = (root / ".agents/console/sessions").iterdir()
            bundle = recording / "sessions/session-001"
            (bundle / "images/image-000001.png").mkdir(parents=True)
            result = client.send(r="emit image")
            assert any(block["type"] == "image" for block in result["content"]), result
            client.expect("zod: still running\n", r="echo still running")
            transcript, stderr = client.finish_with_standard_error()
        capture = [
            json.loads(line)
            for line in (recording / "capture/events.jsonl").read_text().splitlines()
        ]
        assert capture[-1]["event"] == "recording_ended", capture[-1]
        assert any(event["event"] == "image_retained" for event in capture), capture
        assert "recording disabled" not in stderr, stderr
        assert "projections disabled" in stderr, stderr
        transcript.append(
            {
                "bundle image failure": "projection disabled; capture and computation completed"
            }
        )
        return normalize_recording(transcript, root)


@executions(DIRECT, SANDBOXED)
def test_discovery_preserves_status_suffix_and_polled_results(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with McpClient(binary, execution.serve(), current_directory=root) as client:
            client.initialize_and_list_tools()
            client.send(python="answer = input('answer> '); print(answer)")
            (recording,) = (root / ".agents/console/sessions").iterdir()
            capture = live_recording_events(recording)
            result = next(
                event["result"]
                for event in reversed(capture)
                if event["event"] == "tool_result"
            )
            assert result["content"][0]["_meta"] == {
                "mcp-console/session-bundle": True
            }, result
            combined = "".join(
                block["text"] for block in result["content"] if block["type"] == "text"
            )
            assert combined.endswith("[waiting for stdin]"), combined
            client.expect("accepted\n", stdin="accepted\n")
            report = (recording / "sessions/session-001/session.md").read_text()
            assert "answer> " in report and "accepted\n```" in report, report
            assert report.count("## Cell 001") == 1 and "## Cell 002" not in report
            assert "_Execution ended; completion confirmed._" in report, report
            assert len(client.bundle_notices) == 1, client.bundle_notices
            return normalize_recording(client.finish(), root)


@executions(DIRECT, SANDBOXED)
def test_report_keeps_rejected_dependency_outcome(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with McpClient(binary, execution.serve(), current_directory=root) as client:
            client.initialize_and_list_tools()
            client.expect("ready\n", python="print('ready')")
            rejected = client.send(requirements={})
            assert rejected["isError"], rejected
            (recording,) = (root / ".agents/console/sessions").iterdir()
            report = (recording / "sessions/session-001/session.md").read_text()
            assert rejected["content"][0]["text"] in report, (rejected, report)
            return normalize_recording(client.finish(), root)


if __name__ == "__main__":
    run_this_suite(__file__)
