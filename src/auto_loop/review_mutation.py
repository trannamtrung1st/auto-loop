"""Resume reconciliation after a reviewer product-mutation fail-safe."""

from __future__ import annotations

from pathlib import Path

from auto_loop.config import AutoLoopConfig
from auto_loop.git import GitProtocolError
from auto_loop.lifecycle import LifecycleState, ReviewMutationRecovery
from auto_loop.models import SessionSlot
from auto_loop.product_state import product_excludes
from auto_loop.protection import (
    ProductFingerprint,
    capture_product_fingerprint,
    diff_product_fingerprints,
)
from auto_loop.review_targets import fingerprint_path, resolve_review_path, sha256_file


def short_sha(value: str | None) -> str:
    if not value:
        return "n/a"
    return value[:7]


def format_review_mutation_message(
    *,
    expected_head: str | None,
    observed_head: str | None,
    details: list[str],
    config_rel: str,
) -> str:
    lines = [
        "Reviewer product mutation detected.",
        f"Expected HEAD: {short_sha(expected_head)}",
        f"Observed HEAD: {short_sha(observed_head)}",
    ]
    for detail in details:
        if detail.startswith("HEAD changed"):
            continue
        lines.append(detail)
    lines.extend(
        [
            "",
            "Review result discarded.",
            "State preserved.",
            "",
            "Inspect the repository if desired, then run:",
            f"  auto-loop resume {config_rel}",
        ]
    )
    return "\n".join(lines)


def evidence_restored(
    repo: Path,
    config: AutoLoopConfig,
    recovery: ReviewMutationRecovery,
) -> bool:
    """True when repository evidence matches the discarded review snapshot."""
    try:
        current = capture_product_fingerprint(
            repo, excludes=product_excludes(config), config=config
        )
    except (OSError, GitProtocolError, ValueError):
        return False
    expected = ProductFingerprint(
        head=recovery.expected_head or "",
        rows=tuple(tuple(row) for row in recovery.expected_product_rows),
    )
    if diff_product_fingerprints(expected, current):
        return False
    plan_path = repo / config.plan_file
    actual_plan = sha256_file(plan_path) if plan_path.is_file() else None
    if actual_plan != recovery.expected_plan_sha256:
        return False
    review = recovery.suspended_review
    if review is None:
        return False
    if (
        review.current_candidate_head
        and recovery.expected_head
        and review.current_candidate_head != recovery.expected_head
    ):
        return False
    for target in review.targets:
        if target.kind == "git_range":
            if recovery.expected_head and target.head_commit != recovery.expected_head:
                return False
            continue
        if target.kind == "content":
            continue
        try:
            resolved = resolve_review_path(repo, target.path)
            digest, exists = fingerprint_path(resolved)
        except (OSError, GitProtocolError, ValueError):
            return False
        if exists != target.exists or digest != target.fingerprint:
            return False
    return True


def resume_slot(repo: Path, config: AutoLoopConfig, state: LifecycleState) -> SessionSlot:
    """Session that resume should run while mutation recovery is still pending."""
    recovery = state.review_mutation_recovery
    if recovery is None:
        return state.next_session
    if recovery.reconciled:
        return state.next_session
    if evidence_restored(repo, config, recovery):
        return recovery.reviewer_slot
    return recovery.implementer_slot
