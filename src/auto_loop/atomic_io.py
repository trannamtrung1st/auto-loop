"""Atomic text/JSON writes for durable runtime state."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any


class AtomicWriteError(Exception):
    """Atomic write could not complete."""


WriteHook = Callable[[], None]


def atomic_write_text(
    path: Path,
    content: str,
    *,
    before_replace: WriteHook | None = None,
) -> None:
    """Write via a temp file and atomic replace so readers never see partial content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        if before_replace is not None:
            before_replace()
        os.replace(tmp_path, path)
    except OSError as exc:
        if tmp_path.is_file():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise AtomicWriteError(str(exc)) from exc
    finally:
        if tmp_path.is_file():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def atomic_write_json(
    path: Path,
    payload: Any,
    *,
    indent: int = 2,
    before_replace: WriteHook | None = None,
) -> None:
    text = json.dumps(payload, indent=indent) + "\n"
    atomic_write_text(path, text, before_replace=before_replace)
