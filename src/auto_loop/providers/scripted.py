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
