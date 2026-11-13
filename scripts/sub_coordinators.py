"""Validate and route optional 3-tier domain sub-coordinator hierarchies.

Why this module exists:
- Upstream Scion (`resources/platform_skills/team-creation/SKILL.md` and `Agent.Ancestry`)
  tracks multi-level agent creators (`user -> lead -> sub-coordinator -> specialist`),
  rendering the hierarchy in the Hub tree view and routing child notifications to the immediate creator.
- Small and medium factories (`< 8` non-lead build roles or Single-VM `runtime: docker`) run best
  with a flat 2-tier team (`lead -> specialists`), avoiding extra hop latency and coordinator tokens.
- Large multi-subsystem repositories (`>= 3` disjoint domains or `>= 8` non-lead build roles) can opt
  into domain `sub-coordinator` nodes (`default_kind: sub-coordinator`) that manage domain builders
  and auditors (`reports_to: <sub-coordinator>`) while the root `lead` retains sole trunk-merge,
  release, and PO-escalation authority.
"""

from __future__ import annotations

from typing import Any, Callable

import role_library

SUB_COORDINATOR_KIND = "sub-coordinator"
ROOT_ONLY_KINDS = frozenset({
    "lead",
    "eval-lead",
    "coordinator",
    "deployer",
    "eval-deployer",
    "kaizen-analyst",
    "release-manager",
    "evaluator",
})
CROSS_CUTTING_KINDS = ROOT_ONLY_KINDS
MIN_RECOMMENDED_BUILD_ROLES = 8


def role_kind(role_spec: dict[str, Any]) -> str:
    """Return the resolved capability kind for a team roster entry."""
    return role_library.resolve_role_kind(role_spec)


def is_sub_coordinator(role_spec: dict[str, Any]) -> bool:
    """Return True if the roster entry is a domain sub-coordinator."""
    return role_kind(role_spec) == SUB_COORDINATOR_KIND


def resolve_parent_role(team: list[dict[str, Any]], role_spec: dict[str, Any], root_lead_role: str) -> str:
    """Return the immediate parent role slug (`reports_to` if valid, else `root_lead_role`)."""
    raw_parent = str(role_spec.get("reports_to") or "").strip()
    if not raw_parent:
        return root_lead_role
    by_name = {str(r.get("role") or ""): r for r in team if isinstance(r, dict)}
    target = by_name.get(raw_parent)
    if target is not None and is_sub_coordinator(target):
        return raw_parent
    return root_lead_role


def direct_children(team: list[dict[str, Any]], parent_role: str, *, is_root_lead: bool) -> list[dict[str, Any]]:
    """Return the roster entries started directly by `parent_role`."""
    children: list[dict[str, Any]] = []
    by_name = {str(r.get("role") or ""): r for r in team if isinstance(r, dict)}
    for r in team:
        if not isinstance(r, dict):
            continue
        role = str(r.get("role") or "")
        if not role or role == parent_role:
            continue
        rep = str(r.get("reports_to") or "").strip()
        if is_root_lead:
            if not rep or rep not in by_name or not is_sub_coordinator(by_name[rep]):
                children.append(r)
        elif rep == parent_role:
            children.append(r)
    return children


def subtree_reviewer_slug(
    spec: dict[str, Any],
    team: list[dict[str, Any]],
    role_spec: dict[str, Any],
    root_lead_role: str,
    agent_fn: Callable[[dict[str, Any], str], str],
) -> str:
    """Return the reviewer slug for `role_spec`: its subtree reviewer if present, else the root reviewer."""
    parent_role = resolve_parent_role(team, role_spec, root_lead_role)
    if parent_role != root_lead_role:
        for ch in direct_children(team, parent_role, is_root_lead=False):
            if role_kind(ch) == "reviewer":
                return agent_fn(spec, str(ch["role"]))
    for r in direct_children(team, root_lead_role, is_root_lead=True):
        if role_kind(r) == "reviewer":
            return agent_fn(spec, str(r["role"]))
    for r in team:
        if isinstance(r, dict) and role_kind(r) == "reviewer":
            return agent_fn(spec, str(r["role"]))
    return agent_fn(spec, "reviewer")


def root_sizing_agents(
    spec: dict[str, Any],
    team: list[dict[str, Any]],
    root_lead_role: str,
    agent_fn: Callable[[dict[str, Any], str], str],
) -> dict[str, str]:
    """Build `{kind: backticked_agent}` for root lead sizing without naming subtree leaves directly.

    Root-level specialists map directly by kind. If a specialist kind exists only inside a
    domain subtree, the kind maps to its owning sub-coordinator so the root lead delegates there.
    """
    agents: dict[str, str] = {}
    for r in direct_children(team, root_lead_role, is_root_lead=True):
        agents.setdefault(role_kind(r), f"`{agent_fn(spec, str(r['role']))}`")
    for r in team:
        if not isinstance(r, dict):
            continue
        parent = resolve_parent_role(team, r, root_lead_role)
        if parent != root_lead_role:
            agents.setdefault(role_kind(r), f"`{agent_fn(spec, parent)}`")
    return agents


def _paths_overlap(paths_a: list[str], paths_b: list[str]) -> bool:
    """Return True if two repo-relative path lists overlap or contain `.`."""
    norm_a = [p.strip().strip("/") for p in paths_a if p.strip()]
    norm_b = [p.strip().strip("/") for p in paths_b if p.strip()]
    if not norm_a or not norm_b:
        return True
    for a in norm_a:
        for b in norm_b:
            if a in ("", ".") or b in ("", ".") or a == b:
                return True
            if a.startswith(f"{b}/") or b.startswith(f"{a}/"):
                return True
    return False


def validate_hierarchy(
    spec: dict[str, Any],
    root_lead_fn: Callable[[list[dict[str, Any]], str], str],
) -> tuple[list[str], list[str]]:
    """Validate optional 3-tier sub-coordinator invariants across build and eval rosters."""
    errors: list[str] = []
    warnings: list[str] = []
    team_cfg = spec.get("team") or {}
    runtime = str((spec.get("infrastructure") or {}).get("runtime") or "docker").strip().lower()

    for key, pref_lead in (("build_roles", "lead"), ("eval_roles", "eval-lead")):
        roster = [r for r in (team_cfg.get(key) or []) if isinstance(r, dict)]
        if not roster:
            continue
        root_role = root_lead_fn(roster, pref_lead)
        by_name = {str(r.get("role") or ""): r for r in roster if r.get("role")}
        sub_coords = [r for r in roster if is_sub_coordinator(r) and str(r.get("role") or "") != root_role]
        children_by_sub: dict[str, list[str]] = {str(sc["role"]): [] for sc in sub_coords}

        for r in roster:
            role = str(r.get("role") or "")
            rep = str(r.get("reports_to") or "").strip()
            if not rep:
                continue
            if role == root_role:
                errors.append(f"team.{key}: root lead '{role}' cannot set reports_to")
                continue
            if is_sub_coordinator(r):
                errors.append(
                    f"team.{key}: sub-coordinator '{role}' cannot set reports_to "
                    "(maximum hierarchy depth is 3 tiers: lead -> sub-coordinator -> specialist)"
                )
                continue
            if role_kind(r) in ROOT_ONLY_KINDS or r.get("gcp_identity"):
                errors.append(
                    f"team.{key}: role '{role}' ({role_kind(r)}) manages root leadership, release, or wave gates "
                    f"and must report directly to '{root_role}', not '{rep}'"
                )
                continue
            target = by_name.get(rep)
            if target is None or rep == role or not is_sub_coordinator(target):
                errors.append(
                    f"team.{key}: role '{role}' sets reports_to='{rep}', which must name a "
                    f"sub-coordinator role in team.{key}"
                )
                continue
            children_by_sub[rep].append(role)

        for sc in sub_coords:
            sc_role = str(sc["role"])
            if not children_by_sub.get(sc_role):
                errors.append(
                    f"team.{key}: sub-coordinator '{sc_role}' has zero specialists with "
                    f"reports_to: '{sc_role}' (assign at least 1 domain specialist or remove '{sc_role}')"
                )

        if key == "build_roles" and sub_coords:
            non_lead_count = sum(1 for r in roster if str(r.get("role") or "") != root_role)
            if non_lead_count < MIN_RECOMMENDED_BUILD_ROLES:
                warnings.append(
                    f"Sub-coordinators are configured on a small build roster ({non_lead_count} non-lead roles). "
                    f"Prefer the default 2-tier flat team when fewer than {MIN_RECOMMENDED_BUILD_ROLES} non-lead roles are active."
                )
            if runtime == "docker":
                warnings.append(
                    "Sub-coordinators add extra containers on Single-VM 'runtime: docker' (<= 10 container budget). "
                    "Prefer the flat 2-tier team on Single-VM Docker or switch to 'runtime: kubernetes'."
                )

        for idx, sc_a in enumerate(sub_coords):
            paths_a = list(((sc_a.get("focus") or {}).get("paths") or []))
            if not paths_a:
                warnings.append(
                    f"Sub-coordinator '{sc_a['role']}' omits explicit focus.paths. "
                    "Assign disjoint directory scopes to prevent cross-domain merge conflicts."
                )
                continue
            for sc_b in sub_coords[idx + 1:]:
                paths_b = list(((sc_b.get("focus") or {}).get("paths") or []))
                if paths_b and _paths_overlap(paths_a, paths_b):
                    warnings.append(
                        f"Sub-coordinators '{sc_a['role']}' ({paths_a}) and '{sc_b['role']}' ({paths_b}) "
                        "have overlapping focus.paths. Partition domains into disjoint directories."
                    )

    return errors, warnings
