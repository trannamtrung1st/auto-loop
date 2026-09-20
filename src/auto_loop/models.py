"""Domain and protocol result models (proposal section 40)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

ReviewScope = Literal["plan", "batch", "final"]
WorkerStatus = Literal["review_requested", "blocked"]
ReviewerVerdict = Literal["pass", "revise", "complete", "blocked"]
VerificationResult = Literal["pass", "fail", "not_run"]


class ReviewRequest(BaseModel):
    scope: ReviewScope
    target: str
    summary: str
    base_commit: str | None = None
    head_commit: str | None = None


class VerificationEvidence(BaseModel):
    command: str
    result: VerificationResult
    note: str | None = None


class WorkerResult(BaseModel):
    schema_version: Literal[1]
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
    schema_version: Literal[1]
    actor: Literal["reviewer"]
    verdict: ReviewerVerdict
    scope: ReviewScope
    target: str
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
