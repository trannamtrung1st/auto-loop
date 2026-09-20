"""Markdown review artifact rendering."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from auto_loop.models import Finding, ReviewerResult, WorkerResult
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


def render_review_markdown(
    *,
    sequence: int,
    title: str,
    kind: ReviewKind,
    worker: WorkerResult | None,
    reviewer: ReviewerResult,
    created_at: datetime | None = None,
    worker_session_id: str | None = None,
    reviewer_session_id: str | None = None,
) -> str:
    """Render an append-only review artifact (proposal section 41)."""
    ts = (created_at or datetime.now(timezone.utc)).isoformat()
    scope = reviewer.scope
    verdict = _verdict_label(reviewer.verdict)

    base = normalize_commit(reviewer.reviewed_base_commit)
    head = normalize_commit(reviewer.reviewed_head_commit)

    lines = [
        f"# Review {sequence:04d} — {title}",
        "",
        f"- Scope: {scope}",
        f"- Kind: {kind}",
        f"- Verdict: {verdict}",
    ]

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
        lines.append(f"- Worker session: `{worker_session_id}`")
    if reviewer_session_id:
        lines.append(f"- Reviewer session: `{reviewer_session_id}`")
    lines.append(f"- Created at: {ts}")
    lines.append("")

    if worker is not None:
        lines.extend(["## Worker summary", "", worker.work_summary.strip(), ""])

    lines.extend(["## Reviewer summary", "", reviewer.summary.strip(), ""])
    lines.extend(["## Findings", "", _format_findings(reviewer.findings).rstrip(), ""])

    if worker is not None:
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
