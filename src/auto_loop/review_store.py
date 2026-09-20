"""Append-only review artifact paths."""

from __future__ import annotations

import re
from pathlib import Path

from auto_loop.config import AutoLoopConfig

_REVIEW_NAME = re.compile(r"^(\d{4})-.+\.md$")


def reviews_dir(repo: Path, config: AutoLoopConfig) -> Path:
    return repo / config.reviews_dir


def next_review_sequence(repo: Path, config: AutoLoopConfig) -> int:
    directory = reviews_dir(repo, config)
    directory.mkdir(parents=True, exist_ok=True)
    highest = 0
    for path in directory.iterdir():
        if not path.is_file():
            continue
        match = _REVIEW_NAME.match(path.name)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def review_artifact_path(
    repo: Path,
    config: AutoLoopConfig,
    *,
    sequence: int,
    slug: str,
) -> Path:
    safe_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", slug).strip("-") or "review"
    return reviews_dir(repo, config) / f"{sequence:04d}-{safe_slug}.md"
