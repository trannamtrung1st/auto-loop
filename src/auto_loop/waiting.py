"""Bounded external waits: fingerprints, delays, and interruptible sleep."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

from auto_loop.blockers import BlockerFingerprintInput

AbortCheck = Callable[[], str | None]


class Clock(Protocol):
    def now(self) -> datetime: ...

    def sleep_until(self, when: datetime, abort: AbortCheck) -> str | None:
        """Block until ``when``. Return an abort reason, or None when the deadline is reached."""


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep_until(self, when: datetime, abort: AbortCheck) -> str | None:
        while True:
            reason = abort()
            if reason is not None:
                return reason
            remaining = (when - self.now()).total_seconds()
            if remaining <= 0:
                return None
            time.sleep(min(0.25, remaining))


def compute_wait_fingerprint(ctx: BlockerFingerprintInput) -> str:
    """Evidence-aware key for confirmed waits. Distinct prefix from blocker fingerprints."""
    from auto_loop.blockers import compute_blocker_fingerprint

    blocked = compute_blocker_fingerprint(ctx)
    return f"waiting:{blocked}"


def clamp_retry_seconds(
    suggested: int | None,
    *,
    default_seconds: int,
    max_seconds: int,
    remaining_seconds: int,
) -> int:
    """Controller policy clamps agent retry hints to the remaining wait window."""
    if remaining_seconds <= 0:
        return 0
    hint = suggested if suggested is not None and suggested > 0 else default_seconds
    return max(1, min(hint, max_seconds, remaining_seconds))


def deadline_from(start: datetime, max_seconds: int) -> datetime:
    return start + timedelta(seconds=max_seconds)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
