"""Filesystem product snapshots must observe symlink nodes without following externals."""

from pathlib import Path

from auto_loop.config import default_config
from auto_loop.product_state import (
    capture_product_working_fingerprint,
    product_path_fingerprint,
    product_working_fingerprints_equal,
)


def _snap(repo: Path) -> list[list[str]]:
    config = default_config()
    config.git.mode = "off"
    _head, rows = capture_product_working_fingerprint(repo, config=config)
    return rows


def test_internal_file_symlink_create_delete_and_retarget(tmp_path: Path):
    repo = tmp_path / "w"
    repo.mkdir()
    (repo / "real.txt").write_text("content\n", encoding="utf-8")
    link = repo / "link.txt"
    link.symlink_to("real.txt")
    before = _snap(repo)
    link.unlink()
    after_delete = _snap(repo)
    assert not product_working_fingerprints_equal(before, after_delete)
    link.symlink_to("real.txt")
    after_recreate = _snap(repo)
    assert product_working_fingerprints_equal(before, after_recreate)
    link.unlink()
    link.symlink_to("other.txt")
    (repo / "other.txt").write_text("x\n", encoding="utf-8")
    after_retarget = _snap(repo)
    assert not product_working_fingerprints_equal(before, after_retarget)


def test_external_symlink_is_recorded_without_following_target(tmp_path: Path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    repo = tmp_path / "w"
    repo.mkdir()
    link = repo / "out.link"
    link.symlink_to(outside)
    rows = _snap(repo)
    rels = {row[0] for row in rows}
    assert "out.link" in rels
    assert "outside.txt" not in rels
    fp = product_path_fingerprint(link)
    link.unlink()
    link.symlink_to(outside)
    assert product_path_fingerprint(link) == fp


def test_dangling_symlink_is_included(tmp_path: Path):
    repo = tmp_path / "w"
    repo.mkdir()
    link = repo / "dangle"
    link.symlink_to("missing-target")
    before = _snap(repo)
    link.unlink()
    after = _snap(repo)
    assert before and not product_working_fingerprints_equal(before, after)


def test_directory_symlink_is_included_without_walking_target(tmp_path: Path):
    repo = tmp_path / "w"
    repo.mkdir()
    inner = repo / "pkg"
    inner.mkdir()
    (inner / "module.py").write_text("x\n", encoding="utf-8")
    dirlink = repo / "pkglink"
    dirlink.symlink_to("pkg", target_is_directory=True)
    rows = _snap(repo)
    rels = {row[0] for row in rows}
    assert "pkglink" in rels
    assert "pkg/module.py" in rels
    dirlink.unlink()
    dirlink.symlink_to("pkg", target_is_directory=True)
    assert product_working_fingerprints_equal(rows, _snap(repo))
    dirlink.unlink()
    dirlink.symlink_to("other", target_is_directory=True)
    (repo / "other").mkdir()
    assert not product_working_fingerprints_equal(rows, _snap(repo))
