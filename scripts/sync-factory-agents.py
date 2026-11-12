#!/usr/bin/env python3
"""Sync live agent state from the Scion Hub into Firestore for the eval dashboard. Runs ON the Hub VM.

Reads the Hub's SQLite DB read-only and upserts one document per live agent of the factory projects into the
Firestore collection `factory_agent_status` (document ID `<project_slug>__<agent_name>`; contract in
docs/plans/2026-10-20-firestore-control-plane.md). Documents that were not refreshed for PRUNE_MINUTES (deleted
agents) are removed. install-sync-cron.sh ships this file and factory_store.py to the VM and runs it every minute
from the Hub OS user's crontab.

Configuration (environment):
  FIRESTORE_PROJECT    project that holds the Firestore database (default: SANDBOX_PROJECT)
  FIRESTORE_DATABASE   database ID (default: (default))
  FACTORY_PROJECTS     comma-separated Scion project slugs (default: BUILD_PROJECT,EVAL_PROJECT)
  BILLING_PROJECT      quota project sent as x-goog-user-project (default: the Firestore project). The VM's
                       service account defaults to the Hub project for quota, which fails without it
                       (lessons-learned.md L12). The VM SA needs roles/serviceusage.serviceUsageConsumer and
                       roles/datastore.user on the Firestore project.
  PRUNE_MINUTES        default 10
  SCION_HOME           default ~/.scion

Turn and model-call counters are written as NULL when the Hub reports 0: the Hub doesn't populate them today,
so "unknown" is the honest value, not zero (lessons-learned.md L9).

  python3 sync-factory-agents.py            # upsert + prune
  python3 sync-factory-agents.py --dry-run  # print the Firestore requests, write nothing
"""
import argparse
import datetime
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import factory_store  # noqa: E402  (shipped next to this file by install-sync-cron.sh)


def read_hub_project_id(scion_home):
    for k in ("HUB_PROJECT_ID", "SCION_GCP_PROJECT_ID", "HUB_PROJECT"):
        v = os.environ.get(k, "").strip()
        if v:
            return v
    hub_env = os.path.join(scion_home, "hub.env")
    if os.path.exists(hub_env):
        with open(hub_env) as f:
            for line in f:
                line = line.strip()
                if line.startswith("SCION_GCP_PROJECT_ID="):
                    return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def get_gcp_access_token():
    meta_timeout = 10 if os.environ.get("KUBERNETES_SERVICE_HOST") else 3
    try:
        req = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
            headers={"Metadata-Flavor": "Google"},
        )
        with urllib.request.urlopen(req, timeout=meta_timeout) as resp:
            return json.loads(resp.read().decode()).get("access_token", "")
    except Exception:
        try:
            res = subprocess.run(
                ["gcloud", "auth", "print-access-token"],
                capture_output=True, text=True, timeout=5
            )
            return res.stdout.strip() if res.returncode == 0 else ""
        except Exception:
            return ""


def fetch_monitoring_counts(project_id, token, metric_type, days=7):
    """Return {agent_id: int_count} from Cloud Monitoring CUMULATIVE series via ALIGN_DELTA + REDUCE_SUM."""
    if not project_id or not token:
        return {}
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    start_dt = now_dt - datetime.timedelta(days=days)
    params = urllib.parse.urlencode({
        "filter": f'metric.type = "{metric_type}"',
        "interval.startTime": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "interval.endTime": now_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "aggregation.alignmentPeriod": f"{days * 86400}s",
        "aggregation.perSeriesAligner": "ALIGN_DELTA",
        "aggregation.crossSeriesReducer": "REDUCE_SUM",
        "aggregation.groupByFields": "metric.label.scion_agent_id",
    })
    url = f"https://monitoring.googleapis.com/v3/projects/{project_id}/timeSeries?{params}"
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        counts = {}
        for ts_entry in data.get("timeSeries", []):
            aid = ts_entry.get("metric", {}).get("labels", {}).get("scion_agent_id", "")
            if not aid:
                continue
            total = 0
            for pt in ts_entry.get("points", []):
                val = pt.get("value", {})
                total += int(val.get("int64Value") or val.get("doubleValue") or 0)
            if total > 0:
                counts[aid] = counts.get(aid, 0) + total
        return counts
    except Exception:
        return {}


def load_sync_env():
    env_paths = [
        os.path.join(os.path.expanduser(os.environ.get("SCION_HOME", "~/.scion")), "sync.env"),
        os.path.expanduser("~/factory-sync/sync.env"),
    ]
    for p in env_paths:
        if os.path.exists(p):
            env_vars = {}
            with open(p) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        env_vars[k.strip()] = v.strip().strip("'\"")
            if "PATH" in env_vars:
                os.environ["PATH"] = env_vars["PATH"]
            for k, v in env_vars.items():
                if k not in os.environ:
                    os.environ[k] = v
            return


def parse_ts(value):
    """Go/SQLite timestamp text (e.g. '2026-10-12 23:45:52.44 +0000 UTC') -> aware datetime, or None."""
    if not value:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})", str(value))
    if not m:
        return None
    return datetime.datetime.strptime(f"{m.group(1)}T{m.group(2)}", "%Y-%m-%dT%H:%M:%S").replace(
        tzinfo=datetime.timezone.utc)


def counter(value):
    """Counters the Hub doesn't populate are unknown (None), not zero (L9)."""
    return int(value) if value else None


def truncate(value, max_len=500):
    if value is None:
        return None
    s = str(value)
    return s if len(s) <= max_len else s[: max_len - 1] + "…"


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")



def collect_agent_docs_from_api(client, projects: list[str], api_calls_by_agent: dict[str, int], tool_calls_by_agent: dict[str, int]) -> list[dict]:
    import hub_client

    docs = []
    id_by_slug = {proj["slug"]: proj["id"] for proj in client.list_projects() if proj.get("slug") and proj.get("id")}
    for p in projects:
        if p == "global":
            continue
        pid = id_by_slug.get(p, p)
        try:
            agents = client.list_agents(pid)
        except hub_client.HubAPIError as exc:
            if exc.status == 403 and len(projects) > 1:
                continue
            raise
        names_by_id = {a.get("id"): (a.get("slug") or a.get("name")) for a in agents if a.get("id")}
        for a in agents:
            aid = a.get("id")
            turns_val = max(int(a.get("current_turns") or a.get("currentTurns") or 0), int(tool_calls_by_agent.get(aid, 0)))
            calls_val = max(int(a.get("current_model_calls") or a.get("currentModelCalls") or 0), int(api_calls_by_agent.get(aid, 0)))
            
            agent_name = a.get("slug") or a.get("name")
            harness = a.get("harnessConfig") or a.get("harness")
            raw_parent = a.get("parent_agent") or a.get("createdBy") or a.get("created_by")
            parent_agent = names_by_id.get(raw_parent, raw_parent if a.get("parent_agent") else None)
            
            docs.append({
                "project_slug": p,
                "agent_name": agent_name,
                "agent_id": aid,
                "template": a.get("template"),
                "harness": harness,
                "phase": a.get("phase") or a.get("status"),
                "activity": a.get("activity"),
                "parent_agent": parent_agent,
                "current_turns": counter(turns_val),
                "model_calls": counter(calls_val),
                "last_summary": truncate(a.get("taskSummary") or a.get("message")),
                "last_activity_at": parse_ts(a.get("lastActivityEvent") or a.get("last_activity_event") or a.get("updated") or a.get("created")),
            })
    return docs

def main():
    load_sync_env()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="print the Firestore requests instead of sending them")
    args = parser.parse_args()

    projects = os.environ.get("FACTORY_PROJECTS") or ",".join(
        p for p in (os.environ.get("BUILD_PROJECT"), os.environ.get("EVAL_PROJECT")) if p)
    projects = [p.strip() for p in projects.split(",") if p.strip() and p.strip() != "global"]
    if not projects:
        sys.exit("set FACTORY_PROJECTS (or BUILD_PROJECT/EVAL_PROJECT)")
    try:
        store = factory_store.store_from_env(dry_run=args.dry_run)
    except ValueError as err:
        sys.exit(str(err))
    prune_minutes = int(os.environ.get("PRUNE_MINUTES", "10"))
    scion_home = os.path.expanduser(os.environ.get("SCION_HOME", "~/.scion"))
    hub_db = os.path.join(scion_home, "hub.db")
    hub_project = read_hub_project_id(scion_home)
    gcp_token = get_gcp_access_token() if hub_project else ""
    api_calls_by_agent = fetch_monitoring_counts(hub_project, gcp_token, "workload.googleapis.com/gen_ai.api.calls")
    tool_calls_by_agent = fetch_monitoring_counts(hub_project, gcp_token, "workload.googleapis.com/agent.tool.calls")

    hub_url = os.environ.get("HUB_URL", "").strip()
    if hub_url:
        import hub_client
        client = hub_client.HubClient.from_env()
        docs = collect_agent_docs_from_api(client, projects, api_calls_by_agent, tool_calls_by_agent)
        if not docs:
            print(f"{now()} no agents found in {projects}; nothing to sync")
            return 0
    else:
        db = sqlite3.connect(f"file:{hub_db}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        names = {r["id"]: r["name"] for r in db.execute("SELECT id, name FROM agents")}
        rows = db.execute(
            f"""SELECT p.slug AS project_slug, a.name, a.id, a.template, a.phase, a.activity, a.created_by,
                       a.current_turns, a.current_model_calls, a.task_summary, a.message, a.last_activity_event,
                       COALESCE(json_extract(a.applied_config, '$.harnessConfig'), a.harness_config) AS harness
                FROM agents a JOIN projects p ON p.id = a.project_id
                WHERE a.deleted_at IS NULL AND p.slug IN ({",".join("?" * len(projects))})
                ORDER BY p.slug, a.name""",
            projects,
        ).fetchall()
        if not rows:
            print(f"{now()} no agents found in {projects}; nothing to sync")
            return 0
    
        docs = []
        for r in rows:
            parent = names.get(r["created_by"])  # None when a user (not an agent) created it
            aid = r["id"]
            turns_val = max(int(r["current_turns"] or 0), int(tool_calls_by_agent.get(aid, 0)))
            calls_val = max(int(r["current_model_calls"] or 0), int(api_calls_by_agent.get(aid, 0)))
            docs.append({
                "project_slug": r["project_slug"],
                "agent_name": r["name"],
                "agent_id": r["id"],
                "template": r["template"],
                "harness": r["harness"],
                "phase": r["phase"],
                "activity": r["activity"],
                "parent_agent": parent,
                "current_turns": counter(turns_val),
                "model_calls": counter(calls_val),
                "last_summary": truncate(r["task_summary"] or r["message"]),
                "last_activity_at": parse_ts(r["last_activity_event"]),
            })
    
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=prune_minutes)
    try:
        written = store.commit(factory_store.agent_status_writes(store, docs))
        pruned = factory_store.prune_agent_status(store, cutoff)
    except RuntimeError as err:
        print(f"{now()} ERROR: {err}", file=sys.stderr)
        return 1
    if not args.dry_run:
        print(f"{now()} synced {written} agents, pruned {pruned}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
