import os
import signal
import subprocess
import sys
from pathlib import Path
from collections.abc import Sequence


def process_group_exists(process_group: int) -> bool:
    assert process_group > 1, process_group
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_exists(process_id: int) -> bool:
    assert process_id > 1, process_id
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_process_id(process_id: int | None) -> None:
    if process_id is None:
        return
    assert process_id > 1, process_id
    try:
        os.kill(process_id, signal.SIGKILL)
    except ProcessLookupError:
        pass


def stop_process_group(process_group: int | None) -> None:
    if process_group is None:
        return
    assert process_group > 1, process_group
    assert process_group != os.getpgrp(), process_group
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        pass


def stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.kill()
    process.wait()


ProcessIdentity = tuple[int, int, int]

if sys.platform == "darwin":
    from support.macos import (
        current_darwin_process_identity as current_process_identity,
        capture_darwin_process_identity as capture_process_identity,
        darwin_child_process_identities as child_process_identities,
        darwin_process_file_descriptors as process_file_descriptors,
        live_darwin_processes as live_processes,
        signal_darwin_process as signal_process,
        kill_darwin_processes as kill_processes,
    )
else:

    def current_process_identity(pid: int) -> ProcessIdentity | None:
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        except (FileNotFoundError, ProcessLookupError):
            # Exit can remove the entry before open or invalidate an open stat file.
            return None
        return (pid, int(fields[19]), 0)

    def capture_process_identity(pid: int) -> ProcessIdentity:
        identity = current_process_identity(pid)
        assert identity is not None, f"process {pid} exited before identity capture"
        return identity

    def child_process_identities(
        parent: ProcessIdentity,
    ) -> tuple[ProcessIdentity, ...]:
        for _ in range(10):
            assert current_process_identity(parent[0]) == parent
            children = set()
            for task in Path(f"/proc/{parent[0]}/task").iterdir():
                try:
                    task_children = (task / "children").read_text()
                except (FileNotFoundError, ProcessLookupError):
                    # Exiting tasks can reparent children to an already-scanned
                    # task. Start over rather than return an incomplete set.
                    break
                children.update(map(int, task_children.split()))
            else:
                assert current_process_identity(parent[0]) == parent
                return tuple(capture_process_identity(pid) for pid in sorted(children))
        raise RuntimeError(
            f"could not observe children of process {parent[0]} after 10 task scans"
        )

    def process_file_descriptors(identity: ProcessIdentity) -> set[int]:
        assert current_process_identity(identity[0]) == identity
        descriptors = {
            int(path.name) for path in Path(f"/proc/{identity[0]}/fd").iterdir()
        }
        assert current_process_identity(identity[0]) == identity
        return descriptors

    def live_processes(identities: Sequence[ProcessIdentity]) -> list[int]:
        return [
            identity[0]
            for identity in identities
            if current_process_identity(identity[0]) == identity
        ]

    def signal_process(identity: ProcessIdentity, number: int) -> bool:
        if current_process_identity(identity[0]) != identity:
            return False
        try:
            descriptor = os.pidfd_open(identity[0])
        except ProcessLookupError:
            return False
        try:
            if current_process_identity(identity[0]) != identity:
                return False
            try:
                signal.pidfd_send_signal(descriptor, number)
            except ProcessLookupError:
                return False
            return True
        finally:
            os.close(descriptor)

    def kill_processes(identities: Sequence[ProcessIdentity]) -> list[int]:
        survivors = live_processes(identities)
        for identity in identities:
            signal_process(identity, signal.SIGKILL)
        return survivors


def host_process_id(process_id: int, owner: int) -> int:
    """Resolve a live fixture's namespace PID only among its host owner's descendants."""
    assert process_id > 0 and owner > 1
    if sys.platform != "linux":
        return process_id
    pending = [owner]
    matches = []
    while pending:
        pid = pending.pop()
        try:
            status = Path(f"/proc/{pid}/status").read_text()
            namespace_ids = next(
                line.split()[1:]
                for line in status.splitlines()
                if line.startswith("NSpid:")
            )
            if int(namespace_ids[-1]) == process_id:
                matches.append(pid)
            for task in Path(f"/proc/{pid}/task").iterdir():
                pending.extend(map(int, (task / "children").read_text().split()))
        except (FileNotFoundError, ProcessLookupError):
            # Other descendants may exit while the gated fixture remains alive.
            continue
    assert len(matches) == 1, (process_id, owner, matches)
    return matches[0]
