import os
import json
import subprocess
from pathlib import Path
import pytest
import sys
import importlib.util

ROOT = Path(__file__).resolve().parent.parent

def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

sys.path.insert(0, str(ROOT / "scripts"))
import hub_client
from hub_client import HubClient

make_hc = _load("make_factory_harness_configs", ROOT / "scripts" / "make-factory-harness-configs.py")

HOOK_PATH = ROOT / "config/harness/home/.scion/hooks/pre-start.d/10-direct-iap-token"

def test_pre_start_direct_iap_hook_writes_both_transport_and_iap_tokens_0600(fake_metadata_server, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "SCION_TRANSPORT_MODE": "iap",
        "SCION_TRANSPORT_AUDIENCE": "369001918367-abc.apps.googleusercontent.com",
        "SCION_METADATA_URL": fake_metadata_server["url"],
        "SCION_IAP_RETRY_SLEEP_SEC": "0.01",
    }
    res = subprocess.run(["sh", str(HOOK_PATH)], env=env, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    for name in ("transport-token", "iap-access-token"):
        p = home / ".scion" / name
        assert p.read_text() == "tok-direct-iap-ok"
        assert (p.stat().st_mode & 0o777) == 0o600
    assert "userinfo.email" in fake_metadata_server["queries"][0] and "openid" in fake_metadata_server["queries"][0]


def test_pre_start_direct_iap_hook_retries_metadata_and_never_bricks_pod_on_unreachable(fake_metadata_server, tmp_path):
    # 1) Fails once (503), succeeds on retry -> writes both files
    fake_metadata_server["fail_first"] = 1
    home1 = tmp_path / "h1"
    home1.mkdir()
    r1 = subprocess.run(
        ["sh", str(HOOK_PATH)],
        env={**os.environ, "HOME": str(home1), "SCION_TRANSPORT_MODE": "iap",
             "SCION_TRANSPORT_AUDIENCE": "369001918367-abc.apps.googleusercontent.com",
             "SCION_METADATA_URL": fake_metadata_server["url"], "SCION_IAP_RETRY_SLEEP_SEC": "0.01"},
        capture_output=True, text=True,
    )
    assert r1.returncode == 0 and (home1 / ".scion" / "transport-token").read_text() == "tok-direct-iap-ok"

    # 2) Completely unreachable metadata -> exits 0 with warning so sciontool init does not fail pod startup
    home2 = tmp_path / "h2"
    home2.mkdir()
    r2 = subprocess.run(
        ["sh", str(HOOK_PATH)],
        env={**os.environ, "HOME": str(home2), "SCION_TRANSPORT_MODE": "iap",
             "SCION_TRANSPORT_AUDIENCE": "369001918367-abc.apps.googleusercontent.com",
             "SCION_METADATA_URL": "http://127.0.0.1:1", "SCION_METADATA_FALLBACKS": "0",
             "SCION_IAP_RETRY_SLEEP_SEC": "0.01"},
        capture_output=True, text=True,
    )
    assert r2.returncode == 0
    assert "warning" in r2.stderr.lower()


def test_make_factory_harness_configs_packages_pre_start_hook_local_and_via_api(fake_hub, tmp_path):
    scion_home = tmp_path / "scion"
    hc_src = scion_home / "harness-configs" / "antigravity"
    hc_src.mkdir(parents=True)
    (hc_src / "config.yaml").write_text('harness: antigravity\ncommand:\n  base: ["agy", "chat"]\n')
    dst = Path(make_hc.build(str(scion_home), "antigravity", apply=True))
    hook_dst = dst / "home" / ".scion" / "hooks" / "pre-start.d" / "10-direct-iap-token"
    assert hook_dst.is_file() and (hook_dst.stat().st_mode & 0o777) == 0o755
    # via_api uploads home/.scion/hooks/pre-start.d/10-direct-iap-token with manifest mode "0755"
    client = HubClient(base_url=fake_hub.url, token="tok-admin")
    make_hc.via_api(client, ["antigravity"], apply=True)
    fin = next(r for r in fake_hub.requests if r["method"] == "POST" and r["path"].endswith("/finalize"))
    files_by_path = {f["path"]: f for f in json.loads(fin["body"].decode())["manifest"]["files"]}
    assert files_by_path["home/.scion/hooks/pre-start.d/10-direct-iap-token"]["mode"] == "0755"

import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import socket

def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]

class FakeMetadataHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        state = self.server.state
        state["queries"].append(self.path)
        if state.get("fail_first") and not state.get("failed_once"):
            state["failed_once"] = True
            self.send_response(503)
            self.end_headers()
            return
        if self.path == "/computeMetadata/v1/instance/service-accounts/default/token?scopes=https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/userinfo.email,openid":
            if self.headers.get("Metadata-Flavor") == "Google":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"access_token": "tok-direct-iap-ok", "expires_in": 3599}).encode())
                return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        pass

@pytest.fixture
def fake_metadata_server():
    port = get_free_port()
    server = HTTPServer(("127.0.0.1", port), FakeMetadataHandler)
    server.state = {"queries": []}
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    server.state["url"] = f"http://127.0.0.1:{port}"
    yield server.state
    server.shutdown()
    server.server_close()
