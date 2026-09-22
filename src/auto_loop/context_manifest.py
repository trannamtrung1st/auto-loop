"""Context resource validation and compact resource manifest rendering."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, field_validator, model_validator

from auto_loop.exits import ExitCode
from auto_loop.manifest_models import ManifestModel

CONTEXT_VERSION = 1
RoleName = Literal["planner", "worker", "reviewer"]
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


class ContextError(Exception):
    exit_code = ExitCode.CONFIG_ERROR


class ManifestEntry(ManifestModel):
    path: str
    purpose: str = ""
    required: bool = True

    @model_validator(mode="before")
    @classmethod
    def accept_path_string(cls, value: object) -> object:
        if isinstance(value, str):
            return {"path": value, "purpose": value, "required": True}
        return value

    @model_validator(mode="after")
    def default_purpose(self) -> ManifestEntry:
        if not self.purpose.strip():
            object.__setattr__(self, "purpose", self.path)
        return self


class RoleManifestSection(ManifestModel):
    resources: list[ManifestEntry] = Field(default_factory=list)
    skills: list[ManifestEntry] = Field(default_factory=list)


class ContextDocument(ManifestModel):
    version: int = CONTEXT_VERSION
    shared: RoleManifestSection = Field(default_factory=RoleManifestSection)
    planner: RoleManifestSection = Field(default_factory=RoleManifestSection)
    worker: RoleManifestSection = Field(default_factory=RoleManifestSection)
    reviewer: RoleManifestSection = Field(default_factory=RoleManifestSection)

    @field_validator("version")
    @classmethod
    def supported_version(cls, value: int) -> int:
        if value != CONTEXT_VERSION:
            raise ValueError(f"Unsupported context version {value}")
        return value


@dataclass
class ValidationIssue:
    severity: Literal["error", "warning"]
    message: str


@dataclass
class ContextValidationResult:
    document: ContextDocument | None = None
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def ok_for_run(self) -> bool:
        return self.document is not None and not any(i.severity == "error" for i in self.issues)


def _resolve_workspace_path(repo: Path, rel: str) -> Path:
    repo_root = repo.resolve()
    candidate = (repo_root / rel).resolve()
    try:
        candidate.relative_to(repo_root)
    except ValueError:
        raise ContextError(f"Resource path escapes workspace: {rel}")
    return candidate


def _validate_skill_frontmatter(skill_md: Path) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    text = skill_md.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        issues.append(ValidationIssue("error", f"Skill missing YAML frontmatter: {skill_md}"))
        return issues
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        issues.append(ValidationIssue("error", f"Invalid skill frontmatter YAML: {skill_md}"))
        return issues
    if not isinstance(meta, dict) or not meta.get("name") or not meta.get("description"):
        issues.append(
            ValidationIssue("error", f"Skill frontmatter requires name and description: {skill_md}")
        )
    return issues


def _validate_entry(repo: Path, entry: ManifestEntry, *, kind: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    try:
        path = _resolve_workspace_path(repo, entry.path)
    except ContextError as exc:
        issues.append(ValidationIssue("error", str(exc)))
        return issues

    if not path.exists():
        severity = "error" if entry.required else "warning"
        issues.append(
            ValidationIssue(severity, f"Missing {kind} path ({'required' if entry.required else 'optional'}): {entry.path}")
        )
        return issues

    if kind == "skill":
        skill_md = path / "SKILL.md" if path.is_dir() else path
        if skill_md.is_dir():
            skill_md = skill_md / "SKILL.md"
        if not skill_md.is_file():
            issues.append(ValidationIssue("error", f"Skill directory missing SKILL.md: {entry.path}"))
        else:
            issues.extend(_validate_skill_frontmatter(skill_md))
        return issues

    if not path.is_file() and not path.is_dir():
        issues.append(ValidationIssue("error", f"Resource path is not a file or directory: {entry.path}"))
    return issues


def validate_context(repo: Path, document: ContextDocument) -> ContextValidationResult:
    result = ContextValidationResult(document=document)
    sections = [
        ("shared", document.shared),
        ("planner", document.planner),
        ("worker", document.worker),
        ("reviewer", document.reviewer),
    ]
    for _name, section in sections:
        for entry in section.resources:
            result.issues.extend(_validate_entry(repo, entry, kind="resource"))
        for entry in section.skills:
            result.issues.extend(_validate_entry(repo, entry, kind="skill"))
    return result


def _entries_for_role(document: ContextDocument, role: RoleName) -> list[tuple[str, ManifestEntry]]:
    ordered: list[tuple[str, ManifestEntry]] = []
    seen: set[str] = set()

    def add(label: str, entry: ManifestEntry) -> None:
        key = f"{label}:{entry.path}"
        if key in seen:
            return
        seen.add(key)
        ordered.append((label, entry))

    for entry in document.shared.resources + document.shared.skills:
        add("Shared", entry)
    if role == "planner":
        role_section = document.planner
        label = "Planner"
    elif role == "worker":
        role_section = document.worker
        label = "Worker"
    else:
        role_section = document.reviewer
        label = "Reviewer"
    for entry in role_section.resources + role_section.skills:
        add(label, entry)
    return ordered


def render_resource_manifest(document: ContextDocument, role: RoleName) -> str:
    entries = _entries_for_role(document, role)
    lines = ["AVAILABLE CONTEXT", ""]
    current_label: str | None = None
    for label, entry in entries:
        if label != current_label:
            if current_label is not None:
                lines.append("")
            lines.append(f"{label}:")
            current_label = label
        lines.append(f"- {entry.path} — {entry.purpose}")
    lines.extend(
        [
            "",
            "Read/use these when relevant. Do not assume they are authoritative over "
            "task.md or frozen task resources.",
        ]
    )
    return "\n".join(lines)
