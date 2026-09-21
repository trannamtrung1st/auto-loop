"""Scripted in-process provider for integration tests."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field

from auto_loop.providers.fake_cursor import FakeCursorEngine


@dataclass
class ScriptedProvider:
    """Return queued terminal responses per role invocation."""

    engine: FakeCursorEngine = field(default_factory=FakeCursorEngine)
    queues: dict[str, deque[str]] = field(
        default_factory=lambda: {"worker": deque(), "reviewer": deque()}
    )

    def queue(self, role: str, final_text: str) -> None:
        self.queues[role].append(final_text)

    def invoke(self, argv: list[str]) -> tuple[int, list[str]]:
        self.engine.strict = False
        return self.engine.run(argv)

    def set_response(self, role: str, payload: dict) -> None:
        text = json.dumps(payload)
        block = f"<AUTO_LOOP_RESULT>\n{text}\n</AUTO_LOOP_RESULT>"
        self.queues[role].append(block)
        self.engine.response_text = block

    def prepare(self, role: str) -> None:
        if self.queues[role]:
            self.engine.response_text = self.queues[role].popleft()

    def set_worker_plan_request(self) -> None:
        self.set_response(
            "worker",
            {
                "schema_version": 1,
                "actor": "worker",
                "status": "review_requested",
                "review": {"scope": "plan", "target": "plan", "summary": "initial plan"},
                "work_summary": "planned",
                "verification": [],
                "notes": [],
            },
        )

    def set_reviewer_pass(self, scope: str, target: str) -> None:
        self.set_response(
            "reviewer",
            {
                "schema_version": 1,
                "actor": "reviewer",
                "verdict": "pass",
                "scope": scope,
                "target": target,
                "summary": "ok",
                "findings": [],
                "verification": [],
            },
        )

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
                "schema_version": 1,
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
                "schema_version": 1,
                "actor": "worker",
                "status": "blocked",
                "review": None,
                "work_summary": summary,
                "verification": [],
                "notes": [],
            },
        )

    def set_reviewer_complete(self, head: str) -> None:
        self.set_response(
            "reviewer",
            {
                "schema_version": 1,
                "actor": "reviewer",
                "verdict": "complete",
                "scope": "final",
                "target": "whole-task",
                "reviewed_head_commit": head,
                "whole_task_reviewed": True,
                "summary": "whole task satisfied",
                "findings": [],
                "verification": [],
            },
        )

    def set_reviewer_blocked(self, summary: str = "external intervention required") -> None:
        self.set_response(
            "reviewer",
            {
                "schema_version": 1,
                "actor": "reviewer",
                "verdict": "blocked",
                "scope": "batch",
                "target": "blocked",
                "summary": summary,
                "findings": [],
                "verification": [],
            },
        )

    def set_reviewer_revise(self, scope: str, target: str, finding_id: str = "f-1") -> None:
        self.set_response(
            "reviewer",
            {
                "schema_version": 1,
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
