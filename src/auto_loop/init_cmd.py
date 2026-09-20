"""Initialize .auto-loop control workspace from packaged templates."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from auto_loop.config import InstructionRoleSettings, InstructionSettings, default_config, dump_config
from auto_loop.exits import ExitCode
from auto_loop.paths import auto_loop_root


class InitError(Exception):
    """Initialization refused or failed."""

    exit_code = ExitCode.CONFIG_ERROR


@dataclass
class InitResult:
    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _read_template(relative: str) -> str:
    package = resources.files("auto_loop").joinpath("templates")
    return package.joinpath(relative).read_text(encoding="utf-8")


def _default_config_yaml(minimal: bool) -> str:
    if minimal:
        cfg = default_config()
        cfg.instructions = InstructionSettings(
            shared=InstructionRoleSettings(files=[]),
            worker=InstructionRoleSettings(files=[]),
            reviewer=InstructionRoleSettings(files=[]),
        )
        return dump_config(cfg)
    return dump_config(default_config())


def _is_nonempty_file(path: Path) -> bool:
    return path.is_file() and path.read_text(encoding="utf-8").strip() != ""


def _write_text(
    path: Path,
    content: str,
    *,
    force: bool,
    result: InitResult,
    rel: str,
) -> None:
    if path.exists() and not force:
        result.skipped.append(rel)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    result.created.append(rel)


def _ensure_dir(path: Path, result: InitResult, rel: str) -> None:
    if path.exists():
        result.skipped.append(rel)
        return
    path.mkdir(parents=True, exist_ok=True)
    result.created.append(rel)


def _write_task(
    path: Path,
    content: str,
    *,
    minimal: bool,
    force: bool,
    result: InitResult,
) -> None:
    rel = ".auto-loop/task.md"
    if path.exists() and _is_nonempty_file(path) and not force:
        if minimal:
            raise InitError(
                f"Refusing to overwrite nonempty task file: {path} (use --force to replace)"
            )
        result.skipped.append(rel)
        return
    if path.exists() and not force:
        result.skipped.append(rel)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    result.created.append(rel)


def run_init(repo: Path, *, force: bool = False, minimal: bool = False) -> InitResult:
    root = auto_loop_root(repo)
    result = InitResult()

    _write_text(
        root / "config.yaml",
        _default_config_yaml(minimal),
        force=force,
        result=result,
        rel=".auto-loop/config.yaml",
    )
    _write_text(
        root / "context.yaml",
        _read_template("context.default.yaml"),
        force=force,
        result=result,
        rel=".auto-loop/context.yaml",
    )

    task_content = "" if minimal else _read_template("task.md")
    _write_task(
        root / "task.md",
        task_content,
        minimal=minimal,
        force=force,
        result=result,
    )

    _write_text(
        root / "plan.md",
        _read_template("plan.md"),
        force=force,
        result=result,
        rel=".auto-loop/plan.md",
    )

    worker_tpl = "agents/worker.minimal.md" if minimal else "agents/worker.md"
    reviewer_tpl = "agents/reviewer.minimal.md" if minimal else "agents/reviewer.md"
    _write_text(
        root / "agents" / "worker.md",
        _read_template(worker_tpl),
        force=force,
        result=result,
        rel=".auto-loop/agents/worker.md",
    )
    _write_text(
        root / "agents" / "reviewer.md",
        _read_template(reviewer_tpl),
        force=force,
        result=result,
        rel=".auto-loop/agents/reviewer.md",
    )

    if not minimal:
        for name in ("shared.md", "worker.md", "reviewer.md"):
            _write_text(
                root / "instructions" / name,
                _read_template(f"instructions/{name}"),
                force=force,
                result=result,
                rel=f".auto-loop/instructions/{name}",
            )
        _ensure_dir(root / "resources", result, ".auto-loop/resources/")

    _ensure_dir(root / "reviews", result, ".auto-loop/reviews/")
    _ensure_dir(root / "runtime", result, ".auto-loop/runtime/")

    return result
