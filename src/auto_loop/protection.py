"""Protected control files and reviewer product mutation checks."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from auto_loop.config import AutoLoopConfig
from auto_loop.exits import ExitCode
from auto_loop.git import head_commit
from auto_loop.models import ActiveReviewTarget
from auto_loop.product_state import DEFAULT_PRODUCT_EXCLUDES, list_product_changes, product_excludes
from auto_loop.review_targets import verify_path_targets_unchanged, sha256_file


class ProtectionViolationError(Exception):
    """Protected control input was modified during a turn."""

    exit_code = ExitCode.PROTECTION_VIOLATION

    def __init__(self, violations: list[ProtectedFileViolation]) -> None:
        self.violations = violations
        paths = ", ".join(v.path for v in violations)
        super().__init__(f"Protected file mutation detected: {paths}")


class ReviewMutationError(Exception):
    """Reviewer activity changed product repository state."""

    exit_code = ExitCode.REVIEW_MUTATION_ERROR

    def __init__(self, details: list[str]) -> None:
        self.details = details
        super().__init__(f"Reviewer product mutation detected: {'; '.join(details)}")


@dataclass(frozen=True)
class ProtectedFileViolation:
    path: str
    expected_sha256: str | None
    actual_sha256: str | None
    reason: str


@dataclass(frozen=True)
class ProtectedBaseline:
    paths: dict[str, str]


@dataclass(frozen=True)
class ProductFingerprint:
    head: str
    changes: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ReviewSnapshot:
    product: ProductFingerprint
    plan_sha256: str | None
    path_target_ids: tuple[str, ...]


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capture_protected_baseline(repo: Path, config: AutoLoopConfig) -> ProtectedBaseline:
    paths: dict[str, str] = {}
    for rel in config.protection.protected_files:
        path = repo / rel
        if path.is_file():
            paths[rel] = _sha256_file(path)
    return ProtectedBaseline(paths=paths)


def verify_protected_baseline(
    repo: Path,
    config: AutoLoopConfig,
    baseline: ProtectedBaseline,
) -> list[ProtectedFileViolation]:
    violations: list[ProtectedFileViolation] = []
    for rel in config.protection.protected_files:
        path = repo / rel
        expected = baseline.paths.get(rel)
        if not path.is_file():
            if expected is not None:
                violations.append(
                    ProtectedFileViolation(
                        path=rel,
                        expected_sha256=expected,
                        actual_sha256=None,
                        reason="protected file removed",
                    )
                )
            continue
        actual = _sha256_file(path)
        if expected is None:
            violations.append(
                ProtectedFileViolation(
                    path=rel,
                    expected_sha256=None,
                    actual_sha256=actual,
                    reason="unexpected protected file appeared",
                )
            )
        elif actual != expected:
            violations.append(
                ProtectedFileViolation(
                    path=rel,
                    expected_sha256=expected,
                    actual_sha256=actual,
                    reason="protected file content changed",
                )
            )
    return violations


def assert_protected_unchanged(
    repo: Path,
    config: AutoLoopConfig,
    baseline: ProtectedBaseline,
) -> None:
    violations = verify_protected_baseline(repo, config, baseline)
    if violations:
        raise ProtectionViolationError(violations)


def capture_product_fingerprint(
    repo: Path,
    *,
    excludes: tuple[str, ...] | None = None,
    config: AutoLoopConfig | None = None,
) -> ProductFingerprint:
    from auto_loop.git_policy import git_usable

    if excludes is None:
        excludes = product_excludes(config) if config is not None else DEFAULT_PRODUCT_EXCLUDES
    if config is not None and not git_usable(repo, config):
        return ProductFingerprint(head="", changes=())
    if config is None:
        from auto_loop.git import is_git_repository

        if not is_git_repository(repo):
            return ProductFingerprint(head="", changes=())
    changes = list_product_changes(repo, excludes=excludes)
    return ProductFingerprint(
        head=head_commit(repo),
        changes=tuple(sorted((change.path, change.status) for change in changes)),
    )


def diff_product_fingerprints(before: ProductFingerprint, after: ProductFingerprint) -> list[str]:
    details: list[str] = []
    if before.head != after.head:
        details.append(f"HEAD changed from {before.head[:7]} to {after.head[:7]}")
    if before.changes != after.changes:
        before_map = dict(before.changes)
        after_map = dict(after.changes)
        for path in sorted(set(before_map) | set(after_map)):
            if before_map.get(path) != after_map.get(path):
                details.append(
                    f"product path {path}: {before_map.get(path)!r} -> {after_map.get(path)!r}"
                )
    return details


def assert_reviewer_product_unchanged(before: ProductFingerprint, after: ProductFingerprint) -> None:
    details = diff_product_fingerprints(before, after)
    if details:
        raise ReviewMutationError(details)


def capture_review_snapshot(
    repo: Path,
    *,
    plan_path: Path,
    targets: list[ActiveReviewTarget] | None = None,
    excludes: tuple[str, ...] | None = None,
    config: AutoLoopConfig | None = None,
) -> ReviewSnapshot:
    plan_hash = sha256_file(plan_path) if plan_path.is_file() else None
    return ReviewSnapshot(
        product=capture_product_fingerprint(repo, excludes=excludes, config=config),
        plan_sha256=plan_hash,
        path_target_ids=tuple(t.id for t in (targets or []) if t.kind == "path"),
    )


def assert_review_snapshot_unchanged(
    repo: Path,
    *,
    plan_path: Path,
    before: ReviewSnapshot,
    targets: list[ActiveReviewTarget] | None = None,
    excludes: tuple[str, ...] | None = None,
    config: AutoLoopConfig | None = None,
) -> None:
    after_product = capture_product_fingerprint(repo, excludes=excludes, config=config)
    details = diff_product_fingerprints(before.product, after_product)
    after_plan = sha256_file(plan_path) if plan_path.is_file() else None
    if before.plan_sha256 != after_plan:
        details.append("plan.md changed during review")
    if targets:
        details.extend(verify_path_targets_unchanged(repo, targets))
    if details:
        raise ReviewMutationError(details)


def format_protected_violations(violations: list[ProtectedFileViolation]) -> str:
    lines = []
    for item in violations:
        lines.append(f"{item.path}: {item.reason}")
    return "\n".join(lines)
