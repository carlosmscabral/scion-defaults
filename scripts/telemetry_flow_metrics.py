#!/usr/bin/env python3
"""Compute topology-agnostic flow latencies, artifact audits, and friction signals.

Why this module exists:
- Keyword counts alone cannot distinguish healthy parallel execution from serial
  slot queues, TUI inbox stalls, or regex undercounting (`PASS` vs `APPROVE`).
- Different Scion factories compile different verifier, producer, and releaser
  rosters (`reviewer`, `security-auditor`, `deployer`, or zero releasers).
- This pure-stdlib module computes p50/p95 turnaround latencies, disk verdict
  reconciliation, and unanswered directive lag dynamically from role capabilities.
"""

from __future__ import annotations

import datetime, glob, math, os, re, time
from typing import Any

SYMPTOM_PATTERNS: dict[str, re.Pattern[str]] = {
    "verdict_approve": re.compile(r"\bVERDICT:\s*(?:PASS|APPROVE)\b|\bAPPROVED\b", re.I),
    "verdict_fail": re.compile(r"\bVERDICT:\s*FAIL|\bREJECT(?:ED)?\b|\bREQUEST[_\s-]CHANGES\b", re.I),
    "rebase_churn": re.compile(r"rebase-only|trunk moved|non-fast-forward|merge conflict", re.I),
    "commit_slot_wait": re.compile(
        r"(?:COMMIT|edit)\s+SLOT\b(?!.*\bgranted\b)|queued behind|waiting for slot", re.I
    ),
    "missing_route_or_symbol_claim": re.compile(
        r"does not exist|not implemented|missing endpoint|missing route", re.I
    ),
    "rate_limit_or_quota": re.compile(r"\b429\b|RESOURCE_EXHAUSTED|rate limit|quota exceeded", re.I),
    "hot_upgrade_delta": re.compile(r"\[HOT-UPGRADE (?:RULE DELTA|STAFFING)\]", re.I),
}

SLOT_GRANT_RE = re.compile(r"\b(?:COMMIT|EDIT)\s+SLOT\b.*\b(?:granted|go)\b|\bCOMMIT\s+SLOT\s+#", re.I)
SLOT_DONE_RE = re.compile(
    r"\b(?:landed|fast-forwarded|pushed\s+to\s+(?:origin/)?main|slot\s+released)\b", re.I
)
RELEASE_DONE_RE = re.compile(
    r"\bdeployed\s+and\s+verified\b|\blive\s+revision\b.*000\d+|\bLIVE\.md\s+pushed\b", re.I
)
DISK_PASS_RE = re.compile(r"\bVERDICT\s*:\s*(?:PASS|APPROVE)\b", re.I)
DISK_FAIL_RE = re.compile(r"\bVERDICT\s*:\s*FAIL\b", re.I)

def parse_utc_epoch(raw: str) -> float | None:
    """Parse SQLite or ISO-8601 UTC timestamps into POSIX seconds."""
    text = (raw or "").strip()
    if not text:
        return None
    text = re.sub(r"\s*\+0000(?:\s+UTC)?$", "Z", text)
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "." in text:
        head, tail = text.split(".", 1)
        tz_idx = max(tail.find("+"), tail.find("-"))
        if tz_idx != -1:
            tail = tail[: min(6, tz_idx)] + tail[tz_idx:]
        else:
            tail = tail[:6]
        text = f"{head}.{tail}"
    try:
        dt = datetime.datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None

def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Compute a linear-interpolated percentile on a non-empty sorted list."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return round(sorted_vals[0], 2)
    pos = (len(sorted_vals) - 1) * pct
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    val = sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)
    return round(val, 2)

def _dist_summary(samples_min: list[float]) -> dict[str, float | int]:
    """Summarize a list of duration samples in minutes."""
    if not samples_min:
        return {"count": 0, "p50": 0.0, "p95": 0.0, "max": 0.0}
    ordered = sorted(samples_min)
    return {
        "count": len(ordered),
        "p50": _percentile(ordered, 0.50),
        "p95": _percentile(ordered, 0.95),
        "max": round(ordered[-1], 2),
    }

def resolve_role_sets(
    roster: list[dict[str, Any]],
    manifest: dict[str, Any] | None = None,
) -> dict[str, set[str]]:
    """Resolve coordinator, verifier, and releaser agent slugs dynamically."""
    coordinators: set[str] = set()
    verifiers: set[str] = set()
    releasers: set[str] = set()

    if manifest:
        if manifest.get("coordinator_role"):
            coordinators.add(str(manifest["coordinator_role"]))
        verifiers.update(str(x) for x in (manifest.get("verifier_roles") or []))
        releasers.update(str(x) for x in (manifest.get("releaser_roles") or []))

    for entry in roster:
        slug = str(entry.get("agent") or "")
        tpl = str(entry.get("template") or slug).lower()
        low = slug.lower()
        if not slug:
            continue
        if tpl in ("lead", "coordinator") or low.endswith("-lead") and "eval" not in low:
            coordinators.add(slug)
        elif any(k in tpl or k in low for k in ("reviewer", "auditor", "performance-analyst", "compat-checker")):
            verifiers.add(slug)
        elif any(k in tpl or k in low for k in ("deployer", "release-manager")):
            if not manifest or manifest.get("deploy_mode", "service") != "none":
                releasers.add(slug)

    if not coordinators:
        coordinators.add("lead")
    return {"coordinators": coordinators, "verifiers": verifiers, "releasers": releasers}

def scan_verdict_artifacts(
    scion_home: str,
    build_slug: str,
    window_hours: int,
    msg_approve: int,
    msg_fail: int,
) -> dict[str, Any]:
    """Scan scratchpad reviews/ files modified in the window to cross-check message regex counts."""
    cutoff = time.time() - (window_hours * 3600)
    roots = sorted(
        glob.glob(os.path.join(scion_home, "project-configs", f"{build_slug}__*", "shared-dirs", "scratchpad", "reviews"))
    )
    if os.path.isdir("/scion-volumes/scratchpad/reviews"):
        roots.append("/scion-volumes/scratchpad/reviews")

    seen: set[str] = set()
    pass_files = 0
    fail_files = 0
    for rdir in roots:
        for dirpath, _, filenames in os.walk(rdir):
            for fname in filenames:
                if not fname.endswith(".md"):
                    continue
                fpath = os.path.realpath(os.path.join(dirpath, fname))
                if fpath in seen:
                    continue
                seen.add(fpath)
                try:
                    if os.path.getmtime(fpath) < cutoff:
                        continue
                    head = open(fpath, encoding="utf-8", errors="ignore").read(2048)
                except OSError:
                    continue
                if DISK_FAIL_RE.search(head):
                    fail_files += 1
                elif DISK_PASS_RE.search(head):
                    pass_files += 1

    drift = (pass_files > 0 and msg_approve * 2 < pass_files) or (fail_files > 0 and msg_fail * 2 < fail_files)
    return {
        "pass_files": pass_files,
        "fail_files": fail_files,
        "sensor_drift_warning": drift,
    }

def compute_flow_and_friction(
    records: list[dict[str, Any]],
    roster: list[dict[str, Any]],
    manifest: dict[str, Any] | None = None,
    now_ts: float | None = None,
) -> tuple[dict[str, int], dict[str, Any], dict[str, Any]]:
    """Compute verifier inbound distribution, flow latencies, and operational friction."""
    roles = resolve_role_sets(roster, manifest)
    coordinators = roles["coordinators"]
    verifiers = roles["verifiers"]
    releasers = roles["releasers"]

    verifier_inbound: dict[str, int] = {}
    timed: list[tuple[float, str, str, str]] = []
    for rec in records:
        sender = str(rec.get("sender") or "")
        recipient = str(rec.get("recipient") or "")
        body = str(rec.get("msg") or "")
        r_slug = recipient.split(":", 1)[-1] if recipient.startswith("agent:") else ""
        if r_slug and (r_slug in verifiers or "reviewer" in r_slug):
            verifier_inbound[r_slug] = verifier_inbound.get(r_slug, 0) + 1
        ts = parse_utc_epoch(str(rec.get("created") or ""))
        if ts is not None:
            timed.append((ts, sender, recipient, body))

    timed.sort(key=lambda x: x[0])
    pending_review: dict[str, float] = {}
    pending_slot: dict[str, float] = {}
    last_directive_ts: dict[str, float] = {}
    last_reply_ts: dict[str, float] = {}
    review_mins: list[float] = []
    slot_mins: list[float] = []
    landings = 0
    releases = 0

    for ts, sender, recipient, body in timed:
        s_slug = sender.split(":", 1)[-1] if sender.startswith("agent:") else ""
        r_slug = recipient.split(":", 1)[-1] if recipient.startswith("agent:") else ""

        if s_slug and s_slug not in coordinators:
            last_reply_ts[s_slug] = ts

        if s_slug in coordinators and r_slug and r_slug not in coordinators:
            last_directive_ts[r_slug] = ts
            if r_slug in verifiers or "reviewer" in r_slug:
                pending_review[r_slug] = ts
            if SLOT_GRANT_RE.search(body):
                pending_slot[r_slug] = ts

        if (s_slug in verifiers or "reviewer" in s_slug) and r_slug in coordinators:
            if SYMPTOM_PATTERNS["verdict_approve"].search(body) or SYMPTOM_PATTERNS["verdict_fail"].search(body):
                start = pending_review.pop(s_slug, None)
                if start is not None and 0.0 <= (ts - start) <= 7200.0:
                    review_mins.append((ts - start) / 60.0)

        if s_slug and r_slug in coordinators and SLOT_DONE_RE.search(body):
            if s_slug not in releasers:
                landings += 1
            start = pending_slot.pop(s_slug, None)
            if start is not None and 0.0 <= (ts - start) <= 7200.0:
                slot_mins.append((ts - start) / 60.0)

        if s_slug in releasers and r_slug in coordinators and RELEASE_DONE_RE.search(body):
            releases += 1

    ref_now = now_ts if now_ts is not None else time.time()
    roster_by_slug = {str(r.get("agent") or ""): r for r in roster if r.get("agent")}
    open_lag_by_agent: dict[str, float] = {}
    for ag_slug, sent_ts in last_directive_ts.items():
        if sent_ts <= last_reply_ts.get(ag_slug, 0.0):
            continue
        r_entry = roster_by_slug.get(ag_slug) or {}
        activity = str(r_entry.get("activity") or "").lower()
        upd_ts = parse_utc_epoch(str(r_entry.get("updated") or ""))
        completed_after_dm = activity in ("completed", "idle", "blocked") and (
            upd_ts is not None and upd_ts >= sent_ts - 5.0
        )
        if completed_after_dm:
            continue
        lag_m = max(0.0, (ref_now - sent_ts) / 60.0)
        if lag_m <= 360.0:
            open_lag_by_agent[ag_slug] = round(lag_m, 2)

    phase_drift = sorted(
        str(r.get("agent") or "")
        for r in roster
        if str(r.get("container_status") or "").startswith("Up") and str(r.get("phase") or "") != "running"
    )
    lagging = {k: v for k, v in sorted(open_lag_by_agent.items()) if v >= 15.0}
    ratio = round(landings / releases, 2) if releases > 0 else float(landings)

    flow = {
        "verifier_turnaround_min": _dist_summary(review_mins),
        "slot_hold_min": _dist_summary(slot_mins),
        "trunk_landings_24h": landings,
        "releases_24h": releases,
        "landings_per_release": ratio,
        "solo_release_serialization": bool(releases >= 3 and ratio < 1.5),
    }
    friction = {
        "max_unanswered_directive_lag_min": max(open_lag_by_agent.values(), default=0.0),
        "lagging_agents_over_15m": lagging,
        "phase_drift_agents": phase_drift,
    }
    return verifier_inbound, flow, friction
