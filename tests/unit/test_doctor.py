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


def _write_sample_layout(repo: Path, *, git_mode: str = "optional") -> Path:
    """Layout aligned with samples/kanban-board (.ai/run.yaml, workspace ..)."""
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "task.md").write_text("goal\n", encoding="utf-8")
    ai = repo / ".ai"
    ai.mkdir(parents=True, exist_ok=True)
    yaml_path = ai / "run.yaml"
    yaml_path.write_text(
        "version: 2\n"
        "workspace: ..\n"
        "task:\n"
        "  source: task.md\n"
        "artifacts:\n  root: .ai/auto-loop\n"
        f"git:\n  mode: {git_mode}\n",
        encoding="utf-8",
    )
    return yaml_path


def _artifact_gitignore_checks(report):
    return [c for c in report.checks if c.check_id == "artifacts:gitignore"]


def test_doctor_no_git_worktree_skips_artifact_ignore_warning(tmp_path: Path, monkeypatch):
    repo = tmp_path / "plain"
    yaml_path = _write_sample_layout(repo, git_mode="off")
    monkeypatch.setattr("auto_loop.doctor.resolve_cursor_binary", lambda _cfg: "/usr/bin/fake-agent")
    monkeypatch.setattr("auto_loop.doctor.subprocess.run", _cursor_subprocess)
    report = run_doctor(load_run_manifest(yaml_path))
    assert not _artifact_gitignore_checks(report)


def test_doctor_warns_when_artifact_not_gitignored(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)
    yaml_path = _write_sample_layout(repo)
    monkeypatch.setattr("auto_loop.doctor.resolve_cursor_binary", lambda _cfg: "/usr/bin/fake-agent")
    monkeypatch.setattr("auto_loop.doctor.subprocess.run", _cursor_subprocess)
    report = run_doctor(load_run_manifest(yaml_path))
    warnings = _artifact_gitignore_checks(report)
    assert len(warnings) == 1
    assert "not ignored by Git" in warnings[0].message


def test_doctor_ok_when_workspace_local_gitignore_covers_artifact(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)
    yaml_path = _write_sample_layout(repo)
    (repo / ".gitignore").write_text(".ai/auto-loop/\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "ignore artifacts")
    monkeypatch.setattr("auto_loop.doctor.resolve_cursor_binary", lambda _cfg: "/usr/bin/fake-agent")
    monkeypatch.setattr("auto_loop.doctor.subprocess.run", _cursor_subprocess)
    report = run_doctor(load_run_manifest(yaml_path))
    assert not _artifact_gitignore_checks(report)


def test_doctor_ok_when_ancestor_gitignore_ignores_artifact_path(tmp_path: Path, monkeypatch):
    outer = tmp_path / "mono"
    outer.mkdir()
    _git(outer, "init")
    _git(outer, "config", "user.email", "t@example.com")
    _git(outer, "config", "user.name", "T")
    repo = outer / "packages" / "app"
    yaml_path = _write_sample_layout(repo)
    (outer / ".gitignore").write_text("packages/app/.ai/auto-loop/\n", encoding="utf-8")
    _git(outer, "add", ".")
    _git(outer, "commit", "-m", "init")
    monkeypatch.setattr("auto_loop.doctor.resolve_cursor_binary", lambda _cfg: "/usr/bin/fake-agent")
    monkeypatch.setattr("auto_loop.doctor.subprocess.run", _cursor_subprocess)
    report = run_doctor(load_run_manifest(yaml_path))
    assert not _artifact_gitignore_checks(report)


def test_doctor_ok_when_ancestor_gitignore_ignores_workspace(tmp_path: Path, monkeypatch):
    outer = tmp_path / "outer"
    outer.mkdir()
    _git(outer, "init")
    _git(outer, "config", "user.email", "t@example.com")
    _git(outer, "config", "user.name", "T")
    (outer / ".gitignore").write_text("nested/\n", encoding="utf-8")
    repo = outer / "nested" / "project"
    yaml_path = _write_sample_layout(repo)
    _git(outer, "add", ".")
    _git(outer, "commit", "-m", "init")
    monkeypatch.setattr("auto_loop.doctor.resolve_cursor_binary", lambda _cfg: "/usr/bin/fake-agent")
    monkeypatch.setattr("auto_loop.doctor.subprocess.run", _cursor_subprocess)
    report = run_doctor(load_run_manifest(yaml_path))
    assert not _artifact_gitignore_checks(report)


def test_doctor_ok_when_git_info_exclude_ignores_artifact(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)
    yaml_path = _write_sample_layout(repo)
    exclude = repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text(".ai/auto-loop/\n", encoding="utf-8")
    monkeypatch.setattr("auto_loop.doctor.resolve_cursor_binary", lambda _cfg: "/usr/bin/fake-agent")
    monkeypatch.setattr("auto_loop.doctor.subprocess.run", _cursor_subprocess)
    report = run_doctor(load_run_manifest(yaml_path))
    assert not _artifact_gitignore_checks(report)


def test_doctor_gitignore_negation_follows_git(tmp_path: Path, monkeypatch):
    from auto_loop.git import is_path_git_ignored

    repo = _repo(tmp_path)
    yaml_path = _write_sample_layout(repo)
    (repo / ".gitignore").write_text(".ai/\n!.ai/run.yaml\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "partial ignore")
    source = load_run_manifest(yaml_path)
    ignored = is_path_git_ignored(source.artifact_root)
    monkeypatch.setattr("auto_loop.doctor.resolve_cursor_binary", lambda _cfg: "/usr/bin/fake-agent")
    monkeypatch.setattr("auto_loop.doctor.subprocess.run", _cursor_subprocess)
    report = run_doctor(source)
    warnings = _artifact_gitignore_checks(report)
    if ignored:
        assert not warnings
    else:
        assert len(warnings) == 1


def test_doctor_git_mode_off_reports_no_repository_required(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)
    yaml_path = _write_sample_layout(repo, git_mode="off")
    monkeypatch.setattr("auto_loop.doctor.resolve_cursor_binary", lambda _cfg: "/usr/bin/fake-agent")
    monkeypatch.setattr("auto_loop.doctor.subprocess.run", _cursor_subprocess)
    report = run_doctor(load_run_manifest(yaml_path))
    git_checks = [c for c in report.checks if c.check_id == "git"]
    assert any("Git mode is off" in c.message for c in git_checks)
    assert not any(c.severity == Severity.ERROR for c in git_checks)


def test_doctor_does_not_mutate_gitignore(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)
    yaml_path = _write_sample_layout(repo)
    ignore = repo / ".gitignore"
    ignore.write_text("notes.txt\n", encoding="utf-8")
    before = ignore.read_text(encoding="utf-8")
    monkeypatch.setattr("auto_loop.doctor.resolve_cursor_binary", lambda _cfg: "/usr/bin/fake-agent")
    monkeypatch.setattr("auto_loop.doctor.subprocess.run", _cursor_subprocess)
    run_doctor(load_run_manifest(yaml_path))
    assert ignore.read_text(encoding="utf-8") == before


def test_doctor_product_exclude_unchanged_when_artifact_unignored(tmp_path: Path, monkeypatch):
    from auto_loop.product_state import is_product_tree_clean, product_excludes

    repo = _repo(tmp_path)
    yaml_path = _write_sample_layout(repo)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "inputs")
    source = load_run_manifest(yaml_path)
    artifact = source.artifact_root
    artifact.mkdir(parents=True, exist_ok=True)
    (artifact / "runtime").mkdir(parents=True, exist_ok=True)
    (artifact / "runtime" / "state.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("auto_loop.doctor.resolve_cursor_binary", lambda _cfg: "/usr/bin/fake-agent")
    monkeypatch.setattr("auto_loop.doctor.subprocess.run", _cursor_subprocess)
    report = run_doctor(source)
    assert len(_artifact_gitignore_checks(report)) == 1
    assert is_product_tree_clean(repo, excludes=product_excludes(source.config))


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
