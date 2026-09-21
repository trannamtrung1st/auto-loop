#!/usr/bin/env python3
"""Minimal fake Cursor CLI for subprocess session-retry tests."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SESSION_ID = "retry-sess-fixed-001"


def _counter_path() -> Path:
    raw = os.environ.get("AUTO_LOOP_CURSOR_RETRY_COUNTER")
    if not raw:
        raise SystemExit("AUTO_LOOP_CURSOR_RETRY_COUNTER not set")
    return Path(raw)


def _resume_id(argv: list[str]) -> str | None:
    for arg in argv:
        if arg.startswith("--resume="):
            return arg.split("=", 1)[1]
    return None


def main() -> None:
    counter = _counter_path()
    attempt = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
    resume = _resume_id(sys.argv)

    if attempt == 0:
        print(json.dumps({"type": "system", "session_id": SESSION_ID}))
        counter.write_text("1", encoding="utf-8")
        return

    if resume != SESSION_ID:
        print(f"expected resume {SESSION_ID!r}, got {resume!r}", file=sys.stderr)
        raise SystemExit(2)

    payload = json.dumps(
        {
            "schema_version": 2,
            "actor": "planner",
            "status": "review_requested",
            "review": {"scope": "plan", "target": "plan", "summary": "ok"},
            "plan_summary": "planned",
            "notes": [],
        }
    )
    block = f"<AUTO_LOOP_RESULT>\n{payload}\n</AUTO_LOOP_RESULT>"
    print(json.dumps({"type": "system", "session_id": SESSION_ID}))
    print(json.dumps({"type": "result", "session_id": SESSION_ID, "result": block}))
    counter.write_text("2", encoding="utf-8")


if __name__ == "__main__":
    main()
