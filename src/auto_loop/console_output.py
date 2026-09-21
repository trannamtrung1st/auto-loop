"""Run console rendering for quiet/normal/verbose modes."""

from __future__ import annotations

import sys
from typing import TextIO

from auto_loop.config import ConsoleLevel


class RunConsole:
    def __init__(self, level: ConsoleLevel, stream: TextIO | None = None) -> None:
        self.level = level
        self.stream = stream or sys.stdout

    def _emit(self, message: str, *, min_level: ConsoleLevel = "normal") -> None:
        order = {"quiet": 0, "normal": 1, "verbose": 2}
        if order[self.level] < order[min_level]:
            return
        print(message, file=self.stream)

    def lifecycle_started(
        self,
        lifecycle_id: str,
        *,
        goal_summary: str | None = None,
        user_config_rel: str = "run.yaml",
        resuming: bool = False,
        artifact_root_rel: str = ".ai/auto-loop",
    ) -> None:
        if resuming:
            self._emit("Resuming Auto Loop", min_level="normal")
            if goal_summary:
                self._emit(f"Goal: {goal_summary}", min_level="normal")
            self._emit(
                f"Lifecycle {lifecycle_id} resumed",
                min_level="verbose",
            )
            return
        self._emit("Starting Auto Loop", min_level="normal")
        self._emit("", min_level="normal")
        if goal_summary:
            self._emit(f"Goal: {goal_summary}", min_level="normal")
        self._emit(f"Config: {user_config_rel}", min_level="normal")
        self._emit("", min_level="normal")
        self._emit("Planner   starting", min_level="normal")
        self._emit("Worker    waiting", min_level="normal")
        self._emit("Reviewer  waiting", min_level="normal")
        self._emit("", min_level="normal")
        self._emit(f"Generated state will be stored in {artifact_root_rel}/", min_level="normal")
        self._emit("You normally do not need to edit that directory.", min_level="normal")
        self._emit(f"Lifecycle {lifecycle_id} started", min_level="verbose")

    def plan_ready(self, plan_path: str) -> None:
        self._emit("", min_level="normal")
        self._emit(f"Plan ready: {plan_path}", min_level="normal")
        self._emit("Starting worker...", min_level="normal")

    def turn_started(self, turn: int, actor: str) -> None:
        self._emit(f"Turn {turn}: {actor}", min_level="normal")

    def session_created(self, actor: str, session_id: str) -> None:
        self._emit(f"{actor} session created {session_id[:8]}...", min_level="normal")

    def session_resumed(self, actor: str, session_id: str) -> None:
        self._emit(f"{actor} resuming session {session_id[:8]}...", min_level="verbose")

    def review_requested(self, scope: str, target: str) -> None:
        self._emit(f"Review requested: {scope} ({target})", min_level="normal")

    def review_result(self, verdict: str, scope: str, finding_count: int = 0) -> None:
        extra = f", {finding_count} finding(s)" if finding_count else ""
        self._emit(f"Review {scope}: {verdict.upper()}{extra}", min_level="normal")

    def baseline_advanced(self, head: str) -> None:
        self._emit(f"Approved baseline advanced to {head[:7]}", min_level="normal")

    def terminal(self, message: str) -> None:
        self._emit(message, min_level="normal")

    def verbose(self, message: str) -> None:
        self._emit(message, min_level="verbose")
