"""Failed prelaunch remains an MCP-owned error during integrator admission."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.integrations.test_control import ControlClient
from support.checkpoints import wait_for_checkpoint
from support.client import McpClient
from support.execution import SANDBOXED, Execution, executions
from support.normalization import normalize_recording
from support.r import r_test_environment
from support.records import TranscriptWithCompanions
from support.requirements import MACOS_SANDBOX, R, requires
from support.suites import run_this_suite


def assert_prelaunch_failure(
    client: McpClient, control: ControlClient, root: Path, *, language: str, source: str
) -> str:
    # Activate the public journal without observing startup output or admitting
    # code while the prelaunch failure is pending.
    invalid = control.receive(control.start(op="unknown"), invalid=True)
    assert invalid["error"]["code"] == "invalid_request", invalid
    recordings = root / ".agents/console/sessions"

    def failed_prelaunch() -> Path | None:
        for path in recordings.glob("*/capture/events.jsonl"):
            events = [
                json.loads(line)
                for line in path.read_text().splitlines(keepends=True)
                if line.endswith("\n")
            ]
            if any(event["event"] == "startup_failed" for event in events):
                return path
        return None

    wait_for_checkpoint(
        failed_prelaunch,
        "worker recorded its prelaunch failure",
        root=recordings,
        recursive=True,
        client=client,
    )
    for policy in ({"busy": "wait", "wait_ms": 30000}, {}):
        result = control.request(
            op="evaluate", language=language, code=source, **policy
        )
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


@requires(MACOS_SANDBOX, R)
@executions(SANDBOXED)
def test_failed_prelaunch_rejects_code_without_consuming_mcp_error(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-control-"
    ) as directory:
        root = Path(directory)
        endpoint = root / "control.sock"
        environment, rscript = r_test_environment()
        environment["RETICULATE_PYTHON"] = sys.executable
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
            with ControlClient(endpoint) as control:
                diagnostic = assert_prelaunch_failure(
                    client,
                    control,
                    root,
                    language="r",
                    source='stop("failed prelaunch ran integrator code")',
                )
                assert "Renviron" in diagnostic, diagnostic
                assert "failed prelaunch ran integrator code" not in diagnostic
                transcript = normalize_recording(client.finish(), root)
            transcript, sideband = json.loads(
                json.dumps(
                    [transcript, normalize_recording(control.transcript, root)]
                ).replace(json.dumps(environment["R_HOME"])[1:-1], "<R_HOME>")
            )
            return TranscriptWithCompanions(
                [
                    {
                        "scenario": "Integrator admission rejects a failed worker prelaunch and leaves its diagnostic for MCP.",
                        "fixture": "Native macOS Seatbelt denies the selected R installation's etc/Renviron file during worker startup. An invalid control request activates recording before a journal checkpoint observes prelaunch failure.",
                    },
                    *transcript,
                ],
                {"control.yaml": sideband},
            )


if __name__ == "__main__":
    run_this_suite(__file__)
