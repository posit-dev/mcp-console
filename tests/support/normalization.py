import json
import re
from pathlib import Path
from textwrap import dedent

from support.assertions import last_tool_text
from support.client import McpClient
from support.progress import normalize_elapsed
from support.records import Transcript


def code(source: str) -> str:
    return dedent(source).removeprefix("\n")


def normalize_recording(transcript: Transcript, workspace: Path) -> Transcript:
    """Normalize locations and timing only after public content assertions."""
    for entry in transcript:
        for block in entry.get("result", {}).get("content", []):
            if block["type"] == "text":
                block["text"] = normalize_elapsed(block["text"])
    data = json.dumps(transcript)
    recordings = workspace / ".agents/console/sessions"
    if recordings.is_dir():
        for recording in recordings.iterdir():
            data = data.replace(recording.name, "<recording ID>")
    for path in sorted(
        {workspace, workspace.resolve()}, key=lambda p: len(str(p)), reverse=True
    ):
        data = data.replace(json.dumps(str(path))[1:-1], "<workspace>")
    return json.loads(data)


def normalize_process_diagnostic(text: str) -> str:
    """Share diagnostic presentation without dropping status or traceback text."""
    return re.sub(
        r"(failed with )exit code: (-?\d+):",
        r"\1exit status: \2:",
        text.replace("\r\n", "\n"),
    )


def normalize_python_resolution_error(
    error: str, invalid: str | None = None, *, executable: str | None = None
) -> str:
    error = normalize_python_traceback_paths(error)
    if executable is not None:
        selected = f'  "python": {json.dumps(executable)},'
        assert error.count(selected) == 1, error
        error = error.replace(selected, '  "python": "<running Python>",')
    else:
        error, python_patch = re.subn(
            r'(?m)^(  "python": "\d+\.\d+)\.\d+( \(reticulate default\))?(",)$',
            r"\1.XX\2\3",
            error,
            count=1,
        )
        assert python_patch == 1, error
    has_python_version = '\n  "python_version": [\n' in error
    error, python_version_patch = re.subn(
        r'(?m)^(  "python_version": \[\n    "\d+\.\d+)\.\d+("\n  \])$',
        r"\1.XX\2",
        error,
        count=1,
    )
    assert python_version_patch == int(has_python_version), error
    if invalid is not None:
        assert invalid in error, error
    return error


def normalize_python_traceback_paths(error: str) -> str:
    replacements = (
        (
            r'(?m)^(\s+File ")[^"\n]*/reticulate/python/(rpytools/(?:loader|call)\.py")',
            r"\1<reticulate>/python/\2",
        ),
        (
            r'(?m)^(\s+File ")[^"\n]*/lib/python\d+\.\d+/(importlib/__init__\.py")',
            r"\1<python-stdlib>/\2",
        ),
        (
            r'(?m)^(\s+File ")[^"\n]*/(tests/fixtures/checkpoint_uv")'
            r", line \d+",
            r"\1<workspace>/\2, line <line>",
        ),
    )
    for pattern, replacement in replacements:
        error = re.sub(pattern, replacement, error)
    assert re.search(r'(?m)^\s+File "/', error) is None, error
    return error


def normalize_onnx_device_probe(output: str) -> str:
    # DuckDB's VSS extension probes host devices when ONNX Runtime loads.
    # Azure's synthetic PCI paths can warn once per process, independently of
    # the database operation. Keep unexpected diagnostics, including IO errors.
    # Previews remove SGR; subprocess output encoded inside JSON retains it.
    output = re.sub(
        r"(?m)^(?:\x1b\[0;93m)?[^\n]+ \[W:onnxruntime:Default, "
        r"device_discovery\.cc:\d+ GetPciBusId\] Skipping pci_bus_id for PCI path at "
        r'"/sys/devices/[^"\n]+" because filename "[^"\n]+" did not match expected '
        r"pattern of \[0-9a-f\]\+:\[0-9a-f\]\+:\[0-9a-f\]\+\[\.\]\[0-9a-f\]\+"
        r"(?:\x1b\[m)?\n",
        "",
        output,
    )
    # raghilda's unused document converter pins an older ONNX on Windows.
    # Its OS-name check rejects Server 2025 even though this fixture uses only
    # local scalar embeddings, with no ONNX inference. Preserve other warnings.
    return re.sub(
        r"(?m)^[^\n]+[/\\]onnxruntime[/\\]capi[/\\]onnxruntime_validation\.py:\d+: "
        r"UserWarning: Unsupported Windows version \(2025server\)\. "
        r"ONNX Runtime supports Windows 10 and above, only\.\n  warnings\.warn\(\n",
        "",
        output,
    )


def normalize_duckdb_progress(client: McpClient) -> str:
    output = normalize_onnx_device_probe(last_tool_text(client))
    sections = output.split("\r")
    assert all(
        not section.strip() or section.startswith("DuckDB progress:")
        for section in sections[:-1]
    ), output
    output = sections[-1]
    client.transcript[-1]["result"]["content"][0]["text"] = output
    return normalize_trailing_spaces(client)


def normalize_trailing_spaces(client: McpClient) -> str:
    output = last_tool_text(client)
    trailing_newline = output.endswith("\n")
    output = "\n".join(line.rstrip() for line in output.splitlines())
    if trailing_newline:
        output += "\n"
    client.transcript[-1]["result"]["content"][0]["text"] = output
    return output
