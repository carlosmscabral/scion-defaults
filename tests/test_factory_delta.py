from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import factory_delta  # noqa: E402
import hub_client  # noqa: E402


class FakeDeltaStore:
    def __init__(self):
        self.docs: dict[tuple[str, str], dict] = {}

    def get_document(self, collection: str, doc_id: str) -> dict | None:
        doc = self.docs.get((collection, doc_id))
        return dict(doc) if doc is not None else None

    def upsert_document(self, collection: str, doc_id: str, data: dict) -> None:
        clean = {k: ("2026-11-10T20:00:00Z" if v == "REQUEST_TIME" else v) for k, v in data.items()}
        self.docs[(collection, doc_id)] = clean


class _DeltaHubHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, status: int, payload: dict | list) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(raw)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        self.server.requests.append({"method": "GET", "path": self.path})
        if self.path == "/api/v1/projects":
            self._send_json(200, {"projects": list(self.server.projects.values())})
            return

        m_agents = re.match(r"^/api/v1/projects/([^/]+)/agents$", self.path)
        if m_agents:
            pid = m_agents.group(1)
            agents = list(self.server.agents.get(pid, {}).values())
            self._send_json(200, {"agents": agents})
            return

        m_agent = re.match(r"^/api/v1/projects/([^/]+)/agents/([^/]+)$", self.path)
        if m_agent:
            pid, slug = m_agent.group(1), m_agent.group(2)
            agent = self.server.agents.get(pid, {}).get(slug)
            if not agent:
                self._send_json(404, {"error": "not found"})
                return
            self._send_json(200, agent)
            return

        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        body = self._read_body()
        self.server.requests.append({"method": "POST", "path": self.path, "body": body})

        m_create = re.match(r"^/api/v1/projects/([^/]+)/agents$", self.path)
        if m_create:
            pid = m_create.group(1)
            slug = body.get("name") or body.get("slug")
            rec = {
                "id": f"id-{slug}",
                "slug": slug,
                "name": slug,
                "template": body.get("template", ""),
                "phase": "running",
                "activity": "idle",
                "create_body": body,
            }
            self.server.agents.setdefault(pid, {})[slug] = rec
            self._send_json(201, {"agent": rec})
            return

        m_start = re.match(r"^/api/v1/projects/([^/]+)/agents/([^/]+)/start$", self.path)
        if m_start:
            pid, slug = m_start.group(1), m_start.group(2)
            agent = self.server.agents.get(pid, {}).get(slug)
            if not agent:
                self._send_json(404, {"error": "not found"})
                return
            agent["phase"] = "running"
            self._send_json(200, {"agent": agent})
            return

        m_stop = re.match(r"^/api/v1/projects/([^/]+)/agents/([^/]+)/stop$", self.path)
        if m_stop:
            pid, slug = m_stop.group(1), m_stop.group(2)
            agent = self.server.agents.get(pid, {}).get(slug)
            if agent:
                agent["phase"] = "stopped"
            self._send_json(200, {"status": "stopped"})
            return

        m_exec = re.match(r"^/api/v1/projects/([^/]+)/agents/([^/]+)/exec$", self.path)
        if m_exec:
            if getattr(self.server, "exec_denied", False):
                self._send_json(403, {"error": "active bindings do not include permission \"agent.attach\""})
                return
            code = getattr(self.server, "exec_exit_code", 0)
            self._send_json(200, {"exitCode": code, "output": ""})
            return

        m_msg = re.match(r"^/api/v1/projects/([^/]+)/agents/([^/]+)/message$", self.path)
        if m_msg:
            pid, slug = m_msg.group(1), m_msg.group(2)
            agent = self.server.agents.get(pid, {}).get(slug)
            if not agent or agent.get("phase") != "running":
                self._send_json(409, {"error": "agent not running"})
                return
            self.server.messages.append({"pid": pid, "slug": slug, "body": body})
            self._send_json(200, {"id": f"msg-{len(self.server.messages)}"})
            return

        self._send_json(404, {"error": "not found"})

    def do_DELETE(self):
        self.server.requests.append({"method": "DELETE", "path": self.path})
        m_agent = re.match(r"^/api/v1/projects/([^/]+)/agents/([^/]+)$", self.path)
        if m_agent:
            pid, slug = m_agent.group(1), m_agent.group(2)
            if getattr(self.server, "delete_denied", False):
                self._send_json(403, {"error": {"code": "forbidden", "message": "insufficient permission for this agent action"}})
                return
            self.server.agents.get(pid, {}).pop(slug, None)
            self._send_json(200, {"status": "deleted"})
            return
        self._send_json(404, {"error": "not found"})


@pytest.fixture
def delta_hub():
    srv = HTTPServer(("127.0.0.1", 0), _DeltaHubHandler)
    srv.projects = {"bananacast": {"id": "uuid-bc", "slug": "bananacast"}}
    srv.agents = {"uuid-bc": {}}
    srv.requests = []
    srv.messages = []
    srv.url = f"http://127.0.0.1:{srv.server_port}"
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


def test_classify_po_decision_distinguishes_uncommented_agent_and_human_comments():
    assert factory_delta.AGENT_COMMENT_MARKER == "<!-- scion-agent:"

    issues = [
        {"number": 1, "title": "Weekly digest", "labels": [{"name": "digest"}]},
        {"number": 2, "title": "Old wave", "labels": [{"name": "hillclimb-archive"}]},
        {"number": 3, "title": "Already shipped", "labels": [{"name": "status:verified"}]},
        {"number": 4, "title": "PO question, zero comments", "labels": [{"name": "status:po-decision"}]},
        {"number": 5, "title": "PO question, agent comment last", "labels": [{"name": "status:po-decision"}]},
        {"number": 6, "title": "Blocked on infra, agent comment last", "labels": [{"name": "status:blocked"}]},
        {"number": 7, "title": "PO question, human replied", "labels": [{"name": "status:po-decision"}], "comments": 2},
        {"number": 8, "title": "PO approved via label", "labels": [{"name": "status:po-decision"}, {"name": "status:approved"}]},
        {"number": 9, "title": "Implement healthz endpoint", "labels": [{"name": "factory:bananacast-hc1"}]},
    ]
    comments_by_issue = {
        4: [],
        5: [{"id": 501, "body": "<!-- scion-agent: bananacast-lead -->\nNeed decision on retention window."}],
        6: [{"id": 601, "body": "<!-- scion-agent: bananacast-backend -->\nBlocked waiting on bucket name."}],
        7: [
            {"id": 701, "body": "<!-- scion-agent: bananacast-lead -->\nOption A or B?"},
            {"id": 702, "body": "Use Option A with 7-day TTL."},
        ],
        8: [{"id": 801, "body": "<!-- scion-agent: bananacast-lead -->\nAwaiting approval."}],
    }

    classified = factory_delta.classify_github_issues(issues, comments_by_issue)
    assert [i["number"] for i in classified["waiting_po"]] == [4, 5, 6]
    assert [i["number"] for i in classified["po_replies"]] == [7, 8]
    assert [i["number"] for i in classified["actionable"]] == [9]

    # Deterministic fingerprint across input ordering + sensitivity to PO reply comment ID
    fp1 = factory_delta.compute_delta_fingerprint(
        classified,
        [{"slug": "bananacast-backend", "phase": "halted", "silent_bucket": 0}],
    )
    classified_reversed = factory_delta.classify_github_issues(list(reversed(issues)), comments_by_issue)
    fp2 = factory_delta.compute_delta_fingerprint(
        classified_reversed,
        [{"slug": "bananacast-backend", "phase": "halted", "silent_bucket": 0}],
    )
    assert fp1 == fp2 and len(fp1) == 64

    comments_by_issue_new = {
        **comments_by_issue,
        7: [
            *comments_by_issue[7],
            {"id": 703, "body": "Actually make it 14-day TTL."},
        ],
    }
    fp3 = factory_delta.compute_delta_fingerprint(
        factory_delta.classify_github_issues(issues, comments_by_issue_new),
        [{"slug": "bananacast-backend", "phase": "halted", "silent_bucket": 0}],
    )
    assert fp3 != fp1


def test_delta_tick_wakes_sleeping_lead_once_and_stays_silent_on_unchanged_fingerprint(delta_hub):
    store = FakeDeltaStore()
    store.upsert_document(
        "factory_control_state",
        "delta__bananacast",
        {"last_fingerprint": "", "idle_since": 900.0, "lead_sleeping": True, "lead_turns_in_epoch": 0},
    )
    delta_hub.agents["uuid-bc"]["bananacast-lead"] = {
        "id": "id-lead",
        "slug": "bananacast-lead",
        "name": "bananacast-lead",
        "template": "lead",
        "phase": "stopped",
    }

    issues = [
        {
            "number": 12,
            "title": "Add strict CORS origin check",
            "labels": [{"name": "factory:bananacast-hc1"}],
            "comments": 0,
        }
    ]
    client = hub_client.HubClient(base_url=delta_hub.url, token="tok-admin")

    # Dry-run first: zero mutations to Hub or Firestore
    dry = factory_delta.run_delta_tick(
        client,
        store,
        project_slug="bananacast",
        lead_slug="bananacast-lead",
        apply=False,
        now=1000.0,
        github_fetcher=lambda *_: (issues, {}),
    )
    assert dry["woke_lead"] is True
    assert len(delta_hub.messages) == 0
    assert delta_hub.agents["uuid-bc"]["bananacast-lead"]["phase"] == "stopped"

    # First apply tick: starts lead, checks tmux, sends 1 DELTA: message, clears lead_sleeping
    res1 = factory_delta.run_delta_tick(
        client,
        store,
        project_slug="bananacast",
        lead_slug="bananacast-lead",
        apply=True,
        now=1000.0,
        github_fetcher=lambda *_: (issues, {}),
    )
    assert res1["woke_lead"] is True
    assert delta_hub.agents["uuid-bc"]["bananacast-lead"]["phase"] == "running"
    assert len(delta_hub.messages) == 1
    assert delta_hub.messages[0]["body"]["message"].startswith("DELTA:")
    assert "#12" in delta_hub.messages[0]["body"]["message"]
    exec_reqs = [r for r in delta_hub.requests if r["method"] == "POST" and r["path"].endswith("/exec")]
    assert any(r["body"].get("command") == ["tmux", "has-session", "-t", "scion"] for r in exec_reqs)

    saved = store.get_document("factory_control_state", "delta__bananacast")
    assert saved["lead_sleeping"] is False
    assert saved["idle_since"] == 0
    assert saved["lead_turns_in_epoch"] == 1
    assert saved["last_fingerprint"]

    # Second tick with identical issues: sends 0 messages
    res2 = factory_delta.run_delta_tick(
        client,
        store,
        project_slug="bananacast",
        lead_slug="bananacast-lead",
        apply=True,
        now=1120.0,
        github_fetcher=lambda *_: (issues, {}),
    )
    assert res2["woke_lead"] is False
    assert len(delta_hub.messages) == 1
    assert store.get_document("factory_control_state", "delta__bananacast")["lead_turns_in_epoch"] == 1


def test_delta_tick_auto_sleeps_lead_and_stops_idle_specialists_after_grace(delta_hub):
    store = FakeDeltaStore()
    store.upsert_document(
        "factory_control_state",
        "delta__bananacast",
        {"last_fingerprint": "prev-fp", "idle_since": 0.0, "lead_sleeping": False, "lead_turns_in_epoch": 6},
    )
    delta_hub.agents["uuid-bc"] = {
        "bananacast-lead": {"id": "id-lead", "slug": "bananacast-lead", "template": "lead", "phase": "running", "activity": "idle"},
        "bananacast-reviewer": {"id": "id-rev", "slug": "bananacast-reviewer", "template": "reviewer", "phase": "running", "activity": "idle"},
        "bananacast-backend": {"id": "id-be", "slug": "bananacast-backend", "template": "backend", "phase": "stopped"},
    }
    # Only verified and waiting-on-PO issues exist -> actionable == [] and po_replies == []
    issues = [
        {"number": 12, "title": "Done", "labels": [{"name": "status:verified"}]},
        {"number": 14, "title": "Waiting PO", "labels": [{"name": "status:po-decision"}]},
    ]
    comments = {14: [{"id": 1401, "body": "<!-- scion-agent: bananacast-lead -->\nNeed PO input."}]}
    client = hub_client.HubClient(base_url=delta_hub.url, token="tok-admin")

    # Tick at t=2000: records idle_since=2000, does not sleep yet
    r1 = factory_delta.run_delta_tick(
        client,
        store,
        project_slug="bananacast",
        lead_slug="bananacast-lead",
        apply=True,
        now=2000.0,
        idle_grace_sec=600,
        github_fetcher=lambda *_: (issues, comments),
    )
    assert r1["auto_slept"] is False
    st1 = store.get_document("factory_control_state", "delta__bananacast")
    assert st1["idle_since"] == 2000.0
    assert st1["lead_sleeping"] is False
    assert delta_hub.agents["uuid-bc"]["bananacast-lead"]["phase"] == "running"

    # Tick at t=2605 (elapsed 605 >= 600): stops/deletes specialists, stops lead, sets lead_sleeping=True & turns=0
    r2 = factory_delta.run_delta_tick(
        client,
        store,
        project_slug="bananacast",
        lead_slug="bananacast-lead",
        apply=True,
        now=2605.0,
        idle_grace_sec=600,
        github_fetcher=lambda *_: (issues, comments),
    )
    assert r2["auto_slept"] is True
    stop_paths = [r["path"] for r in delta_hub.requests if r["method"] == "POST" and r["path"].endswith("/stop")]
    assert "/api/v1/projects/uuid-bc/agents/bananacast-lead/stop" in stop_paths
    # Idle/leftover specialists must also be stopped or deleted
    specialist_cleaned = (
        "/api/v1/projects/uuid-bc/agents/bananacast-reviewer/stop" in stop_paths
        or any(
            r["method"] == "DELETE" and r["path"] == "/api/v1/projects/uuid-bc/agents/bananacast-reviewer"
            for r in delta_hub.requests
        )
    )
    assert specialist_cleaned

    st2 = store.get_document("factory_control_state", "delta__bananacast")
    assert st2["lead_sleeping"] is True
    assert st2["lead_turns_in_epoch"] == 0

    # Subsequent idle tick at t=2725 does not re-issue /stop on already-stopped sleeping lead
    stops_before = len([r for r in delta_hub.requests if r["method"] == "POST" and r["path"].endswith("/bananacast-lead/stop")])
    factory_delta.run_delta_tick(
        client,
        store,
        project_slug="bananacast",
        lead_slug="bananacast-lead",
        apply=True,
        now=2725.0,
        idle_grace_sec=600,
        github_fetcher=lambda *_: (issues, comments),
    )
    stops_after = len([r for r in delta_hub.requests if r["method"] == "POST" and r["path"].endswith("/bananacast-lead/stop")])
    assert stops_after == stops_before


def test_delta_tick_rotates_lead_session_after_max_turns_when_no_children_running(delta_hub):
    store = FakeDeltaStore()
    store.upsert_document(
        "factory_control_state",
        "delta__bananacast",
        {"last_fingerprint": "old-fp", "idle_since": 0.0, "lead_sleeping": False, "lead_turns_in_epoch": 25},
    )
    delta_hub.agents["uuid-bc"] = {
        "bananacast-lead": {"id": "id-lead", "slug": "bananacast-lead", "template": "lead", "phase": "running"},
        "bananacast-backend": {"id": "id-be", "slug": "bananacast-backend", "template": "backend", "phase": "running", "activity": "working"},
    }
    client = hub_client.HubClient(base_url=delta_hub.url, token="tok-admin")

    # 1) While a child specialist is actively running, do NOT rotate lead mid-flight
    issues_wave_a = [{"number": 19, "title": "In-flight", "labels": [{"name": "factory:bananacast-hc1"}], "comments": 1}]
    res_mid = factory_delta.run_delta_tick(
        client,
        store,
        project_slug="bananacast",
        lead_slug="bananacast-lead",
        apply=True,
        now=3000.0,
        max_turns_before_rotate=25,
        github_fetcher=lambda *_: (issues_wave_a, {}),
    )
    assert res_mid.get("rotated_lead") is False
    assert not any(r["method"] == "DELETE" and r["path"].endswith("/bananacast-lead") for r in delta_hub.requests)
    assert store.get_document("factory_control_state", "delta__bananacast")["lead_turns_in_epoch"] == 26

    # 2) Once running_children == [], next actionable delta deletes + recreates lead and resets counter
    delta_hub.agents["uuid-bc"].pop("bananacast-backend")
    issues_wave_b = [{"number": 20, "title": "Wave 2 feature", "labels": [{"name": "factory:bananacast-hc1"}], "comments": 0}]
    res_rot = factory_delta.run_delta_tick(
        client,
        store,
        project_slug="bananacast",
        lead_slug="bananacast-lead",
        apply=True,
        now=3120.0,
        max_turns_before_rotate=25,
        github_fetcher=lambda *_: (issues_wave_b, {}),
    )
    assert res_rot["rotated_lead"] is True
    delete_idx = next(
        i for i, r in enumerate(delta_hub.requests)
        if r["method"] == "DELETE" and r["path"] == "/api/v1/projects/uuid-bc/agents/bananacast-lead"
    )
    create_idx = next(
        i for i, r in enumerate(delta_hub.requests)
        if r["method"] == "POST" and r["path"] == "/api/v1/projects/uuid-bc/agents"
    )
    assert delete_idx < create_idx
    created_task = delta_hub.requests[create_idx]["body"]["task"]
    assert "/scion-volumes/scratchpad/STATE.json" in created_task

    st = store.get_document("factory_control_state", "delta__bananacast")
    assert st["lead_turns_in_epoch"] == 1
    assert "#20" in delta_hub.messages[-1]["body"]["message"]


def test_delta_tick_wakes_lead_on_abnormal_halted_or_silent_child(delta_hub):
    store = FakeDeltaStore()
    delta_hub.agents["uuid-bc"] = {
        "bananacast-lead": {"id": "id-lead", "slug": "bananacast-lead", "template": "lead", "phase": "running"},
        "bananacast-backend": {
            "id": "id-be",
            "slug": "bananacast-backend",
            "template": "backend",
            "phase": "running",
            "activity": "working",
            "lastActivityEvent": "2026-11-10T19:00:00Z",
        },
    }
    client = hub_client.HubClient(base_url=delta_hub.url, token="tok-admin")
    # 2026-11-10T19:35:00Z is 2100s after 19:00:00Z (>= 1800s -> silent_bucket == 1)
    import datetime

    now_ts = datetime.datetime(2026, 11, 10, 19, 35, 0, tzinfo=datetime.timezone.utc).timestamp()
    res = factory_delta.run_delta_tick(
        client,
        store,
        project_slug="bananacast",
        lead_slug="bananacast-lead",
        apply=True,
        now=now_ts,
        child_silence_sec=1800,
        github_fetcher=lambda *_: ([], {}),
    )
    assert res["woke_lead"] is True
    assert "bananacast-backend" in delta_hub.messages[-1]["body"]["message"]


def test_github_fetcher_stdlib_and_cli_dry_run_never_leak_token(delta_hub):
    seen_auth = []

    class _GHHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            seen_auth.append(self.headers.get("Authorization", ""))
            if self.path.startswith("/repos/duboc/bananacast/issues/14/comments"):
                payload = [{"id": 91, "body": "Human PO says ship it"}]
            elif self.path.startswith("/repos/duboc/bananacast/issues"):
                payload = [
                    {"number": 11, "title": "PR item", "labels": [], "pull_request": {"url": "x"}},
                    {"number": 14, "title": "Decision", "labels": [{"name": "status:po-decision"}], "comments": 1},
                ]
            else:
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b'{"message":"Bad credentials"}')
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode("utf-8"))

    gh_srv = HTTPServer(("127.0.0.1", 0), _GHHandler)
    th = threading.Thread(target=gh_srv.serve_forever, daemon=True)
    th.start()
    secret_val = "test-only-redacted-token-999"
    try:
        issues, comments = factory_delta.fetch_github_issues_and_comments(
            "duboc/bananacast",
            issue_label="factory:bananacast-hc1",
            github_token=secret_val,
            api_base=f"http://127.0.0.1:{gh_srv.server_port}",
        )
        assert [i["number"] for i in issues] == [14]
        assert comments[14][0]["id"] == 91
        assert seen_auth[0] == f"Bearer {secret_val}"

        # Verify DRY_RUN=1 overrides --apply on CLI and never prints GITHUB_TOKEN
        res = subprocess.run(
            [sys.executable, "scripts/factory_delta.py", "--apply"],
            cwd=ROOT,
            env={
                **os.environ,
                "DRY_RUN": "1",
                "HUB_URL": delta_hub.url,
                "SCION_HUB_TOKEN": "tok-admin",
                "FACTORY_PROJECTS": "bananacast",
                "FIRESTORE_PROJECT": "demo-sandbox",
                "GITHUB_REPO": "duboc/bananacast",
                "GITHUB_API_URL": f"http://127.0.0.1:{gh_srv.server_port}",
                "GITHUB_TOKEN": secret_val,
            },
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, res.stderr
        assert secret_val not in res.stdout
        assert secret_val not in res.stderr
        assert len(delta_hub.messages) == 0
    finally:
        gh_srv.shutdown()
        gh_srv.server_close()


def test_delta_tick_waits_for_tmux_90s_default_and_defers_delivery_when_tmux_not_ready(delta_hub, monkeypatch):
    store = FakeDeltaStore()
    store.upsert_document(
        "factory_control_state",
        "delta__bananacast",
        {"last_fingerprint": "fp-prev", "idle_since": 0.0, "lead_sleeping": True, "lead_turns_in_epoch": 2},
    )
    delta_hub.agents["uuid-bc"]["bananacast-lead"] = {
        "id": "id-lead",
        "slug": "bananacast-lead",
        "name": "bananacast-lead",
        "template": "lead",
        "phase": "stopped",
    }
    issues = [
        {
            "number": 31,
            "title": "Fix slow monorepo checkout race",
            "labels": [{"name": "factory:bananacast-hc1"}, {"name": "status:ready"}],
            "comments": 0,
        }
    ]
    client = hub_client.HubClient(base_url=delta_hub.url, token="tok-admin")

    # 1. Verify default wait_sec is 90.0 when DELTA_TMUX_WAIT_SEC is unset
    seen_deadlines: list[float] = []
    fake_clock = [1000.0]
    monkeypatch.delenv("DELTA_TMUX_WAIT_SEC", raising=False)
    monkeypatch.setattr(factory_delta.time, "time", lambda: fake_clock[0])

    def advance_clock(sec: float) -> None:
        fake_clock[0] += float(sec)

    monkeypatch.setattr(factory_delta.time, "sleep", advance_clock)
    delta_hub.exec_exit_code = 1
    assert factory_delta._wait_for_tmux(client, "uuid-bc", "bananacast-lead", poll_sec=30.0) is False
    seen_deadlines.append(fake_clock[0] - 1000.0)
    assert seen_deadlines[0] >= 90.0

    # 2. When _wait_for_tmux returns False, run_delta_tick returns action="waiting_for_tmux", posts NO message, and keeps fingerprint/turns
    monkeypatch.setenv("DELTA_TMUX_WAIT_SEC", "0.05")
    res_wait = factory_delta.run_delta_tick(
        client,
        store,
        project_slug="bananacast",
        lead_slug="bananacast-lead",
        apply=True,
        now=2000.0,
        github_fetcher=lambda *_: (issues, {}),
    )
    assert res_wait["action"] == "waiting_for_tmux"
    assert res_wait["woke_lead"] is False
    assert len(delta_hub.messages) == 0
    saved_wait = store.get_document("factory_control_state", "delta__bananacast")
    assert saved_wait["last_fingerprint"] == "fp-prev"
    assert saved_wait["lead_turns_in_epoch"] == 2

    # 3. Next */2 tick when tmux is ready delivers DELTA: and advances last_fingerprint and lead_turns_in_epoch
    delta_hub.exec_exit_code = 0
    res_ok = factory_delta.run_delta_tick(
        client,
        store,
        project_slug="bananacast",
        lead_slug="bananacast-lead",
        apply=True,
        now=2120.0,
        github_fetcher=lambda *_: (issues, {}),
    )
    assert res_ok["woke_lead"] is True
    assert len(delta_hub.messages) == 1
    assert "#31" in delta_hub.messages[0]["body"]["message"]
    saved_ok = store.get_document("factory_control_state", "delta__bananacast")
    assert saved_ok["last_fingerprint"] == res_ok["fingerprint"]
    assert saved_ok["lead_turns_in_epoch"] == 3



def test_wait_for_tmux_falls_back_to_hub_reported_activity_when_exec_is_denied(delta_hub, monkeypatch):
    # Current Scion keeps agent.attach (exec) for project-owner, and the control-plane identity is
    # project-admin: exec answers 403. The controller must not report waiting_for_tmux forever
    # (hillclimb Run 1, sixth attempt); it reads readiness from the agent record instead.
    client = hub_client.HubClient(base_url=delta_hub.url, token="tok-control")
    delta_hub.exec_denied = True
    monkeypatch.setattr(factory_delta.time, "sleep", lambda _s: None)
    lead = {"id": "id-lead", "slug": "bananacast-lead", "name": "bananacast-lead", "phase": "running",
            "containerStatus": "Running", "activity": "completed"}
    delta_hub.agents["uuid-bc"]["bananacast-lead"] = lead
    assert factory_delta._wait_for_tmux(client, "uuid-bc", "bananacast-lead", wait_sec=0.05) is True
    exec_calls = [r for r in delta_hub.requests if r["method"] == "POST" and r["path"].endswith("/exec")]
    assert len(exec_calls) == 1  # no retry storm once exec is known to be denied

    # A running pod whose harness has not reported any activity yet is not ready
    lead["activity"] = ""
    assert factory_delta._wait_for_tmux(client, "uuid-bc", "bananacast-lead", wait_sec=0.05) is False


def test_delta_auto_sleep_stops_lead_when_agent_delete_is_denied_and_tolerates_tick_jitter(delta_hub):
    # project-admin may stop agents but not delete them. In hillclimb Run 1 the 403 on deleting a
    # stopped specialist crashed every tick before lead was stopped, so the factory never slept.
    store = FakeDeltaStore()
    store.upsert_document(
        "factory_control_state",
        "delta__bananacast",
        {"last_fingerprint": "fp-old", "idle_since": 1000.0, "lead_sleeping": False, "lead_turns_in_epoch": 3},
    )
    delta_hub.delete_denied = True
    delta_hub.agents["uuid-bc"]["bananacast-lead"] = {"id": "l", "slug": "bananacast-lead", "name": "bananacast-lead", "template": "lead", "phase": "running"}
    delta_hub.agents["uuid-bc"]["bananacast-frontend"] = {"id": "f", "slug": "bananacast-frontend", "name": "bananacast-frontend", "template": "frontend", "phase": "stopped"}
    client = hub_client.HubClient(base_url=delta_hub.url, token="tok-control")

    # 599.86s idle: the tick that lands milliseconds short of the 600s grace still sleeps
    res = factory_delta.run_delta_tick(
        client, store, project_slug="bananacast", lead_slug="bananacast-lead",
        apply=True, now=1599.86, github_fetcher=lambda *_: ([], {}),
    )
    assert res["auto_slept"] is True
    assert res["cleanup_warnings"] == ["could not delete stopped agent bananacast-frontend (HTTP 403)"]
    assert delta_hub.agents["uuid-bc"]["bananacast-lead"]["phase"] == "stopped"
    saved = store.get_document("factory_control_state", "delta__bananacast")
    assert saved["lead_sleeping"] is True and saved["lead_turns_in_epoch"] == 0


def test_delta_suppresses_in_flight_echo_wakes_and_recovers_stalled_wip_after_specialist_stop(delta_hub):
    store = FakeDeltaStore()
    delta_hub.agents["uuid-bc"] = {
        "lead": {
            "id": "id-lead",
            "slug": "lead",
            "name": "lead",
            "template": "lead",
            "phase": "running",
            "activity": "idle",
            "lastActivityEvent": "2026-11-11T19:30:00Z",
        }
    }
    client = hub_client.HubClient(base_url=delta_hub.url, token="tok-owner")

    import datetime

    t0 = datetime.datetime(2026, 11, 11, 19, 30, 0, tzinfo=datetime.timezone.utc).timestamp()

    # 1. Initial wave wake with 2 ready issues
    issues_ready = [
        {"number": 358, "title": "Auth fix", "labels": [{"name": "status:ready"}], "comments": 0},
        {"number": 359, "title": "Proxy fix", "labels": [{"name": "status:ready"}], "comments": 0},
    ]
    r1 = factory_delta.run_delta_tick(
        client, store, project_slug="bananacast", lead_slug="lead", apply=True, now=t0, github_fetcher=lambda *_: (issues_ready, {})
    )
    assert r1["woke_lead"] is True
    assert len(delta_hub.messages) == 1

    # 2. Lead starts backend + reviewer and marks #358/#359 status:in-progress (with comments=1).
    #    While backend is busy, delta MUST NOT send a redundant echo wake for the same issue numbers!
    delta_hub.agents["uuid-bc"]["backend"] = {
        "id": "id-be",
        "slug": "backend",
        "name": "backend",
        "template": "backend",
        "phase": "running",
        "activity": "working",
        "lastActivityEvent": "2026-11-11T19:32:00Z",
    }
    issues_in_prog = [
        {"number": 358, "title": "Auth fix", "labels": [{"name": "status:in-progress"}], "comments": 1},
        {"number": 359, "title": "Proxy fix", "labels": [{"name": "status:in-progress"}], "comments": 1},
    ]
    r2 = factory_delta.run_delta_tick(
        client, store, project_slug="bananacast", lead_slug="lead", apply=True, now=t0 + 120, github_fetcher=lambda *_: (issues_in_prog, {})
    )
    assert r2["woke_lead"] is False
    assert r2["action"] == "suppressed_in_flight_echo"
    assert len(delta_hub.messages) == 1

    # 3. #359 finishes (status:verified) while backend is still working on #358 -> still no echo wake!
    issues_358_only = [
        {"number": 358, "title": "Auth fix", "labels": [{"name": "status:in-progress"}], "comments": 2},
    ]
    r3 = factory_delta.run_delta_tick(
        client, store, project_slug="bananacast", lead_slug="lead", apply=True, now=t0 + 240, github_fetcher=lambda *_: (issues_358_only, {})
    )
    assert r3["woke_lead"] is False
    assert len(delta_hub.messages) == 1

    # 4. Eviction drill: kubectl delete pod stops backend (SIGTERM -> phase="stopped") while #358 is still open,
    #    even if reviewer ended its setup turn with `sciontool status blocked`!
    #    After stalled_wip_sec (120s) with zero busy children, delta wakes lead with STALLED_WIP recovery instructions.
    delta_hub.agents["uuid-bc"]["backend"]["phase"] = "stopped"
    delta_hub.agents["uuid-bc"]["backend"]["activity"] = "completed"
    delta_hub.agents["uuid-bc"]["reviewer"] = {
        "id": "id-rev",
        "slug": "reviewer",
        "name": "reviewer",
        "template": "reviewer",
        "phase": "running",
        "activity": "blocked",
        "lastActivityEvent": "2026-11-11T19:33:00Z",
    }
    r4 = factory_delta.run_delta_tick(
        client, store, project_slug="bananacast", lead_slug="lead", apply=True, now=t0 + 400, github_fetcher=lambda *_: (issues_358_only, {})
    )
    assert r4["woke_lead"] is True
    assert r4["stalled_wip"] is True
    assert len(delta_hub.messages) == 2
    assert "STALLED_WIP" in delta_hub.messages[-1]["body"]["message"]
    assert "backend" in delta_hub.messages[-1]["body"]["message"]


def test_delta_handles_concurrent_409_start_in_progress_and_skips_restart_when_already_starting(delta_hub, monkeypatch):
    store = FakeDeltaStore()
    delta_hub.agents["uuid-bc"] = {
        "lead": {"id": "id-lead", "slug": "lead", "name": "lead", "template": "lead", "phase": "starting"}
    }
    client = hub_client.HubClient(base_url=delta_hub.url, token="tok-owner")
    issues = [{"number": 401, "title": "Test", "labels": [{"name": "status:ready"}], "comments": 0}]

    # 1. Lead already in `starting` phase: skips redundant ensure_agent_started(force_resume=True) and waits on tmux
    monkeypatch.setattr(factory_delta, "_wait_for_tmux", lambda *_a, **_kw: False)
    res_starting = factory_delta.run_delta_tick(
        client, store, project_slug="bananacast", lead_slug="lead", apply=True, now=4900.0, github_fetcher=lambda *_: (issues, {})
    )
    assert res_starting["action"] == "waiting_for_tmux"
    assert not any(r["method"] == "POST" and r["path"].endswith("/start") for r in delta_hub.requests)

    # 2. Lead `stopped` while a concurrent caller starts it -> HTTP 409 start_in_progress returns cleanly
    delta_hub.agents["uuid-bc"]["lead"]["phase"] = "stopped"

    def raise_409(*_args, **_kwargs):
        raise hub_client.HubAPIError(409, "POST", "/api/v1/projects/uuid-bc/agents/lead/start", "start_in_progress")

    monkeypatch.setattr(client, "ensure_agent_started", raise_409)
    res = factory_delta.run_delta_tick(
        client, store, project_slug="bananacast", lead_slug="lead", apply=True, now=5000.0, github_fetcher=lambda *_: (issues, {})
    )
    assert res["woke_lead"] is False
    assert res["action"] == "start_in_progress"
