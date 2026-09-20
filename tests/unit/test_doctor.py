"""Doctor diagnostics tests."""

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from auto_loop.cli import app
from auto_loop.doctor import Severity, run_doctor
from auto_loop.init_cmd import run_init

runner = CliRunner()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "commit", "--allow-empty", "-m", "init")
    return repo


def test_doctor_passes_after_default_init(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)
    run_init(repo)

    def fake_resolve(_settings):
        return "/usr/bin/fake-agent"

    def fake_run(argv, **kwargs):
        cmd = " ".join(argv)
        if "--version" in cmd:
            return subprocess.CompletedProcess(argv, 0, stdout="agent 1.0\n", stderr="")
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="--resume stream-json --mode ask",
            stderr="",
        )

    monkeypatch.setattr("auto_loop.doctor.resolve_cursor_binary", lambda _cfg: "/usr/bin/fake-agent")
    monkeypatch.setattr("auto_loop.doctor.subprocess.run", fake_run)

    report = run_doctor(repo, verbose=True)
    assert report.ok
    assert any(c.severity == Severity.OK and c.check_id == "config" for c in report.checks)


def test_doctor_reports_missing_workspace(tmp_path: Path):
    repo = _repo(tmp_path)
    report = run_doctor(repo)
    assert not report.ok
    assert any(c.check_id == "workspace" for c in report.checks)


def test_minimal_init_reports_missing_instruction_templates(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)
    run_init(repo, minimal=True)
    monkeypatch.setattr("auto_loop.doctor.resolve_cursor_binary", lambda _cfg: "/usr/bin/fake-agent")
    monkeypatch.setattr(
        "auto_loop.doctor.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout="--resume stream-json ask", stderr=""),
    )
    report = run_doctor(repo)
    assert not report.ok
    assert any(c.check_id == "instructions" and "init --minimal" in c.message for c in report.checks)


def test_doctor_cli_exits_nonzero_on_failure(tmp_path: Path):
    repo = _repo(tmp_path)
    result = runner.invoke(app, ["doctor", str(repo)])
    assert result.exit_code == 10


def test_doctor_cli_path_argument(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)
    run_init(repo)
    monkeypatch.setattr("auto_loop.doctor.resolve_cursor_binary", lambda _cfg: "/usr/bin/fake-agent")
    monkeypatch.setattr(
        "auto_loop.doctor.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout="--resume stream-json ask", stderr=""),
    )
    result = runner.invoke(app, ["doctor", str(repo)])
    assert result.exit_code == 0
