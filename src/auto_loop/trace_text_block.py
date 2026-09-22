"""Presentation of contiguous thinking and message blocks.

``AssistantTraceNormalizer`` decides which text is emitted. This renderer decides
how that text is displayed: one ``[thinking]`` or ``[message]`` prefix when a
block starts, with later lines indented to the prefix width.
"""

from __future__ import annotations

from auto_loop.providers.cursor import TraceEventKind, trace_text_prefix


def continuation_prefix(kind: TraceEventKind) -> str:
    """Spaces matching the visible width of the thinking or message prefix."""
    prefix = trace_text_prefix(kind)
    return " " * _visible_width(prefix)


def _visible_width(prefix: str) -> int:
    """Column width of a plain prefix. Current prefixes are ASCII."""
    return len(prefix)


class TraceTextBlock:
    """Format one thinking or message stream as a single prefixed block.

    A block stays open across same-kind chunks and explicit newlines. It closes
    on a kind change and when the caller finishes the segment (tool event,
    provider attempt, turn boundary, or trace finalization).
    """

    def __init__(self) -> None:
        self._kind: TraceEventKind | None = None
        self._at_line_start = True
        self._prefixed = False

    def feed(self, kind: TraceEventKind, text: str) -> str:
        """Append ``text`` to the open block, or start one, and return display text."""
        if not text:
            return ""
        chunks: list[str] = []
        if self._kind is not None and self._kind is not kind:
            chunks.append(self.close())
        if self._kind is None:
            self._kind = kind
            self._at_line_start = True
            self._prefixed = False
        lines = text.split("\n")
        for index, line in enumerate(lines):
            if index:
                chunks.append("\n")
                self._at_line_start = True
            if not line:
                continue
            if self._at_line_start:
                if self._prefixed:
                    chunks.append(continuation_prefix(kind))
                else:
                    chunks.append(trace_text_prefix(kind))
                    self._prefixed = True
                self._at_line_start = False
            chunks.append(line)
        return "".join(chunks)

    def close(self) -> str:
        """End the open block. Returns a newline when the current line is unfinished."""
        if self._kind is None:
            return ""
        newline = "" if self._at_line_start else "\n"
        self._kind = None
        self._at_line_start = True
        self._prefixed = False
        return newline
