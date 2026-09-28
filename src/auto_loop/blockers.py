"""External blocker fingerprints for controller convergence."""

from __future__ import annotations

import hashlib
import re

from auto_loop.lifecycle import LifecycleState


def normalize_blocker_summary(summary: str) -> str:
    collapsed = re.sub(r"\s+", " ", (summary or "").strip())
    return collapsed.casefold()


def compute_blocker_fingerprint(
    *,
    summary: str,
    head: str | None,
) -> str:
    """Stable key for repeated external blocker detection (HEAD + normalized reason)."""
    payload = f"blocked|{normalize_blocker_summary(summary)}|{head or ''}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def clear_blocker_tracking(state: LifecycleState) -> None:
    state.last_blocker_fingerprint = None
