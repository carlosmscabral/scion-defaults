import os
import sys
import subprocess
import time
import importlib.util

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from unittest import mock
import hub_client
import factory_store

def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


import pytest

class FakeFirestore:
    def __init__(self):
        self._docs = {}
    def get_document(self, collection, doc_id):
        return self._docs.get((collection, doc_id))
    def upsert_document(self, collection, doc_id, data):
        self._docs[(collection, doc_id)] = data

@pytest.fixture
def fake_firestore():
    return FakeFirestore()

watchdog_mod = load_script("watchdog_mod", os.path.join(ROOT, "scripts", "factory-watchdog.py"))
install_k8s = load_script("install_k8s", os.path.join(ROOT, "scripts", "install-k8s-control-plane.py"))

def test_sync_factory_agents_reads_from_hub_api_without_sqlite(fake_hub, monkeypatch):
    fake_hub.seed_agent("proj-1", slug="lead", phase="running", activity="working", harness="claude", task_summary="Wave 1")
    monkeypatch.setenv("HUB_URL", fake_hub.url)
    monkeypatch.setenv("SCION_HUB_TOKEN", "tok-admin")
    monkeypatch.setenv("FACTORY_PROJECTS", "demo")
    monkeypatch.setenv("FIRESTORE_PROJECT", "sandbox-proj")
    # Run sync-factory-agents.py --dry-run with SCION_HOME pointing at an empty dir (no hub.db!)
    res = subprocess.run(
        [sys.executable, "scripts/sync-factory-agents.py", "--dry-run"],
        cwd=ROOT, env={**os.environ, "SCION_HOME": "/nonexistent-scion-home", "HUB_URL": fake_hub.url, "SCION_HUB_TOKEN": "tok-admin", "FACTORY_PROJECTS": "demo"},
        capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    assert "demo__lead" in res.stdout


def test_watchdog_api_mode_reaps_hung_subprocess_via_exec_and_persists_circuit_breaker_in_firestore(fake_hub, fake_firestore):
    fake_hub.seed_agent("proj-1", slug="backend", phase="running", runtime="kubernetes", pod_name="scion-backend")
    fake_hub.exec_outputs[("proj-1", "backend")] = (
        "  PID ELAPSED COMMAND         COMMAND\n"
        "    1    1200 sciontool       sciontool init\n"
        "  442     905 uvicorn         uvicorn app.main:app --port 8080\n"
    )
    client = hub_client.HubClient(base_url=fake_hub.url, token="tok-admin")
    state = factory_store.load_control_state(fake_firestore, "watchdog__default")
    watchdog_mod.run_api_watchdog(client, state, apply=True, runtime="kubernetes")
    factory_store.save_control_state(fake_firestore, "watchdog__default", state)

    print(f'EXEC COMMANDS: {fake_hub.exec_commands}'); assert any(cmd == ["kill", "-9", "442"] for cmd in fake_hub.exec_commands[("proj-1", "backend")])

    # Simulate 3 crash restarts -> 4th run in a fresh process loads state from Firestore and trips circuit breaker
    state["restarts"] = {"proj-1/backend": [time.time() - 10, time.time() - 5, time.time() - 1]}
    # Seed alerted as a dict to ensure watchdog can handle legacy or dict-based states
    state["alerted"] = {"old-agent": 12345.0}
    factory_store.save_control_state(fake_firestore, "watchdog__default", state)
    fake_hub.set_agent_phase("proj-1", "backend", "error")
    reloaded = factory_store.load_control_state(fake_firestore, "watchdog__default")
    watchdog_mod.run_api_watchdog(client, reloaded, apply=True, runtime="kubernetes")
    factory_store.save_control_state(fake_firestore, "watchdog__default", reloaded)
    assert not fake_hub.started_agents  # circuit breaker blocked the 4th restart!
    
    # Assert circuit breaker persisted the alert as a dict with timestamp
    final_state = factory_store.load_control_state(fake_firestore, "watchdog__default")
    assert isinstance(final_state["alerted"], dict)
    assert "proj-1/backend" in final_state["alerted"]
    assert final_state["alerted"]["proj-1/backend"] > 0
    assert final_state["alerted"]["old-agent"] == 12345.0


def test_install_k8s_control_plane_renders_cronjobs_and_workload_identity_ksa():
    manifests = install_k8s.render_manifests(
        namespace="scion-hub-prod",
        hub_url="https://hub.example.com",
        factory_projects="demo,demo-eval",
        sandbox_project="my-sandbox",
        firestore_project="my-sandbox",
        gsa_email="agent-worker@my-sandbox.iam.gserviceaccount.com",
    )
    kinds = {(m["kind"], m["metadata"]["name"]) for m in manifests}
    assert ("ServiceAccount", "scion-factory-control") in kinds
    assert ("ConfigMap", "scion-factory-control-scripts") in kinds
    assert ("CronJob", "scion-factory-sync") in kinds
    assert ("CronJob", "scion-factory-watchdog") in kinds
    sa = next(m for m in manifests if m["kind"] == "ServiceAccount")
    assert sa["metadata"]["annotations"]["iam.gke.io/gcp-service-account"] == "agent-worker@my-sandbox.iam.gserviceaccount.com"

def test_watchdog_main_uses_api_when_hub_url_set(fake_hub, monkeypatch, tmp_path):
    monkeypatch.setenv("HUB_URL", fake_hub.url)
    monkeypatch.setenv("SCION_HUB_TOKEN", "tok-admin")
    monkeypatch.setenv("WATCHDOG_STATE_BACKEND", "firestore")
    monkeypatch.setenv("SCION_HOME", "/nonexistent-scion-home")

    store = FakeFirestore()
    import factory_store
    monkeypatch.setattr(factory_store, "store_from_env", lambda dry_run=False, project=None, database=None: store)

    fake_hub.seed_agent("proj-1", slug="backend", phase="error", runtime="kubernetes", pod_name="scion-backend")

    # Set up arguments for main
    monkeypatch.setattr(sys, "argv", ["factory-watchdog.py", "--apply"])
    
    watchdog_mod.main()

    # The mock will attempt to restart the errored agent via /start
    assert any(cmd["path"] == "/api/v1/projects/proj-1/agents/backend/start" for cmd in fake_hub.requests)


def test_install_k8s_control_plane_apply_secret_via_stdin_without_ps_args(monkeypatch, tmp_path):
    fake_kubectl = tmp_path / "kubectl"
    fake_kubectl.write_text(
        "#!/bin/bash\n"
        "echo \"$@\" >> fake_kubectl.log\n"
        "cat - >> fake_kubectl_stdin.log\n"
    )
    fake_kubectl.chmod(0o755)

    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH')}")
    monkeypatch.setenv("SCION_HUB_TOKEN", "super-secret-token")

    monkeypatch.setattr(sys, "argv", ["install-k8s-control-plane.py", "--apply"])
    
    old_cwd = os.getcwd()
    os.chdir(str(tmp_path))
    try:
        install_k8s.main()
    finally:
        os.chdir(old_cwd)

    logs = (tmp_path / "fake_kubectl.log").read_text()
    assert "super-secret-token" not in logs
    
    assert "apply -f -" in logs

    stdin_logs = (tmp_path / "fake_kubectl_stdin.log").read_text()
    import base64
    encoded = base64.b64encode(b"super-secret-token").decode("ascii")
    assert encoded in stdin_logs
    assert "scion-factory-hub-auth" in stdin_logs


def test_sync_and_watchdog_resolve_project_slug_to_uuid_and_handle_schedules_dict(fake_hub, fake_firestore):
    fake_hub.strict_project_uuid = True
    fake_hub.projects_by_slug = {"demo": {"id": "uuid-demo-123", "slug": "demo"}}
    fake_hub.agents_by_project = {
        "uuid-demo-123": [{"id": "ag-1", "slug": "lead", "phase": "running", "activity": "working", "harnessConfig": "antigravity-factory"}]
    }
    fake_hub.schedules = {"uuid-demo-123": [{"id": "sch-1", "name": "factory-heartbeat", "status": "error"}]}
    fake_hub.exec_outputs[("uuid-demo-123", "lead")] = (
        "  PID ELAPSED COMMAND         COMMAND\n"
        "    1    1800 sciontool       sciontool init\n"
        "   55    1700 agy             agy chat --headless\n"
    )

    # 1. Sync resolves "demo" -> "uuid-demo-123" for /agents API, keeps project_slug="demo"
    res = subprocess.run(
        [sys.executable, "scripts/sync-factory-agents.py", "--dry-run"],
        cwd=ROOT,
        env={
            **os.environ,
            "SCION_HOME": "/nonexistent-scion-home",
            "HUB_URL": fake_hub.url,
            "SCION_HUB_TOKEN": "tok-admin",
            "FACTORY_PROJECTS": "demo",
            "FIRESTORE_PROJECT": "sandbox-proj",
        },
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    assert "demo__lead" in res.stdout

    # 2. Watchdog uses uuid-demo-123, unwraps {"schedules": [...]}, sends body={"status": "active"}, and spares `agy`
    client = hub_client.HubClient(base_url=fake_hub.url, token="tok-admin")
    state = factory_store.load_control_state(fake_firestore, "watchdog__default")
    old_fp = os.environ.get("FACTORY_PROJECTS")
    os.environ["FACTORY_PROJECTS"] = "demo"
    try:
        watchdog_mod.run_api_watchdog(client, state, apply=True, runtime="kubernetes")
    finally:
        if old_fp is None:
            os.environ.pop("FACTORY_PROJECTS", None)
        else:
            os.environ["FACTORY_PROJECTS"] = old_fp

    patch_reqs = [
        r for r in fake_hub.requests
        if r["method"] == "PATCH" and r["path"] == "/api/v1/projects/uuid-demo-123/schedules/sch-1"
    ]
    assert len(patch_reqs) == 1
    assert b'"active"' in patch_reqs[0]["body"]
    # `agy` harness CLI must NOT be reaped as a hung subprocess!
    exec_cmds = fake_hub.exec_commands.get(("uuid-demo-123", "lead"), [])
    assert not any(cmd[:2] == ["kill", "-9"] for cmd in exec_cmds)


def test_install_k8s_control_plane_cli_supports_iap_audience_and_optional_secret(monkeypatch, tmp_path):
    out_file = tmp_path / "manifests.yaml"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "install-k8s-control-plane.py",
            "--namespace",
            "tfha-e2e",
            "--hub-url",
            "https://hub.example.com",
            "--iap-audience",
            "123-abc.apps.googleusercontent.com",
            "--gsa-email",
            "factory-control@sandbox.iam.gserviceaccount.com",
            "--dry-run",
            "--out",
            str(out_file),
        ],
    )
    install_k8s.main()
    raw = out_file.read_text()
    docs = [install_k8s.json.loads(d) for d in raw.split("\n---\n") if d.strip()]
    sync_cj = next(d for d in docs if d["kind"] == "CronJob" and d["metadata"]["name"] == "scion-factory-sync")
    env_list = sync_cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["env"]
    env_by_name = {e["name"]: e for e in env_list}
    assert env_by_name["HUB_IAP_AUDIENCE"]["value"] == "123-abc.apps.googleusercontent.com"
    assert env_by_name["SCION_HUB_TOKEN"]["valueFrom"]["secretKeyRef"]["optional"] is True
    assert sync_cj["spec"]["jobTemplate"]["spec"]["backoffLimit"] == 1


def test_firestore_store_control_state_roundtrip_over_http(monkeypatch):
    import http.server
    import json
    import threading

    docs: dict[str, dict] = {}

    class _FSHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            key = self.path.removeprefix("/v1/")
            if key not in docs:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'{"error":{"code":404,"message":"not found"}}')
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"name": key, "fields": docs[key]}).encode("utf-8"))

        def do_POST(self):
            assert self.path.endswith(":commit"), f"unexpected POST path {self.path}"
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0") or 0)).decode("utf-8"))
            for w in payload.get("writes", []):
                upd = w.get("update", {})
                docs[upd["name"]] = upd.get("fields", {})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"writeResults":[{}]}')

        def log_message(self, *args):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _FSHandler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        monkeypatch.setenv("FIRESTORE_EMULATOR_HOST", f"127.0.0.1:{srv.server_port}")
        store = factory_store.FirestoreStore(project="demo-proj")
        initial = factory_store.load_control_state(store, "watchdog__default")
        assert initial == {"restarts": {}, "alerted": [], "last_user_msg_ts": ""}
        factory_store.save_control_state(
            store,
            "watchdog__default",
            {"restarts": {"p/lead": [1.0]}, "alerted": {"p/lead": 2.0}, "last_user_msg_ts": "2026-10-28T09:00:00Z"},
        )
        reloaded = factory_store.load_control_state(store, "watchdog__default")
        assert reloaded["restarts"] == {"p/lead": [1.0]}
        assert reloaded["alerted"] == {"p/lead": 2.0}
        assert reloaded["last_user_msg_ts"] == "2026-10-28T09:00:00Z"
    finally:
        srv.shutdown()


def test_watchdog_api_mode_wakes_monorepo_prefixed_lead_and_reads_camel_case_created_at(fake_hub, fake_firestore, monkeypatch):
    alerts = []
    monkeypatch.setattr(watchdog_mod, "log_alert", lambda ev, payload, severity="ERROR": alerts.append((ev, payload, severity)))
    fake_hub.projects_by_slug = {"k8s-version": {"id": "uuid-k8s-ver", "slug": "k8s-version"}}
    fake_hub.agents_by_project = {
        "uuid-k8s-ver": [
            {"id": "ag-lead", "slug": "k8s-version-lead", "template": "lead", "phase": "stopped"},
            {"id": "ag-be", "slug": "k8s-version-backend", "template": "backend", "phase": "stopped"},
        ]
    }
    fake_hub.messages_list = [
        {
            "id": "m-1",
            "projectId": "uuid-k8s-ver",
            "sender": "agent:k8s-version-lead",
            "recipient": "user:po@example.com",
            "msg": "Need architectural sign-off",
            "type": "instruction",
            "createdAt": "2026-10-28T10:00:00Z",
        }
    ]
    client = hub_client.HubClient(base_url=fake_hub.url, token="tok-admin")
    state = {"restarts": {}, "alerted": {}, "last_user_msg_ts": "2026-10-28T09:00:00Z"}
    old_fp = os.environ.get("FACTORY_PROJECTS")
    os.environ["FACTORY_PROJECTS"] = "k8s-version"
    try:
        watchdog_mod.run_api_watchdog(client, state, apply=True, runtime="kubernetes")
    finally:
        if old_fp is None:
            os.environ.pop("FACTORY_PROJECTS", None)
        else:
            os.environ["FACTORY_PROJECTS"] = old_fp

    start_paths = [r["path"] for r in fake_hub.requests if r["method"] == "POST" and r["path"].endswith("/start")]
    assert "/api/v1/projects/uuid-k8s-ver/agents/k8s-version-lead/start" in start_paths
    assert "/api/v1/projects/uuid-k8s-ver/agents/k8s-version-backend/start" not in start_paths
    assert state["last_user_msg_ts"] == "2026-10-28T10:00:00Z"
    assert len(alerts) == 1 and alerts[0][0] == "PO_ESCALATION"


def test_watchdog_api_mode_notifies_lead_when_restarting_worker(fake_hub):
    fake_hub.projects_by_slug = {"spot-demo": {"id": "uuid-spot", "slug": "spot-demo"}}
    fake_hub.agents_by_project = {
        "uuid-spot": [
            {"id": "ag-lead", "slug": "lead", "template": "lead", "phase": "running"},
            {"id": "ag-be", "slug": "backend", "template": "backend", "phase": "error"},
        ]
    }
    client = hub_client.HubClient(base_url=fake_hub.url, token="tok-admin")
    state = {"restarts": {}, "alerted": {}, "last_user_msg_ts": ""}
    old_fp = os.environ.get("FACTORY_PROJECTS")
    os.environ["FACTORY_PROJECTS"] = "spot-demo"
    try:
        watchdog_mod.run_api_watchdog(client, state, apply=True, runtime="kubernetes")
    finally:
        if old_fp is None:
            os.environ.pop("FACTORY_PROJECTS", None)
        else:
            os.environ["FACTORY_PROJECTS"] = old_fp

    start_paths = [r["path"] for r in fake_hub.requests if r["method"] == "POST" and r["path"].endswith("/start")]
    assert "/api/v1/projects/uuid-spot/agents/backend/start" in start_paths
    msg_reqs = [
        r for r in fake_hub.requests
        if r["method"] == "POST" and r["path"] == "/api/v1/projects/uuid-spot/agents/lead/message"
    ]
    assert len(msg_reqs) == 1
    assert b"watchdog auto-restarted agent backend" in msg_reqs[0]["body"]



