import base64
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from support.client import McpClient
from support.checkpoints import wait_for_path
from support.records import ToolResult, TranscriptEntry
from support.progress import ELAPSED, without_elapsed

LARGE_OUTPUT_SIZE = 2 * 1024 * 1024


def tool_text(result: ToolResult) -> str:
    assert result.get("isError") is not True, result
    content = result["content"]
    assert len(content) == 1 and content[0]["type"] == "text", content
    return content[0]["text"]


def last_tool_text(client: McpClient) -> str:
    result = client.transcript[-1]["result"]
    assert isinstance(result, dict), result
    return tool_text(result)


def last_result_text(client: McpClient) -> str:
    return client.transcript[-1]["result"]["content"][0]["text"]


def _send_before(
    client: McpClient, deadline: float, description: str, **arguments: Any
) -> ToolResult:
    remaining = deadline - time.monotonic()
    assert remaining > 0, f"{description} did not complete"
    original_timeout = client.response_timeout
    client.response_timeout = min(original_timeout, remaining)
    try:
        return client.send(**arguments)
    except TimeoutError as error:
        raise TimeoutError(f"{description}: {error}") from error
    finally:
        client.response_timeout = original_timeout


def wait_for_worker_ready(client: McpClient, description: str) -> None:
    """Wait through public startup snapshots and retain the initial empty poll."""
    deadline = time.monotonic() + client.response_timeout
    poll_start = len(client.transcript)
    result = _send_before(client, deadline, description)
    # Phase observation is optional when its owner is busy.
    while tool_text(result) in {
        "[worker starting]",
        "\n[phase: startup]\n[worker starting]",
        "\n[phase: dependency preparation]\n[worker starting]",
    }:
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"{description} did not complete"
        result = _send_before(
            client, deadline, description, timeout_ms=max(1, int(remaining * 1_000))
        )
    assert tool_text(result) == "\n[idle]", result
    submitted = client.transcript[poll_start]
    submitted["result"] = client.transcript[-1]["result"]
    client.transcript[poll_start:] = [submitted]


def wait_for_prepared_ready(client: McpClient) -> str:
    """Drain provider startup diagnostics before checking an evaluation's output."""
    result = client.send(timeout_ms=60_000)
    output = tool_text(result)
    assert output.endswith("\n[idle]"), output
    return output.removesuffix("\n[idle]")


def entry_result_text(entry: TranscriptEntry) -> str:
    result = entry["result"]
    assert isinstance(result, dict), result
    content = result["content"]
    assert len(content) == 1 and content[0]["type"] == "text", content
    return content[0]["text"]


def large_output(prefix: str) -> str:
    return prefix + ("x" * LARGE_OUTPUT_SIZE) + ("y" * LARGE_OUTPUT_SIZE)


def remove_length_marker(output: str, marker_prefix: str) -> tuple[str, int]:
    marker_start = output.find(marker_prefix)
    assert marker_start >= 0, (
        f"raw output lost length marker {marker_prefix!r}: {output[-500:]!r}"
    )
    marker_end = output.find("\n", marker_start)
    if marker_end < 0:
        marker_end = len(output)
        after_marker = marker_end
    else:
        after_marker = marker_end + 1
    length = int(output[marker_start + len(marker_prefix) : marker_end])
    return output[:marker_start] + output[after_marker:], length


def assert_result_content(
    client: McpClient,
    expected: list[str | bytes],
    *,
    image_reference: str = "live Rscript page {page}",
) -> None:
    result = client.transcript[-1]["result"]
    assert result.get("isError") is not True, result
    content = result["content"]
    assert len(content) == len(expected), (
        f"expected {len(expected)} content blocks, got "
        f"{[item.get('type') for item in content]}"
    )
    page = 0
    for item, expected_item in zip(content, expected):
        if isinstance(expected_item, str):
            assert item == {"type": "text", "text": expected_item}, item
            continue

        image = item
        assert image.keys() == {"type", "data", "mimeType"}, image
        assert image["type"] == "image", image
        assert image["mimeType"] == "image/png", image
        data = base64.b64decode(image["data"], validate=True)
        reference = image_reference.format(page=page + 1)
        assert data == expected_item, (
            f"plot bytes differ: worker returned {len(data)} bytes, "
            f"{reference} returned {len(expected_item)} bytes"
        )
        page += 1
        image["data"] = f"<PNG byte-identical to {reference}>"


def release_worker_callback_gate(
    client: McpClient,
    description: str,
    extra_path_labels: tuple[str, ...] = (),
) -> tuple[Path, ...]:
    result = client.transcript[-1]["result"]
    assert result.get("isError") is not True, result
    content = result["content"]
    assert len(content) == 1 and content[0]["type"] == "text", content
    paths = content[0]["text"].splitlines()
    assert len(paths) == 2 + len(extra_path_labels), content
    content[0]["text"] = "\n".join(
        (
            "<worker callback gate>",
            "<worker callback checkpoint>",
            *(f"<worker callback {label}>" for label in extra_path_labels),
        )
    )

    gate, checkpoint, *extra_paths = map(Path, paths)
    gate.touch()
    wait_for_path(checkpoint, description, client=client, timeout=5)
    return tuple(extra_paths)


def wait_for_idle_output(
    client: McpClient,
    expected: str,
    description: str,
    *,
    completion_timeout_seconds: float = 3,
    **send_arguments: Any,
) -> None:
    """Accumulate public idle output until all expected worker events arrive."""
    deadline = time.monotonic() + completion_timeout_seconds
    poll_start = len(client.transcript)
    collected = ""
    while True:
        result = _send_before(client, deadline, description, **send_arguments)
        assert result.get("isError") is not True, result
        content = result["content"]
        assert len(content) == 1 and content[0]["type"] == "text", content
        output = content[0]["text"]
        if output.endswith("\n[idle]"):
            collected += output.removesuffix("\n[idle]")
            complete = collected + "\n[idle]"
        else:
            complete = collected + output
        if complete == expected:
            content[0]["text"] = expected
            break
        assert output.endswith("\n[idle]"), output
        assert expected.startswith(collected), repr(collected)
        if time.monotonic() >= deadline:
            raise AssertionError(f"{description} did not reach the server")
        time.sleep(0.01)

    polls = client.transcript[poll_start:]
    final_poll = polls[-1]
    client.transcript[poll_start:] = [final_poll]


def wait_for_evaluation_output(
    client: McpClient,
    expected: str | Callable[[str], bool] | None,
    description: str,
    *,
    expected_error: bool | None = False,
    completion_timeout_seconds: float = 3,
    initial_cuts: tuple[str, ...] = (),
    output_cuts: list[str] | None = None,
    received: TranscriptEntry | None = None,
    **send_arguments: Any,
) -> str:
    """Accumulate exact output until the expected state; retain the submitted call."""
    assert received is None or not send_arguments, (
        "a received call must not be resubmitted"
    )
    deadline = time.monotonic() + completion_timeout_seconds
    poll_start = len(client.transcript)
    running = "\n[running; poll with an empty send]"
    waiting = "[waiting for stdin]"
    cuts = output_cuts if output_cuts is not None else []
    cuts.extend(cut for cut in initial_cuts if cut)
    collected = "".join(cuts)
    final_progress = ""

    result = (
        received["result"]
        if received is not None
        else _send_before(client, deadline, description, **send_arguments)
    )
    while True:
        content = result["content"]
        assert len(content) == 1 and content[0]["type"] == "text", content
        raw_output = content[0]["text"]
        output = without_elapsed(raw_output)
        if output.endswith(running):
            assert result.get("isError") is not True, result
            cut = output.removesuffix(running)
            if cut:
                cuts.append(cut)
                collected += cut
            if isinstance(expected, str) and collected + running == expected:
                collected += running
                if progress := ELAPSED.search(raw_output):
                    final_progress = progress[0]
                break
        elif output.endswith(waiting):
            assert result.get("isError") is not True, result
            # Empty input requests add a separator; prompt notices already
            # contain their own newline, which belongs to the output.
            if output != "\n" + waiting:
                cut = output.removesuffix(waiting)
                cuts.append(cut)
                collected += cut
            waiting_output = collected + ("" if collected.endswith("\n") else "\n")
            if expected is None or (
                isinstance(expected, str) and waiting_output + waiting == expected
            ):
                collected = waiting_output + waiting
                cuts.append(collected[len("".join(cuts)) :])
                break
        else:
            if output != "[done]" or not collected:
                collected += output
                cuts.append(output)
            assert expected is None or (
                collected == expected
                if isinstance(expected, str)
                else expected(collected)
            ), repr(collected)
            break
        if isinstance(expected, str):
            assert expected.startswith(collected), repr(collected)
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"{description} did not complete"
        poll_ms = max(1, int(remaining * 1_000))
        if isinstance(expected, str) and expected.endswith((running, waiting)):
            # Observe a nonterminal state without making completion the event
            # that wakes the receive at the logical deadline.
            poll_ms = min(poll_ms, 100)
        result = _send_before(client, deadline, description, timeout_ms=poll_ms)

    assert expected_error is None or result.get("isError", False) is expected_error, (
        result
    )
    content[0]["text"] = (
        collected.removesuffix(running) + final_progress + running
        if final_progress
        else collected
    )
    if received is not None:
        received["result"] = result
        del client.transcript[poll_start:]
    else:
        calls = client.transcript[poll_start:]
        submitted = calls[0]
        submitted["result"] = calls[-1]["result"]
        client.transcript[poll_start:] = [submitted]
    return collected


def collect_running_output(
    client: McpClient,
    description: str,
    *,
    timeouts_ms: tuple[int, ...],
    initial_cuts: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Poll a running evaluation and retain its public output cuts."""
    assert timeouts_ms and all(timeout_ms > 0 for timeout_ms in timeouts_ms)
    cuts: list[str] = []
    wait_for_evaluation_output(
        client,
        None,
        description,
        completion_timeout_seconds=sum(timeouts_ms) / 1_000,
        timeout_ms=timeouts_ms[0],
        initial_cuts=initial_cuts,
        output_cuts=cuts,
    )
    return tuple(cuts)


def assert_exact_interleaving(actual: str, first: str, second: str) -> None:
    assert len(actual) == len(first) + len(second), repr(actual)
    first_offsets = {0}
    for offset, character in enumerate(actual):
        next_offsets = set()
        for first_offset in first_offsets:
            second_offset = offset - first_offset
            if first_offset < len(first) and first[first_offset] == character:
                next_offsets.add(first_offset + 1)
            if second_offset < len(second) and second[second_offset] == character:
                next_offsets.add(first_offset)
        first_offsets = next_offsets
    assert len(first) in first_offsets, repr(actual)
