#!/usr/bin/env python3
"""Owner-invoked live hot-upgrade tool for running Scion factory projects.

Why this module exists:
Template edits on Scion Hub affect only newly created or reincarnated containers (Lesson L16).
Operators must never run `docker restart` on `--rm` agent containers (`AGENTS.md` Rule 4).
Host-started specialists also break `lead -> specialist` ancestry and Hub status notifications.
By default, this tool runs in dry-run mode and creates only missing Hub templates (`--overwrite-templates`
opts into overwriting existing templates). With `--apply`, it coordinates three safe live steps:
1. Push new templates through `scripts/push-templates.py` without clobbering live owner edits by default.
2. Send `[HOT-UPGRADE RULE DELTA]` messages to running agents so active turns adopt new rules.
3. Ask the running `lead` agent to `scion start` new specialists and preserve child ancestry.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from hub_client import HubClient  # noqa: E402

_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _load_env_file(env_file: Path | None) -> dict[str, str]:
    """Read the environment file into a dictionary."""
    env: dict[str, str] = {}
    if env_file and env_file.exists():
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("'\"")
    return env


def _validate_slug(value: str, label: str) -> str:
    cleaned = value.strip()
    if not _SLUG_RE.match(cleaned):
        raise ValueError(f"invalid {label} {value!r}: must match ^[a-z0-9][a-z0-9-]*$")
    return cleaned


def parse_spawn_specs(raw_items: list[str] | tuple[str, ...]) -> list[tuple[str, str]]:
    """Parse `template[:agent_name]` entries into validated `(template_slug, agent_slug)` pairs."""
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in raw_items:
        for token in [t.strip() for t in item.split(",") if t.strip()]:
            tpl_raw, agent_raw = token.split(":", 1) if ":" in token else (token, token)
            tpl, agent = _validate_slug(tpl_raw, "spawn template"), _validate_slug(agent_raw, "spawn agent")
            if agent not in seen:
                pairs.append((tpl, agent))
                seen.add(agent)
    return pairs


def _extract_template_slugs(data: Any) -> set[str]:
    """Extract the template slugs from the Hub API response."""
    items = data if isinstance(data, list) else []
    if isinstance(data, dict):
        items = data.get("templates") if isinstance(data.get("templates"), list) else (data.get("items") or [])
    return {
        str(t[k])
        for t in items
        if isinstance(t, dict)
        for k in ("slug", "name")
        if t.get(k)
    }


def _normalize_agents(raw_agents: list[Any]) -> list[dict[str, Any]]:
    """Convert agent strings into agent dictionaries."""
    out: list[dict[str, Any]] = []
    for item in raw_agents:
        if isinstance(item, dict):
            out.append(item)
        elif isinstance(item, str) and item.strip():
            out.append({"slug": item.strip(), "phase": "running"})
    return out


def _resolve_lead_slug(agents: list[dict[str, Any]], explicit_lead: str = "") -> str:
    """Find the lead agent slug. If the explicit lead is not empty, use the explicit lead."""
    if explicit_lead:
        return _validate_slug(explicit_lead, "lead agent")
    for a in agents:
        slug, tpl = str(a.get("slug") or a.get("name") or ""), str(a.get("template") or "")
        if slug == "lead" or tpl == "lead" or (slug.endswith("-lead") and not slug.endswith("-eval-lead")):
            return slug
    return "lead"


def run_hot_upgrade(
    *,
    project_slug: str,
    templates_dir: Path,
    env_file: Path | None = None,
    roles: list[str] | None = None,
    overwrite_templates: bool = False,
    rule_delta: str = "",
    notify_agents: list[str] | None = None,
    spawn_specs: list[tuple[str, str]] | None = None,
    lead_slug: str = "",
    branch: str = "",
    harness_auth: str = "",
    apply: bool = False,
) -> dict[str, Any]:
    """Execute or preview a three-step live upgrade against a running Hub project."""
    project = _validate_slug(project_slug, "project slug")
    role_list = [_validate_slug(r, "role") for r in (roles or []) if r.strip()]
    notify_list = [_validate_slug(a, "agent") for a in (notify_agents or []) if a.strip()]
    spawns = list(spawn_specs or [])

    for r in role_list:
        if not (templates_dir / r).is_dir():
            raise ValueError(f"template directory not found for role {r!r}: {templates_dir / r}")

    eff_env = {**_load_env_file(env_file), **os.environ}
    if env_file and not eff_env.get("FACTORY_ENV"):
        eff_env["FACTORY_ENV"] = str(env_file)
    if str(os.environ.get("DRY_RUN", "")).strip().lower() in ("1", "true", "yes"):
        apply = False

    client = HubClient.from_env(eff_env)
    pid = client.project_id(project)
    agents = _normalize_agents(client.list_agents(pid))
    running_by_slug = {
        str(a.get("slug") or a.get("name")): a
        for a in agents
        if str(a.get("phase") or a.get("status") or "").lower() == "running" and (a.get("slug") or a.get("name"))
    }
    all_agent_slugs = {str(a.get("slug") or a.get("name")) for a in agents if (a.get("slug") or a.get("name"))}

    roles_to_push = list(role_list)
    skipped_existing: list[str] = []
    if roles_to_push and not overwrite_templates:
        st, tpl_data = client.request("GET", f"/api/v1/templates?scope=project&scopeId={pid}&projectId={pid}")
        existing_tpls = _extract_template_slugs(tpl_data) if 200 <= st < 300 else set()
        skipped_existing = [r for r in roles_to_push if r in existing_tpls]
        roles_to_push = [r for r in roles_to_push if r not in existing_tpls]

    if roles_to_push:
        cmd = [
            sys.executable, str(ROOT_DIR / "scripts" / "push-templates.py"),
            "--project", project, "--roles", ",".join(roles_to_push), "--templates", str(templates_dir),
        ]
        if eff_env.get("HUB_URL"):
            cmd.append("--direct")
        if apply:
            cmd.append("--apply")
        subprocess.run(cmd, cwd=ROOT_DIR, env={**os.environ, **eff_env}, check=True, stdout=sys.stderr)

    delta_text = rule_delta.strip()
    targets: list[str] = []
    if delta_text:
        if notify_list:
            missing_running = [a for a in notify_list if a not in running_by_slug]
            if missing_running:
                raise ValueError(f"requested --notify-agents not running in project {project!r}: {missing_running}")
            targets = list(notify_list)
        else:
            role_set = set(role_list)
            targets = sorted(
                slug for slug, rec in running_by_slug.items()
                if slug in role_set or str(rec.get("template") or "") in role_set
            )
        if apply:
            body = {"message": f"[HOT-UPGRADE RULE DELTA] Apply immediately in active and future turns:\n{delta_text}"}
            for slug in targets:
                client.request("POST", f"/api/v1/projects/{pid}/agents/{slug}/message", body=body)

    resolved_lead = _resolve_lead_slug(agents, lead_slug)
    pending_spawns = [(tpl, ag) for tpl, ag in spawns if ag not in all_agent_slugs]
    skipped_spawns = [ag for _tpl, ag in spawns if ag in all_agent_slugs]
    spawn_commands: list[str] = []
    commands_by_parent: dict[str, list[str]] = {}
    for tpl, ag in pending_spawns:
        parts = ["scion", "start", ag, "-t", tpl]
        if branch.strip():
            parts.extend(["-b", branch.strip()])
        parts.append("Read your instructions, then run your wake-up steps.")
        cmd_str = " ".join(shlex.quote(p) for p in parts)
        if harness_auth:
            cmd_str += f" --harness-auth {shlex.quote(harness_auth)}"
        tpl_yaml = templates_dir / tpl / "scion-agent.yaml"
        tpl_text = tpl_yaml.read_text(encoding="utf-8") if tpl_yaml.exists() else ""
        if "gcp_identity" in tpl_text:
            cmd_str += ' ${FACTORY_DEPLOYER_SA_ID:+--service-account "$FACTORY_DEPLOYER_SA_ID"}'
        m_parent = re.search(r'^\s*FACTORY_PARENT_ROLE:\s*"?([a-z0-9-]+)"?', tpl_text, flags=re.MULTILINE)
        parent_role = m_parent.group(1) if m_parent else ""
        if parent_role:
            parent_slug = next(
                (s for s, rec in running_by_slug.items()
                 if s == parent_role or str(rec.get("template") or "") == parent_role or s.endswith(f"-{parent_role}")),
                parent_role,
            )
        else:
            parent_slug = resolved_lead
        commands_by_parent.setdefault(parent_slug, []).append(cmd_str)
        spawn_commands.append(cmd_str)

    lead_notified = False
    if spawn_commands:
        for parent_slug, p_cmds in commands_by_parent.items():
            if parent_slug not in running_by_slug:
                label = "lead agent" if parent_slug == resolved_lead else "parent agent"
                raise RuntimeError(f"{label} {parent_slug!r} is not running in project {project!r}")
            if apply:
                directive = (
                    f"PO -> {parent_slug}: [HOT-UPGRADE STAFFING] Start new child specialist(s):\n"
                    + "\n".join(f"- `{cmd}`" for cmd in p_cmds)
                )
                client.request("POST", f"/api/v1/projects/{pid}/agents/{parent_slug}/message", body={"message": directive})
                lead_notified = True

    return {
        "ok": True, "apply": apply, "project": project, "project_id": pid,
        "overwrite_templates": overwrite_templates, "templates_pushed": roles_to_push,
        "skipped_existing_templates": skipped_existing, "notified_agents": targets,
        "lead_agent": resolved_lead if spawn_commands else None, "lead_notified": lead_notified,
        "spawn_commands": spawn_commands, "skipped_existing_agents": skipped_spawns,
    }


def main(argv: list[str] | None = None) -> int:
    """Parse the command-line arguments and run the hot upgrade."""
    parser = argparse.ArgumentParser(description="Live hot-upgrade for Scion templates, prompt rules, and child agents")
    parser.add_argument("--project", required=True, help="Target Scion Hub project slug")
    parser.add_argument("--templates", required=True, help="Compiled kit templates directory path (<kit>/templates)")
    parser.add_argument("--env", default="", help="Optional factory.env path")
    parser.add_argument("--roles", default="", help="Comma-separated template roles to push")
    parser.add_argument("--create-only", action="store_true", help="Default mode: create only missing Hub templates")
    parser.add_argument("--overwrite-templates", action="store_true", help="Overwrite existing Hub templates in --roles")
    parser.add_argument("--rule-delta", default="", help="Live prompt rule delta sent via Hub message")
    parser.add_argument("--rule-delta-file", default="", help="File containing a live prompt rule delta")
    parser.add_argument("--notify-agents", default="", help="Explicit comma-separated running agents to message")
    parser.add_argument("--spawn", action="append", default=[], help="Specialist to start via lead: template[:agent_name]")
    parser.add_argument("--lead", default="", help="Explicit lead agent slug")
    parser.add_argument("--branch", default="", help="Optional Git branch (-b) for per-agent specialist starts")
    parser.add_argument("--apply", action="store_true", help="Execute mutations (dry-run by default)")
    parser.add_argument("--json", action="store_true", help="Print JSON summary")
    args = parser.parse_args(argv)

    delta = Path(args.rule_delta_file).read_text(encoding="utf-8") if args.rule_delta_file else args.rule_delta
    try:
        report = run_hot_upgrade(
            project_slug=args.project, templates_dir=Path(args.templates),
            env_file=Path(args.env) if args.env else None,
            roles=[r for r in args.roles.split(",") if r.strip()],
            overwrite_templates=bool(args.overwrite_templates and not args.create_only),
            rule_delta=delta,
            notify_agents=[a for a in args.notify_agents.split(",") if a.strip()],
            spawn_specs=parse_spawn_specs(args.spawn), lead_slug=args.lead,
            branch=args.branch, apply=args.apply,
        )
    except (ValueError, RuntimeError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2) if args.json else f"[{'APPLY' if report['apply'] else 'DRY-RUN'}] {report['project']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
