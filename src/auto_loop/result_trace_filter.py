"""Display-only filter that hides AUTO_LOOP_RESULT blocks from normalized traces."""

from __future__ import annotations

from auto_loop.protocol import RESULT_BLOCK_END, RESULT_BLOCK_START


def _hold_suffix(text: str, marker: str) -> str:
    """Longest suffix of ``text`` that is a proper prefix of ``marker``."""
    max_hold = min(len(text), len(marker) - 1)
    for size in range(max_hold, 0, -1):
        if marker.startswith(text[-size:]):
            return text[-size:]
    return ""


class ResultTraceFilter:
    """Stateful filter for assistant message text in live/readable traces."""

    def __init__(self) -> None:
        self._inside = False
        self._pending = ""

    def reset(self) -> None:
        self._inside = False
        self._pending = ""

    def flush(self) -> str:
        """Emit buffered non-marker text at turn end (partial markers are discarded)."""
        if self._inside:
            self._inside = False
            self._pending = ""
            return ""
        pending = self._pending
        self._pending = ""
        if not pending:
            return ""
        if RESULT_BLOCK_START.startswith(pending) or RESULT_BLOCK_END.startswith(pending):
            return ""
        return pending

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        data = self._pending + chunk
        self._pending = ""
        emitted: list[str] = []
        index = 0
        while index < len(data):
            if self._inside:
                end_at = data.find(RESULT_BLOCK_END, index)
                if end_at == -1:
                    tail = data[index:]
                    self._pending = _hold_suffix(tail, RESULT_BLOCK_END)
                    break
                index = end_at + len(RESULT_BLOCK_END)
                self._inside = False
                continue
            start_at = data.find(RESULT_BLOCK_START, index)
            if start_at == -1:
                tail = data[index:]
                hold = _hold_suffix(tail, RESULT_BLOCK_START)
                if hold:
                    emitted.append(tail[: len(tail) - len(hold)])
                    self._pending = hold
                else:
                    emitted.append(tail)
                break
            emitted.append(data[index:start_at])
            index = start_at + len(RESULT_BLOCK_START)
            self._inside = True
        return "".join(emitted)
