"""Review-target path normalization, fingerprints, and Git classification."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from auto_loop.config import GitMode
from auto_loop.git import (
    GitProtocolError,
    ReviewRequestError,
    ReviewRange,
    head_commit,
    is_git_repository,
    normalize_batch_range,
)
from auto_loop.models import (
    ActiveContentTarget,
    ActiveGitTarget,
    ActivePathTarget,
    ActiveReviewTarget,
    ContentTargetRequest,
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


def classify_path(
    repo: Path,
    rel: str,
    *,
    git_available: bool = True,
) -> PathGitClassification:
    if is_control_path(rel):
        return "control"
    if not git_available:
        return "none"
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


def path_target_permitted(
    scope: ReviewScope,
    classification: PathGitClassification,
    rel: str,
    *,
    git_mode: GitMode = "required",
) -> bool:
    if classification in ("ignored", "none"):
        return True
    if classification == "control":
        return scope == "plan" and rel.endswith("plan.md")
    if classification == "tracked":
        return True
    if classification == "untracked":
        return git_mode != "required"
    return False


def normalize_content_target(request: ContentTargetRequest) -> ActiveContentTarget:
    if not request.id.strip():
        raise ReviewRequestError("Content review target id must be non-empty")
    if request.id == "git":
        raise ReviewRequestError("Duplicate or reserved review target id: git")
    digest = sha256_bytes(request.content.encode("utf-8"))
    return ActiveContentTarget(
        id=request.id,
        purpose=request.purpose,
        content=request.content,
        content_sha256=digest,
    )


def _protected_control_target_error(rel: str, scope: ReviewScope) -> ReviewRequestError:
    scope_label = scope if scope in ("batch", "final", "plan") else "review"
    lines = [
        f"Protected control path is not a valid {scope_label} review target: {rel}.",
        "",
    ]
    if scope in ("batch", "final") and rel.endswith("plan.md"):
        lines.extend(
            [
                "Do not include plan.md in batch/final review.targets.",
                "Plan changes are tracked automatically by plan_sha256.",
                "Use scope=plan only when the plan itself is the subject of review.",
            ]
        )
    else:
        lines.append(
            "Auto Loop control files are not batch/final path targets. "
            "Use scope=plan when the plan itself is the subject of review."
        )
    lines.extend(
        [
            "Emit a corrected AUTO_LOOP_RESULT without redoing completed work.",
        ]
    )
    return ReviewRequestError("\n".join(lines))


def normalize_path_target(
    repo: Path,
    request: PathTargetRequest,
    *,
    scope: ReviewScope,
    git_mode: GitMode = "required",
) -> ActivePathTarget:
    if not request.id.strip():
        raise ReviewRequestError("Path review target id must be non-empty")
    resolved = resolve_review_path(repo, request.path)
    try:
        rel = posix_relpath(repo, resolved)
    except ValueError as exc:
        raise GitProtocolError(f"Review path escapes workspace: {request.path}") from exc
    git_available = git_mode != "off" and is_git_repository(repo)
    classification = classify_path(repo, rel, git_available=git_available)
    if classification == "untracked" and not path_target_permitted(
        scope, classification, rel, git_mode=git_mode
    ):
        raise ReviewRequestError(
            f"Path target {rel} is untracked non-ignored product state; "
            "commit it or use an ignored artifact"
        )
    if classification == "control" and not path_target_permitted(
        scope, classification, rel, git_mode=git_mode
    ):
        raise _protected_control_target_error(rel, scope)
    digest, exists = fingerprint_path(resolved)
    return ActivePathTarget(
        id=request.id,
        path=rel,
        fingerprint=digest,
        exists=exists,
        git_classification=classification,
        purpose=request.purpose,
    )


def plan_path_target(
    repo: Path,
    plan_rel: str,
    *,
    git_mode: GitMode = "required",
) -> ActivePathTarget:
    request = PathTargetRequest(id="plan", path=plan_rel, purpose="Current plan document")
    return normalize_path_target(repo, request, scope="plan", git_mode=git_mode)


def _explicit_targets(
    repo: Path,
    request: ReviewRequest,
    *,
    git_mode: GitMode,
) -> list[ActiveReviewTarget]:
    seen_ids: set[str] = set()
    active: list[ActiveReviewTarget] = []
    for item in request.targets:
        if item.kind == "git_range":
            continue
        if item.id in seen_ids or item.id == "git":
            raise ReviewRequestError(f"Duplicate or reserved review target id: {item.id}")
        seen_ids.add(item.id)
        if item.kind == "path":
            active.append(normalize_path_target(repo, item, scope=request.scope, git_mode=git_mode))
        else:
            active.append(normalize_content_target(item))
    return active


def normalize_work_targets(
    repo: Path,
    *,
    last_approved_commit: str | None,
    request: ReviewRequest,
    excludes: tuple[str, ...] | None = None,
    git_mode: GitMode = "required",
    protect_history: bool = True,
    allow_empty: bool = False,
) -> tuple[list[ActiveReviewTarget], tuple[str, ...]]:
    """Normalize explicit targets and, when Git evidence exists, a commit range.

    An empty Git range is not an error when path or content targets exist, or when
    ``allow_empty`` is set for a final review that attests the whole task.
    """
    active = _explicit_targets(repo, request, git_mode=git_mode)
    warnings: tuple[str, ...] = ()
    git_available = git_mode != "off" and is_git_repository(repo)
    if git_mode == "required" and not git_available:
        raise GitProtocolError("git.mode=required requires a Git repository")
    if git_mode == "required" and not last_approved_commit:
        raise GitProtocolError("git.mode=required requires an approved commit baseline")
    if git_available and last_approved_commit:
        normalized = normalize_batch_range(
            repo,
            last_approved_commit=last_approved_commit,
            worker_base_commit=request.base_commit,
            worker_head_commit=request.head_commit,
            allow_empty=True,
            excludes=excludes,
            require_clean=git_mode == "required",
            protect_history=protect_history,
        )
        warnings = normalized.warnings
        if normalized.range.base != normalized.range.head:
            active.insert(
                0,
                ActiveGitTarget(
                    base_commit=normalized.range.base,
                    head_commit=normalized.range.head,
                ),
            )
    if not active and not allow_empty:
        raise ReviewRequestError(
            "Review request contains no reviewable evidence "
            "(empty Git range and no path or content targets)"
        )
    return active, warnings


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
    git_targets = [target for target in targets if target.kind == "git_range"]
    current_head = head_commit(repo) if git_targets else None
    for target in targets:
        if target.kind == "git_range":
            if current_head != target.head_commit:
                observed = current_head[:7] if current_head else "missing"
                details.append(
                    f"HEAD changed from {target.head_commit[:7]} to {observed}"
                )
            continue
        if target.kind == "content":
            continue
        resolved = resolve_review_path(repo, target.path)
        digest, exists = fingerprint_path(resolved)
        if exists != target.exists or digest != target.fingerprint:
            details.append(f"path target {target.id} ({target.path}) changed during review")
    return details
