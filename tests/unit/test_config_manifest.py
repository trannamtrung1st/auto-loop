"""Manifest templates, init, and frozen-config round-trip tests."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from auto_loop.cli import app
from auto_loop.config import (
    ConfigurationError,
    dump_config,
    effective_settings_snapshot,
    load_config,
    parse_config_dict,
    write_resolved_config,
)
from auto_loop.doctor import run_doctor
from auto_loop.exits import ExitCode
from auto_loop.init_cmd import run_init
from auto_loop.manifest import load_run_manifest

runner = CliRunner()

PUBLIC_TOP_LEVEL_SECTIONS = (
    "version",
    "workspace",
    "task",
    "artifacts",
    "models",
    "run",
    "provider",
    "agents",
    "instructions",
    "context",
    "git",
    "protection",
    "logging",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _read_packaged_template(name: str) -> str:
    package = resources.files("auto_loop").joinpath("templates")
    return package.joinpath(name).read_text(encoding="utf-8")


def test_packaged_minimal_template_parses():
    text = _read_packaged_template("run.yaml").replace("{{workspace}}", "..").replace(
        "{{task_source}}", ".ai/proposal.md"
    ).replace("{{artifacts_root}}", ".ai/auto-loop")
    cfg = parse_config_dict(yaml.safe_load(text))
    assert cfg.version == 2
    assert cfg.models.worker == "auto"
    assert cfg.run.max_turns == 100


def test_packaged_full_template_parses():
    text = _read_packaged_template("run.full.yaml").replace("{{workspace}}", "..").replace(
        "{{task_source}}", ".ai/proposal.md"
    ).replace("{{artifacts_root}}", ".ai/auto-loop")
    cfg = parse_config_dict(yaml.safe_load(text))
    assert cfg.run.protocol_retries == 1
    assert cfg.agents["reviewer"].mode == "agent"
    assert cfg.context.version == 1


def test_full_template_includes_public_sections():
    text = (
        _read_packaged_template("run.full.yaml")
        .replace("{{workspace}}", ".")
        .replace("{{task_source}}", ".ai/proposal.md")
        .replace("{{artifacts_root}}", ".ai/auto-loop")
    )
    data = yaml.safe_load(text)
    for key in PUBLIC_TOP_LEVEL_SECTIONS:
        assert key in data, f"missing public section {key}"


def test_init_writes_minimal_manifest(tmp_path: Path):
    target = tmp_path / ".ai" / "run.yaml"
    result = run_init(target)
    assert result.created
    cfg = load_config(target)
    assert cfg.run.max_turns == 100
    body = target.read_text(encoding="utf-8")
    assert "agents:" not in body or "role_file" not in body
    assert "run.full.yaml" not in body


def test_legacy_git_booleans_are_rejected(tmp_path: Path):
    path = tmp_path / "run.yaml"
    path.write_text(
        "version: 2\n"
        "workspace: .\n"
        "task:\n"
        "  source: proposal.md\n"
        "git:\n"
        "  require_repository: true\n",
        encoding="utf-8",
    )
    (tmp_path / "proposal.md").write_text("task\n", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_config(path)


def test_init_full_writes_reference_manifest(tmp_path: Path):
    target = tmp_path / "loop.yml"
    result = run_init(target, full=True)
    assert result.created
    cfg = load_config(target)
    assert cfg.provider.cursor.command == "agent"
    assert cfg.git.mode == "optional"
    assert cfg.git.protect_approved_history is True
    for key in PUBLIC_TOP_LEVEL_SECTIONS:
        assert key in yaml.safe_load(target.read_text(encoding="utf-8"))


def test_cli_init_full_flag(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / ".ai" / "run.yaml"
    result = runner.invoke(app, ["init", str(path), "--full"])
    assert result.exit_code == int(ExitCode.COMPLETE)
    assert path.is_file()
    assert "instructions:" in path.read_text(encoding="utf-8")


def test_frozen_config_round_trip_preserves_effective_settings(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "goal.md").write_text("goal\n", encoding="utf-8")
    yaml_path = repo / "run.yaml"
    yaml_path.write_text(
        "version: 2\nworkspace: .\ntask:\n  source: goal.md\n"
        "models:\n  planner: p1\n  worker: w1\n  reviewer: r1\n"
        "run:\n  max_turns: 7\n  max_runtime_minutes: 9\n"
        "  agent_timeout_seconds: 11\n  agent_idle_timeout_seconds: 12\n"
        "  provider_retries: 1\n  protocol_retries: 0\n"
        "  max_consecutive_worker_no_progress: 2\n",
        encoding="utf-8",
    )
    source = load_run_manifest(yaml_path)
    before = effective_settings_snapshot(source.config)
    write_resolved_config(repo, source.config)
    reloaded = load_config(source.artifact_root / "runtime" / "config.resolved.yaml")
    after = effective_settings_snapshot(reloaded)
    assert after == before
    dumped = yaml.safe_load(dump_config(reloaded))
    assert "limits" not in dumped
    assert "model" not in str(dumped.get("agents", {}))


def test_rejects_agents_role_model(tmp_path: Path):
    path = tmp_path / "run.yaml"
    path.write_text(
        "version: 2\nworkspace: .\ntask:\n  source: t.md\n"
        "agents:\n  worker:\n    model: custom-model\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="agents.worker.model"):
        load_config(path)


def test_rejects_top_level_limits_section(tmp_path: Path):
    path = tmp_path / "run.yaml"
    path.write_text(
        "version: 2\nworkspace: .\ntask:\n  source: t.md\n"
        "limits:\n  max_turns: 9\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="top-level 'limits'"):
        load_config(path)


def test_rejects_unknown_agents_role(tmp_path: Path):
    path = tmp_path / "run.yaml"
    path.write_text(
        "version: 2\nworkspace: .\ntask:\n  source: t.md\n"
        "agents:\n  workr:\n    mode: agent\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="Unknown agents role"):
        load_config(path)


def test_rejects_unknown_agents_role_numeric_key(tmp_path: Path):
    path = tmp_path / "run.yaml"
    path.write_text(
        "version: 2\nworkspace: .\ntask:\n  source: t.md\n"
        "agents:\n  123:\n    mode: agent\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="Unknown agents role"):
        load_config(path)


def test_rejects_unknown_agents_worker_field(tmp_path: Path):
    path = tmp_path / "run.yaml"
    path.write_text(
        "version: 2\nworkspace: .\ntask:\n  source: t.md\n"
        "agents:\n  worker:\n    role_fil: worker.md\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="Extra inputs are not permitted"):
        load_config(path)


def test_rejects_unknown_top_level_manifest_key(tmp_path: Path):
    path = tmp_path / "run.yaml"
    path.write_text(
        "version: 2\nworkspace: .\ntask:\n  source: t.md\n"
        "goal: build something\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="Extra inputs are not permitted"):
        load_config(path)


def test_rejects_unknown_models_key(tmp_path: Path):
    path = tmp_path / "run.yaml"
    path.write_text(
        "version: 2\nworkspace: .\ntask:\n  source: t.md\n"
        "models:\n  workr: gpt-5.6\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="Extra inputs are not permitted"):
        load_config(path)


def test_rejects_unknown_run_key(tmp_path: Path):
    path = tmp_path / "run.yaml"
    path.write_text(
        "version: 2\nworkspace: .\ntask:\n  source: t.md\n"
        "run:\n  max_turn: 200\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="Extra inputs are not permitted"):
        load_config(path)


def test_rejects_unknown_context_resource_field(tmp_path: Path):
    path = tmp_path / "run.yaml"
    path.write_text(
        "version: 2\nworkspace: .\ntask:\n  source: t.md\n"
        "context:\n  shared:\n    resources:\n      - path: README.md\n"
        "        typo: oops\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="Extra inputs are not permitted"):
        load_config(path)


def test_kanban_sample_manifest_passes_doctor(tmp_path: Path, monkeypatch):
    sample = _repo_root() / "samples" / "kanban-board"
    dest = tmp_path / "kanban"
    import shutil
    import subprocess

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

    shutil.copytree(sample, dest)
    yaml_path = dest / ".ai" / "run.yaml"
    source = load_run_manifest(yaml_path)
    report = run_doctor(source)
    assert report.ok, report.render(verbose=True)
