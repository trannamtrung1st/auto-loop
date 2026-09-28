"""Mechanical validation that a persisted active review still matches the repository."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from auto_loop.config import AutoLoopConfig
from auto_loop.git import GitProtocolError, head_commit, resolve_commit
from auto_loop.git_policy import git_usable
from auto_loop.lifecycle import ActiveReview
from auto_loop.review_targets import sha256_file, verify_path_targets_unchanged

STALE_REVIEW_REASON = "active review evidence changed"


@dataclass(frozen=True)
class ActiveReviewEvidenceMismatch:
    reason: str
    details: tuple[str, ...]
    expected_head: str | None
    observed_head: str | None


def active_review_evidence_matches(
    repo: Path,
    config: AutoLoopConfig,
    active: ActiveReview,
) -> bool:
    return assess_active_review_evidence_mismatch(repo, config, active) is None


def assess_active_review_evidence_mismatch(
    repo: Path,
    config: AutoLoopConfig,
    active: ActiveReview,
) -> ActiveReviewEvidenceMismatch | None:
    """Return mismatch details when persisted review evidence differs from the repo."""
    details: list[str] = []
    observed_head: str | None = None
    expected_head: str | None = None

    plan_path = repo / config.plan_file
    if active.plan_sha256 is not None:
        actual_plan = sha256_file(plan_path) if plan_path.is_file() else None
        if actual_plan != active.plan_sha256:
            details.append("plan evidence changed since review was requested")

    path_details = [
        item
        for item in verify_path_targets_unchanged(repo, active.targets)
        if not item.startswith("HEAD changed")
    ]
    details.extend(path_details)

    if git_usable(repo, config):
        try:
            observed_head = head_commit(repo)
        except GitProtocolError:
            observed_head = None

    if observed_head is not None and config.git.mode == "required":
        if active.scope == "final" and active.current_candidate_head:
            expected_head = active.current_candidate_head
            try:
                if resolve_commit(repo, expected_head) != observed_head:
                    details.append(
                        f"final candidate HEAD {expected_head[:7]} != current HEAD "
                        f"{observed_head[:7]}"
                    )
            except GitProtocolError:
                details.append("final candidate commit is not available in the repository")
        elif active.current_candidate_head:
            expected_head = active.current_candidate_head
            try:
                if resolve_commit(repo, expected_head) != observed_head:
                    details.append(
                        f"review candidate HEAD {expected_head[:7]} != current HEAD "
                        f"{observed_head[:7]}"
                    )
            except GitProtocolError:
                details.append("review candidate commit is not available in the repository")

        git_target_details = [
            item
            for item in verify_path_targets_unchanged(repo, active.targets)
            if item.startswith("HEAD changed")
        ]
        details.extend(git_target_details)
    elif observed_head is not None:
        for target in active.targets:
            if target.kind != "git_range":
                continue
            try:
                if resolve_commit(repo, target.head_commit) != observed_head:
                    expected_head = expected_head or target.head_commit
                    details.append(
                        f"Git target HEAD {target.head_commit[:7]} != current HEAD "
                        f"{observed_head[:7]}"
                    )
            except GitProtocolError:
                details.append(f"Git target {target.id} commit is not available in the repository")

    if not details:
        return None
    if expected_head is None:
        expected_head = active.current_candidate_head or active.git_head
    return ActiveReviewEvidenceMismatch(
        reason=STALE_REVIEW_REASON,
        details=tuple(details),
        expected_head=expected_head,
        observed_head=observed_head,
    )
