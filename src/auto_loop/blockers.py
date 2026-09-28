"""External blocker fingerprints for controller convergence.

Blocker evidence uses the same product working-tree snapshot as planner mutation
checks (`capture_product_working_fingerprint`): Git modes use ``git status -uall``
rows with per-path content digests; ``git.mode: off`` uses filesystem product
files. Git-ignored product paths are omitted from that snapshot even when Auto Loop
can review them via explicit path targets, so changing only an ignored product
file does not reset the repeat guard. Plan document hash is always included so
worker updates to ``plan.md`` count as progress.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal

from auto_loop.lifecycle import LifecyclePhase, LifecycleState

ImplementerSlot = Literal["planner", "worker"]


def normalize_blocker_summary(summary: str) -> str:
    collapsed = re.sub(r"\s+", " ", (summary or "").strip())
    return collapsed.casefold()


def product_evidence_fingerprint(rows: list[list[str]] | None) -> str:
    """Hash of sorted product working-tree rows (path, status, content digest)."""
    if not rows:
        return ""
    return hashlib.sha256(
        json.dumps(rows, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class BlockerFingerprintInput:
    phase: LifecyclePhase
    implementer_slot: ImplementerSlot
    summary: str
    git_head: str | None
    plan_sha256: str | None
    evidence_fingerprint: str


def compute_blocker_fingerprint(ctx: BlockerFingerprintInput) -> str:
    """Same blocker text is not enough; relevant observable state must match too."""
    parts = [
        ctx.phase,
        ctx.implementer_slot,
        normalize_blocker_summary(ctx.summary),
        ctx.git_head or "",
        ctx.plan_sha256 or "",
        ctx.evidence_fingerprint,
    ]
    payload = "blocked|" + "|".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def clear_blocker_tracking(state: LifecycleState) -> None:
    state.last_blocker_fingerprint = None
