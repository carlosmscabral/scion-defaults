#!/usr/bin/env python3
"""Export sanitized 24-hour Scion Hub telemetry into the project scratchpad.

Why this module exists:
- Agents inside Scion containers inspect Git and shared volumes, not the host SQLite file.
- Without flow latencies, verifier queue distribution, and disk-reconciled verdict counts,
  a Kaizen analyst cannot distinguish real bottlenecks from regex artifacts.
- This exporter runs with zero LLM calls via SQLite (Single-VM) or HubClient (Cloud Run HA / GKE)
  and writes sanitized JSON metrics to `/scion-volumes/scratchpad/telemetry/hub-24h.json`.
"""

from __future__ import annotations

import argparse, datetime, glob, json, os, sqlite3, sys, time
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from telemetry_flow_metrics import (  # noqa: E402
    SYMPTOM_PATTERNS,

    compute_flow_and_friction,
    scan_verdict_artifacts,
)

def open_hub_db(scion_home: str) -> sqlite3.Connection:
    """Open the Hub SQLite database in read-only mode."""
    conn = sqlite3.connect(f"file:{scion_home}/hub.db?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn

def load_kaizen_manifest(explicit_path: str = "", scion_home: str = "", build_slug: str = "") -> dict[str, Any] | None:
    """Load an optional compiled kaizen-manifest.json from explicit path or scratchpad."""
    candidates = [explicit_path] if explicit_path else []
    if scion_home and build_slug:
        pattern = os.path.join(
            scion_home, "project-configs", f"{build_slug}__*", "shared-dirs", "scratchpad", "telemetry", "kaizen-manifest.json"
        )
        candidates.extend(sorted(glob.glob(pattern)))
    for cpath in candidates:
        if cpath and os.path.isfile(cpath):
            try:
                return json.loads(Path(cpath).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
    return None

def _extract_model_info(cfg: Any, h: str | None) -> dict[str, str]:
    harness, model, p = (h or "").strip(), "", cfg
    if isinstance(cfg, str) and cfg:
        try: p = json.loads(cfg)
        except ValueError: p = None
    if isinstance(p, dict):
        harness = harness or str(p.get("harness_config") or p.get("default_harness_config") or "")
        model = str(p.get("model") or "")
    return {"harness": harness, "model": model}

def _summarize_messages(records: list[dict[str, Any]]) -> tuple[int, dict[str, int], list[dict[str, Any]]]:
    pair_totals, undispatched, symptoms = {}, 0, {k: 0 for k in SYMPTOM_PATTERNS}
    for r in records:
        key = (str(r.get("project") or ""), str(r.get("sender") or ""), str(r.get("recipient") or ""))
        pair_totals[key] = pair_totals.get(key, 0) + 1
        if str(r.get("dispatch_state") or "dispatched") not in ("dispatched", "delivered", "read"): undispatched += 1
        body = str(r.get("msg") or "")
        for n, p in SYMPTOM_PATTERNS.items():
            if p.search(body): symptoms[n] += 1
    top = [{"project": p, "sender": s, "recipient": r, "messages": c} for (p, s, r), c in sorted(pair_totals.items(), key=lambda x: x[1], reverse=True)[:20]]
    return undispatched, symptoms, top


def _assemble_payload(
    source_mode: str,
    project_slugs: list[str],
    window_hours: int,
    records: list[dict[str, Any]],
    roster: list[dict[str, Any]],
    scion_home: str,
    manifest: dict[str, Any] | None,
    session_metrics: list[dict[str, Any]],
    templates: list[str],
    staged_skills: list[str],
) -> dict[str, Any]:
    """Assemble the canonical v2 telemetry dictionary from normalized records and roster."""
    undispatched, symptoms, top_pairs = _summarize_messages(records)
    verifier_in, flow, friction = compute_flow_and_friction(records, roster, manifest)
    verdict_arts = scan_verdict_artifacts(
        scion_home, project_slugs[0], window_hours, symptoms["verdict_approve"], symptoms["verdict_fail"]
    )
    return {
        "schema_version": 2,
        "source_mode": source_mode,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "window_hours": window_hours,
        "projects": project_slugs,
        "total_messages_24h": len(records),
        "undispatched_messages_24h": undispatched,
        "verifier_inbound_messages_24h": verifier_in,
        "reviewer_inbound_messages_24h": verifier_in,
        "symptom_counts_24h": symptoms,
        "verdict_artifacts_24h": verdict_arts,
        "flow_latencies_24h": flow,
        "operational_friction_24h": friction,
        "top_message_pairs_24h": top_pairs,
        "session_metrics_24h": session_metrics,
        "roster": roster,
        "project_templates": templates,
        "scratchpad_staged_skills": staged_skills,
    }

def collect_hub_telemetry(
    conn: sqlite3.Connection,
    scion_home: str,
    project_slugs: list[str],
    window_hours: int = 24,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Query 24-hour roster, flow latencies, verdict artifacts, and token usage from hub.db."""
    ph = ",".join("?" for _ in project_slugs)
    proj_rows = conn.execute(f"SELECT id, slug FROM projects WHERE slug IN ({ph})", tuple(project_slugs)).fetchall()
    proj_by_id = {str(r["id"]): str(r["slug"]) for r in proj_rows}
    if not proj_by_id:
        raise RuntimeError(f"None of projects {project_slugs!r} exist in hub.db.")

    p_ph = ",".join("?" for _ in proj_by_id)
    p_ids = tuple(proj_by_id.keys())
    win = f"-{int(window_hours)} hours"
    agent_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(agents)").fetchall()}
    cs_col = "container_status" if "container_status" in agent_cols else "'' AS container_status"
    gen_col = "generation" if "generation" in agent_cols else "1 AS generation"
    er_col = "exit_reason" if "exit_reason" in agent_cols else "'' AS exit_reason"
    ac_col = "applied_config" if "applied_config" in agent_cols else "'' AS applied_config"
    hc_col = "harness_config" if "harness_config" in agent_cols else "'' AS harness_config"
    up_col = "updated" if "updated" in agent_cols else "'' AS updated"
    del_col = "deleted_at IS NULL" if "deleted_at" in agent_cols else "1"

    roster: list[dict[str, Any]] = []
    id_to_slug: dict[str, str] = {}
    for r in conn.execute(
        f"SELECT id, project_id, slug, template, phase, activity, {cs_col}, {gen_col}, {er_col}, "
        f"{ac_col}, {hc_col}, {up_col} FROM agents WHERE project_id IN ({p_ph}) AND {del_col} "
        f"ORDER BY project_id, slug",
        p_ids,
    ).fetchall():
        id_to_slug[str(r["id"])] = str(r["slug"])
        info = _extract_model_info(r["applied_config"], r["harness_config"])
        roster.append({
            "project": proj_by_id[str(r["project_id"])], "agent": str(r["slug"]),
            "template": str(r["template"] or ""), "harness": info["harness"], "model": info["model"],
            "phase": str(r["phase"] or ""), "activity": str(r["activity"] or ""),
            "container_status": str(r["container_status"] or ""), "generation": int(r["generation"] or 1),
            "exit_reason": str(r["exit_reason"] or ""), "updated": str(r["updated"] or ""),
        })

    records = [
        {
            "project": proj_by_id[str(r["project_id"])], "sender": str(r["sender"] or ""),
            "recipient": str(r["recipient"] or ""), "dispatch_state": str(r["dispatch_state"] or ""),
            "msg": str(r["msg"] or ""), "created": str(r["created"] or ""),
        }
        for r in conn.execute(
            f"SELECT project_id, sender, recipient, dispatch_state, msg, created FROM messages "
            f"WHERE project_id IN ({p_ph}) AND created >= datetime('now', ?)",
            (*p_ids, win),
        ).fetchall()
    ]
    try:
        sessions = [
            {
                "agent": id_to_slug.get(str(r["agent_id"]), str(r["agent_id"])[:8]), "model": str(r["model"] or ""),
                "sessions": int(r["sessions"] or 0), "turns": int(r["turns"] or 0),
                "tokens_in": int(r["tokens_in"] or 0), "tokens_out": int(r["tokens_out"] or 0),
                "tokens_cached": int(r["tokens_cached"] or 0),
            }
            for r in conn.execute(
                f"SELECT agent_id, model, COUNT(*) AS sessions, SUM(COALESCE(turn_count,0)) AS turns, "
                f"SUM(COALESCE(tokens_input,0)) AS tokens_in, SUM(COALESCE(tokens_output,0)) AS tokens_out, "
                f"SUM(COALESCE(tokens_cached,0)) AS tokens_cached FROM agent_session_metrics "
                f"WHERE grove_id IN ({p_ph}) AND created_at >= datetime('now', ?) GROUP BY agent_id, model ORDER BY tokens_in DESC",
                (*p_ids, win),
            ).fetchall()
        ]
    except sqlite3.OperationalError:
        sessions = []
    build_slug = project_slugs[0]
    skills_pat = os.path.join(scion_home, "project-configs", f"{build_slug}__*", "shared-dirs", "scratchpad", "skills", "*", "SKILL.md")
    skills = [os.path.basename(os.path.dirname(p)) for p in sorted(glob.glob(skills_pat))]
    try:
        tpls = sorted({str(r["slug"]) for r in conn.execute(f"SELECT slug FROM templates WHERE scope_id IN ({p_ph})", p_ids).fetchall()})
    except sqlite3.OperationalError:
        tpls = []
    eff_manifest = manifest or load_kaizen_manifest("", scion_home, build_slug)
    return _assemble_payload("sqlite", project_slugs, window_hours, records, roster, scion_home, eff_manifest, sessions, tpls, skills)

def collect_hub_telemetry_api(
    client: Any,
    project_slugs: list[str],
    window_hours: int = 24,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Query 24-hour roster, flow latencies, and message bottlenecks over the Hub REST API."""
    import hub_client

    projects = hub_client._extract_list(client.request("GET", "/api/v1/projects")[1], "projects", "items")
    proj_by_id = {str(p.get("id") or ""): str(p.get("slug") or "") for p in projects if str(p.get("slug") or "") in project_slugs and p.get("id")}
    if not proj_by_id:
        raise RuntimeError(f"None of projects {project_slugs!r} exist on Hub API.")

    roster: list[dict[str, Any]] = []
    for pid, slug in proj_by_id.items():
        for ag in hub_client._extract_list(client.request("GET", f"/api/v1/projects/{pid}/agents")[1], "agents", "items"):
            info = _extract_model_info(ag.get("appliedConfig"), ag.get("harnessConfig"))
            roster.append({
                "project": slug, "agent": str(ag.get("slug") or ag.get("name") or ""),
                "template": str(ag.get("template") or ""), "harness": info["harness"], "model": info["model"],
                "phase": str(ag.get("phase") or ""), "activity": str(ag.get("activity") or ""),
                "container_status": str(ag.get("containerStatus") or ""), "generation": int(ag.get("generation") or 1),
                "exit_reason": str(ag.get("exitReason") or ""), "updated": str(ag.get("updated") or ag.get("updatedAt") or ""),
            })
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=window_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    msgs_raw = hub_client._extract_list(client.request("GET", "/api/v1/messages?limit=500")[1], "messages", "items")
    records = [
        {
            "project": proj_by_id[str(m.get("projectId") or m.get("project_id") or "")],
            "sender": str(m.get("sender") or ""), "recipient": str(m.get("recipient") or ""),
            "dispatch_state": str(m.get("dispatchState") or m.get("dispatch_state") or "dispatched"),
            "msg": str(m.get("msg") or m.get("message") or ""),
            "created": str(m.get("createdAt") or m.get("created") or ""),
        }
        for m in msgs_raw
        if str(m.get("projectId") or m.get("project_id") or "") in proj_by_id
        and (not str(m.get("createdAt") or m.get("created") or "") or str(m.get("createdAt") or m.get("created") or "") >= cutoff)
    ]
    return _assemble_payload("api", project_slugs, window_hours, records, roster, "", manifest, [], [], [])

def write_telemetry_file(payload: dict[str, Any], scion_home: str, build_slug: str, explicit_output: str = "") -> list[str]:
    """Write hub-24h.json atomically to explicit_output or every matching project scratchpad."""
    serialized = json.dumps(payload, indent=2) + "\n"
    targets = [explicit_output] if explicit_output else [
        os.path.join(sp, "telemetry", "hub-24h.json")
        for sp in sorted(glob.glob(os.path.join(scion_home, "project-configs", f"{build_slug}__*", "shared-dirs", "scratchpad")))
    ]
    if not explicit_output and Path("/scion-volumes/scratchpad").is_dir():
        targets.append("/scion-volumes/scratchpad/telemetry/hub-24h.json")
    import tempfile
    written: list[str] = []
    for dst in targets:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=os.path.dirname(dst), delete=False) as f:
            f.write(serialized)
            tmp = f.name
        os.replace(tmp, dst)
        written.append(dst)
    return written

def main(argv: list[str] | None = None) -> int:
    """Export 24-hour Hub telemetry from SQLite or Hub API."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--projects", required=True, help="Comma-separated Hub project slugs (build project first)")
    parser.add_argument("--window-hours", type=int, default=24)
    parser.add_argument("--manifest", default="", help="Optional path to compiled kaizen-manifest.json")
    parser.add_argument("--output", default="", help="Optional explicit output file path")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    scion_home = os.path.expanduser(os.environ.get("SCION_HOME", "~/.scion"))
    slugs = [s.strip() for s in args.projects.split(",") if s.strip()]
    manifest = load_kaizen_manifest(args.manifest, scion_home, slugs[0])
    if (Path(scion_home) / "hub.db").exists():
        conn = open_hub_db(scion_home)
        try:
            payload = collect_hub_telemetry(conn, scion_home, slugs, args.window_hours, manifest)
        finally:
            conn.close()
    else:
        import hub_client

        client = hub_client.HubClient.from_env(scion_home=scion_home)
        payload = collect_hub_telemetry_api(client, slugs, args.window_hours, manifest)

    if args.dry_run or os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes"):
        print(json.dumps(payload, indent=2))
        return 0
    written = write_telemetry_file(payload, scion_home, slugs[0], args.output)
    print(json.dumps({"ok": True, "written": written, "total_messages_24h": payload["total_messages_24h"]}))
    return 0

if __name__ == "__main__":
    sys.exit(main())
