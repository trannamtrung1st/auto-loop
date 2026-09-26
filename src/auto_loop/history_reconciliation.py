"""Restart-safe remap of an approved commit after an intentional history rewrite.

The controller remaps ``last_approved_commit`` only when exactly one commit
reachable from HEAD has the same product-tree fingerprint as the old approved
commit. That candidate must be an ancestor of HEAD. HEAD is never selected
merely because it is current. Ambiguous or unproven mappings are persisted and
the run stops for an explicit operator decision.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import NoReturn

from auto_loop.config import AutoLoopConfig
from auto_loop.events import append_event, load_events
from auto_loop.exits import ExitCode
from auto_loop.git import (
    GitError,
    GitProtocolError,
    commit_exists,
    head_commit,
    is_ancestor,
    list_commit_shas,
    resolve_commit,
)
from auto_loop.lifecycle import (
    ApprovedTargetEvidence,
    HistoryReconciliation,
    LifecycleState,
    utc_now,
)
from auto_loop.product_state import commit_product_tree_fingerprint, product_excludes
from auto_loop.runtime import save_lifecycle_state

_ANCESTRY_INVARIANT = (
    "Approved baseline is no longer an ancestor of HEAD (history rewrite detected)"
)


class HistoryReconciliationError(Exception):
    """Operator history reconciliation preconditions failed."""

    def __init__(self, message: str, *, exit_code: ExitCode = ExitCode.CONFIG_ERROR) -> None:
        self.exit_code = exit_code
        super().__init__(message)


def reconcile_approved_history(
    repo: Path,
    config: AutoLoopConfig,
    state: LifecycleState,
    *,
    artifact_root: Path | None = None,
) -> LifecycleState:
    """Apply a proven rebase mapping, or stop without moving the approved baseline.

    Safe to call again after a crash. A pending record is finished only when its
    candidate is still the unique product-equivalent ancestor of HEAD.
    """
    old = state.last_approved_commit
    if not old:
        return state
    excludes = product_excludes(config)
    head = head_commit(repo)

    pending = state.history_reconciliation
    if pending is not None and pending.new_sha and not pending.applied and not pending.needs_decision:
        if _pending_still_unique(repo, pending, excludes=excludes, head=head):
            return _finish_apply(
                repo,
                config,
                state,
                pending,
                artifact_root=artifact_root,
            )
        state.history_reconciliation = None
        save_lifecycle_state(repo, state, artifact_root=artifact_root)

    if _approved_is_ancestor(repo, old, head):
        if state.history_reconciliation is not None and state.history_reconciliation.needs_decision:
            state.history_reconciliation = None
            state.updated_at = utc_now()
            save_lifecycle_state(repo, state, artifact_root=artifact_root)
        return state

    if not commit_exists(repo, old):
        _stop_for_decision(
            repo,
            state,
            old_sha=old,
            head_sha=head,
            candidates=[],
            evidence=["missing_object"],
            details=(
                f"Approved commit {old} is not in the repository, so an equivalent "
                "rebased commit cannot be proven.",
                "Approved baseline was not changed and was not moved to HEAD.",
                "An explicit operator decision is required.",
            ),
            artifact_root=artifact_root,
        )

    target = commit_product_tree_fingerprint(repo, old, excludes=excludes)
    matches = _matching_commits(repo, head, target, excludes)
    if len(matches) == 1:
        candidate = matches[0]
        evidence = _evidence(repo, old, candidate, target)
        record = HistoryReconciliation(
            old_sha=old,
            new_sha=candidate,
            head_sha=head,
            evidence=evidence,
            candidates=[candidate],
            applied=False,
            needs_decision=False,
        )
        state.history_reconciliation = record
        state.updated_at = utc_now()
        save_lifecycle_state(repo, state, artifact_root=artifact_root)
        return _finish_apply(
            repo,
            config,
            state,
            record,
            artifact_root=artifact_root,
        )

    if not matches:
        details = (
            f"Approved commit {old} is no longer an ancestor of HEAD.",
            "No commit reachable from HEAD has the same product tree.",
            "Approved baseline was not changed and was not moved to HEAD.",
            "An explicit operator decision is required.",
        )
        reason = "no_equivalent"
    else:
        candidate_lines = tuple(f"  {sha}" for sha in matches)
        details = (
            f"Approved commit {old} is no longer an ancestor of HEAD.",
            f"Product-tree mapping is ambiguous ({len(matches)} equivalent commits).",
            "Candidates:",
            *candidate_lines,
            "Approved baseline was not changed and was not moved to HEAD.",
            "An explicit operator decision is required.",
        )
        reason = "ambiguous"
    _stop_for_decision(
        repo,
        state,
        old_sha=old,
        head_sha=head,
        candidates=matches,
        evidence=_decision_evidence(repo, old, reason, excludes=excludes),
        details=details,
        artifact_root=artifact_root,
    )


def apply_operator_history_reconciliation(
    repo: Path,
    config: AutoLoopConfig,
    state: LifecycleState,
    approved_ref: str,
    *,
    artifact_root: Path | None = None,
) -> LifecycleState:
    """Apply an operator-chosen approved baseline against a pending reconciliation record."""
    record = state.history_reconciliation
    if record is None or not record.needs_decision or record.applied:
        raise HistoryReconciliationError(
            "No history reconciliation is waiting for an operator decision. "
            "Run auto-loop status first."
        )
    old = record.old_sha
    if state.last_approved_commit != old:
        raise HistoryReconciliationError(
            "Lifecycle trust state does not match the persisted reconciliation record "
            f"(last_approved_commit={state.last_approved_commit!r}, old_sha={old!r})."
        )
    candidate = resolve_commit(repo, approved_ref)
    head = head_commit(repo)
    if not is_ancestor(repo, candidate, head):
        raise GitProtocolError(
            "Operator-approved commit is not an ancestor of current HEAD",
            commits={"Approved candidate": candidate, "HEAD": head},
        )
    excludes = product_excludes(config)
    if record.candidates:
        allowed = {resolve_commit(repo, item) for item in record.candidates}
        if candidate not in allowed:
            short = ", ".join(sorted({sha[:7] for sha in allowed}))
            raise HistoryReconciliationError(
                f"Approved candidate {candidate[:7]} is not one of the persisted "
                f"equivalent commits ({short})."
            )
    else:
        _assert_operator_product_equivalence(repo, old, candidate, record, excludes=excludes)
    fingerprint = commit_product_tree_fingerprint(repo, candidate, excludes=excludes)
    evidence = list(record.evidence)
    if "operator_approved" not in evidence:
        evidence.append("operator_approved")
    if f"fingerprint:{fingerprint}" not in evidence:
        evidence.append(f"fingerprint:{fingerprint}")
    evidence.append("ancestor_of_head")
    pending = HistoryReconciliation(
        old_sha=old,
        new_sha=candidate,
        head_sha=head,
        evidence=evidence,
        candidates=record.candidates or [candidate],
        applied=False,
        needs_decision=False,
    )
    return _finish_apply(repo, config, state, pending, artifact_root=artifact_root)


def _approved_is_ancestor(repo: Path, approved: str, head: str) -> bool:
    if not commit_exists(repo, approved):
        return False
    return is_ancestor(repo, approved, head)


def _pending_still_unique(
    repo: Path,
    pending: HistoryReconciliation,
    *,
    excludes: tuple[str, ...],
    head: str,
) -> bool:
    candidate = pending.new_sha
    if candidate is None or not commit_exists(repo, pending.old_sha):
        return False
    if not commit_exists(repo, candidate) or not is_ancestor(repo, candidate, head):
        return False
    try:
        old_fp = commit_product_tree_fingerprint(repo, pending.old_sha, excludes=excludes)
        new_fp = commit_product_tree_fingerprint(repo, candidate, excludes=excludes)
    except GitProtocolError:
        return False
    if old_fp != new_fp:
        return False
    return _matching_commits(repo, head, old_fp, excludes) == [candidate]


def _matching_commits(
    repo: Path,
    head: str,
    target: str,
    excludes: tuple[str, ...],
) -> list[str]:
    cache: dict[str, str] = {}
    matches: list[str] = []
    for sha in list_commit_shas(repo, head):
        tree = _git_text(repo, "rev-parse", f"{sha}^{{tree}}")
        fingerprint = cache.get(tree)
        if fingerprint is None:
            fingerprint = commit_product_tree_fingerprint(repo, sha, excludes=excludes)
            cache[tree] = fingerprint
        if fingerprint == target:
            matches.append(sha)
    return matches


def _decision_evidence(
    repo: Path,
    old_sha: str,
    reason: str,
    *,
    excludes: tuple[str, ...],
) -> list[str]:
    evidence = [reason, "product_tree_fingerprint"]
    if commit_exists(repo, old_sha):
        fingerprint = commit_product_tree_fingerprint(repo, old_sha, excludes=excludes)
        evidence.append(f"fingerprint:{fingerprint}")
    return evidence


def _fingerprint_from_evidence(evidence: list[str]) -> str | None:
    for item in evidence:
        if item.startswith("fingerprint:"):
            return item.split(":", 1)[1]
    return None


def _assert_operator_product_equivalence(
    repo: Path,
    old_sha: str,
    candidate: str,
    record: HistoryReconciliation,
    *,
    excludes: tuple[str, ...],
) -> None:
    candidate_fp = commit_product_tree_fingerprint(repo, candidate, excludes=excludes)
    if commit_exists(repo, old_sha):
        old_fp = commit_product_tree_fingerprint(repo, old_sha, excludes=excludes)
        if old_fp != candidate_fp:
            raise HistoryReconciliationError(
                "Operator-approved commit does not match the old approved product tree.",
                exit_code=ExitCode.GIT_PROTOCOL_ERROR,
            )
        return
    stored = _fingerprint_from_evidence(record.evidence)
    if stored is None:
        raise HistoryReconciliationError(
            "Cannot verify product equivalence without persisted candidates or a "
            "stored fingerprint. Restore the old commit object or re-run detection."
        )
    if stored != candidate_fp:
        raise HistoryReconciliationError(
            "Operator-approved commit does not match the stored product-tree fingerprint.",
            exit_code=ExitCode.GIT_PROTOCOL_ERROR,
        )


def _evidence(repo: Path, old: str, candidate: str, fingerprint: str) -> list[str]:
    evidence = [
        "product_tree_fingerprint",
        f"fingerprint:{fingerprint}",
        "ancestor_of_head",
    ]
    old_patch = _patch_id(repo, old)
    new_patch = _patch_id(repo, candidate)
    if old_patch and new_patch and old_patch == new_patch:
        evidence.append(f"patch_id:{old_patch}")
    else:
        evidence.append("patch_id:unavailable")
    return evidence


def _finish_apply(
    repo: Path,
    config: AutoLoopConfig,
    state: LifecycleState,
    pending: HistoryReconciliation,
    *,
    artifact_root: Path | None,
) -> LifecycleState:
    new_sha = pending.new_sha
    if new_sha is None:
        raise GitProtocolError("History reconciliation is missing a candidate commit")
    _retarget_approved_sha(state, pending.old_sha, new_sha)
    state.history_reconciliation = pending
    state.updated_at = utc_now()
    save_lifecycle_state(repo, state, artifact_root=artifact_root)
    _emit_event_once(repo, config, state, pending)
    pending.applied = True
    pending.needs_decision = False
    state.history_reconciliation = pending
    state.updated_at = utc_now()
    save_lifecycle_state(repo, state, artifact_root=artifact_root)
    return state


def _stop_for_decision(
    repo: Path,
    state: LifecycleState,
    *,
    old_sha: str,
    head_sha: str,
    candidates: list[str],
    evidence: list[str],
    details: tuple[str, ...],
    artifact_root: Path | None,
) -> NoReturn:
    state.history_reconciliation = HistoryReconciliation(
        old_sha=old_sha,
        new_sha=None,
        head_sha=head_sha,
        evidence=evidence,
        candidates=list(candidates),
        applied=False,
        needs_decision=True,
    )
    state.updated_at = utc_now()
    save_lifecycle_state(repo, state, artifact_root=artifact_root)
    raise GitProtocolError(
        _ANCESTRY_INVARIANT,
        commits={"Approved": old_sha, "HEAD": head_sha},
        details=details,
    )


def _retarget_approved_sha(state: LifecycleState, old: str, new: str) -> None:
    """Point trust state that named ``old`` at the proven equivalent commit."""
    if state.last_approved_commit == old:
        state.last_approved_commit = new
    if state.initial_base_commit == old:
        state.initial_base_commit = new
    for item in state.approved_evidence:
        _retarget_evidence(item, old, new)
    review = state.active_review
    if review is not None:
        review.approved_base_commit = _replace(review.approved_base_commit, old, new)
        review.production_head_commit = _replace(review.production_head_commit, old, new)
        review.current_candidate_head = _replace(review.current_candidate_head, old, new)
        for target in review.targets:
            if target.kind != "git_range":
                continue
            target.base_commit = _replace(target.base_commit, old, new) or target.base_commit
            target.head_commit = _replace(target.head_commit, old, new) or target.head_commit
    pending = state.pending_revision
    if pending is not None:
        pending.base_commit = _replace(pending.base_commit, old, new)
        pending.production_head_commit = _replace(pending.production_head_commit, old, new)
        pending.last_reviewed_head_commit = _replace(pending.last_reviewed_head_commit, old, new)


def _retarget_evidence(item: ApprovedTargetEvidence, old: str, new: str) -> None:
    item.git_base = _replace(item.git_base, old, new)
    item.git_head = _replace(item.git_head, old, new)
    item.fingerprint = _replace(item.fingerprint, old, new)


def _replace(value: str | None, old: str, new: str) -> str | None:
    if value == old:
        return new
    return value


def _emit_event_once(
    repo: Path,
    config: AutoLoopConfig,
    state: LifecycleState,
    pending: HistoryReconciliation,
) -> None:
    new_sha = pending.new_sha
    if new_sha is None:
        return
    for event in load_events(repo, config):
        if event.get("type") != "history_reconciliation":
            continue
        if (
            event.get("lifecycle_id") == state.lifecycle_id
            and event.get("old_sha") == pending.old_sha
            and event.get("new_sha") == new_sha
        ):
            return
    append_event(
        repo,
        config,
        {
            "type": "history_reconciliation",
            "lifecycle_id": state.lifecycle_id,
            "old_sha": pending.old_sha,
            "new_sha": new_sha,
            "head": pending.head_sha,
            "evidence": list(pending.evidence),
        },
    )


def _git_text(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        raise GitError(stderr or f"git {' '.join(args)} failed")
    return (result.stdout or "").strip()


def _patch_id(repo: Path, commit: str) -> str | None:
    """Stable patch-id of ``commit`` against its first parent, when one exists."""
    try:
        listed = _git_text(repo, "rev-list", "--parents", "-n", "1", commit)
    except GitError:
        return None
    parts = listed.split()
    if len(parts) < 2:
        return None
    parent = parts[1]
    diff = subprocess.run(
        ["git", "diff-tree", "-p", parent, commit],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if diff.returncode != 0 or not diff.stdout.strip():
        return None
    identified = subprocess.run(
        ["git", "patch-id", "--stable"],
        cwd=repo,
        input=diff.stdout,
        capture_output=True,
        check=False,
    )
    if identified.returncode != 0:
        return None
    text = (identified.stdout or b"").decode("utf-8", errors="replace").strip()
    if not text:
        return None
    return text.split()[0]
