"""Review-target path normalization, fingerprints, and Git classification."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from auto_loop.git import GitProtocolError, ReviewRange, head_commit, normalize_batch_range
from auto_loop.models import (
    ActiveGitTarget,
    ActivePathTarget,
    ActiveReviewTarget,
    PathGitClassification,
    PathTargetRequest,
    ReviewRequest,
    ReviewScope,
)
from auto_loop.product_state import is_control_path


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def fingerprint_path(path: Path) -> tuple[str, bool]:
    """Return (fingerprint, exists) for a file or directory. Missing paths hash empty bytes."""
    if not path.exists():
        return sha256_bytes(b""), False
    if path.is_file():
        return sha256_file(path), True
    lines: list[str] = []
    for child in sorted(path.rglob("*")):
        rel = child.relative_to(path).as_posix()
        if child.is_dir():
            lines.append(f"{rel}\tdir\t")
        elif child.is_file():
            lines.append(f"{rel}\tfile\t{sha256_file(child)}")
    return sha256_bytes("\n".join(lines).encode("utf-8")), True


def resolve_review_path(repo: Path, rel: str) -> Path:
    repo_root = repo.resolve()
    candidate = (repo_root / rel).resolve()
    try:
        candidate.relative_to(repo_root)
    except ValueError as exc:
        raise GitProtocolError(f"Review path escapes workspace: {rel}") from exc
    return candidate


def posix_relpath(repo: Path, path: Path) -> str:
    return path.resolve().relative_to(repo.resolve()).as_posix()


def classify_path(repo: Path, rel: str) -> PathGitClassification:
    if is_control_path(rel):
        return "control"
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", rel],
        cwd=repo,
        capture_output=True,
    )
    if ignored.returncode == 0:
        return "ignored"
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", rel],
        cwd=repo,
        capture_output=True,
    )
    if tracked.returncode == 0:
        return "tracked"
    return "untracked"


def path_target_permitted(scope: ReviewScope, classification: PathGitClassification, rel: str) -> bool:
    if classification == "ignored":
        return True
    if classification == "control":
        return scope == "plan" and rel.endswith("plan.md")
    if classification in ("tracked", "untracked"):
        # Allowed only when the product tree is otherwise clean (committed tracked files).
        # Untracked non-ignored product files are blocked by the clean-tree check.
        return classification == "tracked"
    return False


def normalize_path_target(
    repo: Path,
    request: PathTargetRequest,
    *,
    scope: ReviewScope,
) -> ActivePathTarget:
    if not request.id.strip():
        raise GitProtocolError("Path review target id must be non-empty")
    resolved = resolve_review_path(repo, request.path)
    try:
        rel = posix_relpath(repo, resolved)
    except ValueError as exc:
        raise GitProtocolError(f"Review path escapes workspace: {request.path}") from exc
    classification = classify_path(repo, rel)
    if classification in ("untracked",) and not path_target_permitted(scope, classification, rel):
        raise GitProtocolError(
            f"Path target {rel} is untracked non-ignored product state; commit it or use an ignored artifact"
        )
    if classification == "control" and not path_target_permitted(scope, classification, rel):
        raise GitProtocolError(f"Protected control path is not a valid review target: {rel}")
    digest, exists = fingerprint_path(resolved)
    return ActivePathTarget(
        id=request.id,
        path=rel,
        fingerprint=digest,
        exists=exists,
        git_classification=classification,
        purpose=request.purpose,
    )


def plan_path_target(repo: Path, plan_rel: str) -> ActivePathTarget:
    request = PathTargetRequest(id="plan", path=plan_rel, purpose="Current plan document")
    return normalize_path_target(repo, request, scope="plan")


def normalize_work_targets(
    repo: Path,
    *,
    last_approved_commit: str,
    request: ReviewRequest,
    excludes: tuple[str, ...] | None = None,
) -> tuple[list[ActiveReviewTarget], tuple[str, ...]]:
    path_requests = [t for t in request.targets if t.kind == "path"]
    seen_ids: set[str] = set()
    active: list[ActiveReviewTarget] = []
    for item in path_requests:
        if item.id in seen_ids or item.id == "git":
            raise GitProtocolError(f"Duplicate or reserved review target id: {item.id}")
        seen_ids.add(item.id)
        active.append(normalize_path_target(repo, item, scope=request.scope))

    allow_empty = bool(active)
    normalized = normalize_batch_range(
        repo,
        last_approved_commit=last_approved_commit,
        worker_base_commit=request.base_commit,
        worker_head_commit=request.head_commit,
        allow_empty=allow_empty,
        excludes=excludes,
    )
    if normalized.range.base != normalized.range.head:
        active.insert(
            0,
            ActiveGitTarget(
                base_commit=normalized.range.base,
                head_commit=normalized.range.head,
            ),
        )
    elif not active:
        raise GitProtocolError("Batch review range is empty (base equals HEAD)")
    return active, normalized.warnings


def current_head_range(last_approved: str, head: str) -> ReviewRange:
    return ReviewRange(base=last_approved, head=head)


def snapshot_path_fingerprints(targets: list[ActiveReviewTarget]) -> dict[str, str]:
    return {
        target.id: target.fingerprint
        for target in targets
        if target.kind == "path"
    }


def verify_path_targets_unchanged(repo: Path, targets: list[ActiveReviewTarget]) -> list[str]:
    details: list[str] = []
    current_head = head_commit(repo)
    for target in targets:
        if target.kind == "git_range":
            if current_head != target.head_commit:
                details.append(
                    f"HEAD changed from {target.head_commit[:7]} to {current_head[:7]}"
                )
            continue
        resolved = resolve_review_path(repo, target.path)
        digest, exists = fingerprint_path(resolved)
        if exists != target.exists or digest != target.fingerprint:
            details.append(f"path target {target.id} ({target.path}) changed during review")
    return details
