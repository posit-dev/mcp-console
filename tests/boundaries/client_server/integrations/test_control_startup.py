"""Failed prelaunch remains an MCP-owned error during integrator admission."""

import json
import sys
import tempfile
from contextlib import ExitStack, closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.integrations.test_control import ControlClient
from support.checkpoints import FifoCheckpoint, wait_for_checkpoint
from support.client import McpClient
from support.execution import SANDBOXED, Execution, executions
from support.native import LOADER_VARIABLE, build_interposer
from support.normalization import normalize_recording
from support.r import r_test_environment
from support.records import TranscriptWithCompanions
from support.requirements import (
    MACOS_SANDBOX,
    NATIVE_FIXTURES,
    PTHREAD_RUNTIME_PARKING,
    R,
    requires,
)
from support.suites import run_this_suite


def assert_prelaunch_failure(
    client: McpClient,
    control: ControlClient,
    root: Path,
    *,
    startup_release: FifoCheckpoint,
    language: str,
    source: str,
) -> str:
    recordings = root / ".agents/console/sessions"

    def submitted_evaluation() -> Path | None:
        for path in recordings.glob("*/capture/events.jsonl"):
            events = [
                json.loads(line)
                for line in path.read_text().splitlines(keepends=True)
                if line.endswith("\n")
            ]
            if any(
                event["event"] == "tool_call"
                and event["request"]["arguments"] == {language: source}
                for event in events
            ):
                return path
        return None

    for policy in ({"busy": "wait", "wait_ms": 30000}, {}):
        pending = control.start(op="evaluate", language=language, code=source, **policy)
        if policy:
            # The waiting evaluation owns the startup wait. Keep preparation
            # gated until its public request is recorded instead of imposing a
            # separate deadline on startup before submitting any evaluation.
            wait_for_checkpoint(
                submitted_evaluation,
                "integrator evaluation recorded while startup is held",
                root=recordings,
                recursive=True,
                client=client,
            )
            startup_release.release()
        result = control.receive(pending)
        assert "error" in result, result
        assert result["error"]["code"] == "execution_failed", result
    status = control.request(op="status")
    assert status["result"] == {
        "state": "failed",
        "idle": False,
        "evaluation": None,
    }, status
    preview = control.request(op="preview")["result"]["content"]
    failure = client.send()
    assert failure["isError"], failure
    assert failure["content"] == preview, (failure, preview)
    client.expect("\n[idle]")
    return "".join(block["text"] for block in preview)


@requires(MACOS_SANDBOX, NATIVE_FIXTURES, PTHREAD_RUNTIME_PARKING, R)
@executions(SANDBOXED)
def test_failed_prelaunch_rejects_code_without_consuming_mcp_error(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with (
        tempfile.TemporaryDirectory(dir="/tmp", prefix="console-control-") as directory,
        ExitStack() as resources,
    ):
        root = Path(directory)
        endpoint = root / "control.sock"
        checkpoints = {
            name: resources.enter_context(closing(FifoCheckpoint.create(root / name)))
            for name in ("reached", "release")
        }
        environment, rscript = r_test_environment()
        environment["RETICULATE_PYTHON"] = sys.executable
        environment.update(
            {
                LOADER_VARIABLE: str(
                    build_interposer(root, "startup_admission_checkpoint")
                ),
                "MCP_CONSOLE_TEST_ADMISSION_ORDINAL": "0",
                "MCP_CONSOLE_TEST_ADMISSION_REACHED": str(checkpoints["reached"].path),
                "MCP_CONSOLE_TEST_ADMISSION_RELEASE": str(checkpoints["release"].path),
            }
        )
        denied = (rscript.parent.parent / "etc/Renviron").resolve()
        assert denied.is_file(), denied
        with McpClient(
            binary,
            execution.serve(
                "--control-socket",
                str(endpoint),
                "-c",
                "r.packages=[]",
                "-c",
                f"sandbox.filesystem.deny={json.dumps([str(denied)])}",
            ),
            environment,
            root,
        ) as client:
            client.initialize_and_list_tools()
            checkpoints["reached"].wait("startup is held before runtime preparation")
            try:
                with ControlClient(endpoint) as control:
                    diagnostic = assert_prelaunch_failure(
                        client,
                        control,
                        root,
                        startup_release=checkpoints["release"],
                        language="r",
                        source='stop("failed prelaunch ran integrator code")',
                    )
                    assert "Renviron" in diagnostic, diagnostic
                    assert "failed prelaunch ran integrator code" not in diagnostic
                    transcript = normalize_recording(client.finish(), root)
            finally:
                checkpoints["release"].release()
            transcript, sideband = json.loads(
                json.dumps(
                    [transcript, normalize_recording(control.transcript, root)]
                ).replace(json.dumps(environment["R_HOME"])[1:-1], "<R_HOME>")
            )
            return TranscriptWithCompanions(
                [
                    {
                        "scenario": "Integrator admission rejects a failed worker prelaunch and leaves its diagnostic for MCP.",
                        "fixture": "A native checkpoint holds startup before runtime preparation until the integrator's waiting evaluation is journaled. macOS Seatbelt then denies the selected R installation's etc/Renviron file during worker startup.",
                    },
                    *transcript,
                ],
                {"control.yaml": sideband},
            )


if __name__ == "__main__":
    run_this_suite(__file__)
