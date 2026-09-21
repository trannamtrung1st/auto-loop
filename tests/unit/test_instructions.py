"""Instruction composition tests."""

import subprocess
from pathlib import Path

from auto_loop.config import dump_config, load_config
from auto_loop.init_cmd import bootstrap_workspace
from auto_loop.instructions import compose_role_instructions, load_protocol_contract


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


def test_protocol_present_on_first_invocation(tmp_path: Path):
    repo = _repo(tmp_path)
    bootstrap_workspace(repo)
    config = load_config(repo / ".auto-loop" / "config.yaml")
    planner = compose_role_instructions(repo, config, "planner", first_invocation=True)
    worker = compose_role_instructions(repo, config, "worker", first_invocation=True)
    reviewer = compose_role_instructions(repo, config, "reviewer", first_invocation=True)
    assert "AUTO_LOOP_PROTOCOL" in planner
    assert load_protocol_contract("planner")[:40] in planner
    assert "AUTO_LOOP_PROTOCOL" in worker
    assert load_protocol_contract("worker")[:40] in worker
    assert "never declare the overall task" in worker.lower()
    assert "sole whole-task completion authority" in reviewer.lower()
    assert ".auto-loop/agents/reviewer.md" not in worker
    assert ".auto-loop/agents/worker.md" not in reviewer


def test_resume_turn_omits_instruction_stack(tmp_path: Path):
    repo = _repo(tmp_path)
    bootstrap_workspace(repo)
    config = load_config(repo / ".auto-loop" / "config.yaml")
    assert compose_role_instructions(repo, config, "worker", first_invocation=False) == ""


def test_replace_role_omits_playbook_only(tmp_path: Path):
    repo = _repo(tmp_path)
    bootstrap_workspace(repo)
    config = load_config(repo / ".auto-loop" / "config.yaml")
    config.instructions.worker.mode = "replace_role"
    worker = compose_role_instructions(repo, config, "worker", first_invocation=True)
    assert "AUTO_LOOP_PROTOCOL" in worker
    assert "AUTO_LOOP_ROLE_PLAYBOOK" not in worker
    assert "ADVISORY_SHARED" in worker


def test_doctor_reports_missing_configured_custom_instruction(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)
    bootstrap_workspace(repo)
    config_path = repo / ".auto-loop" / "config.yaml"
    config = load_config(config_path)
    config.instructions.worker.files.append("docs/extra-worker.md")
    config_path.write_text(dump_config(config), encoding="utf-8")
    import subprocess

    from auto_loop.doctor import run_doctor

    monkeypatch.setattr("auto_loop.doctor.resolve_cursor_binary", lambda _cfg: "/usr/bin/fake-agent")
    monkeypatch.setattr(
        "auto_loop.doctor.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout="--resume stream-json ask", stderr=""),
    )
    report = run_doctor(repo)
    assert not report.ok
    assert any("docs/extra-worker.md" in check.message for check in report.checks)
