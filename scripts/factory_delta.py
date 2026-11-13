#!/usr/bin/env python3
"""Zero-LLM Python delta controller for Scion GKE software factories.

Runs every 2 minutes inside a stdlib-only `python:3.12-slim` Kubernetes CronJob
(`scion-factory-delta`). Polls open GitHub issues/comments and Scion Hub agent
states, waking `lead` ONLY when an actionable delta arrives, rotating `lead`
sessions after `LEAD_MAX_TURNS_BEFORE_ROTATE` turns when no specialist pod is
mid-flight, and stopping `lead` + idle specialist pods after
`IDLE_STOP_GRACE_SEC` of zero actionable work.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import inspect
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

os.environ.setdefault("CLOUDSDK_CORE_DISABLE_PROMPTS", "1")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import factory_store  # noqa: E402
from hub_client import HubAPIError, HubClient  # noqa: E402

AGENT_COMMENT_MARKER = "<!-- scion-agent:"
IDLE_TICK_JITTER_SEC = 5.0
CONTROL_STATE_COLLECTION = "factory_control_state"

_SKIP_LABELS = frozenset({"digest", "hillclimb-archive", "status:verified"})
_PO_WAIT_LABELS = frozenset({"status:po-decision", "status:blocked"})
_IDLE_ACTIVITIES = frozenset({"idle", "completed", "waiting_for_input", "blocked"})
_SLEEPABLE_CHILD_ACTIVITIES = frozenset({"idle", "completed", "waiting_for_input", "stalled", "blocked"})
_ACTIVE_PHASES = frozenset({"running", "provisioning", "starting", "created"})


def _extract_labels(issue: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for lbl in issue.get("labels") or []:
        if isinstance(lbl, dict):
            name = str(lbl.get("name") or "").strip()
        else:
            name = str(lbl or "").strip()
        if name:
            names.add(name)
    return names


def _parse_epoch(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        num = float(value)
        return num if num > 0 else None
    text = str(value).strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.(\d+))?", text)
    if not m:
        return None
    dt = datetime.datetime.strptime(
        f"{m.group(1)}T{m.group(2)}",
        "%Y-%m-%dT%H:%M:%S",
    ).replace(tzinfo=datetime.timezone.utc)
    frac = float(f"0.{m.group(3)}") if m.group(3) else 0.0
    ts = dt.timestamp() + frac
    # Go zero time.Time serializes as "0001-01-01T00:00:00Z" (negative epoch), which otherwise makes
    # freshly started specialists look silent for >2,000 years before their first activity event.
    return ts if ts > 0 else None


def classify_github_issues(
    issues: list[dict],
    comments_by_issue: dict[int, list[dict]],
) -> dict[str, list[dict]]:
    """Classify open GitHub issues into actionable, po_replies, and waiting_po."""
    actionable: list[dict] = []
    po_replies: list[dict] = []
    waiting_po: list[dict] = []

    for issue in issues or []:
        if not isinstance(issue, dict) or "pull_request" in issue:
            continue
        if str(issue.get("state", "open")).lower() != "open":
            continue

        labels = _extract_labels(issue)
        if labels & _SKIP_LABELS:
            continue

        number = int(issue.get("number", 0))
        if labels & _PO_WAIT_LABELS:
            comments = (
                comments_by_issue.get(number)
                if number in comments_by_issue
                else comments_by_issue.get(str(number))  # type: ignore[arg-type]
            ) or []
            approved = "status:approved" in labels
            human_replied = False
            latest_comment_id = None
            if comments:
                last_comment = comments[-1] or {}
                last_body = str(last_comment.get("body") or "")
                latest_comment_id = last_comment.get("id")
                human_replied = AGENT_COMMENT_MARKER not in last_body

            if approved or human_replied:
                enriched = dict(issue)
                if latest_comment_id is not None:
                    enriched.setdefault("latest_comment_id", latest_comment_id)
                if "comments" not in enriched and comments:
                    enriched["comments"] = len(comments)
                po_replies.append(enriched)
            else:
                waiting_po.append(issue)
        else:
            actionable.append(issue)

    return {
        "actionable": actionable,
        "po_replies": po_replies,
        "waiting_po": waiting_po,
    }


def compute_delta_fingerprint(classified: dict, abnormal_children: list[dict]) -> str:
    """Return deterministic SHA-256 hex digest over classified issues and abnormal children."""
    canonical = {
        "actionable": [
            {
                "number": int(iss.get("number", 0)),
                "labels": sorted(_extract_labels(iss)),
                "comments": int(iss.get("comments", 0) or 0),
            }
            for iss in sorted(classified.get("actionable") or [], key=lambda x: int(x.get("number", 0)))
        ],
        "po_replies": [
            {
                "number": int(iss.get("number", 0)),
                "labels": sorted(_extract_labels(iss)),
                "comments": int(iss.get("comments", 0) or 0),
                "latest_comment_id": iss.get("latest_comment_id"),
            }
            for iss in sorted(classified.get("po_replies") or [], key=lambda x: int(x.get("number", 0)))
        ],
        "abnormal_children": sorted(
            [
                [
                    str(ch.get("slug") or ch.get("name") or ""),
                    str(ch.get("phase") or ch.get("status") or ""),
                    int(ch.get("silent_bucket", 0) or 0),
                ]
                for ch in (abnormal_children or [])
            ]
        ),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_github_repo(github_repo: str) -> str:
    repo = (github_repo or "").strip()
    if repo.startswith("https://github.com/"):
        repo = repo.removeprefix("https://github.com/")
    elif repo.startswith("http://github.com/"):
        repo = repo.removeprefix("http://github.com/")
    if repo.endswith(".git"):
        repo = repo[:-4]
    return repo.strip("/")


def _github_get_json(
    url: str,
    github_token: str = "",
    timeout: int = 20,
    max_pages: int = 5,
) -> list[dict]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "scion-factory-delta/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    items: list[dict] = []
    next_url: str | None = url
    pages = 0
    while next_url and pages < max_pages:
        pages += 1
        req = urllib.request.Request(next_url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace") or "[]")
                if isinstance(payload, list):
                    items.extend(x for x in payload if isinstance(x, dict))
                link_hdr = resp.headers.get("Link") or ""
                m_next = re.search(r'<([^>]+)>;\s*rel="next"', link_hdr)
                next_url = m_next.group(1) if m_next else None
        except urllib.error.HTTPError as err:
            safe_path = urllib.parse.urlparse(next_url).path
            raise RuntimeError(f"GitHub GET {safe_path} failed: HTTP {err.code}") from None
        except urllib.error.URLError as err:
            safe_path = urllib.parse.urlparse(next_url).path
            raise RuntimeError(f"GitHub GET {safe_path} unreachable: {err.reason}") from None
    return items


def fetch_github_issues_and_comments(
    github_repo: str,
    issue_label: str = "",
    github_token: str = "",
    api_base: str = "",
) -> tuple[list[dict], dict[int, list[dict]]]:
    """Fetch open repository issues (excluding PRs) and comments for PO-gated issues."""
    repo = _normalize_github_repo(github_repo)
    if not repo:
        return [], {}

    base = (api_base or os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
    params: dict[str, str] = {"state": "open", "per_page": "100"}
    if issue_label:
        params["labels"] = issue_label
    issues_url = f"{base}/repos/{repo}/issues?{urllib.parse.urlencode(params)}"
    raw_items = _github_get_json(issues_url, github_token=github_token)
    issues = [item for item in raw_items if "pull_request" not in item]

    comments_by_issue: dict[int, list[dict]] = {}
    for issue in issues:
        labels = _extract_labels(issue)
        if labels & _SKIP_LABELS:
            continue
        if not (labels & _PO_WAIT_LABELS):
            continue
        number = int(issue.get("number", 0))
        if not number:
            continue
        if "comments" in issue and int(issue.get("comments") or 0) == 0:
            comments_by_issue[number] = []
            continue
        comments_url = f"{base}/repos/{repo}/issues/{number}/comments?per_page=100"
        comments_by_issue[number] = _github_get_json(comments_url, github_token=github_token)

    return issues, comments_by_issue


def _load_delta_state(store: Any, project_slug: str) -> dict[str, Any]:
    state: dict[str, Any] = {
        "last_fingerprint": "",
        "idle_since": 0.0,
        "lead_sleeping": False,
        "lead_turns_in_epoch": 0,
        "dispatched_issues": [],
        "last_wake_ts": 0.0,
        "stalled_wip_ts": 0.0,
        "blocked_wip_wakes": 0,
        "blocked_wip_fp": "",
    }
    if store is None or not hasattr(store, "get_document"):
        return state
    doc = store.get_document(CONTROL_STATE_COLLECTION, f"delta__{project_slug}")
    if not isinstance(doc, dict):
        return state
    if isinstance(doc.get("state_json"), str):
        try:
            parsed = json.loads(doc["state_json"])
            if isinstance(parsed, dict):
                state.update(parsed)
        except json.JSONDecodeError:
            pass
    for key in (
        "last_fingerprint",
        "idle_since",
        "lead_sleeping",
        "lead_turns_in_epoch",
        "dispatched_issues",
        "last_wake_ts",
        "stalled_wip_ts",
        "blocked_wip_wakes",
        "blocked_wip_fp",
    ):
        if key in doc:
            state[key] = doc[key]
    state["last_fingerprint"] = str(state.get("last_fingerprint") or "")
    state["idle_since"] = float(state.get("idle_since") or 0.0)
    state["lead_sleeping"] = bool(state.get("lead_sleeping", False))
    state["lead_turns_in_epoch"] = int(state.get("lead_turns_in_epoch") or 0)
    raw_dispatched = state.get("dispatched_issues")
    if isinstance(raw_dispatched, list):
        state["dispatched_issues"] = sorted(
            {int(x) for x in raw_dispatched if isinstance(x, int) or (isinstance(x, str) and x.isdigit())}
        )
    else:
        state["dispatched_issues"] = []
    state["last_wake_ts"] = float(state.get("last_wake_ts") or 0.0)
    state["stalled_wip_ts"] = float(state.get("stalled_wip_ts") or 0.0)
    state["blocked_wip_wakes"] = int(state.get("blocked_wip_wakes") or 0)
    state["blocked_wip_fp"] = str(state.get("blocked_wip_fp") or "")
    return state


def _save_delta_state(store: Any, project_slug: str, state: dict[str, Any]) -> None:
    if store is None or not hasattr(store, "upsert_document"):
        return
    dispatched = sorted(
        {
            int(x)
            for x in (state.get("dispatched_issues") or [])
            if isinstance(x, int) or (isinstance(x, str) and x.isdigit())
        }
    )
    payload = {
        "project_slug": project_slug,
        "last_fingerprint": str(state.get("last_fingerprint") or ""),
        "idle_since": float(state.get("idle_since") or 0.0),
        "lead_sleeping": bool(state.get("lead_sleeping", False)),
        "lead_turns_in_epoch": int(state.get("lead_turns_in_epoch") or 0),
        "dispatched_issues": dispatched,
        "last_wake_ts": float(state.get("last_wake_ts") or 0.0),
        "stalled_wip_ts": float(state.get("stalled_wip_ts") or 0.0),
        "blocked_wip_wakes": int(state.get("blocked_wip_wakes") or 0),
        "blocked_wip_fp": str(state.get("blocked_wip_fp") or ""),
        "updated_at": "REQUEST_TIME",
    }
    store.upsert_document(CONTROL_STATE_COLLECTION, f"delta__{project_slug}", payload)


def _resolve_lead_slug(project_slug: str, explicit_lead_slug: str, agents: list[dict]) -> str:
    if explicit_lead_slug.strip():
        return explicit_lead_slug.strip()
    present = {str(a.get("slug") or a.get("name") or "") for a in agents}
    for cand in (f"{project_slug}-lead", "lead"):
        if cand in present:
            return cand
    for a in agents:
        if str(a.get("template") or "").strip() == "lead":
            slug = str(a.get("slug") or a.get("name") or "").strip()
            if slug:
                return slug
    return "lead"


def _wait_for_tmux(
    client: HubClient,
    pid: str,
    slug: str,
    wait_sec: float | None = None,
    poll_sec: float = 1.0,
) -> bool:
    if wait_sec is None:
        wait_sec = float(os.environ.get("DELTA_TMUX_WAIT_SEC", "90.0"))
    deadline = time.time() + max(wait_sec, 0.1)
    exec_denied = False
    while True:
        if not exec_denied:
            try:
                rc, _ = client.exec_agent(pid, slug, ["tmux", "has-session", "-t", "scion"], timeout=10)
                if rc == 0:
                    return True
            except HubAPIError as err:
                # The control-plane identity is project-admin, and current Scion keeps `agent.attach`
                # (exec) for project-owner. Without this fallback the controller answered
                # waiting_for_tmux forever and never woke lead (hillclimb Run 1, sixth attempt).
                exec_denied = err.status == 403
            except Exception:
                pass
        if exec_denied and _agent_session_reported_ready(client, pid, slug):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(poll_sec)


def _agent_session_reported_ready(client: HubClient, pid: str, slug: str) -> bool:
    """Readiness from what the Hub reports, for callers that may not exec into the agent.

    The harness runs inside the tmux session, so a running agent that has reported an activity has
    a session the Hub can deliver a message into.
    """
    try:
        agent = client.get_agent(pid, slug)
    except Exception:
        return False
    if not isinstance(agent, dict):
        return False
    phase = str(agent.get("phase") or agent.get("status") or "").lower()
    container = str(agent.get("containerStatus") or "running").lower()
    return phase == "running" and container == "running" and bool(str(agent.get("activity") or "").strip())


def _call_github_fetcher(fetcher, github_repo: str, issue_label: str, github_token: str) -> tuple[list[dict], dict[int, list[dict]]]:
    sig = inspect.signature(fetcher)
    pos_params = [
        p for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values())
    if has_varargs or len(pos_params) >= 3:
        return fetcher(github_repo, issue_label, github_token)
    if len(pos_params) == 2:
        return fetcher(github_repo, issue_label)
    if len(pos_params) == 1:
        return fetcher(github_repo)
    return fetcher()


def _format_delta_message(
    classified: dict,
    abnormal_children: list[dict],
    *,
    rotated: bool = False,
    stopped_children: list[str] | None = None,
    parked_children: list[str] | None = None,
) -> str:
    parts: list[str] = []
    if rotated:
        parts.append("[session-rotated: read /scion-volumes/scratchpad/STATE.json and /scion-volumes/knowledge-plane/INDEX.md]")

    actionable = sorted(classified.get("actionable") or [], key=lambda x: int(x.get("number", 0)))
    if actionable:
        items = []
        for iss in actionable:
            lbls = ",".join(sorted(_extract_labels(iss)))
            title = str(iss.get("title") or "").strip()[:80]
            items.append(f"#{iss.get('number')} {title!r} [{lbls}]")
        parts.append("ACTIONABLE: " + "; ".join(items))

    po_replies = sorted(classified.get("po_replies") or [], key=lambda x: int(x.get("number", 0)))
    if po_replies:
        items = []
        for iss in po_replies:
            lbls = ",".join(sorted(_extract_labels(iss)))
            title = str(iss.get("title") or "").strip()[:80]
            items.append(f"#{iss.get('number')} {title!r} [{lbls}]")
        parts.append("PO_REPLIES: " + "; ".join(items))

    if abnormal_children:
        items = [
            f"{ch['slug']}(phase={ch['phase']},silent_bucket={ch['silent_bucket']})"
            for ch in sorted(abnormal_children, key=lambda x: str(x.get("slug", "")))
        ]
        parts.append("ABNORMAL_CHILDREN: " + ", ".join(items))

    if stopped_children:
        parts.append(
            "STALLED_WIP: open actionable issues remain with 0 busy specialists "
            f"(stopped: {', '.join(sorted(stopped_children))}); inspect scion list, restart any "
            "stopped specialist still needed by in-progress issues with scion start <slug>, and re-send its brief"
        )
    if parked_children:
        parts.append(
            "STALLED_WIP: open actionable or in-review issues remain with 0 active turns "
            f"(blocked/stalled: {', '.join(sorted(parked_children))}) - inspect teammate status and background "
            "processes for hung shells or missed handoffs, then advance or re-brief"
        )

    waiting_po = sorted(classified.get("waiting_po") or [], key=lambda x: int(x.get("number", 0)))
    if waiting_po:
        parts.append("WAITING_PO: " + ", ".join(f"#{iss.get('number')}" for iss in waiting_po))

    return "DELTA: " + " | ".join(parts)


def run_delta_tick(
    client: HubClient,
    store,
    *,
    project_slug: str,
    lead_slug: str = "",
    github_repo: str = "",
    issue_label: str = "",
    github_token: str = "",
    apply: bool = False,
    now: float | None = None,
    idle_grace_sec: int = 600,
    max_turns_before_rotate: int = 25,
    child_silence_sec: int = 1800,
    stalled_wip_sec: int = 120,
    blocked_wip_sec: int = 900,
    github_fetcher=None,
    build_branch: str = "",
    runtime_profile: str = "",
) -> dict:
    """Execute one 2-minute zero-LLM delta controller tick for `project_slug`."""
    now_ts = float(now) if now is not None else time.time()
    state = _load_delta_state(store, project_slug)

    proj = client.get_project_by_slug(project_slug)
    pid = (proj.get("id") if isinstance(proj, dict) and proj.get("id") else None) or project_slug
    agents = client.list_agents(pid)
    resolved_lead = _resolve_lead_slug(project_slug, lead_slug, agents)

    lead_agent = next(
        (a for a in agents if (a.get("slug") or a.get("name")) == resolved_lead),
        None,
    )
    child_agents = [
        a for a in agents
        if (a.get("slug") or a.get("name")) and (a.get("slug") or a.get("name")) != resolved_lead
    ]

    any_running_children: list[dict] = []
    busy_children: list[dict] = []
    abnormal_children: list[dict] = []
    stopped_children: list[str] = []
    parked_children: list[str] = []
    latest_activity_ts = 0.0

    for ag in ([lead_agent] if lead_agent else []) + child_agents:
        ag_ts = _parse_epoch(
            ag.get("lastActivityEvent")
            or ag.get("last_activity_event")
            or ag.get("updatedAt")
            or ag.get("updated")
            or ag.get("createdAt")
            or ag.get("created")
        )
        if ag_ts is not None and ag_ts > latest_activity_ts:
            latest_activity_ts = ag_ts

    for ch in child_agents:
        ch_slug = str(ch.get("slug") or ch.get("name") or "")
        ch_phase = str(ch.get("phase") or ch.get("status") or "").lower()
        ch_activity = str(ch.get("activity") or "").lower()

        if ch_phase in _ACTIVE_PHASES:
            any_running_children.append(ch)
            if ch_phase != "running" or ch_activity not in _SLEEPABLE_CHILD_ACTIVITIES:
                busy_children.append(ch)
            elif ch_slug and ch_activity in ("blocked", "stalled") and blocked_wip_sec > 0:
                ch_ts = _parse_epoch(
                    ch.get("lastActivityEvent")
                    or ch.get("last_activity_event")
                    or ch.get("createdAt")
                    or ch.get("created")
                )
                if ch_ts is None or (now_ts - ch_ts) >= blocked_wip_sec:
                    parked_children.append(f"{ch_slug}({ch_activity})")
        elif ch_phase == "stopped" and ch_slug:
            stopped_children.append(ch_slug)

        if ch_phase in ("error", "halted"):
            abnormal_children.append({"slug": ch_slug, "phase": ch_phase, "silent_bucket": 0})
        elif ch_phase == "running" and ch_activity not in _IDLE_ACTIVITIES and child_silence_sec > 0:
            last_ts = _parse_epoch(
                ch.get("lastActivityEvent")
                or ch.get("last_activity_event")
                or ch.get("updatedAt")
                or ch.get("updated")
                or ch.get("createdAt")
                or ch.get("created")
            )
            if last_ts is not None and (now_ts - last_ts) >= child_silence_sec:
                bucket = int((now_ts - last_ts) // child_silence_sec)
                abnormal_children.append({"slug": ch_slug, "phase": ch_phase, "silent_bucket": bucket})

    if github_fetcher is not None:
        issues, comments_by_issue = _call_github_fetcher(github_fetcher, github_repo, issue_label, github_token)
    else:
        issues, comments_by_issue = fetch_github_issues_and_comments(
            github_repo,
            issue_label=issue_label,
            github_token=github_token,
        )

    classified = classify_github_issues(issues, comments_by_issue)
    actionable = classified["actionable"]
    po_replies = classified["po_replies"]
    fingerprint = compute_delta_fingerprint(classified, abnormal_children)
    has_actionable_work = bool(actionable or po_replies or abnormal_children)

    lead_phase_cur = str((lead_agent or {}).get("phase") or (lead_agent or {}).get("status") or "").lower()
    lead_activity_cur = str((lead_agent or {}).get("activity") or "").lower()
    lead_busy = bool(
        lead_agent is not None
        and lead_phase_cur in _ACTIVE_PHASES
        and (lead_phase_cur != "running" or (bool(lead_activity_cur) and lead_activity_cur not in _IDLE_ACTIVITIES))
    )

    cur_actionable_nums = sorted({int(iss.get("number", 0)) for iss in actionable if int(iss.get("number", 0)) > 0})
    prev_dispatched = {int(x) for x in (state.get("dispatched_issues") or []) if int(x) > 0}

    # Detect specialist pod eviction / SIGTERM stop OR running specialists stranded in
    # blocked/stalled state leaving open or in-review issues with zero active turns.
    last_progress_ts = max(float(state.get("last_wake_ts") or 0.0), latest_activity_ts)
    last_stall_ts = float(state.get("stalled_wip_ts") or 0.0)
    prior_blocked_wakes = (
        int(state.get("blocked_wip_wakes") or 0)
        if state.get("blocked_wip_fp") == fingerprint
        else 0
    )
    stopped_wip_stalled = bool(
        stopped_children
        and stalled_wip_sec > 0
        and (now_ts - last_progress_ts) >= stalled_wip_sec
        and (now_ts - last_stall_ts) >= max(stalled_wip_sec, 300)
    )
    blocked_wip_stalled = bool(
        parked_children
        and blocked_wip_sec > 0
        and prior_blocked_wakes < 2
        and (now_ts - float(state.get("last_wake_ts") or 0.0)) >= blocked_wip_sec
        and (now_ts - last_stall_ts) >= max(blocked_wip_sec, 300)
    )
    stalled_wip = bool(
        actionable
        and not po_replies
        and not abnormal_children
        and not lead_busy
        and not busy_children
        and (stopped_wip_stalled or blocked_wip_stalled)
    )

    # Suppress self-inflicted mid-wave echo wakes when lead/specialists move already-dispatched issues
    # between status:ready -> status:in-progress -> status:in-review -> status:verified, unless WIP is stalled.
    in_flight_echo = bool(
        has_actionable_work
        and fingerprint != state["last_fingerprint"]
        and not po_replies
        and not abnormal_children
        and not stalled_wip
        and prev_dispatched
        and cur_actionable_nums
        and set(cur_actionable_nums).issubset(prev_dispatched)
    )
    if in_flight_echo:
        if apply:
            state["last_fingerprint"] = fingerprint
            state["dispatched_issues"] = cur_actionable_nums
            state["idle_since"] = 0.0
            _save_delta_state(store, project_slug, state)
        return {
            "project_slug": project_slug,
            "lead_slug": resolved_lead,
            "action": "suppressed_in_flight_echo",
            "woke_lead": False,
            "rotated_lead": False,
            "auto_slept": False,
            "fingerprint": fingerprint,
            "actionable_count": len(actionable),
            "po_replies_count": len(po_replies),
            "waiting_po_count": len(classified["waiting_po"]),
            "abnormal_children_count": len(abnormal_children),
        }

    # 1. Actionable work exists and fingerprint changed (or WIP stalled after specialist stop) -> wake lead
    if has_actionable_work and (fingerprint != state["last_fingerprint"] or stalled_wip):
        rotated_lead = bool(
            int(state["lead_turns_in_epoch"]) >= max_turns_before_rotate and not any_running_children
        )
        if apply:
            eff_branch = (build_branch or os.environ.get("BUILD_BRANCH") or "").strip()
            eff_profile = (runtime_profile or os.environ.get("RUNTIME_PROFILE") or "").strip()
            eff_auth = os.environ.get("HARNESS_AUTH", "").strip() or (
                "vertex-ai" if os.environ.get("AGENT_RUNTIME", "kubernetes") == "kubernetes" else ""
            )
            create_body: dict[str, Any] = {
                "name": resolved_lead,
                "template": "lead",
                "task": (
                    "Read /scion-volumes/scratchpad/STATE.json (if present) and "
                    "/scion-volumes/knowledge-plane/INDEX.md, then stand by for DELTA instructions."
                ),
            }
            if eff_branch:
                create_body["branch"] = eff_branch
            if eff_profile:
                create_body["profile"] = eff_profile
            if eff_auth:
                create_body["harnessAuth"] = eff_auth

            if rotated_lead:
                try:
                    client.request("DELETE", f"/api/v1/projects/{pid}/agents/{resolved_lead}")
                except HubAPIError as err:
                    if err.status != 404:
                        raise
                state["lead_turns_in_epoch"] = 0
                state["dispatched_issues"] = []
                lead_agent = None

            lead_phase = str((lead_agent or {}).get("phase") or (lead_agent or {}).get("status") or "").lower()
            if lead_agent is None or lead_phase not in _ACTIVE_PHASES:
                try:
                    if rotated_lead and client.get_agent(pid, resolved_lead) is not None:
                        payload = {**create_body, "projectId": pid}
                        client.request("POST", f"/api/v1/projects/{pid}/agents", body=payload, timeout=180)
                    client.ensure_agent_started(
                        pid,
                        resolved_lead,
                        create_body=create_body,
                        force_resume=(lead_phase in ("error", "halted")),
                    )
                except HubAPIError as err:
                    # Provisioning or a concurrent start may already be launching lead (HTTP 409 start_in_progress);
                    # defer cleanly to the next tick instead of crashing the CronJob pod.
                    if err.status == 409:
                        return {
                            "project_slug": project_slug,
                            "lead_slug": resolved_lead,
                            "action": "start_in_progress",
                            "woke_lead": False,
                            "rotated_lead": rotated_lead,
                            "auto_slept": False,
                            "fingerprint": fingerprint,
                            "actionable_count": len(actionable),
                            "po_replies_count": len(po_replies),
                            "waiting_po_count": len(classified["waiting_po"]),
                            "abnormal_children_count": len(abnormal_children),
                        }
                    raise

            if not _wait_for_tmux(client, pid, resolved_lead):
                if rotated_lead:
                    _save_delta_state(store, project_slug, state)
                return {
                    "project_slug": project_slug,
                    "lead_slug": resolved_lead,
                    "action": "waiting_for_tmux",
                    "woke_lead": False,
                    "rotated_lead": rotated_lead,
                    "auto_slept": False,
                    "fingerprint": fingerprint,
                    "actionable_count": len(actionable),
                    "po_replies_count": len(po_replies),
                    "waiting_po_count": len(classified["waiting_po"]),
                    "abnormal_children_count": len(abnormal_children),
                }

            stalled_stopped = stopped_children if stopped_wip_stalled else None
            stalled_parked = parked_children if blocked_wip_stalled else None
            msg = _format_delta_message(
                classified,
                abnormal_children,
                rotated=rotated_lead,
                stopped_children=stalled_stopped,
                parked_children=stalled_parked,
            )
            try:
                client.request(
                    "POST",
                    f"/api/v1/projects/{pid}/agents/{resolved_lead}/message",
                    body={"message": msg},
                )
            except HubAPIError as err:
                if err.status == 409:
                    return {
                        "project_slug": project_slug,
                        "lead_slug": resolved_lead,
                        "action": "start_in_progress",
                        "woke_lead": False,
                        "rotated_lead": rotated_lead,
                        "auto_slept": False,
                        "fingerprint": fingerprint,
                        "actionable_count": len(actionable),
                        "po_replies_count": len(po_replies),
                        "waiting_po_count": len(classified["waiting_po"]),
                        "abnormal_children_count": len(abnormal_children),
                    }
                raise

            state["lead_sleeping"] = False
            state["idle_since"] = 0.0
            state["last_fingerprint"] = fingerprint
            state["dispatched_issues"] = cur_actionable_nums
            state["last_wake_ts"] = now_ts
            if stalled_wip:
                state["stalled_wip_ts"] = now_ts
            if state.get("blocked_wip_fp") != fingerprint:
                state["blocked_wip_fp"] = fingerprint
                state["blocked_wip_wakes"] = 1 if blocked_wip_stalled else 0
            elif blocked_wip_stalled:
                state["blocked_wip_wakes"] = prior_blocked_wakes + 1
            state["lead_turns_in_epoch"] = int(state["lead_turns_in_epoch"]) + 1
            _save_delta_state(store, project_slug, state)

        return {
            "project_slug": project_slug,
            "lead_slug": resolved_lead,
            "woke_lead": True,
            "stalled_wip": stalled_wip,
            "rotated_lead": rotated_lead,
            "auto_slept": False,
            "fingerprint": fingerprint,
            "actionable_count": len(actionable),
            "po_replies_count": len(po_replies),
            "waiting_po_count": len(classified["waiting_po"]),
            "abnormal_children_count": len(abnormal_children),
        }

    # 2. Completely idle (zero actionable issues, zero PO replies, zero busy children) -> grace timer & auto-sleep
    if not actionable and not po_replies and not busy_children:
        state_dirty = False
        if float(state.get("idle_since") or 0.0) <= 0.0:
            state["idle_since"] = now_ts
            state_dirty = True

        elapsed_idle = now_ts - float(state["idle_since"])
        # Ticks land a fraction of a second apart from an exact multiple of the cron period, so a
        # strict comparison misses the grace by milliseconds and costs a whole extra tick
        # (599.86s against 600s in hillclimb Run 1).
        if elapsed_idle + IDLE_TICK_JITTER_SEC >= idle_grace_sec:
            cleanup_warnings: list[str] = []
            if apply:
                for ch in child_agents:
                    ch_slug = str(ch.get("slug") or ch.get("name") or "")
                    ch_phase = str(ch.get("phase") or ch.get("status") or "").lower()
                    if not ch_slug:
                        continue
                    if ch_phase != "stopped":
                        try:
                            client.request("POST", f"/api/v1/projects/{pid}/agents/{ch_slug}/stop", body={})
                        except HubAPIError as err:
                            if err.status not in (404, 409):
                                raise
                    try:
                        client.request("DELETE", f"/api/v1/projects/{pid}/agents/{ch_slug}")
                    except HubAPIError as err:
                        # The control-plane identity is project-admin, which may stop agents but
                        # not delete them. A stopped agent holds no pod, so leave it and go on to
                        # stop lead; raising here kept lead running forever (hillclimb Run 1).
                        if err.status == 403:
                            cleanup_warnings.append(f"could not delete stopped agent {ch_slug} (HTTP 403)")
                        elif err.status != 404:
                            raise

                lead_phase = str((lead_agent or {}).get("phase") or (lead_agent or {}).get("status") or "").lower()
                if lead_agent is not None and lead_phase != "stopped":
                    try:
                        client.request("POST", f"/api/v1/projects/{pid}/agents/{resolved_lead}/stop", body={})
                    except HubAPIError as err:
                        if err.status not in (404, 409):
                            raise

                if (
                    not state["lead_sleeping"]
                    or int(state["lead_turns_in_epoch"]) != 0
                    or state["last_fingerprint"] != fingerprint
                    or state.get("dispatched_issues")
                ):
                    state["lead_sleeping"] = True
                    state["lead_turns_in_epoch"] = 0
                    state["last_fingerprint"] = fingerprint
                    state["dispatched_issues"] = []
                    state["stalled_wip_ts"] = 0.0
                    state["blocked_wip_wakes"] = 0
                    state["blocked_wip_fp"] = ""
                    state_dirty = True

                if state_dirty:
                    _save_delta_state(store, project_slug, state)

            return {
                "project_slug": project_slug,
                "lead_slug": resolved_lead,
                "woke_lead": False,
                "rotated_lead": False,
                "auto_slept": True,
                "fingerprint": fingerprint,
                "idle_elapsed_sec": elapsed_idle,
                "waiting_po_count": len(classified["waiting_po"]),
                "cleanup_warnings": cleanup_warnings,
            }

        if apply and state_dirty:
            _save_delta_state(store, project_slug, state)

        return {
            "project_slug": project_slug,
            "lead_slug": resolved_lead,
            "woke_lead": False,
            "rotated_lead": False,
            "auto_slept": False,
            "fingerprint": fingerprint,
            "idle_elapsed_sec": elapsed_idle,
            "waiting_po_count": len(classified["waiting_po"]),
        }

    # 3. Work in flight / unchanged delta -> reset idle timer if previously set, spend zero tokens
    if apply and float(state.get("idle_since") or 0.0) != 0.0:
        state["idle_since"] = 0.0
        _save_delta_state(store, project_slug, state)

    return {
        "project_slug": project_slug,
        "lead_slug": resolved_lead,
        "woke_lead": False,
        "rotated_lead": False,
        "auto_slept": False,
        "fingerprint": fingerprint,
        "actionable_count": len(actionable),
        "po_replies_count": len(po_replies),
        "waiting_po_count": len(classified["waiting_po"]),
        "abnormal_children_count": len(abnormal_children),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Zero-LLM Scion factory delta controller")
    parser.add_argument("--apply", action="store_true", help="Execute wake/rotate/sleep mutations")
    parser.add_argument("--dry-run", action="store_true", help="Compute delta plan without mutating Hub or Firestore")
    parser.add_argument("--project-slug", default="", help="Single factory project slug (default: FACTORY_PROJECTS)")
    parser.add_argument("--lead-slug", default="", help="Lead agent slug (default: LEAD_SLUG or auto-detected)")
    parser.add_argument("--github-repo", default="", help="GitHub owner/repo (default: GITHUB_REPO)")
    parser.add_argument("--issue-label", default="", help="GitHub issue label filter (default: ISSUE_LABEL)")
    parser.add_argument("--build-branch", default="", help="Target Git branch for recreated lead (default: BUILD_BRANCH)")
    parser.add_argument("--runtime-profile", default="", help="Runtime profile for recreated lead (default: RUNTIME_PROFILE)")
    args = parser.parse_args(argv)

    env_dry = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    effective_apply = bool(args.apply) and not bool(args.dry_run) and not env_dry

    raw_projects = (
        args.project_slug
        or os.environ.get("FACTORY_PROJECTS", "")
        or os.environ.get("BUILD_PROJECT", "")
    )
    projects = [
        p.strip()
        for p in raw_projects.split(",")
        if p.strip() and p.strip() != "global" and not p.strip().endswith("-eval")
    ]
    if not projects:
        print("error: set --project-slug or FACTORY_PROJECTS", file=sys.stderr)
        return 1

    github_repo = (args.github_repo or os.environ.get("GITHUB_REPO", "")).strip()
    issue_label = (args.issue_label or os.environ.get("ISSUE_LABEL", "")).strip()
    lead_slug = (args.lead_slug or os.environ.get("LEAD_SLUG", "")).strip()
    build_branch = (args.build_branch or os.environ.get("BUILD_BRANCH", "")).strip()
    runtime_profile = (args.runtime_profile or os.environ.get("RUNTIME_PROFILE", "")).strip()
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()

    idle_grace_sec = int(os.environ.get("IDLE_STOP_GRACE_SEC", "600"))
    max_turns = int(os.environ.get("LEAD_MAX_TURNS_BEFORE_ROTATE", "25"))
    child_silence_sec = int(os.environ.get("CHILD_SILENCE_TIMEOUT_SEC", "1800"))
    blocked_wip_sec = int(os.environ.get("BLOCKED_WIP_TIMEOUT_SEC", "900"))

    client = HubClient.from_env()
    store = factory_store.store_from_env(dry_run=not effective_apply)

    results = []
    ok_count = 0
    for idx, slug in enumerate(projects):
        per_project_lead = lead_slug if (len(projects) == 1 or idx == 0) else ""
        try:
            summary = run_delta_tick(
                client,
                store,
                project_slug=slug,
                lead_slug=per_project_lead,
                github_repo=github_repo,
                issue_label=issue_label,
                github_token=github_token,
                apply=effective_apply,
                idle_grace_sec=idle_grace_sec,
                max_turns_before_rotate=max_turns,
                child_silence_sec=child_silence_sec,
                blocked_wip_sec=blocked_wip_sec,
                build_branch=build_branch,
                runtime_profile=runtime_profile,
            )
            ok_count += 1
            results.append(summary)
        except HubAPIError as exc:
            if exc.status == 403 and len(projects) > 1:
                results.append({"project_slug": slug, "skipped_forbidden": True})
                continue
            raise

    if ok_count == 0 and results:
        return 1
    print(json.dumps({"apply": effective_apply, "results": results}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
