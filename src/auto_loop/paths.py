"""Repository path resolution shared by CLI commands."""

from pathlib import Path


def resolve_repository_path(path: Path | None) -> Path:
    """Resolve the target Git repository root (proposal section 35 PATH argument)."""
    if path is None:
        return Path.cwd().resolve()
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Repository path does not exist: {resolved}")
    if not resolved.is_dir():
        raise NotADirectoryError(f"Repository path is not a directory: {resolved}")
    return resolved


def auto_loop_root(repo: Path) -> Path:
    return repo / ".auto-loop"
