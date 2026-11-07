import json
import re
import socket
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.parse
import pytest
from urllib.parse import urlparse
import time
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts')))
import hub_client

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
        self.server.requests.append({
            "method": "GET",
            "path": self.path,
            "headers": dict(self.headers),
        })

        
        import re
        m_gcp_sa = re.match(r"/api/v1/projects/([^/]+)/gcp-service-accounts", self.path)
        if m_gcp_sa:
            pid = m_gcp_sa.group(1)
            sas = getattr(self.server, "gcp_service_accounts", {}).get(pid, [])
            self._send_json(200, {"items": sas})
            return
            
        m_schedules = re.match(r"/api/v1/projects/([^/]+)/schedules", self.path)
        if m_schedules:
            pid = m_schedules.group(1)
            self._send_json(200, {"schedules": getattr(self.server, "schedules", {}).get(pid, [])})
            return

        m_shared_list = re.match(r"/api/v1/projects/([^/]+)/shared-dirs$", self.path)
        if m_shared_list:
            pid = m_shared_list.group(1)
            existing = getattr(self.server, "shared_dirs", {}).get(pid, [{"name": "scratchpad", "read_only": False}])
            self._send_json(200, {"sharedDirs": existing})
            return
            
        m_agent = re.match(r"/api/v1/projects/([^/]+)/agents/([^/]+)$", self.path)
        if m_agent and m_agent.group(1) != "proj-1":
            pid = m_agent.group(1)
            slug = m_agent.group(2)
            for a in [*getattr(self.server, "agents_by_project", {}).get(pid, []), *self.server.agents]:
                if isinstance(a, dict) and a.get("slug") == slug:
                    self._send_json(200, a)
                    return
            self._send_json(404, {"error": "not found"})
            return

        if "/agents" in self.path:
            parts = self.path.split("/")
            req_pid = parts[4] if len(parts) > 4 else ""
            if getattr(self.server, "strict_project_uuid", False):
                valid_ids = {"proj-1"} | {p["id"] for p in getattr(self.server, "projects_by_slug", {}).values()}
                if req_pid not in valid_ids:
                    self._send_json(404, {"error": f"project {req_pid} not found (expected UUID)"})
                    return
            agents = getattr(self.server, "agents_by_project", {}).get(req_pid, getattr(self.server, "agents", []))
            if not self.path.endswith("/agents"):
                slug = self.path.split("/")[-1]
                for a in agents:
                    if isinstance(a, dict) and a.get("slug") == slug:
                        self._send_json(200, a)
                        return
                for a in getattr(self.server, "agents", []):
                    if isinstance(a, dict) and a.get("slug") == slug:
                        self._send_json(200, a)
                        return
                self._send_json(404, {"error": "not found"})
                return
            self._send_json(200, {"agents": agents})
            return

        if "/schedules" in self.path:
            parts = self.path.split("/")
            req_pid = parts[4] if len(parts) > 4 else ""
            if getattr(self.server, "strict_project_uuid", False):
                valid_ids = {"proj-1"} | {p["id"] for p in getattr(self.server, "projects_by_slug", {}).values()}
                if req_pid not in valid_ids:
                    self._send_json(404, {"error": f"project {req_pid} not found (expected UUID)"})
                    return
            sched_list = getattr(self.server, "schedules", {}).get(req_pid, [])
            self._send_json(200, {"schedules": sched_list, "totalCount": len(sched_list)})
            return

        if self.path == "/health":
            if getattr(self.server, "fail_health_404", False):
                self._send_json(404, {"error": "not found"})
            else:
                self._send_json(200, {"status": "healthy", "scionVersion": "0.9.0"})
            return

        if self.path == "/healthz":
            if getattr(self.server, "fail_healthz_404", False):
                self._send_json(404, {"error": "not found"})
            else:
                self._send_json(200, {"status": "healthy", "scionVersion": "0.9.0"})
            return
            
        if self.path == "/non-utf8":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"hello \xff world")
            return
            
        if self.path == "/error-non-utf8":
            self.send_response(500)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"error \xff page")
            return

        if self.path == "/api/v1/projects":
            if getattr(self.server, "empty_collections", False):
                self._send_json(200, {"projects": [], "totalCount": 0})
                return
            self._send_json(200, {"projects": [{"slug": "proj-1", "id": "proj-1"}] + list(getattr(self.server, "projects_by_slug", {}).values())})
            return
            
        
        if "/api/v1/templates" in self.path:
            self._send_json(200, {"templates": []})
            return

        if self.path.startswith("/api/v1/harness-configs/") and self.path.endswith("/download"):
            self._send_json(200, {"files": [{"path": "config.yaml", "url": self.server.url + "/dl-hc/config.yaml"}]})
            return

        if self.path.startswith("/dl-hc/config.yaml"):
            self.send_response(200)
            self.send_header("Content-Type", "text/yaml")
            self.end_headers()
            self.wfile.write(b'harness: antigravity\ncommand:\n  base: ["agy"]\n')
            return

        if self.path.startswith("/api/v1/harness-configs"):
            self._send_json(
                200,
                {
                    "harnessConfigs": getattr(
                        self.server,
                        "harness_configs_list",
                        [
                            {"id": "hc-agy", "name": "antigravity", "harness": "antigravity"},
                            {"id": "hc-claude", "name": "claude", "harness": "claude"},
                            {"id": "hc-opencode", "name": "opencode", "harness": "opencode"},
                            {"id": "hc-hermes", "name": "hermes", "harness": "hermes"},
                        ],
                    )
                },
            )
            return

        if self.path.startswith("/api/v1/users?"):
            if getattr(self.server, "empty_collections", False):
                self._send_json(200, {"users": [], "totalCount": 0})
                return
            self._send_json(200, {"users": [{"email": "po@example.com", "role": "admin"}]})
            return

        if self.path.startswith("/api/v1/messages"):
            msgs = getattr(self.server, "messages_list", [])
            self._send_json(200, {"items": msgs, "totalCount": len(msgs)})
            return

        m_proj = re.match(r"/api/v1/projects/([^/]+)/settings", self.path)
        if m_proj:
            pid = m_proj.group(1)
            self._send_json(200, getattr(self.server, "project_settings", {}).get(pid, {}))
            return

        m_aroles = re.match(r"/api/v1/projects/([^/]+)/members/assignable-roles", self.path)
        if m_aroles:
            self._send_json(200, {"items": [{"id": "rd-proj-admin", "name": "project-admin"}, {"id": "rd-proj-member", "name": "project-member"}]})
            return

        m_members = re.match(r"/api/v1/projects/([^/]+)/members(?:\?.*)?$", self.path)
        if m_members:
            pid = m_members.group(1)
            items = getattr(self.server, "project_members", {}).get(pid, [])
            self._send_json(200, {"items": items, "totalCount": len(items)})
            return

        if "/shared-dirs/" in self.path:
            parsed_url = urlparse(self.path)
            if "%2F" in parsed_url.path or "%2f" in parsed_url.path:
                self._send_json(400, {"error": "Cloud Run GFE rejects encoded slash in path"})
                return
            parts = parsed_url.path.split("/")
            # /api/v1/projects/{pid}/shared-dirs/{dirname}/files[/{rel_path...}]
            if len(parts) == 6:
                existing = getattr(self.server, "shared_dirs", {}).get(parts[4], [{"name": "scratchpad", "read_only": False}])
                self._send_json(200, {"sharedDirs": existing})
                return
            if len(parts) >= 8 and parts[7] == "files" and len(parts) > 8:
                dirname = parts[6]
                rel_path = urllib.parse.unquote("/".join(parts[8:]))
                files_dict = self.server.shared_dir_files.get((parts[4], dirname), {})
                content = files_dict.get(rel_path, "")
                qs = urllib.parse.parse_qs(parsed_url.query)
                if qs.get("format") == ["json"]:
                    self._send_json(200, {"path": rel_path, "content": content, "encoding": "utf-8"})
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.end_headers()
                    self.wfile.write(content.encode("utf-8"))
                return
            dirname = parts[6]
            files_dict = self.server.shared_dir_files.get((parts[4], dirname), {})
            files_list = [{"path": name, "size": len(content)} for name, content in files_dict.items()]
            self._send_json(200, {"files": files_list, "totalCount": len(files_list)})
            return

        if self.path.startswith("/api/v1/projects/proj-1/agents/lead"):
            if not getattr(self.server, "agent_created", False) and getattr(self.server, "cold_start_503_once", False):
                self._send_json(404, {"error": "not found"})
                return
            phase = getattr(self.server, "agent_phase", "running")
            self._send_json(200, {"phase": phase})
            return

        self._send_json(404, {"error": "not found"})

    def do_PUT(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        self.server.requests.append({
            "method": "PUT",
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
        })
        
        
        if "/api/v1/templates" in self.path:
            self._send_json(200, {"items": []})
            return

        m_proj = re.match(r"/api/v1/projects/([^/]+)/settings", self.path)
        if m_proj:
            pid = m_proj.group(1)
            b = json.loads(body.decode("utf-8")) if body else {}
            if hasattr(self.server, "project_settings"):
                self.server.project_settings.setdefault(pid, {}).update(b)
            self._send_json(200, {"id": "tpl-1"})
            return

        if "/shared-dirs/" in self.path:
            parsed_url = urlparse(self.path)
            if "%2F" in parsed_url.path or "%2f" in parsed_url.path:
                self._send_json(400, {"error": "Cloud Run GFE rejects encoded slash in path"})
                return
            parts = parsed_url.path.split("/")
            if len(parts) == 7:
                # PUT /api/v1/projects/{pid}/shared-dirs/{name} -> 405 Method Not Allowed upstream!
                self._send_json(405, {"error": "method not allowed"})
                return
            dirname = parts[6]
            rel_path = urllib.parse.unquote("/".join(parts[8:]))
            files_dict = self.server.shared_dir_files.setdefault((parts[4], dirname), {})
            data = json.loads(body.decode("utf-8"))
            files_dict[rel_path] = data.get("content", "")
            self._send_json(200, {"path": rel_path})
            return

        if self.path.startswith("/api/v1/env/"):
            parsed_url = urlparse(self.path)
            key = urllib.parse.unquote(parsed_url.path.split("/api/v1/env/", 1)[1])
            qs = urllib.parse.parse_qs(parsed_url.query)
            scope_id = (qs.get("scopeId") or [""])[0]
            data = json.loads(body.decode("utf-8")) if body else {}
            self.server.__dict__.setdefault("project_env", {}).setdefault(scope_id, {})[key] = data
            self._send_json(200, {"key": key, "scopeId": scope_id, **data})
            return

        if self.path.startswith("/api/v1/secrets/"):
            import base64
            parsed_url = urlparse(self.path)
            name = urllib.parse.unquote(parsed_url.path.split("/api/v1/secrets/", 1)[1])
            qs = urllib.parse.parse_qs(parsed_url.query)
            scope_id = (qs.get("scopeId") or [""])[0]
            data = json.loads(body.decode("utf-8")) if body else {}
            val = data.get("value", "")
            try:
                raw_bytes = base64.b64decode(val.encode("ascii"), validate=True)
                if base64.b64encode(raw_bytes).decode("ascii") != val:
                    raise ValueError("non-canonical base64")
            except Exception:
                self._send_json(400, {"error": "value must be base64-encoded"})
                return
            self.server.__dict__.setdefault("project_secrets", {}).setdefault(scope_id, {})[name] = data
            self._send_json(200, {"name": name, "scopeId": scope_id})
            return

        self._send_json(200, {"status": "ok"})

    def do_PATCH(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        self.server.requests.append({
            "method": "PATCH",
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
        })
        self._send_json(200, {"status": "ok"})
        
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        self.server.requests.append({
            "method": "POST",
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
        })
        

        if self.path.endswith("/exec"):
            parts = self.path.split("/")
            pid, slug = parts[4], parts[6]
            import json
            b = json.loads(body.decode("utf-8")) if body else {}
            self.server.exec_commands.setdefault((pid, slug), []).append(b.get("command", []))
            codes = getattr(self.server, "exec_exit_codes", {}).get((pid, slug))
            exit_code = codes.pop(0) if codes else 0
            self._send_json(200, {"output": self.server.exec_outputs.get((pid, slug), ""), "exitCode": exit_code})
            return
        if "/agents/" in self.path and self.path.endswith("/start"):
            parts = self.path.split("/")
            pid, slug = parts[4], parts[6]
            self.server.set_agent_phase(pid, slug, "running")
            self._send_json(200, {})
            return

        import json
        if self.path == "/api/v1/users":
            b = json.loads(body.decode("utf-8")) if body else {}
            users = self.server.__dict__.setdefault("provisioned_users", [])
            rec = {"id": f"u-prov-{len(users) + 1}", "email": b.get("email", ""), "status": "invited"}
            users.append(rec)
            self._send_json(201, {"user": rec, "created": True})
            return

        m_add_member = re.match(r"/api/v1/projects/([^/]+)/members$", self.path)
        if m_add_member:
            pid = m_add_member.group(1)
            b = json.loads(body.decode("utf-8")) if body else {}
            members = self.server.__dict__.setdefault("project_members", {}).setdefault(pid, [])
            members.append(b)
            self._send_json(201, {"binding": b})
            return

        m_proj = re.match(r"/api/v1/projects$", self.path)
        if m_proj:
            b = json.loads(body.decode("utf-8")) if body else {}
            slug = b.get("slug", "proj") if isinstance(b, dict) else str(b)
            pid = f"pid-{slug}"
            if hasattr(self.server, "projects_by_slug"):
                self.server.projects_by_slug[slug] = {"id": pid, "slug": slug}
                self.server.project_settings[pid] = {}
                self.server.agents_by_project[pid] = []
                self.server.schedules[pid] = []
            self._send_json(200, {"id": pid, "slug": slug})
            return
            
        if "/api/v1/templates" in self.path:
            self._send_json(200, {"id": "tpl-1"})
            return

        if self.path.startswith("/api/v1/harness-configs"):
            b = json.loads(body.decode("utf-8")) if body else {}
            if self.path.endswith("/upload"):
                urls = [{"path": f["path"], "url": self.server.url + "/ul-hc/" + f["path"], "method": "PUT"} for f in b.get("files", [])]
                self._send_json(200, {"uploadUrls": urls, "uploadId": "up-hc-1"})
                return
            if self.path.endswith("/finalize"):
                self._send_json(200, {"status": "active"})
                return
            hc = {"id": f"hc-{b.get('name', 'new')}", "name": b.get("name", "new"), "harness": b.get("harness", "")}
            self.server.__dict__.setdefault("harness_configs_list", []).append(hc)
            self._send_json(201, {"harnessConfig": hc})
            return

        m_service_verify = re.match(r"/api/v1/projects/([^/]+)/gcp-service-accounts/([^/]+)/verify$", self.path)
        if m_service_verify:
            pid, sa_id = m_service_verify.group(1), m_service_verify.group(2)
            for sa in getattr(self.server, "gcp_service_accounts", {}).get(pid, []):
                if sa["id"] == sa_id:
                    sa["verified"] = True
                    self._send_json(200, {"verified": True, "serviceAccount": sa})
                    return
            self._send_json(404, {"error": "not found"})
            return

        m_service = re.match(r"/api/v1/projects/([^/]+)/gcp-service-accounts$", self.path)
        if m_service:
            pid = m_service.group(1)
            b = json.loads(body.decode("utf-8")) if body else {}
            if not hasattr(self.server, "gcp_service_accounts"):
                self.server.gcp_service_accounts = {}
            sa_list = self.server.gcp_service_accounts.setdefault(pid, [])
            sa_id = f"sa-uuid-{len(sa_list) + 1}"
            verified = not getattr(self.server, "initial_sa_unverified", False)
            sa = {"id": sa_id, "email": b.get("email"), "projectId": b.get("projectId")}
            if not getattr(self.server, "omit_verified_field", False):
                sa["verified"] = verified
            sa_list.append(sa)
            self._send_json(201, sa)
            return

        m_schedule = re.match(r"/api/v1/projects/([^/]+)/schedules$", self.path)
        if m_schedule:
            pid = m_schedule.group(1)
            b = json.loads(body.decode("utf-8")) if body else {}
            if not b.get("cronExpr") or not b.get("eventType"):
                self._send_json(400, {"error": "cronExpr and eventType are required"})
                return
            b.setdefault("id", f"sched-{len(getattr(self.server, 'schedules', {}).get(pid, [])) + 1}")
            b.setdefault("status", "active")
            if hasattr(self.server, "schedules"):
                self.server.schedules.setdefault(pid, []).append(b)
            self._send_json(201, b)
            return

        m_shared = re.match(r"/api/v1/projects/([^/]+)/shared-dirs$", self.path)
        if m_shared:
            pid = m_shared.group(1)
            b = json.loads(body.decode("utf-8")) if body else {}
            if not hasattr(self.server, "shared_dirs"):
                self.server.shared_dirs = {}
            dirs = self.server.shared_dirs.setdefault(pid, [{"name": "scratchpad", "read_only": False}])
            if any(d["name"] == b.get("name") for d in dirs):
                self._send_json(400, {"error": "shared dir already exists"})
                return
            dirs.append({"name": b.get("name"), "read_only": bool(b.get("read_only", False))})
            self._send_json(201, dirs[-1])
            return

        m_agent_msg = re.match(r"/api/v1/projects/([^/]+)/agents/([^/]+)/message", self.path)
        if m_agent_msg:
            self._send_json(200, {"id": "tpl-1"})
            return

        m_agent_start = re.match(r"/api/v1/projects/([^/]+)/agents/([^/]+)/start", self.path)
        if m_agent_start:
            pid = m_agent_start.group(1)
            slug = m_agent_start.group(2)
            self.server.set_agent_phase(pid, slug, "running")
            self._send_json(200, {})
            return

        m_agents = re.match(r"/api/v1/projects/([^/]+)/agents$", self.path)
        if m_agents and m_agents.group(1) != "proj-1":
            pid = m_agents.group(1)
            b = json.loads(body.decode("utf-8")) if body else {}
            slug = b.get("name")
            if hasattr(self.server, "agents_by_project"):
                self.server.agents_by_project.setdefault(pid, []).append(slug)
                self.server.seed_agent(pid, slug=slug, phase="provisioning")
                self.server.set_agent_phase(pid, slug, "running")
            self._send_json(200, {"phase": "provisioning"})
            return

        if self.path == "/api/v1/projects/proj-1/agents":
            import json
            b = json.loads(body.decode("utf-8")) if body else {}
            slug = b.get("name")
            
            if not any(a.get("slug") == slug for a in self.server.agents):
                self.server.seed_agent("proj-1", slug=slug, phase="provisioning")
                
            self.server.agent_created = True
            if getattr(self.server, "cold_start_503_once", False):
                self.server.cold_start_503_once = False
                self.server.set_agent_phase("proj-1", slug, "running")
                self.send_response(503)
                self.end_headers()
                return
            self._send_json(200, {"phase": "provisioning"})
            return

        self._send_json(200, {"status": "ok"})

    def do_PATCH(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        self.server.requests.append({
            "method": "PATCH",
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
        })
        self._send_json(200, {"status": "ok"})
        
    def do_DELETE(self):
        self.server.requests.append({
            "method": "DELETE",
            "path": self.path,
            "headers": dict(self.headers),
        })
        
        if "/api/v1/templates" in self.path:
            self._send_json(200, {"items": []})
            return

        m_proj = re.match(r"/api/v1/projects/([^/]+)/settings", self.path)
        if m_proj:
            pid = m_proj.group(1)
            b = json.loads(body.decode("utf-8")) if body else {}
            if hasattr(self.server, "project_settings"):
                self.server.project_settings.setdefault(pid, {}).update(b)
            self._send_json(200, {"id": "tpl-1"})
            return

        if "/shared-dirs/" in self.path:
            parsed_url = urlparse(self.path)
            if "%2F" in parsed_url.path or "%2f" in parsed_url.path:
                self._send_json(400, {"error": "Cloud Run GFE rejects encoded slash in path"})
                return
            parts = parsed_url.path.split("/")
            dirname = parts[6]
            rel_path = urllib.parse.unquote("/".join(parts[8:]))
            files_dict = self.server.shared_dir_files.get((parts[4], dirname), {})
            if rel_path in files_dict:
                del files_dict[rel_path]
            self._send_json(200, {"id": "tpl-1"})
            return
        self._send_json(200, {"status": "ok"})

    def do_PATCH(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        self.server.requests.append({
            "method": "PATCH",
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
        })
        self._send_json(200, {"status": "ok"})

class CrossOriginHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass
    def do_PUT(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        self.server.cross_origin_requests.append({
            "method": "PUT",
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
        })
        self.send_response(200)
        self.end_headers()


@pytest.fixture
def fake_hub():
    server = HTTPServer(("127.0.0.1", 0), FakeHubHandler)
    port = server.server_address[1]
    server.agents = []
    server.projects_by_slug = {}
    server.project_settings = {}
    server.schedules = {}
    server.shared_dirs = {}
    server.gcp_service_accounts = {}
    server.agents_by_project = {}
    server.exec_outputs = {}
    server.exec_commands = {}
    server.started_agents = []
    def seed_agent(pid, **kwargs):
        server.agents.append({"id": "ag-1", "slug": kwargs.get("slug"), "phase": kwargs.get("phase"), "activity": kwargs.get("activity"), "harnessConfig": kwargs.get("harness"), "taskSummary": kwargs.get("task_summary"), "runtime": kwargs.get("runtime"), "pod_name": kwargs.get("pod_name")})
    server.seed_agent = seed_agent
    def set_agent_phase(pid, slug, phase):
        for a in server.agents:
            if isinstance(a, dict) and a.get("slug") == slug:
                a["phase"] = phase
        for a in server.agents_by_project.get(pid, []):
            if isinstance(a, dict) and a.get("slug") == slug:
                a["phase"] = phase
    server.set_agent_phase = set_agent_phase
    server.url = f"http://127.0.0.1:{port}"
    server.requests = []
    server.shared_dir_files = {}
    
    cross_server = HTTPServer(("127.0.0.1", 0), CrossOriginHandler)
    cross_port = cross_server.server_address[1]
    cross_server.cross_origin_requests = []
    server.cross_origin_upload_url = f"http://127.0.0.1:{cross_port}/upload"
    server.cross_origin_requests = cross_server.cross_origin_requests
    
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    
    t2 = threading.Thread(target=cross_server.serve_forever, daemon=True)
    t2.start()
    
    yield server
    
    server.shutdown()
    server.server_close()
    cross_server.shutdown()
    cross_server.server_close()

def test_health_falls_back_between_health_and_healthz(fake_hub):
    fake_hub.fail_health_404 = True
    fake_hub.fail_healthz_404 = False
    client = hub_client.HubClient(base_url=fake_hub.url, token="tok-123")
    assert client.health() == {"status": "healthy", "scionVersion": "0.9.0"}

def test_load_credentials_token_normalizes_endpoint(tmp_path):
    creds = {
        "version": 1,
        "hubs": {
            "https://hub.example.com": {
                "accessToken": "scion_oauth_tok",
                "expiresAt": "2099-01-01T00:00:00Z",
            }
        },
    }
    (tmp_path / "credentials.json").write_text(json.dumps(creds))
    assert hub_client.load_credentials_token(str(tmp_path), "https://HUB.example.com/") == "scion_oauth_tok"

def test_iap_proxy_authorization_keeps_hub_bearer_and_strips_on_cross_origin(fake_hub):
    client = hub_client.HubClient(
        base_url=fake_hub.url,
        token="hub-jwt",
        transport_token="iap-oidc-jwt",
        transport_mode="iap",
    )
    status, data = client.request("GET", "/api/v1/projects")
    assert status == 200
    last = fake_hub.requests[-1]["headers"]
    assert last["Authorization"] == "Bearer hub-jwt"
    assert last["Proxy-Authorization"] == "Bearer iap-oidc-jwt"

    status, _ = client.request("PUT", fake_hub.cross_origin_upload_url, raw=b"hello")
    assert status == 200
    upload_hdrs = fake_hub.cross_origin_requests[-1]["headers"]
    assert "Authorization" not in upload_hdrs
    assert "Proxy-Authorization" not in upload_hdrs

def test_ensure_agent_started_polls_after_cold_autopilot_503_without_duplicate_create(fake_hub):
    fake_hub.cold_start_503_once = True
    client = hub_client.HubClient(base_url=fake_hub.url, token="tok-123")
    agent = client.ensure_agent_started(
        "proj-1",
        "lead",
        create_body={"name": "lead", "template": "lead", "task": "Wake up."},
        wait_sec=5,
        poll_sec=0.05,
    )
    assert agent["phase"] == "running"
    create_posts = [r for r in fake_hub.requests if r["method"] == "POST" and r["path"] == "/api/v1/projects/proj-1/agents"]
    assert len(create_posts) == 1

def test_sync_shared_dir_uploads_changed_and_deletes_stale_files(fake_hub, tmp_path):
    src = tmp_path / "knowledge-plane"
    (src / "adrs").mkdir(parents=True)
    (src / "index.md").write_text("# Index\n")
    (src / "adrs" / "ADR-001.md").write_text("# ADR 1\n")
    fake_hub.shared_dir_files[("proj-1", "knowledge-plane")] = {"stale.md": "old", "index.md": "# Index\n"}

    client = hub_client.HubClient(base_url=fake_hub.url, token="tok-123")
    plan = client.sync_shared_dir("proj-1", "knowledge-plane", str(src), apply=False)
    assert plan == {"uploaded": ["adrs/ADR-001.md"], "deleted": ["stale.md"], "unchanged": ["index.md"]}
    assert "stale.md" in fake_hub.shared_dir_files[("proj-1", "knowledge-plane")]

    applied = client.sync_shared_dir("proj-1", "knowledge-plane", str(src), apply=True)
    assert applied == plan
    assert fake_hub.shared_dir_files[("proj-1", "knowledge-plane")] == {
        "index.md": "# Index\n",
        "adrs/ADR-001.md": "# ADR 1\n",
    }

def test_non_utf8_response_and_error(fake_hub):
    client = hub_client.HubClient(base_url=fake_hub.url, token="tok-123")
    status, data = client.request("GET", "/non-utf8")
    assert status == 200
    assert "hello \ufffd world" in data

    with pytest.raises(hub_client.HubAPIError) as exc_info:
        client.request("GET", "/error-non-utf8")
    assert exc_info.value.status == 500
    assert "error \ufffd page" in exc_info.value.body


def test_empty_collections_do_not_return_wrapper_dict(fake_hub):
    fake_hub.empty_collections = True
    client = hub_client.HubClient(base_url=fake_hub.url, token="tok-123")
    assert client.list_projects() == []
    assert client.get_project_by_slug("missing") is None
    assert client.list_agents("proj-1") == []
    assert client.list_shared_dir_files("proj-1", "knowledge-plane") == []
    assert client.find_user("nobody@example.com") is None


def test_from_env_supports_hub_token_and_impersonate_sa_for_iap(fake_hub, tmp_path, monkeypatch):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    gcloud_log = tmp_path / "gcloud.log"
    (fake_bin / "gcloud").write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> "{gcloud_log}"\n'
        'echo "iap-oidc-impersonated"\n'
    )
    (fake_bin / "gcloud").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    # Force metadata server miss quickly
    monkeypatch.setattr(hub_client.urllib.request, "urlopen",
                        lambda req, timeout=60: (_ for _ in ()).throw(OSError("no metadata"))
                        if "metadata.google.internal" in req.full_url
                        else urllib.request.build_opener().open(req, timeout=timeout))

    # 1. Pure IAP proxy mode (no SCION_HUB_TOKEN): sends Proxy-Authorization ONLY so Step 3a triggers!
    client = hub_client.HubClient.from_env(
        {
            "HUB_URL": fake_hub.url,
            "HUB_IAP_AUDIENCE": "123-abc.apps.googleusercontent.com",
            "HUB_IMPERSONATE_SA": "tfha-operator@demo.iam.gserviceaccount.com",
        },
        scion_home=str(tmp_path / "empty-scion"),
    )
    status, _ = client.request("GET", "/api/v1/projects")
    assert status == 200
    hdrs = fake_hub.requests[-1]["headers"]
    assert hdrs.get("Proxy-Authorization") == "Bearer iap-oidc-impersonated"
    assert "Authorization" not in hdrs
    assert "--impersonate-service-account=tfha-operator@demo.iam.gserviceaccount.com" in gcloud_log.read_text()

    # 2. HUB_TOKEN fallback
    client2 = hub_client.HubClient.from_env(
        {"HUB_URL": fake_hub.url, "HUB_TOKEN": "pat-fallback"},
        scion_home=str(tmp_path / "empty-scion"),
    )
    assert client2.token == "pat-fallback"


def test_register_project_service_account_verifies_when_initially_unverified(fake_hub):
    import inspect

    sig = inspect.signature(hub_client.HubClient.register_project_service_account)
    assert sig.parameters["verify_retries"].default * sig.parameters["verify_delay"].default >= 90

    fake_hub.initial_sa_unverified = True
    fake_hub.omit_verified_field = True
    client = hub_client.HubClient(base_url=fake_hub.url, token="tok-123")
    sa = client.register_project_service_account(
        "proj-1", "agent-worker@sandbox.iam.gserviceaccount.com", "sandbox", verify_retries=2, verify_delay=0.01
    )
    assert sa["id"] == "sa-uuid-1"
    assert sa["verified"] is True


def test_from_env_falls_back_to_adc_iamcredentials_when_gcloud_impersonation_fails(fake_hub, tmp_path, monkeypatch):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gcloud").write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == *"print-identity-token"* ]]; then exit 1; fi\n'
        'if [[ "$*" == *"application-default print-access-token"* ]]; then echo "adc-access-tok"; exit 0; fi\n'
        "exit 1\n"
    )
    (fake_bin / "gcloud").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")

    captured_iam = {}
    orig_opener = urllib.request.build_opener()

    class _FakeResp:
        def __init__(self, payload: bytes):
            self._payload = payload

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=60):
        if "iamcredentials.googleapis.com" in req.full_url:
            captured_iam["url"] = req.full_url
            captured_iam["headers"] = {k.lower(): v for k, v in req.header_items()}
            captured_iam["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResp(json.dumps({"token": "oidc-via-iamcredentials"}).encode("utf-8"))
        if "metadata.google.internal" in req.full_url:
            raise OSError("no metadata")
        return orig_opener.open(req, timeout=timeout)

    monkeypatch.setattr(hub_client.urllib.request, "urlopen", fake_urlopen)

    client = hub_client.HubClient.from_env(
        {
            "HUB_URL": fake_hub.url,
            "HUB_IAP_AUDIENCE": "123-abc.apps.googleusercontent.com",
            "HUB_IMPERSONATE_SA": "tfha-operator@demo-proj.iam.gserviceaccount.com",
        },
        scion_home=str(tmp_path / "empty-scion"),
    )
    assert client.transport_token == "oidc-via-iamcredentials"
    assert "tfha-operator@demo-proj.iam.gserviceaccount.com:generateIdToken" in captured_iam["url"]
    assert captured_iam["headers"].get("authorization") == "Bearer adc-access-tok"
    assert captured_iam["headers"].get("x-goog-user-project") == "demo-proj"
    assert captured_iam["body"] == {"audience": "123-abc.apps.googleusercontent.com", "includeEmail": True}


def test_from_env_uses_sa_access_token_for_google_managed_cloud_run_direct_iap(fake_hub, tmp_path, monkeypatch):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gcloud").write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == *"print-access-token --impersonate-service-account"* ]]; then exit 1; fi\n'
        'if [[ "$*" == *"application-default print-access-token"* ]]; then echo "adc-user-tok"; exit 0; fi\n'
        "exit 1\n"
    )
    (fake_bin / "gcloud").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")

    captured_iam = {}
    orig_opener = urllib.request.build_opener()

    class _FakeResp:
        def __init__(self, payload: bytes):
            self._payload = payload

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=60):
        if "iamcredentials.googleapis.com" in req.full_url:
            captured_iam["url"] = req.full_url
            captured_iam["headers"] = {k.lower(): v for k, v in req.header_items()}
            captured_iam["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResp(json.dumps({"accessToken": "sa-oauth-access-tok"}).encode("utf-8"))
        if "metadata.google.internal" in req.full_url:
            captured_meta_urls.append((req.full_url, timeout))
            if len(captured_meta_urls) == 1:
                raise TimeoutError("cold gke-metadata-server warmup timeout")
            if (
                "/instance/service-accounts/default/token?scopes=" in req.full_url
                and "userinfo.email" in req.full_url
                and "openid" in req.full_url
            ):
                return _FakeResp(json.dumps({"access_token": "wi-metadata-access-tok", "token_type": "Bearer"}).encode("utf-8"))
            raise AssertionError(f"Direct IAP metadata /token missing userinfo.email+openid scopes (IAP Error 90): {req.full_url}")
        return orig_opener.open(req, timeout=timeout)

    captured_meta_urls: list[tuple[str, float | None]] = []
    monkeypatch.setattr(hub_client.urllib.request, "urlopen", fake_urlopen)

    managed_aud = "369001918367-syntheticdirectiap.apps.googleusercontent.com"

    # Case 1: Operator impersonating SA via ADC iamcredentials:generateAccessToken
    c_op = hub_client.HubClient.from_env(
        {
            "HUB_URL": fake_hub.url,
            "HUB_IAP_AUDIENCE": managed_aud,
            "HUB_IMPERSONATE_SA": "tfha-operator@demo-proj.iam.gserviceaccount.com",
        },
        scion_home=str(tmp_path / "empty-scion"),
    )
    assert c_op.transport_token == "sa-oauth-access-tok"
    assert "tfha-operator@demo-proj.iam.gserviceaccount.com:generateAccessToken" in captured_iam["url"]
    assert "https://www.googleapis.com/auth/cloud-platform" in captured_iam["body"]["scope"]

    # Case 2: GKE CronJob Workload Identity (no HUB_IMPERSONATE_SA) -> scoped metadata /token with cold-start retry
    c_wi = hub_client.HubClient.from_env(
        {
            "HUB_URL": fake_hub.url,
            "HUB_IAP_AUDIENCE": managed_aud,
            "KUBERNETES_SERVICE_HOST": "10.0.0.1",
        },
        scion_home=str(tmp_path / "empty-scion"),
    )
    assert c_wi.transport_token == "wi-metadata-access-tok"
    assert len(captured_meta_urls) == 2
    assert captured_meta_urls[-1][1] is not None and captured_meta_urls[-1][1] >= 10.0


def test_ensure_agent_started_handles_hub_broker_502_timeout_vs_rollback(monkeypatch):
    client = hub_client.HubClient(base_url="https://hub.example.com", token="tok")

    # 1. Cold Autopilot 120s Hub->broker timeout returns 502 while agent row is provisioning -> running
    states = iter([None, {"phase": "provisioning"}, {"phase": "running"}])
    monkeypatch.setattr(client, "get_agent", lambda pid, slug: next(states))

    def fake_request_timeout(method, path, body=None, **kw):
        assert method == "POST"
        raise hub_client.HubAPIError(502, method, path, '{"error":{"message":"Failed to dispatch to runtime broker"}}')

    monkeypatch.setattr(client, "request", fake_request_timeout)
    started = client.ensure_agent_started("proj-1", "lead", create_body={"name": "lead"}, wait_sec=2, poll_sec=0.01)
    assert started["phase"] == "running"

    # 2. Synchronous broker 500 -> Hub 502 with rolled-back agent (get_agent -> None) must re-raise 502 immediately
    monkeypatch.setattr(client, "get_agent", lambda pid, slug: None)
    with pytest.raises(hub_client.HubAPIError) as exc_info:
        client.ensure_agent_started("proj-1", "lead", create_body={"name": "lead"}, wait_sec=2, poll_sec=0.01)
    assert exc_info.value.status == 502


def test_create_project_and_ensure_agent_started_handle_python311_timeout_error(monkeypatch):
    client = hub_client.HubClient(base_url="https://hub.example.com", token="tok")

    # Simulate large monorepo shared clone where ssl.SSLSocket.read raises TimeoutError (not URLError),
    # workspace/files is initially empty while cloning, then populated once CloneSharedWorkspace finishes.
    ws_polls = iter([{"files": []}, {"files": [{"path": "AGENTS.md", "size": 100}]}])
    proj_polls = iter([None, {"id": "p-uuid", "slug": "k8s-version"}, {"id": "p-uuid", "slug": "k8s-version"}])
    monkeypatch.setattr(client, "get_project_by_slug", lambda slug: next(proj_polls))

    def fake_req(method, path, body=None, **kw):
        if method == "POST" and path == "/api/v1/projects":
            raise TimeoutError("The read operation timed out")
        if method == "GET" and path == "/api/v1/projects/p-uuid/workspace/files":
            return 200, next(ws_polls)
        raise AssertionError(f"Unexpected request: {method} {path}")

    monkeypatch.setattr(client, "request", fake_req)
    proj = client.create_project(
        {"slug": "k8s-version", "name": "k8s-version", "gitRemote": "https://github.com/o/r.git", "workspaceMode": "shared"},
        wait_sec=2,
        poll_sec=0.01,
    )
    assert proj["id"] == "p-uuid"

    # Also verify ensure_agent_started catches TimeoutError on POST /agents and polls to running
    agent_polls = iter([None, {"phase": "provisioning"}, {"phase": "running"}])
    monkeypatch.setattr(client, "get_agent", lambda pid, slug: next(agent_polls))
    monkeypatch.setattr(client, "request", lambda *a, **kw: (_ for _ in ()).throw(TimeoutError("read timed out")))
    agent = client.ensure_agent_started("p-uuid", "lead", create_body={"name": "lead"}, wait_sec=2, poll_sec=0.01)
    assert agent["phase"] == "running"





