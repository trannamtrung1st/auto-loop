"""Repository path resolution tests."""

from pathlib import Path

import pytest

from auto_loop.paths import auto_loop_root, resolve_repository_path


def test_resolve_repository_path_defaults_to_cwd(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert resolve_repository_path(None) == tmp_path.resolve()


def test_resolve_repository_path_expands_user(tmp_path: Path):
    assert resolve_repository_path(tmp_path) == tmp_path.resolve()


def test_resolve_repository_path_missing_raises(tmp_path: Path):
    missing = tmp_path / "nope"
    with pytest.raises(FileNotFoundError):
        resolve_repository_path(missing)


def test_auto_loop_root():
    repo = Path("/tmp/repo")
    assert auto_loop_root(repo) == Path("/tmp/repo/.auto-loop")
