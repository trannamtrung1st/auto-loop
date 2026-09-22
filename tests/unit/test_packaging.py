"""Wheel install smoke tests for the operator experience."""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _build_wheel(tmp_path: Path) -> Path:
    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", str(_repo_root()), "--no-deps", "-w", str(wheel_dir)],
        capture_output=True,
        text=True,
        cwd=_repo_root(),
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    wheels = list(wheel_dir.glob("auto_loop-*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def _build_sdist(tmp_path: Path) -> Path:
    dist_dir = tmp_path / "sdist"
    dist_dir.mkdir()
    install = subprocess.run(
        [sys.executable, "-m", "pip", "install", "build>=1"],
        capture_output=True,
        text=True,
    )
    assert install.returncode == 0, install.stderr or install.stdout
    proc = subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "-o", str(dist_dir), str(_repo_root())],
        capture_output=True,
        text=True,
        cwd=_repo_root(),
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    tarballs = list(dist_dir.glob("auto_loop-*.tar.gz"))
    assert len(tarballs) == 1
    return tarballs[0]


def _wheel_from_sdist(tmp_path: Path, sdist: Path) -> Path:
    wheel_dir = tmp_path / "wheels-from-sdist"
    wheel_dir.mkdir()
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", str(sdist), "--no-deps", "-w", str(wheel_dir)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    wheels = list(wheel_dir.glob("auto_loop-*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def test_sdist_contains_sample_and_wheel_from_sdist_includes_it(tmp_path: Path):
    import tarfile

    sdist = _build_sdist(tmp_path)
    with tarfile.open(sdist, "r:gz") as tf:
        names = tf.getnames()
    assert any("samples/kanban-board/README.md" in n for n in names)
    wheel = _wheel_from_sdist(tmp_path, sdist)
    with zipfile.ZipFile(wheel) as zf:
        wheel_names = zf.namelist()
    assert any(n.endswith("auto_loop/samples/kanban-board/README.md") for n in wheel_names)


def test_wheel_contains_templates_and_license(tmp_path: Path):
    wheel = _build_wheel(tmp_path)
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
    assert any(n.endswith("auto_loop/templates/task.md") for n in names)
    assert any(n.endswith("auto_loop/templates/run.yaml") for n in names)
    assert any(n.endswith("auto_loop/templates/run.full.yaml") for n in names)
    assert any(n.endswith("auto_loop/templates/agents/planner.md") for n in names)
    assert any(n.endswith("auto_loop/templates/protocol/shared.md") for n in names)
    assert any(n.endswith("auto_loop/templates/instructions/shared.md") for n in names)
    assert not any("harness_resources" in n for n in names)
    assert not any(".agents/" in n or n.endswith("AGENTS.md") for n in names)
    assert any(n.endswith("LICENSE") or n.endswith("auto_loop-0.1.0.dist-info/LICENSE") for n in names)


def test_clean_venv_install_help_init_and_import(tmp_path: Path):
    wheel = _build_wheel(tmp_path)
    venv_dir = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True, capture_output=True)
    pip = venv_dir / "bin" / "pip"
    py = venv_dir / "bin" / "python"
    auto_loop = venv_dir / "bin" / "auto-loop"
    subprocess.run([str(pip), "install", str(wheel)], check=True, capture_output=True)

    version = subprocess.run(
        [str(auto_loop), "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "0.1.0" in version.stdout

    help_result = subprocess.run(
        [str(auto_loop), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "init" in help_result.stdout
    assert "doctor" in help_result.stdout
    assert "resume" in help_result.stdout
    assert "resources" not in help_result.stdout

    import_check = subprocess.run(
        [
            str(py),
            "-c",
            "import auto_loop; import importlib.util; "
            "spec = importlib.util.find_spec('auto_loop'); "
            "assert spec and spec.origin and 'site-packages' in spec.origin",
        ],
        capture_output=True,
        text=True,
    )
    assert import_check.returncode == 0, import_check.stderr

    repo = tmp_path / "product"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=repo, check=True)

    init_result = subprocess.run(
        [str(auto_loop), "init", str(repo / ".ai" / "run.yaml")],
        capture_output=True,
        text=True,
        cwd=repo,
    )
    assert init_result.returncode == 0, init_result.stderr or init_result.stdout
    assert (repo / ".ai" / "run.yaml").is_file()
    assert not (repo / "task.md").exists()
    assert not (repo / ".ai" / "auto-loop" / "task.md").exists()

    uninstall = subprocess.run(
        [str(pip), "uninstall", "-y", "auto-loop"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Successfully uninstalled" in uninstall.stdout
