"""Markdown review artifact rendering."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from auto_loop.lifecycle import ActiveReview
from auto_loop.models import Finding, PlannerResult, ReviewerResult, WorkerResult
from auto_loop.protocol import normalize_commit

ReviewKind = Literal["plan", "batch", "revision", "final"]


def _verdict_label(verdict: str) -> str:
    return verdict.upper()


def _format_findings(findings: list[Finding]) -> str:
    if not findings:
        return "None.\n"
    lines = []
    for finding in findings:
        lines.append(f"- **{finding.id}** — {finding.title}: {finding.detail}")
        lines.append(f"  - Evidence: {finding.evidence}")
        lines.append(f"  - Required change: {finding.required_change}")
    return "\n".join(lines) + "\n"


def _format_verification_worker(result: WorkerResult) -> str:
    if not result.verification:
        return "- (none recorded)\n"
    lines = []
    for item in result.verification:
        note = f" ({item.note})" if item.note else ""
        lines.append(f"- `{item.command}` — {item.result}{note}")
    return "\n".join(lines) + "\n"


def _format_verification_reviewer(items: list[str]) -> str:
    if not items:
        return "- (none recorded)\n"
    return "\n".join(f"- {item}" for item in items) + "\n"


def _format_targets(review: ActiveReview | None, reviewer: ReviewerResult) -> list[str]:
    lines = ["## Targets", ""]
    if review and review.targets:
        for target in review.targets:
            if target.kind == "git_range":
                lines.extend(
                    [
                        f"### {target.id}",
                        "- Kind: Git range",
                        f"- Approved baseline: `{target.base_commit}`",
                        f"- Candidate HEAD: `{target.head_commit}`",
                        f"- Range: `{target.base_commit}..{target.head_commit}`",
                        "",
                    ]
                )
            elif target.kind == "content":
                lines.extend(
                    [
                        f"### {target.id}",
                        "- Kind: content",
                        f"- Fingerprint: `sha256:{target.content_sha256}`",
                        "",
                        "```",
                        target.content,
                        "```",
                        "",
                    ]
                )
            else:
                lines.extend(
                    [
                        f"### {target.id}",
                        "- Kind: path",
                        f"- Path: `{target.path}`",
                        f"- Fingerprint: `sha256:{target.fingerprint}`",
                        f"- Git classification: {target.git_classification}",
                        f"- Exists: {'yes' if target.exists else 'no'}",
                        "",
                    ]
                )
        return lines
    base = normalize_commit(reviewer.reviewed_base_commit)
    head = normalize_commit(reviewer.reviewed_head_commit)
    if base or head:
        lines.extend(
            [
                "### git",
                "- Kind: Git range",
            ]
        )
        if base:
            lines.append(f"- Approved baseline: `{base}`")
        if head:
            lines.append(f"- Candidate HEAD: `{head}`")
        if base and head:
            lines.append(f"- Range: `{base}..{head}`")
        lines.append("")
        return lines
    lines.extend(["(none recorded)", ""])
    return lines


def render_review_markdown(
    *,
    sequence: int,
    title: str,
    kind: ReviewKind,
    worker: WorkerResult | PlannerResult | None,
    reviewer: ReviewerResult,
    created_at: datetime | None = None,
    worker_session_id: str | None = None,
    reviewer_session_id: str | None = None,
    session_purpose: str | None = None,
    active_review: ActiveReview | None = None,
) -> str:
    """Render an append-only review artifact."""
    ts = (created_at or datetime.now(timezone.utc)).isoformat()
    scope = reviewer.scope
    verdict = _verdict_label(reviewer.verdict)

    base = normalize_commit(reviewer.reviewed_base_commit)
    head = normalize_commit(reviewer.reviewed_head_commit)
    cycle = active_review.cycle_id if active_review else None
    round_no = active_review.round if active_review else None
    purpose = session_purpose or (active_review.session_purpose if active_review else None)

    lines = [
        f"# Review {sequence:04d} — {title}",
        "",
        f"- Scope: {scope}",
        f"- Kind: {kind}",
        f"- Verdict: {verdict}",
    ]
    if cycle:
        lines.append(f"- Review cycle: `{cycle}`")
    if round_no is not None:
        lines.append(f"- Round: {round_no}")
    if purpose:
        lines.append(f"- Reviewer session purpose: `{purpose}`")

    if scope == "final":
        lines.append(f"- HEAD: `{head or 'unknown'}`")
        lines.append(f"- Whole task reviewed: {'yes' if reviewer.whole_task_reviewed else 'no'}")
    else:
        if base:
            lines.append(f"- Approved baseline: `{base}`")
        if head:
            lines.append(f"- Candidate HEAD: `{head}`")
        if base and head:
            lines.append(f"- Reviewed range: `{base}..{head}`")

    if worker_session_id:
        if session_purpose == "plan_reviewer":
            lines.append(f"- Planner session: `{worker_session_id}`")
        else:
            lines.append(f"- Worker session: `{worker_session_id}`")
    if reviewer_session_id:
        lines.append(f"- Reviewer session: `{reviewer_session_id}`")
    if reviewer.reviewed_target_ids:
        lines.append(f"- Acknowledged targets: {', '.join(reviewer.reviewed_target_ids)}")
    lines.append(f"- Created at: {ts}")
    lines.append("")
    lines.extend(_format_targets(active_review, reviewer))

    if worker is not None:
        summary = worker.plan_summary if isinstance(worker, PlannerResult) else worker.work_summary
        heading = "Planner summary" if isinstance(worker, PlannerResult) else "Worker summary"
        lines.extend([f"## {heading}", "", summary.strip(), ""])

    lines.extend(["## Reviewer summary", "", reviewer.summary.strip(), ""])
    lines.extend(["## Findings", "", _format_findings(reviewer.findings).rstrip(), ""])

    if isinstance(worker, WorkerResult):
        lines.extend(
            [
                "## Verification (worker)",
                "",
                _format_verification_worker(worker).rstrip(),
                "",
            ]
        )
    lines.extend(
        [
            "## Verification performed",
            "",
            _format_verification_reviewer(reviewer.verification).rstrip(),
            "",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"
