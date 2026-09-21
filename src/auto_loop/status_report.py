"""Human-readable lifecycle status (proposal section 35)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from auto_loop.config import AutoLoopConfig, load_config_from_repo
from auto_loop.git import head_commit
from auto_loop.product_state import is_product_tree_clean
from auto_loop.runtime import load_lifecycle_state
from auto_loop.terminal_records import load_completion_record


def _redact_session_id(session_id: str | None) -> str:
    if not session_id:
        return "(not created)"
    if len(session_id) <= 12:
        return session_id
    return f"{session_id[:4]}...{session_id[-4:]}"


def _format_duration(started_at: datetime, now: datetime) -> str:
    delta = now - started_at.astimezone(timezone.utc)
    total = int(delta.total_seconds())
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _format_ago(updated_at: datetime, now: datetime) -> str:
    delta = now - updated_at.astimezone(timezone.utc)
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    return f"{minutes}m ago"


def _latest_review_summary(repo: Path, config: AutoLoopConfig) -> str:
    reviews = sorted((repo / config.reviews_dir).glob("*.md"))
    if not reviews:
        return "(none)"
    text = reviews[-1].read_text(encoding="utf-8")
    verdict = "unknown"
    scope = "unknown"
    for line in text.splitlines():
        if line.startswith("- Verdict:"):
            verdict = line.split(":", 1)[1].strip()
        if line.startswith("- Scope:"):
            scope = line.split(":", 1)[1].strip()
    slug = reviews[-1].stem
    return f"{verdict} {slug} ({scope})"


def build_status_report(repo: Path, *, now: datetime | None = None) -> str:
    config = load_config_from_repo(repo)
    now = now or datetime.now(timezone.utc)
    completion = load_completion_record(repo)
    if completion is not None:
        state = load_lifecycle_state(repo)
        lines = [
            "status: completed",
            f"lifecycle: {completion.lifecycle_id}",
            f"final commit: {completion.final_commit[:7]}",
            f"completed at: {completion.completed_at.isoformat()}",
        ]
        if state is not None:
            lines.append(f"turn: {state.turn}")
        lines.append(f"latest review: {_latest_review_summary(repo, config)}")
        return "\n".join(lines)

    state = load_lifecycle_state(repo)
    if state is None:
        return "status: idle\nlifecycle: (none)\nRun `auto-loop run` to start a lifecycle."

    lines = [
        f"status: {state.status.value}",
        f"lifecycle: {state.lifecycle_id}",
        f"phase: {state.phase}",
        f"turn: {state.turn}",
        f"next session: {state.next_session}",
        "",
        f"plan approved: {'yes' if state.plan_approved else 'no'}",
        "",
        f"planner session: {_redact_session_id(state.sessions['planner'].session_id)} "
        f"({state.sessions['planner'].status}, model={state.sessions['planner'].model})",
        f"plan-reviewer session: {_redact_session_id(state.sessions['plan_reviewer'].session_id)} "
        f"({state.sessions['plan_reviewer'].status}, model={state.sessions['plan_reviewer'].model})",
        f"worker session: {_redact_session_id(state.sessions['worker'].session_id)} "
        f"({state.sessions['worker'].status}, model={state.sessions['worker'].model})",
        f"reviewer session: {_redact_session_id(state.sessions['reviewer'].session_id)} "
        f"({state.sessions['reviewer'].status}, model={state.sessions['reviewer'].model})",
        "",
        f"initial base: {state.initial_base_commit[:7]}",
        f"last approved: {state.last_approved_commit[:7]}",
        f"HEAD: {head_commit(repo)[:7]}",
        f"product tree: {'clean' if is_product_tree_clean(repo) else 'dirty'}",
        "",
        f"latest review: {_latest_review_summary(repo, config)}",
        f"elapsed: {_format_duration(state.started_at, now)}",
        f"last activity: {_format_ago(state.updated_at, now)}",
    ]
    if state.active_review is not None:
        review = state.active_review
        lines.append(
            f"active review: {review.scope}/{review.target} cycle {review.cycle_id} round {review.round}"
        )
    if state.pending_revision is not None:
        pending = state.pending_revision
        lines.append(
            f"pending revision: {pending.scope}/{pending.target} cycle {pending.cycle_id} round {pending.round}"
        )
    if state.inflight is not None:
        lines.append(
            f"inflight: {state.inflight.session_slot} turn {state.inflight.turn} "
            f"since {state.inflight.started_at.isoformat()}"
        )
    return "\n".join(lines)
