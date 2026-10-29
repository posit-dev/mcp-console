#!/usr/bin/env -S uv run --script

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.python.test_startup import selected_python
from support.assertions import last_result_text
from support.client import McpClient, stop_client
from support.execution import SANDBOXED, Execution, executions
from support.normalization import code
from support.processes import (
    capture_process_identity,
    host_process_id,
    kill_processes,
    live_processes,
)
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, PROCESS_EVENTS, requires
from support.sandbox_observation import observed_sandbox_descendants
from support.suites import run_this_suite


@requires(PROCESS_EVENTS, NATIVE_FIXTURES)
@executions(SANDBOXED)
def test_eof_retires_every_named_worker_and_descendant(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        environment = selected_python(root, Path(sys.executable))
        with observed_sandbox_descendants(root, environment) as observe:
            client = McpClient(binary, execution.serve(), environment, root)
            identities = []
            namespaces = set()
            try:
                client.initialize_and_list_tools()
                for session in ("default", "alpha", "beta"):
                    client.expect(
                        None,
                        session=session,
                        # fmt: python
                        python=code("""
                            import json
                            import os
                            import subprocess
                            import sys

                            child = subprocess.Popen(
                                [
                                    sys.executable,
                                    "-c",
                                    "import os, signal; "
                                    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                                    "print(os.getpid(), flush=True); signal.pause()",
                                ],
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                text=True,
                                start_new_session=True,
                            )
                            child_pid = int(child.stdout.readline())
                            print(
                                json.dumps(
                                    {
                                        "worker": os.getpid(),
                                        "relay": os.getppid(),
                                        "child": child_pid,
                                        "pid_namespace": (
                                            os.stat("/proc/self/ns/pid").st_ino if sys.platform == "linux" else None
                                        ),
                                    },
                                    sort_keys=True,
                                )
                            )
                            """),
                    )
                    pids = json.loads(last_result_text(client))
                    namespace = pids.pop("pid_namespace")
                    if sys.platform == "linux":
                        assert isinstance(namespace, int) and namespace > 0
                        assert namespace not in namespaces, namespace
                        namespaces.add(namespace)
                    else:
                        assert namespace is None, namespace
                    assert set(pids) == {"worker", "relay", "child"}, pids
                    assert all(
                        isinstance(pid, int) and pid > 0 for pid in pids.values()
                    )
                    observed = {
                        name: host_process_id(
                            pid, client.process.pid, pid_namespace=namespace
                        )
                        for name, pid in pids.items()
                    }
                    current = [
                        capture_process_identity(pid) for pid in observed.values()
                    ]
                    assert len(set(current)) == 3 and not set(current).intersection(
                        identities
                    )
                    identities.extend(current)
                    # macOS cleanup covers children registered by its native
                    # owner; the child's startup receipt alone cannot prove that.
                    observe(observed["child"], client.process)
                    client.transcript[-1]["result"]["content"][0]["text"] = (
                        '{"child": "<child pid>", "relay": "<relay pid>", '
                        '"worker": "<worker pid>"}\n'
                    )
                    client.transcript[-1]["transcript_normalization"] = {
                        "target": "result.content[0].text",
                        "process_ids_and_namespace": (
                            "omitted after live identity and namespace assertions"
                        ),
                    }
                assert len(live_processes(identities)) == 9
                transcript = client.finish()
                assert live_processes(identities) == [], (
                    "session processes survived EOF"
                )
                return transcript
            finally:
                stop_client(client)
                kill_processes(identities)


if __name__ == "__main__":
    run_this_suite(__file__)
