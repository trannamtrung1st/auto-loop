"""Lifecycle state model and transitions (proposal sections 20-21)."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


class LifecycleStatus(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    LIMIT_REACHED = "limit_reached"
    ERROR = "error"


Actor = Literal["worker", "reviewer"]
ReviewScope = Literal["plan", "batch", "final"]


class RoleSession(BaseModel):
    session_id: str | None = None
    model: str = "auto"


class ActiveReview(BaseModel):
    scope: ReviewScope
    target: str
    summary: str
    base_commit: str | None = None
    head_commit: str | None = None
    worker_summary: str | None = None


class InflightMarker(BaseModel):
    actor: Actor
    turn: int
    session_id: str
    started_at: datetime
    head_before: str | None = None


class LifecycleState(BaseModel):
    schema_version: Literal[1] = 1
    lifecycle_id: str
    status: LifecycleStatus = LifecycleStatus.RUNNING
    turn: int = 1
    next_actor: Actor = "worker"
    plan_approved: bool = False
    initial_base_commit: str
    last_approved_commit: str
    active_review: ActiveReview | None = None
    sessions: dict[str, RoleSession] = Field(default_factory=dict)
    inflight: InflightMarker | None = None
    consecutive_provider_failures: int = 0
    consecutive_protocol_failures: int = 0
    started_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def sessions_present(self) -> LifecycleState:
        for role in ("worker", "reviewer"):
            if role not in self.sessions:
                raise ValueError(f"Missing session record for {role}")
        return self


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_lifecycle_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{uuid4().hex[:6]}"


def create_lifecycle(head_commit: str, *, lifecycle_id: str | None = None) -> LifecycleState:
    now = utc_now()
    lid = lifecycle_id or new_lifecycle_id()
    return LifecycleState(
        lifecycle_id=lid,
        turn=1,
        next_actor="worker",
        plan_approved=False,
        initial_base_commit=head_commit,
        last_approved_commit=head_commit,
        sessions={"worker": RoleSession(), "reviewer": RoleSession()},
        started_at=now,
        updated_at=now,
    )


def session_consistency_errors(state: LifecycleState) -> list[str]:
    errors: list[str] = []
    worker_id = state.sessions["worker"].session_id
    reviewer_id = state.sessions["reviewer"].session_id
    if worker_id and reviewer_id and worker_id == reviewer_id:
        errors.append("worker and reviewer session IDs must be distinct")
    if state.inflight:
        actor = state.inflight.actor
        expected = state.sessions[actor].session_id
        if expected and state.inflight.session_id != expected:
            errors.append("inflight session_id does not match stored role session")
    return errors
