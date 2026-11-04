"""Rich results for ordinary Python calls to Console."""

from __future__ import annotations

from mcp.types import CallToolResult, ImageContent

from ._common import content_text, result_display_text


class ConsoleResult(CallToolResult):
    """An MCP `CallToolResult` with text, image, and notebook conveniences.

    `MCPConsole.send()` and `AsyncMCPConsole.send()` return this type, including
    for text-only output. `content` preserves the order of the original SDK
    blocks; `text` and `images` provide separate views of those blocks.
    All MCP fields and validation/serialization methods are inherited from
    `mcp.types.CallToolResult`. The result can be passed directly to code
    accepting that type. No image data is decoded or re-encoded.

    `print(result)` renders text with MIME placeholders for images. In an
    IPython notebook, `display(result)` or leaving the result as the last
    expression renders each text and image block in order. Ordinary calls and
    text rendering do not import or require IPython.

    Construct results with the SDK's keyword fields, such as
    `ConsoleResult(content=[])`. Use `model_dump()` or `model_dump_json()`
    to serialize the complete result.

    Attributes:
        content: Native MCP content blocks in their original order.
        structured_content: The structured payload, or `None` when omitted.
        is_error: Whether the tool call ended in an MCP tool error.
        meta: MCP result metadata, or `None` when omitted.
        result_type: The SDK's result completion status.

    Examples:
        ```python
        from mcp_console import MCPConsole

        with MCPConsole() as console:
            result = console(r="plot(1:3)")
            print(result)
            for image in result.images:
                print(image.mime_type)
        ```

        Images are native MCP `ImageContent` objects. Their `data` field holds
        base64 text; decode it when saving image bytes:

        ```python
        import base64
        from pathlib import Path

        image = result.images[0]
        Path("plot.png").write_bytes(base64.b64decode(image.data))
        ```
    """

    @property
    def text(self) -> str:
        """Text blocks concatenated without added separators or image placeholders.

        This is the text received from Console, including lifecycle notices.
        Use `structured_content` for complete requirements inspection data.
        """
        return "".join(item.text for item in self.content if item.type == "text")

    @property
    def images(self) -> list[ImageContent]:
        """Native image blocks in order, with their original MIME and base64 data."""
        return [item for item in self.content if item.type == "image"]

    def __str__(self) -> str:
        """Render text and image placeholders without raising on tool errors.

        Successful requirements inspection retains the complete formatted JSON
        representation used by earlier string-returning clients.
        """
        return result_display_text(self)

    def __repr__(self) -> str:
        return (
            f"ConsoleResult(content={len(self.content)} blocks, "
            f"images={len(self.images)}, is_error={self.is_error})"
        )

    def _ipython_display_(self) -> None:
        # A MIME bundle holds alternative representations of one item. Publish
        # blocks separately so multiple images and intervening text keep order.
        from IPython.display import display

        for item in self.content:
            data = {"text/plain": content_text(item)}
            if item.type == "image":
                data[item.mime_type] = item.data
            display(data, raw=True)


class ConsoleError(RuntimeError):
    """An MCP tool error whose `result` retains the complete Console response.

    Raised by `send()` and `console(...)`. Catching `RuntimeError` continues to
    work; `result` also preserves images and structured content produced before
    the failure. Transport and connection errors retain their existing types.
    Language errors reported as ordinary output do not raise this exception.

    Args:
        result: The `ConsoleResult` marked as an MCP tool error.

    Attributes:
        result: The complete MCP result. It can be inspected, serialized,
            printed, or displayed in a notebook.

    Examples:
        ```python
        from mcp_console import ConsoleError, MCPConsole

        with MCPConsole() as console:
            try:
                console(r='quit(save = "no", status = 1L)')
            except ConsoleError as error:
                print(error)
                result = error.result
        ```
    """

    def __init__(self, result: ConsoleResult) -> None:
        self.result = result
        super().__init__(str(result) or "MCP Console returned an error")
