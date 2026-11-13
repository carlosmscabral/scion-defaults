"""Audit skill packages for bloat or stack conflicts and inject role-scoped skills.

This module validates third-party or authored skill directories before template
compilation, injects per-role skill subsets into compiled Scion templates, and
configures Claude coordinator templates for strict Scion container delegation.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
from typing import Any

import yaml

MAX_SKILL_MD_LINES = 300
MAX_SINGLE_FILE_LINES = 1000
MAX_PACKAGE_LINES = 2500
MAX_SKILLS_PER_ROLE = 6

_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", re.DOTALL)
_SAFE_NAME_RE = re.compile(r"\A[a-zA-Z0-9][a-zA-Z0-9._-]*\Z")


def _validate_safe_segment(value: str, field_label: str) -> str:
    """Reject path separators or parent-directory segments in role and skill names."""
    cleaned = str(value).strip()
    if not _SAFE_NAME_RE.fullmatch(cleaned) or ".." in cleaned:
        raise ValueError(f"Unsafe {field_label} {value!r}: must be a single path segment.")
    return cleaned

# Match positive CLI invocations so negation warnings ("Never use pnpm") do not fail.
_YARN_CMD_RE = re.compile(r"(?:^|[`\s$])yarn\s+(?:install|add|run|dlx|build|test)\b", re.MULTILINE)
_PNPM_CMD_RE = re.compile(r"(?:^|[`\s$])pnpm\s+(?:install|i|add|run|dlx|exec|build|test)\b", re.MULTILINE)
_NPM_CMD_RE = re.compile(r"(?:^|[`\s$])npm\s+(?:install|ci|run|exec|test)\b", re.MULTILINE)
_POETRY_CMD_RE = re.compile(r"(?:^|[`\s$])(?:poetry|pipenv)\s+(?:install|add|run|lock)\b", re.MULTILINE)


def _uses_tool(cmd: str, tool: str) -> bool:
    """Return True if a build or test command invokes the named package tool."""
    return bool(re.search(rf"(?:^|\s|/){re.escape(tool)}(?:\s|$)", cmd))


def _check_stack_contradictions(content: str, spec: dict[str, Any]) -> list[str]:
    """Detect positive package-manager instructions that conflict with stack commands."""
    errors: list[str] = []
    stack = spec.get("stack") or {}
    combined = f"{stack.get('build_command', '')} {stack.get('test_command', '')} {stack.get('lint_command', '')}"

    if _uses_tool(combined, "npm"):
        if _PNPM_CMD_RE.search(content) or _YARN_CMD_RE.search(content):
            errors.append("Skill prescribes 'pnpm' or 'yarn' commands while stack uses 'npm'.")
    elif _uses_tool(combined, "pnpm"):
        if _NPM_CMD_RE.search(content) or _YARN_CMD_RE.search(content):
            errors.append("Skill prescribes 'npm' or 'yarn' commands while stack uses 'pnpm'.")
    elif _uses_tool(combined, "yarn"):
        if _NPM_CMD_RE.search(content) or _PNPM_CMD_RE.search(content):
            errors.append("Skill prescribes 'npm' or 'pnpm' commands while stack uses 'yarn'.")

    if _uses_tool(combined, "uv") or _uses_tool(combined, "pytest"):
        if _POETRY_CMD_RE.search(content):
            errors.append("Skill prescribes 'poetry' or 'pipenv' commands while stack uses 'uv' or 'pytest'.")

    return errors


def _check_relative_links(md_path: Path, text: str) -> list[str]:
    """Return broken relative file link descriptions for one Markdown file."""
    broken: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for match in _LINK_RE.finditer(line):
            raw_target = match.group(1).split("#", 1)[0].strip()
            if not raw_target or "://" in raw_target or raw_target.startswith("mailto:"):
                continue
            if not (md_path.parent / raw_target).resolve().exists():
                broken.append(f"{md_path.name} -> {raw_target}")
    return broken


def audit_bank(bank_dir: str | Path, spec: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate every skill package in bank_dir for metadata, links, bloat, and stack fit."""
    report: dict[str, Any] = {"ok": False, "skills_checked": 0, "errors": [], "warnings": []}
    bank_path = Path(bank_dir)
    if not bank_path.is_dir():
        report["errors"].append(f"Skill bank directory not found: {bank_dir}")
        return report

    skill_dirs = sorted(p for p in bank_path.iterdir() if p.is_dir() and not p.name.startswith("."))
    report["skills_checked"] = len(skill_dirs)

    for skill_dir in skill_dirs:
        skill_name = skill_dir.name
        skill_md_path = skill_dir / "SKILL.md"
        if not skill_md_path.is_file():
            report["errors"].append(f"Skill '{skill_name}' is missing SKILL.md.")
            continue

        content = skill_md_path.read_text(encoding="utf-8")
        lines = content.splitlines()
        if len(lines) > MAX_SKILL_MD_LINES:
            report["warnings"].append(
                f"Skill '{skill_name}' SKILL.md has {len(lines)} lines (exceeds {MAX_SKILL_MD_LINES}-line cap)."
            )

        fm_match = _FRONTMATTER_RE.match(content)
        if not fm_match:
            report["errors"].append(f"Skill '{skill_name}' is missing YAML frontmatter.")
        else:
            try:
                fm = yaml.safe_load(fm_match.group(1)) or {}
                if not isinstance(fm, dict):
                    report["errors"].append(f"Skill '{skill_name}' frontmatter must be a YAML mapping.")
                else:
                    if fm.get("name") != skill_name:
                        report["errors"].append(
                            f"Skill '{skill_name}' frontmatter name {fm.get('name')!r} does not match directory."
                        )
                    if not str(fm.get("description") or "").strip():
                        report["errors"].append(f"Skill '{skill_name}' frontmatter is missing a description.")
            except yaml.YAMLError:
                report["errors"].append(f"Skill '{skill_name}' contains invalid YAML frontmatter.")

        total_lines = 0
        combined_md: list[str] = []
        for root, _, files in os.walk(skill_dir):
            for fname in sorted(files):
                fpath = Path(root) / fname
                if fpath.suffix.lower() not in (".md", ".py", ".sh", ".js", ".ts", ".json", ".yaml", ".yml"):
                    continue
                try:
                    ftext = fpath.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                flines = len(ftext.splitlines())
                total_lines += flines
                if flines > MAX_SINGLE_FILE_LINES:
                    report["warnings"].append(
                        f"Skill '{skill_name}' file '{fpath.relative_to(skill_dir)}' has {flines} lines "
                        f"(exceeds {MAX_SINGLE_FILE_LINES}-line cap)."
                    )
                if fpath.suffix.lower() == ".md":
                    combined_md.append(ftext)
                    for broken in _check_relative_links(fpath, ftext):
                        report["errors"].append(f"Skill '{skill_name}' has broken relative link: {broken}")

        if total_lines > MAX_PACKAGE_LINES:
            report["warnings"].append(
                f"Skill '{skill_name}' totals {total_lines} lines (exceeds {MAX_PACKAGE_LINES}-line package budget)."
            )

        if spec:
            for err in _check_stack_contradictions(content, spec):
                report["errors"].append(f"Skill '{skill_name}' SKILL.md stack conflict: {err}")
            ref_md = "\n".join(t for t in combined_md if t != content)
            for warn in _check_stack_contradictions(ref_md, spec):
                report["warnings"].append(f"Skill '{skill_name}' reference stack note: {warn}")

    report["ok"] = len(report["errors"]) == 0
    return report


def inject_skills(spec: dict[str, Any], bank_dir: str | Path, out_dir: str | Path) -> list[str]:
    """Copy role-scoped skills from bank_dir into compiled templates/<role>/skills/."""
    bank_path = Path(bank_dir)
    if not bank_path.is_dir():
        raise ValueError(f"Skill bank directory does not exist: {bank_dir}")

    warnings: list[str] = []
    team = spec.get("team") or {}
    all_roles = list(team.get("build_roles") or []) + list(team.get("eval_roles") or [])
    for role_spec in all_roles:
        role_skills = role_spec.get("skills") or []
        if not role_skills:
            continue
        role = _validate_safe_segment(str(role_spec["role"]), "role")
        if len(role_skills) > MAX_SKILLS_PER_ROLE:
            warnings.append(
                f"Role '{role}' declares {len(role_skills)} skills (exceeds {MAX_SKILLS_PER_ROLE} recommended cap)."
            )
        skills_root = (Path(out_dir) / "templates" / role / "skills").resolve()
        for raw_skill in role_skills:
            skill_name = _validate_safe_segment(str(raw_skill), "skill")
            src = (bank_path / skill_name).resolve()
            if not src.is_relative_to(bank_path.resolve()) or not (src / "SKILL.md").is_file():
                raise ValueError(f"Role '{role}' references missing skill '{skill_name}' in {bank_dir}")
            dst = (skills_root / skill_name).resolve()
            if not dst.is_relative_to(skills_root):
                raise ValueError(f"Unsafe destination path for skill '{skill_name}'")
            if dst.exists():
                shutil.rmtree(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dst, symlinks=False)
    return warnings


def apply_strict_delegation(spec: dict[str, Any], out_dir: str | Path) -> list[str]:
    """Write .claude/settings.json denying in-process Agent/Task tools on Claude leads."""
    team = spec.get("team") or {}
    if not team.get("strict_scion_delegation"):
        return []

    written: list[str] = []
    all_roles = list(team.get("build_roles") or []) + list(team.get("eval_roles") or [])
    for role_spec in all_roles:
        raw_role = str(role_spec.get("role") or "")
        if not raw_role:
            continue
        role = _validate_safe_segment(raw_role, "role")
        kind = str(role_spec.get("default_kind") or role)
        harness = str(role_spec.get("harness") or "")
        if harness != "claude":
            continue
        if kind not in ("lead", "eval-lead") and role not in ("lead", "eval-lead"):
            continue

        rel = f"templates/{role}/home/.claude/settings.json"
        settings_path = Path(out_dir) / rel
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {}
        if settings_path.is_file():
            try:
                data = json.loads(settings_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
        perms = data.setdefault("permissions", {})
        deny = perms.setdefault("deny", [])
        for tool_name in ("Agent", "Task"):
            if tool_name not in deny:
                deny.append(tool_name)
        settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        written.append(rel)
    return written
