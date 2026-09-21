"""Human-readable lifecycle status for a run located by its manifest."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from auto_loop.config import AutoLoopConfig, load_resolved_config_optional
from auto_loop.git import head_commit
from auto_loop.manifest import RunManifestSource
from auto_loop.paths import workspace_relative
from auto_loop.product_state import is_product_tree_clean, product_excludes
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


def _session_label(status: str, session_id: str | None) -> str:
    if status == "retired":
        return "complete"
    if session_id:
        return status
    if status == "pending":
        return "waiting"
    return status


def _phase_label(state) -> str:
    if state.next_session in ("reviewer", "plan_reviewer") or state.active_review is not None:
        return "review"
    return state.phase


def _run_label(status: str) -> str:
    mapping = {
        "running": "active",
        "completed": "complete",
        "stopped": "interrupted",
        "blocked": "blocked",
        "limit_reached": "limit reached",
        "error": "error",
    }
    return mapping.get(status, status)


def _config_label(source: RunManifestSource) -> str:
    rel = workspace_relative(source.workspace, source.path)
    return rel if rel is not None else str(source.path)


def build_status_report(source: RunManifestSource, *, now: datetime | None = None) -> str:
    repo = source.workspace
    artifact_root = source.artifact_root
    frozen = load_resolved_config_optional(artifact_root)
    config = frozen or source.config
    now = now or datetime.now(timezone.utc)
    completion = load_completion_record(repo, artifact_root)
    plan_rel = config.plan_file
    reviews_rel = config.reviews_dir
    config_rel = _config_label(source)
    excludes = product_excludes(config)

    if completion is not None:
        state = load_lifecycle_state(repo, artifact_root)
        lines = [
            "Run:        complete",
            f"Config:     {config_rel}",
            f"Plan:       {plan_rel}",
            f"Reviews:    {reviews_rel}/" if not reviews_rel.endswith("/") else f"Reviews:    {reviews_rel}",
            "",
            "status: completed",
            f"lifecycle: {completion.lifecycle_id}",
            f"final commit: {completion.final_commit[:7]}",
            f"completed at: {completion.completed_at.isoformat()}",
        ]
        if state is not None:
            lines.append(f"turn: {state.turn}")
        lines.append(f"latest review: {_latest_review_summary(repo, config)}")
        return "\n".join(lines)

    state = load_lifecycle_state(repo, artifact_root)
    if state is None:
        return "\n".join(
            [
                "Run:        idle",
                f"Config:     {config_rel}",
                "",
                "status: idle",
                "lifecycle: (none)",
                "No active Auto Loop run.",
                "Start with:",
                f"  auto-loop run {config_rel}",
            ]
        )

    reviews_display = f"{reviews_rel}/" if not str(reviews_rel).endswith("/") else reviews_rel
    lines = [
        f"Run:        {_run_label(state.status.value)}",
        f"Phase:      {_phase_label(state)}",
        f"Planner:    {_session_label(state.sessions['planner'].status, state.sessions['planner'].session_id)}",
        f"Worker:     {_session_label(state.sessions['worker'].status, state.sessions['worker'].session_id)}",
        f"Reviewer:   {_session_label(state.sessions['reviewer'].status, state.sessions['reviewer'].session_id)}",
        f"Plan:       {plan_rel}",
        f"Reviews:    {reviews_display}",
        "",
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
        f"product tree: {'clean' if is_product_tree_clean(repo, excludes=excludes) else 'dirty'}",
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
