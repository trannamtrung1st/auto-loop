"""Lifecycle state model, session slots, and v1→v2 migration."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from auto_loop.models import (
    ROLE_FOR_SLOT,
    ActiveReviewTarget,
    ReviewScope,
    Role,
    SessionSlot,
)

RUNTIME_SCHEMA_VERSION = 2
LEGACY_V1_PLAN_TARGET_PATH = "__auto_loop_legacy_v1_plan__"
LifecyclePhase = Literal["planning", "execution"]
SessionStatus = Literal["pending", "active", "retired", "legacy_not_created"]


class LifecycleStatus(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    LIMIT_REACHED = "limit_reached"
    ERROR = "error"


class SessionRecord(BaseModel):
    role: Role = "worker"
    session_id: str | None = None
    model: str = "auto"
    status: SessionStatus = "pending"


class ActiveReview(BaseModel):
    cycle_id: str
    round: int = 1
    scope: ReviewScope
    target: str
    summary: str
    session_purpose: SessionSlot = "reviewer"
    approved_base_commit: str | None = None
    production_head_commit: str | None = None
    current_candidate_head: str | None = None
    worker_summary: str | None = None
    plan_summary: str | None = None
    plan_sha256: str | None = None
    targets: list[ActiveReviewTarget] = Field(default_factory=list)

    @property
    def has_git_target(self) -> bool:
        return any(target.kind == "git_range" for target in self.targets)

    @property
    def git_head(self) -> str | None:
        for target in self.targets:
            if target.kind == "git_range":
                return target.head_commit
        return None

    @property
    def git_base(self) -> str | None:
        for target in self.targets:
            if target.kind == "git_range":
                return target.base_commit
        return None

    @property
    def target_ids(self) -> list[str]:
        return [target.id for target in self.targets]


class PendingRevision(BaseModel):
    cycle_id: str
    scope: ReviewScope
    target: str
    base_commit: str | None = None
    production_head_commit: str | None = None
    last_reviewed_head_commit: str | None = None
    round: int
    finding_review_file: str | None = None


class InflightMarker(BaseModel):
    session_slot: SessionSlot
    role: Role
    turn: int
    session_id: str
    started_at: datetime
    head_before: str | None = None


class LifecycleState(BaseModel):
    schema_version: Literal[2] = RUNTIME_SCHEMA_VERSION
    lifecycle_id: str
    status: LifecycleStatus = LifecycleStatus.RUNNING
    phase: LifecyclePhase = "planning"
    turn: int = 1
    next_session: SessionSlot = "planner"
    plan_approved: bool = False
    initial_approved_plan_sha256: str | None = None
    current_plan_sha256: str | None = None
    planning_completed_at: datetime | None = None
    initial_base_commit: str
    last_approved_commit: str
    active_review: ActiveReview | None = None
    pending_revision: PendingRevision | None = None
    review_cycle_seq: int = 0
    sessions: dict[str, SessionRecord] = Field(default_factory=dict)
    inflight: InflightMarker | None = None
    consecutive_provider_failures: int = 0
    consecutive_protocol_failures: int = 0
    worker_no_progress_streak: int = 0
    last_worker_progress_key: str | None = None
    legacy_v1_sessions: dict[str, Any] | None = None
    started_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def sessions_present(self) -> LifecycleState:
        for slot in ("planner", "plan_reviewer", "worker", "reviewer"):
            if slot not in self.sessions:
                raise ValueError(f"Missing session record for {slot}")
        return self

    @property
    def next_actor(self) -> Role:
        return ROLE_FOR_SLOT[self.next_session]

    @next_actor.setter
    def next_actor(self, value: str) -> None:
        if value not in ("planner", "plan_reviewer", "worker", "reviewer"):
            raise ValueError(f"Invalid next session: {value}")
        self.next_session = value  # type: ignore[assignment]


RoleSession = SessionRecord


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_lifecycle_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{uuid4().hex[:6]}"


def _empty_sessions() -> dict[str, SessionRecord]:
    return {
        "planner": SessionRecord(role="planner", status="pending"),
        "plan_reviewer": SessionRecord(role="reviewer", status="pending"),
        "worker": SessionRecord(role="worker", status="pending"),
        "reviewer": SessionRecord(role="reviewer", status="pending"),
    }


def create_lifecycle(head_commit: str, *, lifecycle_id: str | None = None) -> LifecycleState:
    now = utc_now()
    lid = lifecycle_id or new_lifecycle_id()
    return LifecycleState(
        lifecycle_id=lid,
        turn=1,
        phase="planning",
        next_session="planner",
        plan_approved=False,
        initial_base_commit=head_commit,
        last_approved_commit=head_commit,
        sessions=_empty_sessions(),
        started_at=now,
        updated_at=now,
    )


def next_cycle_id(state: LifecycleState) -> str:
    state.review_cycle_seq += 1
    return f"review-{state.review_cycle_seq:04d}"


def adopt_session_identity(
    state: LifecycleState,
    slot: SessionSlot,
    observed_session_id: str,
    model: str,
) -> bool:
    """Persist the first observed Cursor session id or fail closed on mismatch/duplication.

    Returns True when this call first assigned the slot's session id.
    """
    from auto_loop.providers.cursor import SessionError

    record = state.sessions[slot]
    created = False
    if record.session_id is None:
        record.session_id = observed_session_id
        record.model = model
        record.status = "active"
        created = True
    elif record.session_id != observed_session_id:
        raise SessionError(
            f"Session slot {slot} observed new id {observed_session_id!r} "
            f"but stored id is {record.session_id!r}"
        )
    for message in session_consistency_errors(state):
        if message.startswith("duplicate session id"):
            raise SessionError(message)
    return created


def session_consistency_errors(state: LifecycleState) -> list[str]:
    errors: list[str] = []
    ids = [
        (slot, rec.session_id)
        for slot, rec in state.sessions.items()
        if rec.session_id
    ]
    seen: dict[str, str] = {}
    for slot, session_id in ids:
        if session_id in seen:
            errors.append(
                f"duplicate session id between {seen[session_id]} and {slot}"
            )
        else:
            seen[session_id] = slot
    if state.inflight:
        slot = state.inflight.session_slot
        expected = state.sessions[slot].session_id
        if expected and state.inflight.session_id != expected:
            errors.append("inflight session_id does not match stored session slot")
        if state.inflight.role != ROLE_FOR_SLOT[slot]:
            errors.append("inflight role does not match session slot")
    if state.phase == "planning" and state.next_session not in ("planner", "plan_reviewer"):
        errors.append("planning phase next_session must be planner or plan_reviewer")
    if state.phase == "execution" and state.next_session not in ("worker", "reviewer"):
        errors.append("execution phase next_session must be worker or reviewer")
    return errors


def _session_status_from_v1(record: dict[str, Any]) -> SessionStatus:
    if record.get("session_id"):
        return "active"
    return "pending"


def _migrate_inflight(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if not raw:
        return None
    if "session_slot" in raw and "role" in raw:
        return raw
    actor = raw.get("actor")
    if actor not in ("worker", "reviewer"):
        return None
    migrated = dict(raw)
    migrated["session_slot"] = actor
    migrated["role"] = actor
    migrated.pop("actor", None)
    return migrated


def _migrate_v1_active_review(
    raw: dict[str, Any] | None,
    migrated: dict[str, Any],
) -> dict[str, Any] | None:
    if not raw or not isinstance(raw, dict):
        return None
    scope = raw.get("scope")
    if scope not in ("plan", "batch", "final"):
        return None
    target = raw.get("target")
    if not isinstance(target, str) or not target.strip():
        return None
    seq = int(migrated.get("review_cycle_seq") or 0)
    cycle_id = raw.get("cycle_id")
    if not isinstance(cycle_id, str) or not cycle_id.strip():
        seq += 1
        migrated["review_cycle_seq"] = seq
        cycle_id = f"review-{seq:04d}"
    round_no = int(raw.get("round") or 1)
    # v1 approved lifecycles only had worker + execution reviewer slots.
    purpose: SessionSlot = "reviewer"
    targets: list[dict[str, Any]] = []
    base = raw.get("approved_base_commit") or raw.get("base_commit")
    head = raw.get("current_candidate_head") or raw.get("head_commit")
    if base and head:
        targets.append(
            {
                "kind": "git_range",
                "id": "git",
                "base_commit": base,
                "head_commit": head,
            }
        )
    elif scope == "plan":
        targets.append(
            {
                "kind": "path",
                "id": "plan",
                "path": LEGACY_V1_PLAN_TARGET_PATH,
                "fingerprint": "",
                "exists": True,
                "git_classification": "control",
                "purpose": "Current plan document",
            }
        )
    return {
        "cycle_id": cycle_id,
        "round": round_no,
        "scope": scope,
        "target": target,
        "summary": raw.get("summary") or f"{scope} review",
        "session_purpose": purpose,
        "approved_base_commit": base,
        "production_head_commit": raw.get("production_head_commit"),
        "current_candidate_head": head,
        "worker_summary": raw.get("worker_summary"),
        "plan_summary": raw.get("plan_summary"),
        "plan_sha256": raw.get("plan_sha256"),
        "targets": targets,
    }


def migrate_lifecycle_data(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize persisted lifecycle JSON to runtime schema v2."""
    version = data.get("schema_version", 1)
    if version == RUNTIME_SCHEMA_VERSION:
        if "next_session" not in data and "next_actor" in data:
            data = dict(data)
            actor = data.pop("next_actor")
            data["next_session"] = actor if actor in ("planner", "plan_reviewer", "worker", "reviewer") else "planner"
        return data
    if version != 1:
        raise ValueError(f"Unsupported lifecycle schema_version {version}")

    plan_approved = bool(data.get("plan_approved"))
    old_sessions = data.get("sessions") or {}
    migrated = dict(data)
    migrated["schema_version"] = RUNTIME_SCHEMA_VERSION
    migrated.setdefault("pending_revision", None)
    migrated.setdefault("review_cycle_seq", 0)
    migrated.pop("next_actor", None)

    if plan_approved:
        next_actor = data.get("next_actor", "worker")
        next_session = next_actor if next_actor in ("worker", "reviewer") else "worker"
        worker = dict(old_sessions.get("worker") or {})
        reviewer = dict(old_sessions.get("reviewer") or {})
        migrated["phase"] = "execution"
        migrated["next_session"] = next_session
        migrated["sessions"] = {
            "planner": {
                "role": "planner",
                "session_id": None,
                "model": "auto",
                "status": "legacy_not_created",
            },
            "plan_reviewer": {
                "role": "reviewer",
                "session_id": None,
                "model": "auto",
                "status": "legacy_not_created",
            },
            "worker": {
                "role": "worker",
                "session_id": worker.get("session_id"),
                "model": worker.get("model", "auto"),
                "status": _session_status_from_v1(worker),
            },
            "reviewer": {
                "role": "reviewer",
                "session_id": reviewer.get("session_id"),
                "model": reviewer.get("model", "auto"),
                "status": _session_status_from_v1(reviewer),
            },
        }
        migrated["inflight"] = _migrate_inflight(data.get("inflight"))
        raw_review = data.get("active_review")
        migrated_review = _migrate_v1_active_review(
            raw_review if isinstance(raw_review, dict) else None,
            migrated,
        )
        if migrated_review is not None:
            migrated["active_review"] = migrated_review
        elif raw_review:
            migrated["active_review"] = None
            if next_session == "reviewer":
                migrated["next_session"] = "worker"
    else:
        migrated["phase"] = "planning"
        migrated["next_session"] = "planner"
        migrated["sessions"] = {
            slot: rec.model_dump(mode="json") for slot, rec in _empty_sessions().items()
        }
        migrated["inflight"] = None
        migrated["legacy_v1_sessions"] = old_sessions
        migrated["active_review"] = None

    return migrated


def _configured_plan_file(repo: Path, artifact_root: Path | None = None) -> str:
    from auto_loop.config import ConfigurationError, load_resolved_config_optional
    from auto_loop.paths import DEFAULT_ARTIFACTS_ROOT, auto_loop_root

    root = artifact_root if artifact_root is not None else auto_loop_root(repo)
    try:
        frozen = load_resolved_config_optional(root)
        if frozen is not None:
            return frozen.plan_file
    except ConfigurationError:
        pass
    return f"{DEFAULT_ARTIFACTS_ROOT}/plan.md"


def resolve_plan_review_path(
    repo: Path,
    stored_path: str,
    artifact_root: Path | None = None,
) -> str:
    """Map v1 migration sentinel plan targets to the workspace's configured plan file."""
    if stored_path == LEGACY_V1_PLAN_TARGET_PATH:
        return _configured_plan_file(repo, artifact_root)
    return stored_path


def _legacy_plan_target_needs_enrichment(target: ActiveReviewTarget) -> bool:
    return (
        target.kind == "path"
        and target.id == "plan"
        and target.path == LEGACY_V1_PLAN_TARGET_PATH
        and not target.fingerprint
    )


def enrich_lifecycle_state(
    repo: Path,
    state: LifecycleState,
    artifact_root: Path | None = None,
) -> LifecycleState:
    """Repository-aware fixes for migration-incomplete active reviews only.

    Ordinary v2 targets with a persisted fingerprint are never recomputed or retargeted.
    """
    from auto_loop.review_targets import fingerprint_path, resolve_review_path

    review = state.active_review
    if review is None:
        return state
    updated_targets: list[ActiveReviewTarget] = []
    changed = False
    for target in review.targets:
        if _legacy_plan_target_needs_enrichment(target):
            rel = resolve_plan_review_path(repo, target.path, artifact_root)
            try:
                resolved = resolve_review_path(repo, rel)
            except Exception:
                updates: dict[str, Any] = {"active_review": None}
                if state.next_session == "reviewer":
                    updates["next_session"] = "worker"
                return state.model_copy(update=updates)
            digest, exists = fingerprint_path(resolved)
            if not exists:
                updates = {"active_review": None}
                if state.next_session == "reviewer":
                    updates["next_session"] = "worker"
                return state.model_copy(update=updates)
            updated_targets.append(
                target.model_copy(
                    update={"path": rel, "fingerprint": digest, "exists": exists}
                )
            )
            changed = True
        else:
            updated_targets.append(target)
    if not changed:
        return state
    return state.model_copy(
        update={"active_review": review.model_copy(update={"targets": updated_targets})}
    )
