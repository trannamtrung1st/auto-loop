"""Maintained kanban-board sample contract (repo tree and wheel bundle)."""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

from auto_loop.manifest import load_run_manifest

_REPO = Path(__file__).resolve().parents[2]
_SAMPLE = _REPO / "samples" / "kanban-board"

_EXPECTED_REL = (
    "README.md",
    "proposal.md",
    ".ai/run.yaml",
    ".ai/task.md",
)

_FORBIDDEN_REL = (
    ".gitignore",
    "bootstrap.sh",
    ".ai/auto-loop",
)


def _sample_tree_files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_maintained_sample_contains_only_pre_run_inputs():
    for rel in _EXPECTED_REL:
        assert (_SAMPLE / rel).is_file(), rel
    for rel in _FORBIDDEN_REL:
        assert not (_SAMPLE / rel).exists(), rel
    files = _sample_tree_files(_SAMPLE)
    assert files == set(_EXPECTED_REL)


def test_maintained_sample_git_mode_off():
    source = load_run_manifest(_SAMPLE / ".ai" / "run.yaml")
    assert source.config.git.mode == "off"


def test_root_gitignore_covers_sample_generated_artifacts():
    text = (_REPO / ".gitignore").read_text(encoding="utf-8")
    assert "samples/**/.ai/auto-loop/" in text


def _build_wheel(tmp_path: Path) -> Path:
    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", str(_REPO), "--no-deps", "-w", str(wheel_dir)],
        capture_output=True,
        text=True,
        cwd=_REPO,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    wheels = list(wheel_dir.glob("auto_loop-*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def test_wheel_contains_kanban_sample_without_bootstrap_or_gitignore(tmp_path: Path):
    wheel = _build_wheel(tmp_path)
    prefix = "auto_loop/samples/kanban-board/"
    with zipfile.ZipFile(wheel) as zf:
        names = [n for n in zf.namelist() if n.startswith(prefix)]
    for rel in _EXPECTED_REL:
        assert f"{prefix}{rel}" in names, rel
    for forbidden in _FORBIDDEN_REL:
        assert not any(forbidden in n for n in names), forbidden
    assert not any(".ai/auto-loop" in n for n in names)
