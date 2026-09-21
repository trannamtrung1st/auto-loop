"""Atomic runtime write behavior."""

import json
from pathlib import Path

import pytest

from auto_loop.atomic_io import AtomicWriteError, atomic_write_json


def test_atomic_write_preserves_prior_json_on_replace_failure(tmp_path: Path):
    path = tmp_path / "state.json"
    atomic_write_json(path, {"version": 1, "ok": True})

    def fail_before_replace() -> None:
        raise OSError("simulated crash")

    with pytest.raises(AtomicWriteError):
        atomic_write_json(path, {"version": 2, "ok": False}, before_replace=fail_before_replace)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["ok"] is True
