"""Delay each readiness reply until the next request, without wall-clock sleeps."""

from typing import Any

from ipykernel.ipkernel import IPythonKernel
from ipykernel.kernelapp import IPKernelApp


class DelayedInfoKernel(IPythonKernel):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.pending_info: tuple[Any, Any, dict] | None = None

    async def kernel_info_request(self, stream: Any, ident: Any, parent: dict) -> None:
        assert self.session is not None
        reply = self.session.msg(
            "kernel_info_reply", {"status": "ok", **self.kernel_info}, parent
        )
        # Construct the reply before stock dispatch emits idle. Hold only its
        # delivery: independent Shell and IOPub channels need not arrive together.
        # A subsequent request releases it and deterministically exercises retry
        # correlation without changing deadlines or sleeping in the fixture.
        if self.pending_info is not None:
            previous_stream, previous_ident, previous_reply = self.pending_info
            self.session.send(previous_stream, previous_reply, ident=previous_ident)
        self.pending_info = stream, ident, reply


IPKernelApp.launch_instance(kernel_class=DelayedInfoKernel)
