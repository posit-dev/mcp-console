import difflib
import json
import sys
from collections.abc import Iterator
from pathlib import Path

from yaml12 import Yaml, format_yaml, parse_yaml, read_yaml

from support.records import (
    McpTranscript,
    Transcript,
    TranscriptWithCompanions,
    YamlStream,
)
from support.progress import normalize_elapsed

root = Path(__file__).resolve().parents[2]
snapshot_directory = root / "tests" / "snapshots"
initialization_suite = "client_server/server/test_tools"
initialization_case = "initializes_and_lists_tools"


def snapshot_path(suite_name: str, case_name: str) -> Path:
    return snapshot_directory / suite_name / f"{case_name}.yaml"


initialization_reference = (
    snapshot_path(initialization_suite, initialization_case)
    .relative_to(root)
    .as_posix()
)


def execution_snapshots(case):
    """Keep distinct direct/sandbox transcripts when their captured policy differs."""
    case.execution_snapshots = True
    return case


def platform_snapshots(*platforms: str, reason: str):
    """Reserve variants for the platform contract this case explicitly tests."""
    assert reason.strip(), "platform snapshots require a nonempty contract reason"

    def decorate(case):
        case.snapshot_platforms = platforms
        case.snapshot_platform_reason = reason
        return case

    return decorate


def identical(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            identical(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            identical(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )
    return left == right


def iter_strings(value: object) -> Iterator[str]:
    if isinstance(value, Yaml):
        yield from iter_strings(value.value)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from iter_strings(key)
            yield from iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_strings(item)
    elif isinstance(value, str):
        yield value


def format_transcript(value: YamlStream) -> str:
    strings = tuple(iter_strings(value))
    prefix = "__MCP_CONSOLE_WHITESPACE_SCALAR_"
    while any(prefix in string for string in strings):
        prefix = f"_{prefix}"

    replacements: dict[str, str] = {}

    def protect(item: object) -> object:
        if isinstance(item, Yaml):
            return Yaml(protect(item.value), tag=item.tag)
        if isinstance(item, dict):
            return {protect(key): protect(mapped) for key, mapped in item.items()}
        if isinstance(item, list):
            return [protect(value) for value in item]
        if isinstance(item, str) and item:
            lines = item.splitlines()
            first_nonempty = next((line for line in lines if line), "")
            needs_quotes = (
                item.isspace()
                or first_nonempty.startswith((" ", "\t"))
                or any(line.isspace() for line in lines)
            )
        else:
            needs_quotes = False
        if needs_quotes:
            placeholder = f"{prefix}{len(replacements)}__"
            replacements[placeholder] = item
            return placeholder
        return item

    formatted = format_yaml(protect(value), multi=True)
    for placeholder, original in replacements.items():
        assert formatted.count(placeholder) == 1, placeholder
        formatted = formatted.replace(placeholder, json.dumps(original))
    formatted = "\n".join(
        "" if line.isspace() else line for line in formatted.split("\n")
    )
    assert identical(value, parse_yaml(formatted, multi=True)), (
        "formatted transcript did not round-trip"
    )
    return formatted


def check_snapshot(
    snapshot: Path, actual: YamlStream, case: str, *, update: bool
) -> None:
    actual_text = format_transcript(actual)

    if update:
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(actual_text, encoding="utf-8")
        print(f"updated {snapshot.relative_to(root)}", flush=True)
        return
    if not snapshot.exists():
        raise SystemExit(
            f"{snapshot.relative_to(root)} is missing; run scripts/test --update {case}"
        )

    expected = read_yaml(snapshot, multi=True)
    if not identical(actual, expected):
        expected_text = format_transcript(expected)
        difference = "".join(
            difflib.unified_diff(
                expected_text.splitlines(keepends=True),
                actual_text.splitlines(keepends=True),
                fromfile=str(snapshot.relative_to(root)),
                tofile="actual",
            )
        )
        raise SystemExit(f"{difference}{case} differs from its snapshot")


def check_text_snapshot(
    snapshot: Path, actual: str, case: str, *, update: bool
) -> None:
    if update:
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(actual, encoding="utf-8")
        print(f"updated {snapshot.relative_to(root)}", flush=True)
        return
    if not snapshot.exists():
        raise SystemExit(
            f"{snapshot.relative_to(root)} is missing; run scripts/test --update {case}"
        )

    expected = snapshot.read_text(encoding="utf-8")
    if actual != expected:
        difference = "".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=str(snapshot.relative_to(root)),
                tofile="actual",
            )
        )
        raise SystemExit(f"{difference}{case} differs from its snapshot")


def normalize_request_ids(transcript: Transcript) -> Transcript:
    # CLI transcripts also record raw input strings under the same key.
    cancelled_ids = {
        message["params"]["requestId"]
        for entry in transcript
        if isinstance(message := entry.get("input"), dict)
        and message.get("method") == "notifications/cancelled"
    }
    labels = {}
    for entry in transcript:
        if "id" in entry and entry["id"] in cancelled_ids:
            labels[entry["id"]] = f"<cancelled request {len(labels) + 1}>"
    rendered = []
    for entry in transcript:
        entry = entry.copy()
        if isinstance(result := entry.get("result"), dict) and "content" in result:
            entry["result"] = {
                **result,
                "content": [
                    {**part, "text": normalize_elapsed(part["text"])}
                    if part["type"] == "text" and isinstance(part["text"], str)
                    else part
                    for part in result["content"]
                ],
            }
        if entry.keys() & {"input", "send"}:
            request_id = entry.pop("id", None)
            if request_id in labels:
                entry["id"] = labels[request_id]
        message = entry.get("input", {})
        if (
            isinstance(message, dict)
            and message.get("method") == "notifications/cancelled"
        ):
            params = message["params"]
            if params["requestId"] in labels:
                entry["input"] = {
                    **message,
                    "params": {**params, "requestId": labels[params["requestId"]]},
                }
        rendered.append(entry)
    return rendered


def compact_initializations(
    actual: Transcript,
    references: list[Path],
    *,
    execution: str | None,
    execution_specific: bool,
) -> YamlStream:
    expected = [
        (path, normalize_request_ids(read_yaml(path, multi=True)))
        for path in references
    ]
    assert all(reference for _, reference in expected), "empty initialization reference"
    compacted = []
    index = 0
    while index < len(actual):
        for path, reference in expected:
            if identical(actual[index : index + len(reference)], reference):
                variant = (
                    path.stem.removeprefix(Path(initialization_reference).stem)
                    .removesuffix(".direct")
                    .removeprefix(".")
                )
                variant = variant.replace(".win32", "").removeprefix("win32")
                if execution is not None and not execution_specific:
                    # Fixture write grants are checked in full above, but their
                    # reference label need not split a portable transcript.
                    if variant == "writable" or variant.endswith("-writable"):
                        variant = variant.removesuffix("writable").removesuffix("-")
                target = (
                    f"{variant + ' ' if variant else ''}MCP initialization for this execution mode"
                    if execution is not None
                    else path.relative_to(root).as_posix()
                )
                compacted.append(Yaml(target, tag="!same-as"))
                index += len(reference)
                break
        else:
            compacted.append(actual[index])
            index += 1
    return compacted


def check_recording(
    suite_name: str,
    case_name: str,
    recorded: Transcript | TranscriptWithCompanions,
    *,
    update: bool,
    execution: str | None = None,
    platform_specific: bool = False,
    execution_specific: bool = False,
) -> set[Path]:
    snapshot = snapshot_path(suite_name, case_name)
    initialization = snapshot == root / initialization_reference
    mode_suffix = (f".{sys.platform}" if platform_specific else "") + (
        ".direct"
        if initialization and execution == "direct"
        else ".sandbox"
        if execution_specific and execution == "sandbox"
        else ""
    )
    primary = snapshot.with_suffix(f"{mode_suffix}.yaml")
    case = f"{suite_name}::{case_name}"
    if execution is not None:
        case += f"[{execution}]"
    if isinstance(recorded, TranscriptWithCompanions):
        actual = normalize_request_ids(recorded.transcript)
        companions = []
        for name, contents in recorded.companions.items():
            assert name and Path(name).name == name and not name.startswith("."), name
            assert name in {"md", "qmd"} or name.endswith(".yaml"), name
            suffix = (
                f".{name.removesuffix('.yaml')}{mode_suffix}.yaml"
                if initialization
                else f"{mode_suffix}.{name}"
            )
            companions.append((snapshot.with_suffix(suffix), contents))
    else:
        actual = normalize_request_ids(recorded)
        companions = []
    if not initialization:
        reference = root / initialization_reference
        references = [
            reference,
            # Prefer the canonical direct handshake when variant schemas are equal.
            *sorted(
                reference.parent.glob(f"{reference.stem}.*.yaml"),
                key=lambda path: (
                    path
                    != reference.with_suffix(
                        ".win32.direct.yaml"
                        if sys.platform == "win32"
                        else ".direct.yaml"
                    ),
                    path != reference.with_suffix(".direct.yaml"),
                    path,
                ),
            ),
        ]
        if execution is not None:
            references = [
                path
                for path in references
                if path.stem.endswith(".direct") == (execution == "direct")
            ]
        if sys.platform != "win32" or any(
            "win32" in path.stem.split(".") for path in references
        ):
            references = [
                path
                for path in references
                if ("win32" in path.stem.split(".")) == (sys.platform == "win32")
            ]
        assert references, f"no initialization reference for {execution}"
        actual = compact_initializations(
            actual,
            references,
            execution=execution,
            execution_specific=execution_specific,
        )
    check_snapshot(primary, actual, case, update=update)
    checked = {primary}
    for companion, contents in companions:
        if isinstance(contents, str):
            check_text_snapshot(companion, contents, case, update=update)
        else:
            assert companion.suffix == ".yaml", companion
            if isinstance(contents, McpTranscript):
                contents = normalize_request_ids(contents.transcript)
                if not initialization:
                    contents = compact_initializations(
                        contents,
                        references,
                        execution=execution,
                        execution_specific=execution_specific,
                    )
            elif initialization:
                # Canonical companions are MCP handshakes; ordinary YAML
                # companions can carry protocol IDs that must remain visible.
                contents = normalize_request_ids(contents)
            check_snapshot(companion, contents, case, update=update)
        checked.add(companion)
    return checked
