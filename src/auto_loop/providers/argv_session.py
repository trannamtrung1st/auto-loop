"""Parse session resume flags from provider argv."""

from __future__ import annotations

import re

_RESUME_RE = re.compile(r"--resume=([^\s]+)")


def resume_session_id_from_argv(argv: list[str]) -> str | None:
    for arg in argv:
        match = _RESUME_RE.match(arg)
        if match:
            return match.group(1)
    return None
