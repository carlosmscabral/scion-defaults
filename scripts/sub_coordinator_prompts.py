"""Render prompt sections (`Chain of command`, `Operating Loop`, and lead routing) for 3-tier sub-coordinators.

Why this module exists:
- A 3-tier Scion hierarchy (`lead -> sub-coordinator -> specialist`) stays deadlock-free only when
  every prompt section (`Role protocol`, `Chain of command`, `Operating Loop`, and root `Sizing`)
  agrees on who dispatches briefs, who reviews branches, who relays verdicts, and who grants trunk slots.
- Keeping 3-tier prompt templates in a dedicated module preserves single-responsibility boundaries
  and keeps `sub_coordinators.py` and `scion_factory.py` concise.
"""

from __future__ import annotations

from typing import Any, Callable

import sub_coordinators


def render_root_lead_subtrees_note(
    spec: dict[str, Any],
    team: list[dict[str, Any]],
    root_lead_role: str,
    agent_fn: Callable[[dict[str, Any], str], str],
    *,
    vcs_enabled: bool = True,
) -> str:
    """Render the domain routing and slot/merge relay guide injected into the root lead prompt."""
    sub_coords = [
        r for r in team
        if isinstance(r, dict) and sub_coordinators.is_sub_coordinator(r) and r.get("role") != root_lead_role
    ]
    if not sub_coords:
        return ""
    slot_name = "commit slot" if vcs_enabled else "edit slot"
    lines = [
        "\n### Domain sub-coordinators (3-tier delegation)",
        "Delegate domain-scoped work to the owning sub-coordinator rather than starting or messaging its leaf specialists directly.",
        f"You retain sole authority over trunk merges, {slot_name}s, deployments, and `status:po-decision` escalations.",
        f"When a sub-coordinator relays a verified `{slot_name}` request (or a `PASS` `<N>-review.md` verdict in delta mode), "
        f"grant the {slot_name} to the named specialist with a copy to the sub-coordinator (or merge the branch in delta mode), "
        "and never stop a sub-coordinator while any of its subtree children is running:",
    ]
    for sc in sub_coords:
        sc_role = str(sc["role"])
        sc_slug = agent_fn(spec, sc_role)
        paths = ((sc.get("focus") or {}).get("paths") or ["."])
        kids = [f"`{agent_fn(spec, str(c['role']))}`" for c in sub_coordinators.direct_children(team, sc_role, is_root_lead=False)]
        paths_str = ", ".join(f"`{p}`" for p in paths)
        lines.append(f"- `{sc_slug}` (scope: {paths_str}) → manages {', '.join(kids) or 'none'}")
    return "\n".join(lines) + "\n"


def render_sub_coordinator_chain(
    *,
    spec: dict[str, Any],
    role_spec: dict[str, Any],
    team: list[dict[str, Any]],
    root_lead_slug: str,
    agent_fn: Callable[[dict[str, Any], str], str],
    team_branch: str,
    omit_vertex_harness_auth: bool,
    is_delta: bool,
    is_eval: bool,
    vcs_enabled: bool,
    briefs_dir: str,
    reports_dir: str,
    durability_rule: str,
) -> str:
    """Render the `## Chain of command` section for a domain sub-coordinator."""
    my_role = str(role_spec["role"])
    my_slug = agent_fn(spec, my_role)
    kids = sub_coordinators.direct_children(team, my_role, is_root_lead=False)
    ref = "#N" if vcs_enabled else "<item ID>"
    rows: list[str] = []
    for ch in kids:
        ch_slug = agent_fn(spec, str(ch["role"]))
        cmd = f"scion start {ch_slug} -t {ch['role']}"
        if team_branch:
            cmd += f" -b {team_branch}"
        if not omit_vertex_harness_auth:
            cmd += " --harness-auth vertex-ai"
        start_prompt = (
            f"{my_slug} -> {ch_slug}: {ref}: read {briefs_dir}/<issue>-{ch_slug}.md"
            if is_delta
            else "Read your instructions, then run your wake-up steps."
        )
        rows.append(f'| `{ch_slug}` | `{cmd} "{start_prompt}"` |')
    table = "\n".join(rows) or "| *(none assigned)* | *(assign via `reports_to` in `factory.yaml`)* |"

    vcs = spec.get("vcs") or {}
    has_eval = bool(str((spec.get("project") or {}).get("eval_slug") or "").strip())
    target_branch = (vcs.get("eval_branch") if (is_eval and has_eval) else vcs.get("build_branch")) or "main"

    if is_delta:
        return f"""## Chain of command
You report to `{root_lead_slug}` and coordinate the `{my_slug}` domain subtree in ephemeral delta mode. Every specialist you start from the table below is your Scion child, so the Hub routes its status notifications directly to you.

| Domain specialist | Start it with |
|---|---|
{table}

- **On-demand subtree staffing (single-turn launch):** Write each child brief to `{briefs_dir}/<issue>-<specialist>.md` before starting `<specialist>` with the table command above (never send a duplicate follow-up `scion message` right after `scion start`). Use `scion message <specialist> "{my_slug} -> <specialist>: {ref}: read {briefs_dir}/<file>"` only when `<specialist>` is already `running` or right after `scion resume`.
- **Domain integration & upward handoff:** Collect your specialists' branch commits and `{reports_dir}/<N>-review.md` verdicts, verify domain acceptance checks pass, stop idle child pods (`scion stop <specialist>`), and relay `PASS` with the verified branch SHA and review report path up to `{root_lead_slug}` (`scion message {root_lead_slug} "{my_slug} -> {root_lead_slug}: PASS {ref} (<sha>): see {reports_dir}/<N>-review.md"`).
- **Hard boundaries (3-tier cap):** Never write product code yourself, never merge onto `{target_branch}`, never trigger deployments, never close issues, and never message `user:<PO_USER>` directly. Prepend `<!-- scion-agent: {my_slug} -->` to any GitHub comment.
- {durability_rule}

"""

    slot_boundary = (
        f"never grant `{target_branch}` commit slots or push to `{target_branch}` directly (request a commit slot from `{root_lead_slug}` on behalf of your verified child)"
        if vcs_enabled
        else f"never grant shared `/workspace` edit slots yourself (request an edit slot from `{root_lead_slug}` on behalf of your child)"
    )
    return f"""## Chain of command
You report to `{root_lead_slug}` and coordinate the `{my_slug}` domain subtree. Every specialist you start from the table below is your Scion child, so the Hub displays your subtree and sends child notifications directly to you.

| Domain specialist | Start it with |
|---|---|
{table}

- At wake-up, run `scion list` first. Start or `scion resume` only the domain specialists in your table above when work arrives from `{root_lead_slug}` (`{briefs_dir}/<issue>-{my_slug}.md`).
- Write scoped child briefs to `{briefs_dir}/<issue>-<specialist>.md` and dispatch via `scion message <specialist> "{my_slug} -> <specialist>: {ref}: read {briefs_dir}/<file>"` (never inline brief bodies).
- Verify your specialists' diffs, local test gates, and subtree audit reports before requesting a slot or escalating readiness to `{root_lead_slug}` (`scion message {root_lead_slug} "{my_slug} -> {root_lead_slug}: ..."`).
- **Hard boundaries (3-tier cap):** Start only the specialists in your table above. Never write product code yourself, {slot_boundary}, never run deployments, never close issues, and never message `user:<PO_USER>` directly (escalate blockers to `{root_lead_slug}`).
- {durability_rule}

"""


def render_sub_coordinator_loop(
    *,
    root_lead_slug: str,
    my_slug: str,
    kp_ref: str,
    test_command: str,
    briefs_dir: str,
    reports_dir: str,
    is_delta: bool,
    vcs_enabled: bool,
) -> str:
    """Render the dedicated `## Operating Loop` steps for a domain sub-coordinator."""
    slot = "commit slot" if vcs_enabled else "edit slot"
    if is_delta and vcs_enabled:
        return f"""1. Take domain work from the brief file named in `{root_lead_slug}`'s message (`{briefs_dir}/<N>-{my_slug}.md`), or inspect `/scion-volumes/scratchpad/STATE.json` on fresh startup before going idle (never pick unassigned backlog issues yourself).
2. Read {kp_ref} and the requirements and ADRs cited in the brief, then decompose subsystem slices across your assigned subtree specialists (`scion list`).
3. Write scoped child briefs to `{briefs_dir}/<N>-<specialist>.md` and start or message only the needed subtree specialists (never write product code yourself).
4. Collect your specialists' branch commits and `{reports_dir}/<N>-review.md` verdicts, verify `{test_command}` and domain invariants pass, and stop idle subtree pods (`scion stop <specialist>`).
5. Relay `PASS` (or unresolved `FAIL` / blocker) with the branch SHA and `{reports_dir}/<N>-review.md` path to `{root_lead_slug}`."""
    return f"""1. Take domain work only from the brief file named in `{root_lead_slug}`'s message (`{briefs_dir}/<issue>-{my_slug}.md`). Never pick unassigned work from the backlog yourself.
2. Read {kp_ref} and the requirements and ADRs cited in the brief, then inspect `scion list` for your subtree specialists.
3. Write scoped child briefs to `{briefs_dir}/<issue>-<specialist>.md` and dispatch your assigned subtree specialists (never write product code yourself).
4. Verify your specialists' results and `{test_command}` evidence, then request a `{slot}` from `{root_lead_slug}` on behalf of the verified specialist when a write must land.
5. Report verified domain deliverables, report paths, and any cross-domain blockers only to `{root_lead_slug}`."""
