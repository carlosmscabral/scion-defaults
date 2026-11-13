#!/usr/bin/env python3
"""Synthesize topology-bound Kaizen manifests and prompts at factory compile time.

Why this module exists:
- Scion factories vary stochastically across stacks, role rosters, version-control
  modes, and deployment targets (`service` vs `none`).
- Hardcoding fixed agent names or deployment steps inside static Kaizen prompts
  breaks library factories, non-Git factories, or custom verifier rosters.
- Running at the end of `compile_factory_boilerplate(spec)` binds continuous
  improvement invariants (Code-Over-Prose, 4 Admission Gates, Bounded Rule Cap,
  Fast NO-OP, and 3-Axis Coordinator Triage) to the resolved factory topology.
"""

from __future__ import annotations

from typing import Any

try:
    import role_library  # type: ignore
except ImportError:
    role_library = None  # type: ignore

_FALLBACK_KINDS = {
    "backend": "implementer",
    "frontend": "frontend",
    "test-engineer": "test-engineer",
    "reviewer": "reviewer",
    "security-auditor": "security-auditor",
    "performance-analyst": "performance-analyst",
    "deployer": "deployer",
    "release-manager": "release-manager",
    "kaizen-analyst": "kaizen-analyst",
    "sub-coordinator": "sub-coordinator",
    "eval-lead": "eval-lead",
}

PRODUCER_KINDS = {
    "implementer",
    "frontend",
    "migration-specialist",
    "doc-writer",
}
AUTOMATION_KINDS = {"test-engineer"}
VERIFIER_KINDS = {
    "reviewer",
    "security-auditor",
    "performance-analyst",
    "api-compat-checker",
}
RELEASER_KINDS = {"deployer", "release-manager"}
MAX_ACTIVE_PLAYBOOK_RULES = 15


def _role_kind(role_spec: dict[str, Any]) -> str:
    """Resolve the canonical capability kind for a build or eval role entry."""
    if role_library is not None and hasattr(role_library, "resolve_role_kind"):
        return role_library.resolve_role_kind(role_spec)
    explicit = str(role_spec.get("default_kind") or "").strip()
    if explicit:
        return explicit
    role_name = str(role_spec.get("role") or "").strip()
    if role_name.endswith("-sub-coordinator"):
        return "sub-coordinator"
    return str(_FALLBACK_KINDS.get(role_name) or role_name)


def _agent_name(spec: dict[str, Any], role: str) -> str:
    """Resolve the runtime agent slug for a role within the factory spec."""
    monorepo = spec.get("monorepo") or {}
    team = spec.get("team") or {}
    prefix = str(monorepo.get("agent_prefix") or team.get("agent_prefix") or "").strip()
    return f"{prefix}-{role}" if prefix else role


def build_kaizen_manifest(spec: dict[str, Any]) -> dict[str, Any]:
    """Derive a machine-readable Kaizen topology manifest from a resolved FactorySpec."""
    proj = spec.get("project") or {}
    stack = spec.get("stack") or {}
    infra = spec.get("infrastructure") or {}
    vcs = spec.get("vcs") or {}
    team = spec.get("team") or {}

    deploy = str(infra.get("deploy") or "service").strip() or "service"
    git_enabled = bool(vcs.get("enabled"))
    build_roles = list(team.get("build_roles") or [])
    eval_roles = list(team.get("eval_roles") or [])

    coordinator = "lead"
    sub_coords: list[str] = []
    producers: list[str] = []
    automators: list[str] = []
    verifiers: list[str] = []
    releasers: list[str] = []

    for idx, r in enumerate(build_roles):
        name = _agent_name(spec, str(r.get("role") or ""))
        kind = _role_kind(r)
        if idx == 0 or kind in ("lead", "coordinator"):
            coordinator = name
        elif kind == "sub-coordinator":
            sub_coords.append(name)
        elif kind in AUTOMATION_KINDS:
            automators.append(name)
        elif kind in VERIFIER_KINDS or "reviewer" in name or "auditor" in name:
            verifiers.append(name)
        elif kind in RELEASER_KINDS:
            if deploy != "none":
                releasers.append(name)
        elif kind in PRODUCER_KINDS or bool(r.get("writes", kind not in ("researcher", "investigator", "kaizen-analyst", "sub-coordinator"))):
            if kind not in ("kaizen-analyst", "researcher", "investigator", "sub-coordinator"):
                producers.append(name)

    if not automators:
        automators = list(producers)

    eval_lead = ""
    evaluators: list[str] = []
    for idx, r in enumerate(eval_roles):
        name = _agent_name(spec, str(r.get("role") or ""))
        kind = _role_kind(r)
        if idx == 0 or kind == "eval-lead":
            eval_lead = name
        else:
            evaluators.append(name)

    gates: list[str] = []
    for key in ("build_command", "test_command", "lint_command"):
        cmd = str(stack.get(key) or "").strip()
        if cmd and cmd not in gates:
            gates.append(cmd)
    subdir = str((spec.get("monorepo") or {}).get("subdir") or "").strip()
    kp_path = f"{subdir}/knowledge-plane/kp-check.py" if subdir else "knowledge-plane/kp-check.py"
    gates.append(f"python3 {kp_path}")

    return {
        "schema_version": 1,
        "build_project": str(proj.get("slug") or ""),
        "eval_project": str(proj.get("eval_slug") or ""),
        "deploy_mode": deploy,
        "workflow_mode": "git_commit_slot" if git_enabled else "shared_edit_slot",
        "repo": str(vcs.get("repo") or "") if git_enabled else "",
        "build_branch": str(vcs.get("build_branch") or "main") if git_enabled else "",
        "coordinator_role": coordinator,
        "sub_coordinator_roles": sub_coords,
        "producer_roles": producers,
        "automation_roles": automators,
        "verifier_roles": verifiers,
        "releaser_roles": releasers,
        "eval_lead_role": eval_lead,
        "evaluator_roles": evaluators,
        "executable_gates": gates,
        "max_active_rules": MAX_ACTIVE_PLAYBOOK_RULES,
    }


def render_kaizen_analyst_protocol(spec: dict[str, Any]) -> str:
    """Render the compile-time Kaizen analyst protocol tailored to the resolved spec."""
    m = build_kaizen_manifest(spec)
    lead = m["coordinator_role"]
    gates_str = ", ".join(f"`{g}`" for g in m["executable_gates"])
    verifiers_str = ", ".join(f"`{v}`" for v in m["verifier_roles"]) or "verifier roles"
    automators_str = ", ".join(f"`{a}`" for a in m["automation_roles"]) or "implementer roles"

    flow_bullets = [
        "- Inspect `flow_latencies_24h` (`verifier_turnaround_min`, `slot_hold_min`) and `operational_friction_24h` "
        "(`max_unanswered_directive_lag_min`, `phase_drift_agents`) alongside `verdict_artifacts_24h`.",
        "- Never propose assigning a second concurrent slice to a producer while its prior slice awaits verification, "
        "because long active turns block incoming slot grants.",
    ]
    if len(m["verifier_roles"]) >= 2:
        flow_bullets.append(
            f"- Check load balance across {verifiers_str} in `verifier_inbound_messages_24h` (target 40-60% share when replicas are healthy)."
        )
    else:
        flow_bullets.append(
            f"- Monitor single-verifier queue wait on {verifiers_str} (`verifier_turnaround_min`). Propose a second verifier replica only when sustained queue wait blocks throughput."
        )
    if m["deploy_mode"] != "none" and m["releaser_roles"]:
        rels = ", ".join(f"`{r}`" for r in m["releaser_roles"])
        flow_bullets.append(
            f"- Check `solo_release_serialization` and `landings_per_release` across {rels}. When multiple non-urgent slices serialize on single-commit releases, propose release batching."
        )

    intake_line = (
        f"File at most one `kaizen-proposal` issue per 24h window in `{m['repo']}` at `status:ready` "
        f"(`--label kaizen-proposal --label status:ready --label factory:{m['build_project']}`, adding `--label kaizen-eval` when `[eval]` items exist)."
        if m["workflow_mode"] == "git_commit_slot"
        else f"Write at most one consolidated proposal to `/scion-volumes/scratchpad/kaizen-$(date -u +%F).md` and notify `{lead}`."
    )
    flow_block = "\n".join(flow_bullets)

    return f"""You run read-only 24-hour continuous-improvement (Kaizen) sweeps bound to this factory's compiled topology (`/scion-volumes/scratchpad/telemetry/hub-24h.json` and `/scion-volumes/scratchpad/telemetry/kaizen-manifest.json` or `config/kaizen-manifest.json`).

### 1. Day+1 Lifecycle Audit & Pruning (Mandatory First Step)
Read `/scion-volumes/scratchpad/PLAYBOOK-KAIZEN.md` and the prior Kaizen proposal first. Grade each prior `KZ-*` item (`ADOPTED & MEASURED`, `PARTIAL`, or `UNADOPTED`) and classify each standing rule:
- `VALIDATED`: enforced by an executable gate ({gates_str}) on trunk with zero recurrences -> propose `retire_rules` tagged `DUPLICATED (graduated to <path>)`.
- `CONTRADICTED`: the targeted defect recurred despite a prose rule -> do NOT add more prose. Promote enforcement to a deterministic script, linter, or automated test with mutation proof owned by {automators_str}.
- `WRONG` or `POINTLESS`: superseded by a product decision or completed wave -> propose `retire_rules`.

### 2. Fast `NO-OP` Gate
If zero escaped defects occurred in the window, `verdict_artifacts_24h.fail_files <= verdict_artifacts_24h.pass_files * 0.25`, `max_unanswered_directive_lag_min < 15`, and `solo_release_serialization` is false, DO NOT propose new rules. Emit a concise 3-line `[kaizen no-op]` summary (plus any `retire_rules` cleanup) to `{lead}` and complete your turn.

### 3. Four Admission Gates & Code-Over-Prose Law
Propose at most **3 atomic items total** (never split an item into `01a/01b/01c` sub-rules). Every item must state its `[build]` or `[eval]` scope, owner role, executable gate, and mutation proof, and pass:
1. **Premise & Corroboration:** Backed by `N >= 2` occurrences or `1` escaped evaluation/user defect, cross-checking `hub-24h.json` against disk files in `/scion-volumes/scratchpad/reviews/` (`sensor_drift_warning`).
2. **Code Over Prose (Single-Home Rule):** Route durable checks into executable CI gates ({gates_str}) via {automators_str}. **Never add manual re-execution sweeps or manual mutation rituals to {verifiers_str} when an automated check can enforce the invariant.**
3. **Topology-Aware Flow Diagnostics:**
{flow_block}
4. **Bounded Playbook Cap (`<= {m['max_active_rules']}` active rules):** Keep `/scion-volumes/scratchpad/PLAYBOOK-KAIZEN.md` as the single canonical home (`<= 40` words per rule, zero hardcoded issue numbers inside rule imperatives). When at the cap, retire or merge a lower-value rule for each new rule proposed.

### 4. Handoff Discipline
{intake_line}
Never modify product code, never self-approve, and never message busy specialists directly. Send your summary only to `{lead}`."""


def render_coordinator_kaizen_rubric(spec: dict[str, Any]) -> str:
    """Render the 3-Axis Kaizen Triage Rubric for the build coordinator."""
    m = build_kaizen_manifest(spec)
    verifiers_str = ", ".join(f"`{v}`" for v in m["verifier_roles"]) or "verifier roles"
    batch_clause = (
        " and batch `>= 2` verified non-urgent slices per deployment when `solo_release_serialization` is true"
        if m["deploy_mode"] != "none" and m["releaser_roles"]
        else ""
    )
    return f"""### Kaizen 3-Axis Triage Rubric
Evaluate each `kaizen-proposal` item across three axes before updating `/scion-volumes/scratchpad/PLAYBOOK-KAIZEN.md` (the single canonical Kaizen rule file, capped at `<= {m['max_active_rules']}` active rules):
1. **Premise:** Verify cited counts against `/scion-volumes/scratchpad/reviews/` and trunk history. Reject telemetry artifacts or isolated `N = 1` nits.
2. **Single-Home (`Code Over Prose`):** Reject any manual ritual on {verifiers_str} that duplicates an executable gate (`{m['executable_gates'][0]}` / test suite), and retire prose rules from `PLAYBOOK-KAIZEN.md` as soon as their automated gate lands on trunk.
3. **Cycle-Time Cost vs. Milestone Phase:** Reject ceremony that adds disproportionate minutes per slice during polish or pre-milestone windows{batch_clause}.
"""
