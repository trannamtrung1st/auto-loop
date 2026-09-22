"""Doctor diagnostics tests."""

import json
import socket
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from auto_loop.cli import app
from auto_loop.doctor import Severity, run_doctor
from auto_loop.init_cmd import bootstrap_workspace
from auto_loop.locking import acquire_workspace_lock
from auto_loop.manifest import load_run_manifest

runner = CliRunner()
_REAL_SUBPROCESS_RUN = subprocess.run


def _cursor_subprocess(argv, *args, **kwargs):
    """Stub the Cursor CLI without hiding Git commands doctor evaluates."""
    if argv and argv[0] == "git":
        return _REAL_SUBPROCESS_RUN(argv, *args, **kwargs)
    cmd = " ".join(str(part) for part in argv)
    if "--version" in cmd:
        return subprocess.CompletedProcess(argv, 0, stdout="agent 1.0\n", stderr="")
    return subprocess.CompletedProcess(
        argv,
        0,
        stdout="--resume stream-json --mode ask",
        stderr="",
    )


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


def _source(repo: Path):
    return load_run_manifest(repo / ".ai" / "run.yaml")


def test_doctor_passes_after_bootstrap(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)
    bootstrap_workspace(repo)

    monkeypatch.setattr("auto_loop.doctor.resolve_cursor_binary", lambda _cfg: "/usr/bin/fake-agent")
    monkeypatch.setattr("auto_loop.doctor.subprocess.run", _cursor_subprocess)

    report = run_doctor(_source(repo), verbose=True)
    assert report.ok
    assert any(c.severity == Severity.OK and c.check_id == "config" for c in report.checks)


def test_doctor_reports_missing_config_file(tmp_path: Path):
    result = runner.invoke(app, ["doctor", str(tmp_path / "missing.yaml")])
    assert result.exit_code == 10


def test_package_defaults_do_not_require_generated_instructions(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)
    bootstrap_workspace(repo)
    monkeypatch.setattr("auto_loop.doctor.resolve_cursor_binary", lambda _cfg: "/usr/bin/fake-agent")
    monkeypatch.setattr("auto_loop.doctor.subprocess.run", _cursor_subprocess)
    report = run_doctor(_source(repo))
    assert report.ok
    assert not (repo / ".ai" / "auto-loop" / "instructions").exists()


def test_doctor_reports_workspace_lock(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)
    bootstrap_workspace(repo)
    source = _source(repo)
    monkeypatch.setattr("auto_loop.doctor.resolve_cursor_binary", lambda _cfg: "/usr/bin/fake-agent")
    monkeypatch.setattr("auto_loop.doctor.subprocess.run", _cursor_subprocess)
    monkeypatch.setattr("auto_loop.locking.is_pid_alive", lambda _pid: True)
    handle = acquire_workspace_lock(repo, "lc-doc", source.artifact_root)
    report = run_doctor(source)
    handle.release()
    assert any(c.check_id == "lock" and c.severity == Severity.WARNING for c in report.checks)


def test_doctor_cli_path_argument(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)
    bootstrap_workspace(repo)
    monkeypatch.setattr("auto_loop.doctor.resolve_cursor_binary", lambda _cfg: "/usr/bin/fake-agent")
    monkeypatch.setattr("auto_loop.doctor.subprocess.run", _cursor_subprocess)
    result = runner.invoke(app, ["doctor", str(repo / ".ai" / "run.yaml")])
    assert result.exit_code == 0


def test_doctor_reconciles_dead_controller_ownership(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)
    bootstrap_workspace(repo)
    source = _source(repo)
    from auto_loop.git import head_commit
    from auto_loop.lifecycle import LifecycleStatus, create_lifecycle
    from auto_loop.runtime import load_lifecycle_state, save_lifecycle_state
    from auto_loop.stop_control import active_run_path

    state = create_lifecycle(head_commit(repo))
    save_lifecycle_state(repo, state, artifact_root=source.artifact_root)
    path = active_run_path(repo, source.artifact_root)
    path.write_text(
        json.dumps(
            {
                "controller_pid": 999_999_999,
                "controller_hostname": socket.gethostname(),
                "controller_started_at": 1.0,
                "lifecycle_id": state.lifecycle_id,
                "provider_pid": None,
                "provider_create_time": None,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("auto_loop.doctor.resolve_cursor_binary", lambda _cfg: "/usr/bin/fake-agent")
    monkeypatch.setattr("auto_loop.doctor.subprocess.run", _cursor_subprocess)
    report = run_doctor(source)
    assert any(check.check_id == "runtime" and check.severity == Severity.WARNING for check in report.checks)
    assert not path.is_file()
    loaded = load_lifecycle_state(repo, source.artifact_root)
    assert loaded is not None
    assert loaded.status == LifecycleStatus.STOPPED


def test_doctor_does_not_mutate_remote_ownership(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)
    bootstrap_workspace(repo)
    source = _source(repo)
    from auto_loop.git import head_commit
    from auto_loop.lifecycle import LifecycleStatus, create_lifecycle
    from auto_loop.runtime import load_lifecycle_state, save_lifecycle_state
    from auto_loop.stop_control import active_run_path

    state = create_lifecycle(head_commit(repo))
    state.status = LifecycleStatus.RUNNING
    save_lifecycle_state(repo, state, artifact_root=source.artifact_root)
    path = active_run_path(repo, source.artifact_root)
    path.write_text(
        json.dumps(
            {
                "controller_pid": 4242,
                "controller_hostname": "remote-host.example",
                "controller_started_at": 1.0,
                "lifecycle_id": state.lifecycle_id,
                "provider_pid": None,
                "provider_create_time": None,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("auto_loop.doctor.resolve_cursor_binary", lambda _cfg: "/usr/bin/fake-agent")
    monkeypatch.setattr("auto_loop.doctor.subprocess.run", _cursor_subprocess)
    report = run_doctor(source)
    assert path.is_file()
    loaded = load_lifecycle_state(repo, source.artifact_root)
    assert loaded is not None
    assert loaded.status == LifecycleStatus.RUNNING
    assert any(
        check.check_id == "runtime" and "another host" in check.message.lower()
        for check in report.checks
    )


def test_doctor_reports_missing_custom_instruction(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)
    bootstrap_workspace(repo)
    yaml_path = repo / ".ai" / "run.yaml"
    text = yaml_path.read_text(encoding="utf-8")
    yaml_path.write_text(text + "\ninstructions:\n  worker:\n    files:\n      - missing.md\n", encoding="utf-8")
    monkeypatch.setattr("auto_loop.doctor.resolve_cursor_binary", lambda _cfg: "/usr/bin/fake-agent")
    monkeypatch.setattr("auto_loop.doctor.subprocess.run", _cursor_subprocess)
    report = run_doctor(load_run_manifest(yaml_path))
    assert not report.ok
    assert any("missing.md" in check.message for check in report.checks)
