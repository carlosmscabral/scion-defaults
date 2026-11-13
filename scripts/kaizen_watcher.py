#!/usr/bin/env python3
"""Run zero-LLM daily Kaizen sweeps and route ready proposals to Build and Eval leads.

Why this daemon exists:
- Agents inside Scion containers cannot read host SQLite tables directly.
- Filing Kaizen proposals at status:po-decision stalls continuous improvement behind human inbox latency.
- In two-project Blind Eval topologies, the Build lead cannot message eval-lead directly, causing
  Eval-layer Kaizen actions to go unadopted unless a host/control-plane watcher routes them.
- Every 5 minutes with zero idle LLM calls, this controller:
  1. Exports sanitized 24h telemetry to /scion-volumes/scratchpad/telemetry/hub-24h.json.
  2. Wakes kaizen-analyst once per UTC day (or on --trigger-now).
  3. Announces each open kaizen-proposal issue at status:ready once to the Build lead, and also
     to eval-lead when the proposal carries label kaizen-eval or [eval]-scoped items.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hub_client  # noqa: E402
import hub_telemetry_exporter as exporter  # noqa: E402

EVAL_SCOPE_RE = re.compile(r"\[eval\]|\beval-lead\b|\bdomain-evaluator\b|\bcontract-tester\b", re.I)


def github_get_json(url: str, token: str) -> Any:
    """Execute an authenticated GET request against the GitHub REST API."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "scion-kaizen-watcher/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        return json.loads(raw.decode("utf-8")) if raw else []


def resolve_github_token() -> str:
    """Resolve a GitHub token from GITHUB_TOKEN, GH_TOKEN, or gh auth token."""
    for key in ("GITHUB_TOKEN", "GH_TOKEN"):
        val = os.environ.get(key, "").strip()
        if val:
            return val
    return ""


def issue_targets_eval(issue: dict[str, Any]) -> bool:
    """Return True if a kaizen-proposal issue carries kaizen-eval or [eval]-tagged items."""
    labels = {
        str(lbl.get("name") if isinstance(lbl, dict) else lbl)
        for lbl in (issue.get("labels") or [])
    }
    if "kaizen-eval" in labels:
        return True
    body = str(issue.get("body") or "")
    return bool(EVAL_SCOPE_RE.search(body))


def build_autonomous_lead_notice(repo: str, issue_num: int, issue_title: str) -> str:
    """Format the autonomous Kaizen triage notice sent to the Build lead."""
    return (
        f"AUTONOMOUS KAIZEN TRIAGE #{issue_num} ({issue_title} on {repo}): "
        f"Read `gh issue view {issue_num} -R {repo}` and critique each [build] item against your "
        f"live WIP queue, active P0/P1 work, and recent commits:\n"
        f"1) Execute operational, test, review, and knowledge-plane improvements autonomously without "
        f"interrupting busy specialists mid-task (fold into upcoming briefs, commit-slot gates, or wave boundaries).\n"
        f"2) Require a deterministic executable gate plus mutation proof before accepting any defect-class rule.\n"
        f"3) Do NOT page the PO unless an item requires new cost/budget, IAM/secrets/quotas, product scope changes, "
        f"or an architectural shift (only then open a status:po-decision issue).\n"
        f"4) Comment your item-by-item triage decision on #{issue_num}, update its status label when verified, "
        f"and summarize outcomes under `### Auto-improvement (Kaizen)` in today's daily digest."
    )


def build_autonomous_eval_notice(repo: str, issue_num: int, issue_title: str) -> str:
    """Format the blind-safe Kaizen triage notice sent to eval-lead for [eval] items."""
    return (
        f"AUTONOMOUS EVAL KAIZEN TRIAGE #{issue_num} ({issue_title} on {repo}): "
        f"Read `gh issue view {issue_num} -R {repo}` and adopt every [eval]-scoped item on your "
        f"evaluation trunk without reading or modifying the build branch:\n"
        f"1) Update your L1-L5 probes, golden checks, or evaluation scripts to close the reported blind spot.\n"
        f"2) Verify that the hardened probe fails on a synthetic bad response and passes against a valid target.\n"
        f"3) Comment your [eval] adoption evidence (`commit` or `run_id`) directly on #{issue_num}. "
        f"Never message build agents directly."
    )


def build_daily_wake_message(repo: str, today_key: str, lead_agent: str) -> str:
    """Format the 24-hour sweep instruction sent to kaizen-analyst."""
    if not repo:
        return (
            f"DAILY KAIZEN SWEEP ({today_key}): "
            f"1) Read `/scion-volumes/scratchpad/telemetry/hub-24h.json` and audit the effect of the previous "
            f"improvements. "
            f"2) Analyze the last 24h of escaped defects, review FAIL loops, and queue skew. "
            f"3) File a consolidated kaizen proposal at `/scion-volumes/scratchpad/kaizen-{today_key}.md` "
            f"and notify `{lead_agent}`."
        )
    return (
        f"DAILY KAIZEN SWEEP ({today_key}): "
        f"1) Read `/scion-volumes/scratchpad/telemetry/hub-24h.json` and audit the effect of the previous "
        f"`kaizen-proposal` issue on {repo} (`ADOPTED & MEASURED`, `PARTIAL`, or `UNADOPTED`, plus any stale "
        f"`status:po-decision` issue that already has a PO answer). "
        f"2) Analyze the last 24h of escaped defects (`eval-finding`, PO bug reports), review FAIL loops, and "
        f"queue skew. "
        f"3) File at most one consolidated `kaizen-proposal` issue at `status:ready` (add `--label kaizen-eval` "
        f"if any item targets `[eval]`), tagging each `KZ-{today_key}-NN` item with `[build]` or `[eval]`, owner "
        f"role, durable rule, executable gate, and mutation proof, then notify `{lead_agent}`."
    )


def _save_state(state_path: Path, state: dict[str, Any]) -> None:
    """Safely persist watcher state."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    dst = str(state_path)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=os.path.dirname(dst), delete=False) as f:
        json.dump(state, f, indent=2)
        f.write("\n")
        tmp = f.name
    os.replace(tmp, dst)


def _resolve_hub_client(scion_home: str, hub_url: str, po_email: str) -> hub_client.HubClient:
    """Create an authenticated HubClient using env credentials or a short-lived local admin JWT."""
    env = dict(os.environ)
    env.setdefault("HUB_URL", hub_url)
    if not env.get("HUB_TOKEN") and po_email:
        minted = hub_client.mint_local_admin_jwt(scion_home, po_email)
        if minted:
            env["HUB_TOKEN"] = minted
    return hub_client.HubClient.from_env(env=env, scion_home=scion_home)


def main(argv: list[str] | None = None) -> int:
    """Export 24h telemetry, wake kaizen-analyst once per day, and route ready proposals."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default="", help="GitHub owner/repo")
    parser.add_argument("--build-project", required=True, help="Build Hub project slug")
    parser.add_argument("--eval-project", default="", help="Optional Eval Hub project slug")
    parser.add_argument("--kaizen-agent", default="kaizen-analyst")
    parser.add_argument("--lead-agent", default="lead")
    parser.add_argument("--eval-lead-agent", default="eval-lead")
    parser.add_argument("--po-email", default="")
    parser.add_argument("--hub-url", default="http://localhost:8080")
    parser.add_argument("--daily-hour-utc", type=int, default=11, help="UTC hour for daily sweep (default: 11)")
    parser.add_argument("--telemetry-output", default="", help="Optional explicit hub-24h.json path")
    parser.add_argument("--trigger-now", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    dry_run = args.dry_run or os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    scion_home = os.path.expanduser(os.environ.get("SCION_HOME", "~/.scion"))
    state_path = Path(scion_home) / f"kaizen-watcher-{args.build_project}.json"
    state: dict[str, Any] = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}

    slugs = [s for s in (args.build_project, args.eval_project) if s]
    client = _resolve_hub_client(scion_home, args.hub_url, args.po_email)
    db_path = Path(scion_home) / "hub.db"
    if db_path.exists():
        conn = exporter.open_hub_db(scion_home)
        try:
            telemetry = exporter.collect_hub_telemetry(conn, scion_home, slugs, window_hours=24)
            proj_rows = conn.execute(
                f"SELECT id, slug FROM projects WHERE slug IN ({','.join('?' for _ in slugs)})",
                tuple(slugs),
            ).fetchall()
            pid_by_slug = {str(r["slug"]): str(r["id"]) for r in proj_rows}
        finally:
            conn.close()
    else:
        telemetry = exporter.collect_hub_telemetry_api(client, slugs, window_hours=24)
        projects = hub_client._extract_list(client.request("GET", "/api/v1/projects")[1], "projects", "items")
        pid_by_slug = {str(p.get("slug") or ""): str(p.get("id") or "") for p in projects}

# Auto-copy kaizen-manifest.json to scratchpad if present alongside watcher
    manifest_src = Path(__file__).parent.parent / "config" / "kaizen-manifest.json"
    manifest_dst = Path(scion_home) / "project-configs" / f"{args.build_project}__1" / "shared-dirs" / "scratchpad" / "telemetry" / "kaizen-manifest.json"
    if manifest_src.exists() and not dry_run:
        manifest_dst.parent.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copy2(manifest_src, manifest_dst)

    if not dry_run:
        exporter.write_telemetry_file(telemetry, scion_home, args.build_project, args.telemetry_output)

    build_pid = pid_by_slug.get(args.build_project, "")
    if not build_pid:
        raise RuntimeError(f"Build project {args.build_project!r} not found on Hub.")
    eval_pid = pid_by_slug.get(args.eval_project, "") if args.eval_project else ""

    running_by_project: dict[str, set[str]] = {}
    for entry in telemetry["roster"]:
        if entry.get("phase") == "running":
            running_by_project.setdefault(str(entry["project"]), set()).add(str(entry["agent"]))

    now_utc = time.gmtime()
    today_key = time.strftime("%Y-%m-%d", now_utc)
    should_wake = args.trigger_now or (
        now_utc.tm_hour >= args.daily_hour_utc and state.get("last_daily_sweep_date") != today_key
    )
    if should_wake and args.kaizen_agent in running_by_project.get(args.build_project, set()):
        wake_msg = build_daily_wake_message(args.repo, today_key, args.lead_agent)
        if not dry_run:
            client.request(
                "POST",
                f"/api/v1/projects/{build_pid}/agents/{args.kaizen_agent}/message",
                body={"message": wake_msg},
            )
            state["last_daily_sweep_date"] = today_key
            state["last_wake_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", now_utc)
            _save_state(state_path, state)
        print(f"{'dry-run' if dry_run else 'woke'}: {args.kaizen_agent} ({today_key})")

    gh_tok = resolve_github_token()
    if not args.repo:
        if not dry_run:
            _save_state(state_path, state)
        return 0

    try:
        issues = github_get_json(
            f"https://api.github.com/repos/{args.repo}/issues?state=open&labels=kaizen-proposal,status:ready&per_page=20",
            gh_tok,
        )
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"warning: GitHub API fetch failed: {exc}\n")
        if not dry_run:
            _save_state(state_path, state)
        return 0
    notified_build: list[int] = list(state.get("notified_ready_issues") or [])
    notified_eval: list[int] = list(state.get("notified_eval_issues") or [])

    for iss in issues or []:
        if "pull_request" in iss:
            continue
        num = int(iss["number"])
        title = str(iss.get("title") or "")

        if num not in notified_build and args.lead_agent in running_by_project.get(args.build_project, set()):
            notice = build_autonomous_lead_notice(args.repo, num, title)
            if not dry_run:
                client.request(
                    "POST",
                    f"/api/v1/projects/{build_pid}/agents/{args.lead_agent}/message",
                    body={"message": notice},
                )
                notified_build.append(num)
                state["notified_ready_issues"] = notified_build
            print(f"{'dry-run' if dry_run else 'notified-lead'}: issue #{num}")

        if (
            eval_pid
            and issue_targets_eval(iss)
            and num not in notified_eval
            and args.eval_lead_agent in running_by_project.get(args.eval_project, set())
        ):
            eval_notice = build_autonomous_eval_notice(args.repo, num, title)
            if not dry_run:
                client.request(
                    "POST",
                    f"/api/v1/projects/{eval_pid}/agents/{args.eval_lead_agent}/message",
                    body={"message": eval_notice},
                )
                notified_eval.append(num)
                state["notified_eval_issues"] = notified_eval
            print(f"{'dry-run' if dry_run else 'notified-eval-lead'}: issue #{num}")

    if not dry_run:
        _save_state(state_path, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
