"""Provider request/result contracts (proposal section 25)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from auto_loop.models import Role, SessionSlot

AgentMode = Literal["agent", "ask"]


class AgentRequest(BaseModel):
    role: Role
    session_purpose: SessionSlot
    workspace: Path
    prompt: str
    model: str
    mode: AgentMode
    timeout_seconds: int
    idle_timeout_seconds: int
    extra_args: list[str] = Field(default_factory=list)


class AgentRunResult(BaseModel):
    session_id: str
    request_id: str | None = None
    exit_code: int | None = None
    started_at: datetime
    finished_at: datetime
    timed_out: bool = False
    idle_timed_out: bool = False
    interrupted: bool = False
    final_text: str
    raw_stream_path: Path
    readable_log_path: Path
    usage: dict[str, Any] | None = None
