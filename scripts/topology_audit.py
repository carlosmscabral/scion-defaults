"""Audit factory roster topology, workload balance, and container binary readiness.

This module inspects a FactorySpec before compilation to surface single-role wave
bottlenecks, Single-VM Docker container bloat, per-role skill overload, and a
deterministic container runtime checklist.
"""

from __future__ import annotations

from typing import Any

import scion_factory
import sub_coordinators

BOTTLENECK_SHARE_THRESHOLD = 0.60
DOCKER_ROLE_CAP = 10
ROLE_SKILL_CAP = 6


def _collect_waves(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the list of wave dictionaries from top-level waves or legacy backlog."""
    waves = spec.get("waves")
    if isinstance(waves, list):
        return waves
    legacy = (spec.get("backlog") or {}).get("waves")
    return legacy if isinstance(legacy, list) else []


def _check_owner_bottleneck(issues: list[dict[str, Any]], label: str) -> list[str]:
    """Return bottleneck warnings if one owner_role holds >60% of the given issues."""
    counts: dict[str, int] = {}
    total = 0
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        owner = str(issue.get("owner_role") or "").strip()
        if owner:
            counts[owner] = counts.get(owner, 0) + 1
            total += 1
    if total < 4:
        return []

    warnings: list[str] = []
    for owner, count in sorted(counts.items()):
        if (count / total) > BOTTLENECK_SHARE_THRESHOLD:
            warnings.append(
                f"Workload bottleneck in {label}: role '{owner}' owns {count}/{total} issues "
                "(>60%). Split this role into parallel domain roles using 'default_kind'."
            )
    return warnings


def audit(spec: dict[str, Any]) -> dict[str, Any]:
    """Evaluate workload balance, VM container sizing, and runtime binary checklist."""
    warnings: list[str] = []
    checklist: list[str] = []

    norm_spec = scion_factory.normalize_team(spec)[0] if spec.get("team") else spec
    team = norm_spec.get("team") or {}
    max_wip = int(team.get("max_wip") or 1)
    waves = _collect_waves(spec)

    if max_wip >= 3 and waves:
        all_issues: list[dict[str, Any]] = []
        for wave in waves:
            if isinstance(wave, dict):
                all_issues.extend(wave.get("issues") or [])
        warnings.extend(_check_owner_bottleneck(all_issues, "backlog"))
        if not warnings and isinstance(waves[0], dict):
            warnings.extend(_check_owner_bottleneck(waves[0].get("issues") or [], "Wave 1"))

    infra = spec.get("infrastructure") or {}
    runtime = str(infra.get("runtime") or "docker")
    build_roles = list(team.get("build_roles") or [])
    eval_roles = list(team.get("eval_roles") or [])
    total_roles = len(build_roles) + len(eval_roles)

    if runtime == "docker" and total_roles > DOCKER_ROLE_CAP:
        warnings.append(
            f"Single-VM Docker container bloat ({total_roles} active roles > {DOCKER_ROLE_CAP}). "
            "Prune unused defaults via 'team.dropped_defaults' and consolidate evaluation probes "
            "into 'domain-evaluator' to prevent host memory exhaustion."
        )

    hier_errors, hier_warnings = sub_coordinators.validate_hierarchy(norm_spec, scion_factory._team_lead)
    warnings.extend(hier_errors)
    warnings.extend(hier_warnings)

    for role_spec in build_roles + eval_roles:
        if not isinstance(role_spec, dict):
            continue
        skills = role_spec.get("skills") or []
        if len(skills) > ROLE_SKILL_CAP:
            warnings.append(
                f"Role '{role_spec.get('role', '?')}' attaches {len(skills)} skills (> {ROLE_SKILL_CAP}). "
                "Trim role skills to 3-6 focused packages to avoid context window pollution."
            )

    checklist.append(
        "End custom Scion harness Dockerfiles with 'USER root' so 'sciontool init' can remap container UIDs."
    )
    if runtime == "docker" and (spec.get("vcs") or {}).get("enabled") and max_wip > 1:
        checklist.append(
            "Verify Hub project uses 'workspaceMode: per-agent' so concurrent Docker agents get isolated Git clones."
        )

    stack = spec.get("stack") or {}
    cmds = f"{stack.get('build_command', '')} {stack.get('test_command', '')} {stack.get('lint_command', '')}"
    lang = str(stack.get("primary_language") or "").lower()
    has_frontend = any(
        str(r.get("default_kind") or r.get("role") or "") in ("frontend", "ui-auditor")
        for r in build_roles + eval_roles
        if isinstance(r, dict)
    )

    if lang in ("typescript", "javascript") or any(tok in cmds for tok in ("npm", "pnpm", "yarn", "node")):
        checklist.append(
            "Verify non-login container PATH exposes global npm/Node CLIs (or symlink into /usr/local/bin)."
        )
    if has_frontend or any(tok in cmds.lower() for tok in ("playwright", "puppeteer", "cypress")):
        checklist.append(
            "Verify container pre-installs system Chromium + shared libraries and sets "
            "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 and CHROMIUM_PATH=/usr/bin/chromium."
        )

    cli_tools: list[str] = []
    for candidate in ("uv", "pytest", "go", "cargo", "gcloud", "gh"):
        if f"{candidate} " in f"{cmds} ":
            cli_tools.append(candidate)
    if cli_tools:
        checklist.append(f"Verify required CLI binaries exist in container PATH: {', '.join(cli_tools)}.")

    return {"ok": len(hier_errors) == 0, "errors": hier_errors, "warnings": warnings, "checklist": checklist}
