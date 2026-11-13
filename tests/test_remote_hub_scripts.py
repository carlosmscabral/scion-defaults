import json
import os
import subprocess
import sys
import socket
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import pytest
import importlib.util

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import hub_client

def write_minimal_env(tmp_path):
    env_file = tmp_path / "factory.env"
    env_file.write_text("SCION_ENV=test\nBUILD_BRANCH=main\nBUILD_PROJECT=p\nDEPLOYER_SA=sa\nEVAL_BRANCH=main\nEVAL_PROJECT=p\nGITHUB_REPO=repo\nHUB_PROJECT=p\nPO_EMAIL=a@b.c\nSANDBOX_PROJECT=p\nSERVING_SERVICE=s\n")
    return env_file

def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]

class FakeHubHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def do_GET(self):
        if self.path == "/health" or self.path == "/healthz":
            self._send_json(200, {"status": "healthy", "scionVersion": "0.9.0"})
            return
        if self.path.startswith("/api/v1/users?search=po%40example.com"):
            self._send_json(200, {"users": [{"email": "po@example.com"}]})
            return
        if self.path.startswith("/api/v1/users/"):
            self._send_json(200, {"email": "po@example.com"})
            return

        if self.path == "/api/v1/projects" or self.path.startswith("/api/v1/projects?"):
            self._send_json(200, {"projects": [{"slug": "demo", "id": "proj-1"}]})
            return
        if self.path.startswith("/api/v1/projects/proj-1/gcp-service-accounts"):
            self._send_json(200, {"items": getattr(self.server.hub_state, "service_accounts", [])})
            return
        if self.path.startswith("/api/v1/projects/proj-1/settings"):
            self._send_json(200, self.server.hub_state.project_settings.get("proj-1", {}))
            return
        if self.path.startswith("/api/v1/projects/proj-1"):
            self._send_json(200, {"id": "proj-1", "slug": "demo"})
            return
        if self.path.startswith("/api/v1/projects/demo"):
            self._send_json(200, {"id": "proj-1", "slug": "demo"})
            return
        if self.path.startswith("/api/v1/templates"):
            self._send_json(200, {"templates": getattr(self.server.hub_state, "templates", [])})
            return
        if self.path.startswith("/api/v1/harness-configs/hc-claude/download") and not self.path.endswith("/download-urls"):
            dl_base = getattr(self.server.hub_state, "cross_origin_dl_url", self.server.url)
            self._send_json(200, {"files": [{"path": "config.yaml", "url": dl_base + "/dl/config.yaml"}]})
            return
        if self.path.startswith("/api/v1/harness-configs?scope=global"):
            self._send_json(200, {"harnessConfigs": list(self.server.hub_state.harness_configs.values())})
            return
        if self.path.startswith("/dl/config.yaml"):
            if "Authorization" in self.headers or "Proxy-Authorization" in self.headers:
                self._send_json(400, {"error": "GCS signed URL rejects Authorization headers"})
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"command:\n  base: [\"echo\"]\n")
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        
        if self.path.startswith("/api/v1/templates"):
            if self.path.endswith("/upload"):
                data = json.loads(body.decode("utf-8"))
                urls = [{"path": f["path"], "url": self.server.url + "/ul/tmpl/" + f["path"], "method": "PUT"} for f in data.get("files", [])]
                self._send_json(200, {"uploadUrls": urls, "uploadId": "up-tmpl-1"})
                return
            elif self.path.endswith("/finalize"):
                data = json.loads(body.decode("utf-8"))
                manifest = data.get("manifest") or {}
                for f in manifest.get("files", []):
                    if not isinstance(f.get("mode"), str):
                        self._send_json(400, {"error": "json: cannot unmarshal number into Go struct field TemplateFile.manifest.files.mode of type string"})
                        return
                tid = self.path.split("/")[-2]
                for t in self.server.hub_state.templates:
                    if t.get("id") == tid:
                        t["manifest"] = manifest
                        t["uploadId"] = data.get("uploadId")
                self._send_json(200, {"status": "active", "contentHash": "sha256:abc"})
                return
            else:
                data = json.loads(body.decode("utf-8"))
                tmpl = dict(data)
                tmpl["id"] = f"tmpl-new-{len(self.server.hub_state.templates) + 1}"
                self.server.hub_state.templates.append(tmpl)
                self._send_json(201, {"template": tmpl, "uploadId": "up-init"})
                return
                
        if self.path.startswith("/api/v1/harness-configs"):
            if self.path.endswith("/upload"):
                data = json.loads(body.decode("utf-8"))
                urls = [{"path": f["path"], "url": self.server.url + "/ul/hc/" + f["path"], "method": "PUT"} for f in data.get("files", [])]
                self._send_json(200, {"uploadUrls": urls, "uploadId": "up-hc-1"})
                return
            elif self.path.endswith("/finalize"):
                data = json.loads(body.decode("utf-8"))
                manifest = data.get("manifest") or {}
                for f in manifest.get("files", []):
                    if not isinstance(f.get("mode"), str):
                        self._send_json(400, {"error": "mode must be octal string"})
                        return
                self._send_json(200, {"status": "active"})
                return
            else:
                data = json.loads(body.decode("utf-8"))
                if not data.get("harness"):
                    self._send_json(400, {"error": "harness is required"})
                    return
                name = data["name"]
                if name in self.server.hub_state.harness_configs:
                    self._send_json(409, {"error": "already exists"})
                    return
                hc = {"id": f"hc-{name}", "name": name, "harness": data["harness"], "files": {}}
                self.server.hub_state.harness_configs[name] = hc
                self._send_json(201, {"harnessConfig": hc})
                return
                
        if self.path.startswith("/api/v1/projects/proj-1/gcp-service-accounts"):
            if self.path.endswith("/verify"):
                self._send_json(200, {"verified": True})
                return
            sa = json.loads(body.decode("utf-8"))
            sa["id"] = "sa-new"
            sa["verified"] = True
            self.server.hub_state.service_accounts.append(sa)
            self._send_json(201, sa)
            return

        self._send_json(200, {"status": "ok"})
        
    def do_PATCH(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        if self.path.startswith("/api/v1/templates/"):
            tid = self.path.split("/")[-1]
            data = json.loads(body.decode("utf-8"))
            for t in self.server.hub_state.templates:
                if t.get("id") == tid:
                    t.update(data)
            self._send_json(200, {"status": "ok"})
            return
        self._send_json(200, {"status": "ok"})
        
    def do_PUT(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        if self.path.startswith("/api/v1/projects/proj-1/settings"):
            self.server.hub_state.project_settings["proj-1"] = json.loads(body.decode("utf-8"))
            self._send_json(200, self.server.hub_state.project_settings["proj-1"])
            return
        if self.path.startswith("/ul/hc/"):
            path = self.path[len("/ul/hc/"):]
            if "claude-factory" not in self.server.hub_state.harness_configs:
                self.server.hub_state.harness_configs["claude-factory"] = {"files": {}}
            self.server.hub_state.harness_configs["claude-factory"]["files"][path] = body.decode("utf-8")
            self.send_response(200)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()

class FakeHubState:
    def __init__(self, url):
        self.url = url
        self.templates = []
        self.harness_configs = {"claude": {"id": "hc-claude", "name": "claude", "files": {}}}
        self.service_accounts = []
        self.project_settings = {"proj-1": {}}

    def seed_shared_storage_collision(self, project_id, role):
        self.templates.append({
            "id": f"tmpl-{role}",
            "name": role,
            "slug": role,
            "storagePath": "gs://bucket/path",
            "scope": "project",
            "scopeId": project_id,
        })
        self.templates.append({
            "id": "tmpl-other",
            "name": "other",
            "slug": "other",
            "storagePath": "gs://bucket/path",
            "scope": "project",
            "scopeId": project_id,
        })

@pytest.fixture
def fake_hub():
    server = HTTPServer(("127.0.0.1", 0), FakeHubHandler)
    port = server.server_address[1]
    server.url = f"http://127.0.0.1:{port}"
    server.hub_state = FakeHubState(server.url)

    dl_server = HTTPServer(("127.0.0.1", 0), FakeHubHandler)
    dl_port = dl_server.server_address[1]
    dl_server.url = f"http://127.0.0.1:{dl_port}"
    dl_server.hub_state = server.hub_state
    server.hub_state.cross_origin_dl_url = dl_server.url

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t_dl = threading.Thread(target=dl_server.serve_forever, daemon=True)
    t.start()
    t_dl.start()

    yield server.hub_state

    server.shutdown()
    server.server_close()
    dl_server.shutdown()
    dl_server.server_close()



def test_push_templates_direct_api_isolates_shared_storage_and_uploads(fake_hub, tmp_path):
    fake_hub.seed_shared_storage_collision(project_id="proj-1", role="lead")
    env = {
        **os.environ,
        "HUB_URL": fake_hub.url,
        "SCION_HUB_TOKEN": "tok-admin",
        "FACTORY_ENV": str(write_minimal_env(tmp_path)),
    }
    shared_dir = tmp_path / "templates" / "_shared"
    lead_dir = tmp_path / "templates" / "lead"
    shared_dir.mkdir(parents=True)
    lead_dir.mkdir(parents=True)
    (shared_dir / "agents.md").write_text("# Shared protocol\n")
    (lead_dir / "system-prompt.md").write_text("# Lead prompt\n")
    # Compiled kits emit default_harness_config instead of harness:
    (lead_dir / "scion-agent.yaml").write_text(
        "schema_version: '1'\ndefault_harness_config: antigravity-factory\n"
    )
    helper = lead_dir / "home" / ".local" / "bin" / "helper.sh"
    helper.parent.mkdir(parents=True, exist_ok=True)
    helper.write_text("echo helper")
    helper.chmod(0o755)

    res = subprocess.run(
        [sys.executable, "scripts/push-templates.py", "--project", "demo", "--roles", "lead", "--apply", "--templates", str(tmp_path / "templates")],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    assert "rename it to lead-legacy" in res.stdout
    assert "other templates unchanged: yes" in res.stdout
    
    new_lead = [t for t in fake_hub.templates if t["slug"] == "lead" and t["id"] != "tmpl-lead"][0]
    assert new_lead["harness"] == "antigravity"
    assert new_lead.get("uploadId") == "up-tmpl-1"
    assert "manifest" in new_lead
    manifest_files = {f["path"]: f for f in new_lead["manifest"]["files"]}
    assert "home/.local/bin/helper.sh" in manifest_files
    assert manifest_files["home/.local/bin/helper.sh"]["mode"] == "0755"




def test_scion_admin_project_settings_and_service_accounts_over_api(fake_hub, tmp_path):
    env = {**os.environ, "HUB_URL": fake_hub.url, "SCION_HUB_TOKEN": "tok-admin"}
    r1 = subprocess.run(
        [sys.executable, "scripts/scion-admin.py", "service-accounts", "--project", "demo",
         "add", "--email", "agent-worker@sandbox.iam.gserviceaccount.com", "--gcp-project", "sandbox"],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    assert r1.returncode == 0, r1.stderr
    r2 = subprocess.run(
        [sys.executable, "scripts/scion-admin.py", "project-settings", "--project", "demo",
         "--profile", "gke", "--gcp-identity", "assign",
         "--service-account", "agent-worker@sandbox.iam.gserviceaccount.com"],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    assert r2.returncode == 0, r2.stderr
    assert fake_hub.project_settings["proj-1"]["activeProfile"] == "gke"
    assert fake_hub.project_settings["proj-1"]["defaultGCPIdentityMode"] == "assign"
    assert fake_hub.project_settings["proj-1"]["defaultGCPIdentityServiceAccountID"] == "sa-new"


def test_make_factory_harness_configs_via_api_wraps_command_base(fake_hub):
    env = {**os.environ, "HUB_URL": fake_hub.url, "SCION_HUB_TOKEN": "tok-admin"}
    res = subprocess.run(
        [sys.executable, "scripts/make-factory-harness-configs.py", "--via-api", "--harness", "claude", "--apply"],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    uploaded = fake_hub.harness_configs["claude-factory"]
    assert uploaded["harness"] == "claude"
    assert '["sh", "/home/scion/.local/bin/factory-cd.sh"' in uploaded["files"]["config.yaml"]
    assert "home/.local/bin/factory-cd.sh" in uploaded["files"]

    res2 = subprocess.run(
        [sys.executable, "scripts/make-factory-harness-configs.py", "--via-api", "--harness", "claude", "--apply"],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    assert res2.returncode == 0, res2.stderr


def test_hub_preflight_api_mode_checks_registry_images_and_merges_k8s_projects(fake_hub):
    spec = importlib.util.spec_from_file_location("hub_preflight", os.path.join(ROOT, "scripts", "hub-preflight.py"))
    hub_preflight = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hub_preflight)

    client = hub_client.HubClient(base_url=fake_hub.url, token="tok-admin")
    report = hub_preflight.build_api_report(
        client=client,
        po_email="po@example.com",
        new_projects=["alpha", "alpha-eval"],
        images={"us-central1-docker.pkg.dev/proj/scion/scion-claude:v1",
                "us-central1-docker.pkg.dev/proj/scion/scion-antigravity:v1"},
        watchdog_projects=["existing"],
        sync_projects=["existing"],
        github_token_present=True,
        runtime="kubernetes",
        k8s_ready=True,
    )
    assert report["ok"] is True
    assert report["harness_configs"] == ["claude"]
    assert report["factory_projects"] == "existing,alpha,alpha-eval"
    assert any("scion-opencode" in w for w in report["warnings"])

def test_scion_admin_projects_local_fallback(tmp_path):
    import sqlite3
    db_path = tmp_path / "hub.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE projects (id TEXT, slug TEXT)")
    conn.execute("INSERT INTO projects (id, slug) VALUES ('proj-local', 'local-slug')")
    conn.commit()
    conn.close()

    env = {**os.environ, "SCION_HOME": str(tmp_path)}
    # Ensure HUB_URL is NOT set
    if "HUB_URL" in env:
        del env["HUB_URL"]

    res = subprocess.run(
        [sys.executable, "scripts/scion-admin.py", "projects"],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    assert "local-slug" in res.stdout
    assert "proj-local" in res.stdout
