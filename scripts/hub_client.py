"""Scion Hub REST client for local admin JWTs, Direct IAP transport, and project provisioning.

HubClient wraps the Scion Hub HTTP API with standard-library urllib.
Setup and provisioning scripts run without external Python packages.
"""
import http.client
import urllib.request
import urllib.parse
import urllib.error
import json
import hmac
import hashlib
import base64
import sqlite3
import subprocess
import time
import os
from pathlib import Path
from typing import Mapping, Any


_TRANSIENT_CONN_ERRORS = (
    http.client.RemoteDisconnected,
    ConnectionError,
    TimeoutError,
    urllib.error.URLError,
)


def _extract_list(res: Any, *keys: str) -> list[dict]:
    if isinstance(res, list):
        return res
    if isinstance(res, dict):
        for k in keys:
            val = res.get(k)
            if isinstance(val, list):
                return val
    return []


def normalize_endpoint(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    return f"{scheme}://{netloc}"


def load_credentials_token(scion_home: str, hub_url: str) -> str | None:
    creds_file = Path(scion_home) / "credentials.json"
    if not creds_file.exists():
        return None
    try:
        creds = json.loads(creds_file.read_text())
        hubs = creds.get("hubs", {})
        norm = normalize_endpoint(hub_url)
        for k, v in hubs.items():
            if normalize_endpoint(k) == norm:
                expires = v.get("expiresAt")
                import datetime
                if expires:
                    expires_dt = datetime.datetime.strptime(expires, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
                    if expires_dt < datetime.datetime.now(datetime.timezone.utc):
                        return None
                return v.get("accessToken")
    except Exception:
        return None
    return None


def mint_local_admin_jwt(scion_home: str, email: str = "", ttl: int = 300) -> str | None:
    db_path = Path(scion_home) / "hub.db"
    env_path = Path(scion_home) / "hub.env"
    if not db_path.exists() or not env_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        user = conn.execute("SELECT id, email, role FROM users WHERE email = ?", (email,)).fetchone()
        if not user:
            return None

        secret = None
        for line in env_path.read_text().splitlines():
            if line.startswith("SCION_SERVER_SESSION_SECRET="):
                secret = line.split("=", 1)[1].strip()
        if not secret:
            return None

        key = hashlib.sha256(("scion-hub-signing-key:user_signing_key:" + secret).encode()).digest()

        def b64(data):
            return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

        now = int(time.time())
        claims = {
            "iss": "scion-hub", "sub": user["id"], "aud": ["scion-hub-api"], "exp": now + ttl, "nbf": now - 10,
            "iat": now, "uid": user["id"], "email": user["email"], "name": user["email"], "role": user["role"],
            "type": "access", "client": "web",
        }
        signing_input = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()) + "." + b64(json.dumps(claims).encode())
        signature = hmac.new(key, signing_input.encode(), hashlib.sha256).digest()
        return signing_input + "." + b64(signature)
    except Exception:
        return None


class HubAPIError(RuntimeError):
    def __init__(self, status: int, method: str, path: str, body: str):
        super().__init__(f"Hub API Error {status}: {method} {path} - {body}")
        self.status = status
        self.method = method
        self.path = path
        self.body = body


# Google-managed Cloud Run Direct IAP global browser client prefix: Direct IAP
# rejects OIDC ID tokens against this client and accepts SA OAuth2 access tokens
# in Proxy-Authorization unless a custom OAuth client is bound via Cloud Console.
GOOGLE_MANAGED_DIRECT_IAP_PREFIX = "369001918367-"


class HubClient:
    def __init__(self, base_url: str, token: str | None = None, transport_token: str | None = None, transport_mode: str = "iap"):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.transport_token = transport_token
        self.transport_mode = transport_mode
        self.is_remote = not (self.base_url.startswith("http://127.0.0.1") or self.base_url.startswith("http://localhost"))

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, scion_home: str | None = None) -> "HubClient":
        if env is None:
            env = os.environ
        if scion_home is None:
            scion_home = os.path.expanduser(env.get("SCION_HOME", "~/.scion"))

        hub_url = env.get("HUB_URL", "").strip()
        base_url = hub_url or env.get("HUB_LOCAL_URL", "http://127.0.0.1:8080")

        token = (env.get("SCION_HUB_TOKEN") or env.get("HUB_TOKEN") or "").strip()
        if not token:
            token = load_credentials_token(scion_home, base_url)
        if not token:
            email = env.get("PO_EMAIL") or env.get("ADMIN_EMAIL") or ""
            token = mint_local_admin_jwt(scion_home, email)

        transport_token = env.get("HUB_TRANSPORT_TOKEN", "").strip()
        transport_mode = env.get("HUB_TRANSPORT_MODE", "iap").strip() or "iap"

        audience = (env.get("HUB_IAP_AUDIENCE") or env.get("SCION_TRANSPORT_AUDIENCE") or "").strip()
        impersonate_sa = (env.get("HUB_IMPERSONATE_SA") or "").strip()
        token_type = (env.get("HUB_TRANSPORT_TOKEN_TYPE") or "").strip()
        use_access_token = (token_type == "access_token") or audience.startswith(GOOGLE_MANAGED_DIRECT_IAP_PREFIX)
        if not transport_token and (audience or use_access_token):
            if use_access_token:
                if impersonate_sa:
                    try:
                        out = subprocess.check_output(
                            ["gcloud", "auth", "print-access-token", f"--impersonate-service-account={impersonate_sa}"],
                            text=True,
                            stderr=subprocess.DEVNULL,
                            timeout=15,
                        )
                        transport_token = out.strip()
                    except Exception:
                        pass
                    if not transport_token:
                        try:
                            adc_tok = subprocess.check_output(
                                ["gcloud", "auth", "application-default", "print-access-token"],
                                text=True,
                                stderr=subprocess.DEVNULL,
                                timeout=10,
                            ).strip()
                            if adc_tok:
                                quota_proj = env.get("HUB_PROJECT") or env.get("SANDBOX_PROJECT") or ""
                                if not quota_proj and "@" in impersonate_sa and ".iam.gserviceaccount.com" in impersonate_sa:
                                    quota_proj = impersonate_sa.split("@", 1)[1].split(".iam.gserviceaccount.com", 1)[0]
                                iam_headers = {
                                    "Authorization": f"Bearer {adc_tok}",
                                    "Content-Type": "application/json",
                                }
                                if quota_proj:
                                    iam_headers["x-goog-user-project"] = quota_proj
                                iam_req = urllib.request.Request(
                                    f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{urllib.parse.quote(impersonate_sa, safe='@.')}:generateAccessToken",
                                    data=json.dumps({
                                        "scope": [
                                            "https://www.googleapis.com/auth/cloud-platform",
                                            "https://www.googleapis.com/auth/userinfo.email",
                                            "openid",
                                        ]
                                    }).encode("utf-8"),
                                    headers=iam_headers,
                                    method="POST",
                                )
                                with urllib.request.urlopen(iam_req, timeout=10) as iam_resp:
                                    iam_data = json.loads(iam_resp.read().decode("utf-8", errors="replace"))
                                    transport_token = (iam_data.get("accessToken") or "").strip()
                        except Exception:
                            pass
                if not transport_token:
                    meta_scopes = "https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/userinfo.email,openid"
                    meta_timeout = 10.0 if env.get("KUBERNETES_SERVICE_HOST") else 2.0
                    meta_attempts = 2 if env.get("KUBERNETES_SERVICE_HOST") else 1
                    for _attempt in range(meta_attempts):
                        try:
                            req = urllib.request.Request(
                                f"http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token?scopes={meta_scopes}",
                                headers={"Metadata-Flavor": "Google"},
                            )
                            with urllib.request.urlopen(req, timeout=meta_timeout) as resp:
                                meta_data = json.loads(resp.read().decode("utf-8", errors="replace"))
                                transport_token = (meta_data.get("access_token") or "").strip()
                            if transport_token:
                                break
                        except Exception:
                            pass
                    if not transport_token:
                        try:
                            out = subprocess.check_output(
                                ["gcloud", "auth", "print-access-token"],
                                text=True,
                                stderr=subprocess.DEVNULL,
                                timeout=10,
                            )
                            transport_token = out.strip()
                        except Exception:
                            pass
            else:
                if impersonate_sa:
                    try:
                        out = subprocess.check_output(
                            [
                                "gcloud",
                                "auth",
                                "print-identity-token",
                                f"--impersonate-service-account={impersonate_sa}",
                                "--include-email",
                                f"--audiences={audience}",
                            ],
                            text=True,
                            stderr=subprocess.DEVNULL,
                            timeout=15,
                        )
                        transport_token = out.strip()
                    except Exception:
                        pass
                    if not transport_token:
                        try:
                            adc_tok = subprocess.check_output(
                                ["gcloud", "auth", "application-default", "print-access-token"],
                                text=True,
                                stderr=subprocess.DEVNULL,
                                timeout=10,
                            ).strip()
                            if adc_tok:
                                quota_proj = env.get("HUB_PROJECT") or env.get("SANDBOX_PROJECT") or ""
                                if not quota_proj and "@" in impersonate_sa and ".iam.gserviceaccount.com" in impersonate_sa:
                                    quota_proj = impersonate_sa.split("@", 1)[1].split(".iam.gserviceaccount.com", 1)[0]
                                iam_headers = {
                                    "Authorization": f"Bearer {adc_tok}",
                                    "Content-Type": "application/json",
                                }
                                if quota_proj:
                                    iam_headers["x-goog-user-project"] = quota_proj
                                iam_req = urllib.request.Request(
                                    f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{urllib.parse.quote(impersonate_sa, safe='@.')}:generateIdToken",
                                    data=json.dumps({"audience": audience, "includeEmail": True}).encode("utf-8"),
                                    headers=iam_headers,
                                    method="POST",
                                )
                                with urllib.request.urlopen(iam_req, timeout=10) as iam_resp:
                                    iam_data = json.loads(iam_resp.read().decode("utf-8", errors="replace"))
                                    transport_token = (iam_data.get("token") or "").strip()
                        except Exception:
                            pass
                if not transport_token:
                    try:
                        req = urllib.request.Request(
                            f"http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity?audience={urllib.parse.quote(audience, safe='')}&format=full",
                            headers={"Metadata-Flavor": "Google"},
                        )
                        with urllib.request.urlopen(req, timeout=2.0) as resp:
                            transport_token = resp.read().decode("utf-8", errors="replace").strip()
                    except Exception:
                        try:
                            out = subprocess.check_output(
                                ["gcloud", "auth", "print-identity-token", f"--audiences={audience}"],
                                text=True,
                                stderr=subprocess.DEVNULL,
                                timeout=10,
                            )
                            transport_token = out.strip()
                        except Exception:
                            pass

        return cls(base_url, token, transport_token, transport_mode)

    def _is_same_origin(self, url: str) -> bool:
        if url.startswith("/"):
            return True
        p1 = urllib.parse.urlparse(self.base_url)
        p2 = urllib.parse.urlparse(url)

        def eff_port(p):
            if p.port:
                return p.port
            return 443 if p.scheme == "https" else 80

        return p1.scheme == p2.scheme and p1.hostname == p2.hostname and eff_port(p1) == eff_port(p2)

    def request(self, method: str, path_or_url: str, body: Any = None, raw: bytes | None = None, headers: dict[str, str] | None = None, timeout: int = 60) -> tuple[int, Any]:
        url = self.base_url + path_or_url if path_or_url.startswith("/") else path_or_url

        req_headers = {}
        if headers:
            req_headers.update(headers)

        if self._is_same_origin(url):
            if self.token:
                req_headers["Authorization"] = f"Bearer {self.token}"
            if self.transport_token:
                if self.transport_mode == "iap":
                    req_headers["Proxy-Authorization"] = f"Bearer {self.transport_token}"
                elif self.transport_mode == "serverless":
                    req_headers["X-Serverless-Authorization"] = f"Bearer {self.transport_token}"

        data = None
        if raw is not None:
            data = raw
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            req_headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=req_headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp_body = resp.read()
                if resp_body:
                    try:
                        return resp.status, json.loads(resp_body.decode("utf-8", errors="replace"))
                    except Exception:
                        return resp.status, resp_body.decode("utf-8", errors="replace")
                return resp.status, {}
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise HubAPIError(e.code, method, url, err_body)

    def health(self) -> dict | None:
        try:
            return self.request("GET", "/health")[1]
        except HubAPIError as e:
            if e.status == 404:
                try:
                    return self.request("GET", "/healthz")[1]
                except Exception:
                    return None
            return None
        except Exception:
            return None

    def current_user(self) -> dict | None:
        try:
            return self.request("GET", "/api/v1/auth/me")[1]
        except Exception:
            return None

    def find_user(self, email: str) -> dict | None:
        try:
            res = self.request("GET", f"/api/v1/users?search={urllib.parse.quote(email)}")[1]
            items = _extract_list(res, "users", "items")
            for u in items:
                if u.get("email", "").lower() == email.lower():
                    return u
            return None
        except Exception:
            return None

    def list_projects(self) -> list[dict]:
        res = self.request("GET", "/api/v1/projects")[1]
        return _extract_list(res, "projects", "items")

    def get_project_by_slug(self, slug: str) -> dict | None:
        for p in self.list_projects():
            if p.get("slug") == slug:
                return p
        return None

    def project_id(self, slug: str) -> str:
        p = self.get_project_by_slug(slug)
        if not p:
            raise ValueError(f"Project not found: {slug}")
        return p["id"]

    def create_project(self, body: dict | str, wait_sec: int = 1800, poll_sec: float = 5.0) -> dict:
        if isinstance(body, str):
            body = {"name": body, "slug": body}
        slug = body.get("slug") or body.get("name")
        if slug:
            existing = self.get_project_by_slug(slug)
            if existing:
                return existing
        try:
            res = self.request("POST", "/api/v1/projects", body=body, timeout=360)[1]
            if isinstance(res, dict) and isinstance(res.get("project"), dict):
                return res["project"]
            return res
        except _TRANSIENT_CONN_ERRORS:
            pass
        except HubAPIError as e:
            if e.status == 409 and slug:
                existing = self.get_project_by_slug(slug)
                if existing:
                    return existing
            if e.status not in (502, 503, 504):
                raise

        wait_shared_clone = bool(body.get("workspaceMode") == "shared" and body.get("gitRemote"))
        seen_row = False
        start = time.time()
        while time.time() - start < wait_sec:
            p = self.get_project_by_slug(slug)
            if p:
                seen_row = True
                if not wait_shared_clone or not p.get("id"):
                    return p
                try:
                    _, wf = self.request("GET", f"/api/v1/projects/{p['id']}/workspace/files", timeout=30)
                    if _extract_list(wf, "files", "items"):
                        return p
                except HubAPIError as e:
                    if e.status == 404:
                        return p
                except _TRANSIENT_CONN_ERRORS:
                    pass
            elif seen_row:
                raise RuntimeError(f"Hub rolled back project {slug!r} (shared clone failed)")
            time.sleep(poll_sec)
        raise RuntimeError("Timeout waiting for project creation")

    def get_project_settings(self, project_id: str) -> dict:
        return self.request("GET", f"/api/v1/projects/{project_id}/settings")[1]

    def put_project_settings(self, project_id: str, patch: dict) -> dict:
        current = self.get_project_settings(project_id)
        if not isinstance(current, dict):
            current = {}
        current.update(patch)
        return self.request("PUT", f"/api/v1/projects/{project_id}/settings", body=current)[1]

    def list_project_service_accounts(self, project_id: str) -> list[dict]:
        res = self.request("GET", f"/api/v1/projects/{project_id}/gcp-service-accounts")[1]
        return _extract_list(res, "items", "serviceAccounts")

    def register_project_service_account(
        self,
        project_id: str,
        email: str,
        gcp_project: str,
        name: str = "",
        verify_retries: int = 12,
        verify_delay: float = 10.0,
    ) -> dict:
        sa = None
        for existing in self.list_project_service_accounts(project_id):
            if existing.get("email", "").lower() == email.lower():
                sa = existing
                break
        if sa is None:
            body = {"email": email, "projectId": gcp_project, "name": name or email.split("@")[0]}
            res = self.request("POST", f"/api/v1/projects/{project_id}/gcp-service-accounts", body=body)[1]
            sa = res.get("serviceAccount") if isinstance(res, dict) and isinstance(res.get("serviceAccount"), dict) else res

        if isinstance(sa, dict) and not sa.get("verified") and sa.get("id"):
            for attempt in range(verify_retries):
                try:
                    vres = self.request("POST", f"/api/v1/projects/{project_id}/gcp-service-accounts/{sa['id']}/verify", body={})[1]
                    if isinstance(vres, dict):
                        if isinstance(vres.get("serviceAccount"), dict):
                            sa = vres["serviceAccount"]
                        elif vres.get("verified"):
                            sa.update(vres)
                    if sa.get("verified"):
                        break
                except HubAPIError:
                    pass
                if attempt + 1 < verify_retries:
                    time.sleep(verify_delay)
        return sa

    def put_project_env(
        self,
        project_id: str,
        key: str,
        value: str,
        injection_mode: str = "always",
    ) -> dict:
        """Write a project-scoped environment variable to the Hub.

        If injection_mode is 'always', the Hub injects this variable into every container in the project.
        """
        enc_key = urllib.parse.quote(key, safe="")
        enc_pid = urllib.parse.quote(project_id, safe="")
        body = {"value": value, "injectionMode": injection_mode}
        return self.request("PUT", f"/api/v1/env/{enc_key}?scope=project&scopeId={enc_pid}", body=body)[1]

    def put_project_secret(
        self,
        project_id: str,
        name: str,
        raw_value: str,
        injection_mode: str = "always",
        secret_type: str = "environment",
    ) -> dict:
        """Encode a project-scoped secret as base64 and store it on the Hub.

        The Hub API rejects plain UTF-8 strings on PUT /api/v1/secrets/* with HTTP 400.
        This method converts raw_value to ASCII base64 before sending the request.
        """
        enc_name = urllib.parse.quote(name, safe="")
        enc_pid = urllib.parse.quote(project_id, safe="")
        b64_val = base64.b64encode(raw_value.encode("utf-8")).decode("ascii")
        body = {"value": b64_val, "injectionMode": injection_mode, "type": secret_type}
        return self.request("PUT", f"/api/v1/secrets/{enc_name}?scope=project&scopeId={enc_pid}", body=body)[1]

    def list_agents(self, project_id: str) -> list[dict]:
        res = self.request("GET", f"/api/v1/projects/{project_id}/agents")[1]
        return _extract_list(res, "agents", "items")

    def get_agent(self, project_id: str, slug: str) -> dict | None:
        try:
            res = self.request("GET", f"/api/v1/projects/{project_id}/agents/{slug}")[1]
            if isinstance(res, dict) and isinstance(res.get("agent"), dict):
                return res["agent"]
            return res
        except HubAPIError as e:
            if e.status == 404:
                return None
            raise

    def ensure_agent_started(self, project_id: str, slug: str, create_body: dict | None = None, force_resume: bool = False, wait_sec: int = 360, poll_sec: float = 2.0) -> dict:
        agent = self.get_agent(project_id, slug)

        if not agent and create_body:
            payload = dict(create_body)
            payload.setdefault("projectId", project_id)
            try:
                self.request("POST", f"/api/v1/projects/{project_id}/agents", body=payload, timeout=180)
            except _TRANSIENT_CONN_ERRORS:
                pass
            except HubAPIError as e:
                if e.status == 502:
                    cur = self.get_agent(project_id, slug)
                    if not cur or cur.get("phase") == "error":
                        raise
                    if cur.get("phase") == "running":
                        return cur
                elif e.status not in (503, 504):
                    raise
        elif agent and agent.get("phase") != "running":
            body = {"forceResume": force_resume} if force_resume else {}
            try:
                self.request("POST", f"/api/v1/projects/{project_id}/agents/{slug}/start", body=body, timeout=180)
            except _TRANSIENT_CONN_ERRORS:
                pass
            except HubAPIError as e:
                if e.status == 502:
                    cur = self.get_agent(project_id, slug)
                    if not cur or cur.get("phase") == "error":
                        raise
                    if cur.get("phase") == "running":
                        return cur
                elif e.status not in (503, 504):
                    raise

        start = time.time()
        while time.time() - start < wait_sec:
            agent = self.get_agent(project_id, slug)
            if agent:
                phase = agent.get("phase")
                if phase == "running":
                    return agent
                if phase == "error":
                    raise HubAPIError(500, "GET", f"/agents/{slug}", f"Agent in error phase: {agent}")
            time.sleep(poll_sec)
        raise RuntimeError("Timeout waiting for agent to start")

    def exec_agent(self, project_id: str, slug: str, command: list[str], timeout: int = 10) -> tuple[int, str]:
        body = {"command": command, "timeout": timeout}
        res = self.request("POST", f"/api/v1/projects/{project_id}/agents/{slug}/exec", body=body)[1]
        return int(res.get("exitCode", 0)), str(res.get("output", ""))

    def list_shared_dir_files(self, project_id: str, dir_name: str) -> list[dict]:
        res = self.request("GET", f"/api/v1/projects/{project_id}/shared-dirs/{dir_name}/files")[1]
        return _extract_list(res, "files", "items")

    def sync_shared_dir(self, project_id: str, dir_name: str, src_dir: str, apply: bool = False) -> dict:
        remote_files = self.list_shared_dir_files(project_id, dir_name)
        remote_dict = {}
        for f in remote_files:
            rel_key = f.get("path") or f.get("name")
            if rel_key:
                remote_dict[rel_key] = f

        local_files = {}
        src_path = Path(src_dir)
        for p in src_path.rglob("*"):
            if p.is_file():
                rel_path = p.relative_to(src_path).as_posix()
                local_files[rel_path] = p

        uploaded = []
        deleted = []
        unchanged = []

        for rel_path, path in local_files.items():
            content = path.read_text()
            remote_f = remote_dict.get(rel_path)
            quoted = urllib.parse.quote(rel_path, safe="/")

            needs_upload = True
            if remote_f:
                remote_content = remote_f.get("content")
                if remote_content is None:
                    try:
                        fetched = self.request("GET", f"/api/v1/projects/{project_id}/shared-dirs/{dir_name}/files/{quoted}?format=json")[1]
                        remote_content = fetched.get("content") if isinstance(fetched, dict) else fetched
                    except Exception:
                        pass

                if remote_content == content:
                    needs_upload = False

            if needs_upload:
                uploaded.append(rel_path)
                if apply:
                    self.request("PUT", f"/api/v1/projects/{project_id}/shared-dirs/{dir_name}/files/{quoted}", body={"content": content})
            else:
                unchanged.append(rel_path)

        for rel_path in remote_dict.keys():
            if rel_path not in local_files:
                deleted.append(rel_path)
                if apply:
                    quoted = urllib.parse.quote(rel_path, safe="/")
                    self.request("DELETE", f"/api/v1/projects/{project_id}/shared-dirs/{dir_name}/files/{quoted}")

        return {
            "uploaded": sorted(uploaded),
            "deleted": sorted(deleted),
            "unchanged": sorted(unchanged),
        }
