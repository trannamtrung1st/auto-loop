"""Domain and protocol result models."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

PROTOCOL_SCHEMA_VERSION = 2

Role = Literal["planner", "worker", "reviewer"]
SessionSlot = Literal["planner", "plan_reviewer", "worker", "reviewer"]
ReviewScope = Literal["plan", "batch", "final"]
WorkerStatus = Literal["review_requested", "blocked"]
PlannerStatus = Literal["review_requested", "blocked"]
ReviewerVerdict = Literal["pass", "revise", "complete", "blocked"]
VerificationResult = Literal["pass", "fail", "not_run"]
PathGitClassification = Literal["tracked", "untracked", "ignored", "control", "none"]

ROLE_FOR_SLOT: dict[SessionSlot, Role] = {
    "planner": "planner",
    "plan_reviewer": "reviewer",
    "worker": "worker",
    "reviewer": "reviewer",
}


class GitRangeTarget(BaseModel):
    kind: Literal["git_range"] = "git_range"
    id: str = "git"
    base_commit: str
    head_commit: str


class PathTargetRequest(BaseModel):
    kind: Literal["path"] = "path"
    id: str
    path: str
    purpose: str | None = None


class ContentTargetRequest(BaseModel):
    kind: Literal["content"] = "content"
    id: str
    purpose: str | None = None
    content: str


ReviewTargetRequest = Annotated[
    GitRangeTarget | PathTargetRequest | ContentTargetRequest,
    Field(discriminator="kind"),
]


class ActiveGitTarget(BaseModel):
    kind: Literal["git_range"] = "git_range"
    id: str = "git"
    base_commit: str
    head_commit: str


class ActivePathTarget(BaseModel):
    kind: Literal["path"] = "path"
    id: str
    path: str
    fingerprint: str
    exists: bool
    git_classification: PathGitClassification
    purpose: str | None = None


class ActiveContentTarget(BaseModel):
    kind: Literal["content"] = "content"
    id: str
    purpose: str | None = None
    content: str
    content_sha256: str


ActiveReviewTarget = Annotated[
    ActiveGitTarget | ActivePathTarget | ActiveContentTarget,
    Field(discriminator="kind"),
]


class ReviewRequest(BaseModel):
    scope: ReviewScope
    target: str
    summary: str
    targets: list[ReviewTargetRequest] = Field(default_factory=list)
    base_commit: str | None = None
    head_commit: str | None = None


class VerificationEvidence(BaseModel):
    command: str
    result: VerificationResult
    note: str | None = None


class PlannerResult(BaseModel):
    schema_version: Literal[2]
    actor: Literal["planner"]
    status: PlannerStatus
    review: ReviewRequest | None = None
    plan_summary: str
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def planner_constraints(self) -> PlannerResult:
        if self.status == "review_requested":
            if self.review is None:
                raise ValueError("Planner review_requested requires a review request")
            if self.review.scope != "plan":
                raise ValueError("Planner may only request scope=plan")
        return self


class WorkerResult(BaseModel):
    schema_version: Literal[2]
    actor: Literal["worker"]
    status: WorkerStatus
    review: ReviewRequest | None = None
    work_summary: str
    verification: list[VerificationEvidence] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class Finding(BaseModel):
    id: str
    title: str
    detail: str
    evidence: str
    required_change: str


class ReviewerResult(BaseModel):
    schema_version: Literal[2]
    actor: Literal["reviewer"]
    verdict: ReviewerVerdict
    scope: ReviewScope
    target: str
    reviewed_target_ids: list[str] = Field(default_factory=list)
    reviewed_base_commit: str | None = None
    reviewed_head_commit: str | None = None
    whole_task_reviewed: bool = False
    summary: str
    findings: list[Finding] = Field(default_factory=list)
    verification: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def verdict_constraints(self) -> ReviewerResult:
        if self.verdict in ("pass", "complete") and self.findings:
            raise ValueError("PASS/COMPLETE verdicts cannot include findings")
        if self.verdict == "revise" and not self.findings:
            raise ValueError("REVISE verdict requires at least one finding")
        if self.verdict == "complete":
            if self.scope != "final":
                raise ValueError("COMPLETE verdict requires final scope")
            if not self.whole_task_reviewed:
                raise ValueError("COMPLETE verdict requires whole_task_reviewed=true")
        return self
