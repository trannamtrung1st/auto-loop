"""Scripted in-process provider for integration tests."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field

from auto_loop.providers.fake_cursor import FakeCursorEngine
from auto_loop.providers.argv_session import resume_session_id_from_argv
from auto_loop.providers.supervision import (
    ProviderAttemptResult,
    provider_attempt_from_process_output,
)

_EMPTY_QUEUES = {
    "planner": deque(),
    "plan_reviewer": deque(),
    "worker": deque(),
    "reviewer": deque(),
}


@dataclass
class ScriptedProvider:
    """Return queued terminal responses per session slot."""

    engine: FakeCursorEngine = field(default_factory=FakeCursorEngine)
    queues: dict[str, deque[str]] = field(
        default_factory=lambda: {
            "planner": deque(),
            "plan_reviewer": deque(),
            "worker": deque(),
            "reviewer": deque(),
        }
    )

    def queue(self, role: str, final_text: str) -> None:
        self.queues[role].append(final_text)

    def invoke(self, argv: list[str]) -> ProviderAttemptResult:
        self.engine.strict = False
        code, lines = self.engine.run(argv)
        return provider_attempt_from_process_output(
            lines,
            code,
            expected_session_id=resume_session_id_from_argv(argv),
        )

    def set_response(self, role: str, payload: dict) -> None:
        text = json.dumps(payload)
        block = f"<AUTO_LOOP_RESULT>\n{text}\n</AUTO_LOOP_RESULT>"
        self.queues.setdefault(role, deque()).append(block)
        self.engine.response_text = block

    def prepare(self, role: str) -> None:
        if role not in self.queues or not self.queues[role]:
            raise RuntimeError(f"No scripted provider response queued for role={role!r}")
        self.engine.response_text = self.queues[role].popleft()

    def set_invalid_protocol_response(self, role: str) -> None:
        self.queues.setdefault(role, deque()).append("Thanks for waiting, but I forgot the JSON block.")

    def set_planner_review_request(self, target: str = "plan") -> None:
        self.set_response(
            "planner",
            {
                "schema_version": 2,
                "actor": "planner",
                "status": "review_requested",
                "review": {"scope": "plan", "target": target, "summary": "initial plan"},
                "plan_summary": "planned",
                "notes": [],
            },
        )

    def set_worker_plan_request(self) -> None:
        """Compatibility helper: planning now uses the planner slot."""
        self.set_planner_review_request()

    def set_reviewer_pass(self, scope: str, target: str, *, slot: str | None = None) -> None:
        if slot is None:
            slot = "plan_reviewer" if scope == "plan" else "reviewer"
        ids = ["plan"] if scope == "plan" else (["git"] if scope != "final" else [])
        self.set_response(
            slot,
            {
                "schema_version": 2,
                "actor": "reviewer",
                "verdict": "pass",
                "scope": scope,
                "target": target,
                "reviewed_target_ids": ids,
                "summary": "ok",
                "findings": [],
                "verification": [],
            },
        )

    def set_plan_reviewer_pass(self, target: str = "plan") -> None:
        self.set_reviewer_pass("plan", target, slot="plan_reviewer")

    def set_worker_final_request(self, head: str | None = None) -> None:
        review: dict = {
            "scope": "final",
            "target": "whole-task",
            "summary": "requesting whole-task acceptance",
        }
        if head:
            review["head_commit"] = head
        self.set_response(
            "worker",
            {
                "schema_version": 2,
                "actor": "worker",
                "status": "review_requested",
                "review": review,
                "work_summary": "ready for final review",
                "verification": [],
                "notes": [],
            },
        )

    def set_worker_blocked(self, summary: str = "blocked on external dependency") -> None:
        self.set_response(
            "worker",
            {
                "schema_version": 2,
                "actor": "worker",
                "status": "blocked",
                "review": None,
                "work_summary": summary,
                "verification": [],
                "notes": [],
            },
        )

    def set_reviewer_complete(self, head: str | None = None, *, target_ids: list[str] | None = None) -> None:
        payload: dict = {
            "schema_version": 2,
            "actor": "reviewer",
            "verdict": "complete",
            "scope": "final",
            "target": "whole-task",
            "whole_task_reviewed": True,
            "summary": "whole task satisfied",
            "findings": [],
            "verification": [],
        }
        if head:
            payload["reviewed_head_commit"] = head
        if target_ids:
            payload["reviewed_target_ids"] = target_ids
        self.set_response("reviewer", payload)

    def set_reviewer_blocked(self, summary: str = "external intervention required") -> None:
        self.set_response(
            "reviewer",
            {
                "schema_version": 2,
                "actor": "reviewer",
                "verdict": "blocked",
                "scope": "batch",
                "target": "blocked",
                "summary": summary,
                "findings": [],
                "verification": [],
            },
        )

    def set_reviewer_revise(
        self,
        scope: str,
        target: str,
        finding_id: str = "f-1",
        *,
        slot: str | None = None,
    ) -> None:
        if slot is None:
            slot = "plan_reviewer" if scope == "plan" else "reviewer"
        self.set_response(
            slot,
            {
                "schema_version": 2,
                "actor": "reviewer",
                "verdict": "revise",
                "scope": scope,
                "target": target,
                "summary": "needs changes",
                "findings": [
                    {
                        "id": finding_id,
                        "title": "Fix required",
                        "detail": "Address the gap before approval.",
                        "evidence": "tests/integration/example",
                        "required_change": "Update implementation and re-request review.",
                    }
                ],
                "verification": [],
            },
        )
