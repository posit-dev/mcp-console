"""Explicit launch fixtures; selecting a mode never rewrites McpClient arguments."""

from collections.abc import Callable
from dataclasses import dataclass
import os
from pathlib import Path

from support.requirements import SANDBOX, WORKER, Case, Requirement


@dataclass(frozen=True)
class Execution:
    name: str
    requirements: tuple[Requirement, ...] = ()

    def serve(self, *arguments: str) -> tuple[str, ...]:
        assert self.name in {"direct", "sandbox"}, self.name
        assert "--no-sandbox" not in arguments, (
            "execution.serve() selects --no-sandbox; pass only shared serve arguments"
        )
        assert self.name == "sandbox" or not any(
            argument == "--writable-root" or argument.startswith("--writable-root=")
            for argument in arguments
        ), "--writable-root requires sandbox execution; use SANDBOXED.serve()"
        return (
            "serve",
            *(("--no-sandbox",) if self.name == "direct" else ()),
            *arguments,
        )

    def command(self, binary: Path, *arguments: str) -> list[str]:
        assert self.name in {"direct", "sandbox"}, self.name
        target = [str(binary), *arguments]
        return (
            [str(binary), "sandbox", "--", *target]
            if self.name == "sandbox"
            else target
        )


DIRECT = Execution("direct", (WORKER,))
SANDBOXED = Execution("sandbox", (WORKER, SANDBOX))

# Ordinary runtime semantics run once. Windows shared cases use direct execution;
# its native acceptance suite owns sandbox coverage.
RUNTIME = DIRECT if os.name == "nt" else SANDBOXED


def executions(*modes: Execution) -> Callable[[Case], Case]:
    assert modes and len({mode.name for mode in modes}) == len(modes)

    def decorate(case: Case) -> Case:
        case.executions = modes
        return case

    return decorate
