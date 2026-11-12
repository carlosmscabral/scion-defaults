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
    import inspect

    assert "import base64" not in inspect.getsource(make_hc.pre_start_iap_bytes)

    scion_home = tmp_path / "scion"
    hc_src = scion_home / "harness-configs" / "antigravity"
    hc_src.mkdir(parents=True)
    (hc_src / "config.yaml").write_text('harness: antigravity\ncommand:\n  base: ["agy", "chat"]\n')
    dst = Path(make_hc.build(str(scion_home), "antigravity", apply=True))
    hook_dst = dst / "home" / ".scion" / "hooks" / "pre-start.d" / "10-direct-iap-token"
    assert hook_dst.is_file() and (hook_dst.stat().st_mode & 0o777) == 0o755
    pre_stop_dst = dst / "home" / ".scion" / "hooks" / "pre-stop.d" / "10-persist-telemetry"
    assert pre_stop_dst.is_file() and (pre_stop_dst.stat().st_mode & 0o777) == 0o755

    # via_hub embeds both pre-start and pre-stop hooks into the remote script
    fake_runner = tmp_path / "fake-run-on-hub.sh"
    fake_runner.write_text('#!/bin/sh\nd=$(mktemp -d)\ncp "$1" "$d/remote.py"\nshift\nexec python3 "$d/remote.py" "$@"\n')
    fake_runner.chmod(0o755)
    hub_scion_home = tmp_path / "scion-via-hub"
    hub_hc_src = hub_scion_home / "harness-configs" / "antigravity"
    hub_hc_src.mkdir(parents=True)
    (hub_hc_src / "config.yaml").write_text('harness: antigravity\ncommand:\n  base: ["agy", "chat"]\n')
    r_vh = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "make-factory-harness-configs.py"),
            "--via-hub",
            "--scion-home",
            str(hub_scion_home),
            "--harness",
            "antigravity",
            "--apply",
            "--no-sync",
        ],
        env={**os.environ, "RUN_ON_HUB": str(fake_runner)},
        capture_output=True,
        text=True,
    )
    assert r_vh.returncode == 0, r_vh.stderr
    vh_pre_stop = hub_scion_home / "harness-configs" / "antigravity-factory" / "home" / ".scion" / "hooks" / "pre-stop.d" / "10-persist-telemetry"
    assert vh_pre_stop.is_file() and (vh_pre_stop.stat().st_mode & 0o777) == 0o755

    # via_api uploads both hooks with manifest mode "0755"
    client = HubClient(base_url=fake_hub.url, token="tok-admin")
    make_hc.via_api(client, ["antigravity"], apply=True)
    fin = next(r for r in fake_hub.requests if r["method"] == "POST" and r["path"].endswith("/finalize"))
    files_by_path = {f["path"]: f for f in json.loads(fin["body"].decode())["manifest"]["files"]}
    assert files_by_path["home/.scion/hooks/pre-start.d/10-direct-iap-token"]["mode"] == "0755"
    assert files_by_path["home/.scion/hooks/pre-stop.d/10-persist-telemetry"]["mode"] == "0755"


def test_pre_stop_persist_telemetry_hook_and_factory_cd_mirror_logs_and_transcripts(tmp_path):
    pre_stop_hook = ROOT / "config" / "harness" / "home" / ".scion" / "hooks" / "pre-stop.d" / "10-persist-telemetry"
    assert pre_stop_hook.is_file()
    assert (pre_stop_hook.stat().st_mode & 0o777) == 0o755

    # 1. Direct pre-stop hook invocation
    home1 = tmp_path / "home1"
    scratch1 = tmp_path / "scratch1"
    scratch1.mkdir()
    (home1 / ".scion").mkdir(parents=True)
    (home1 / ".scion" / "agent.log").write_text("2026-11-10T22:00:00Z INFO agent started\n")
    t1 = home1 / ".gemini" / "jetski" / "brain" / "conv-abc" / ".system_generated" / "logs" / "transcript_full.jsonl"
    t1.parent.mkdir(parents=True)
    t1.write_text('{"type":"USER_INPUT","content":"hello"}\n')

    r_hook = subprocess.run(
        ["sh", str(pre_stop_hook)],
        env={
            **os.environ,
            "HOME": str(home1),
            "SCION_SCRATCHPAD_DIR": str(scratch1),
            "SCION_AGENT_SLUG": "backend",
            "HOSTNAME": "scion-backend-pod-1",
        },
        capture_output=True,
        text=True,
    )
    assert r_hook.returncode == 0, r_hook.stderr
    mirrored_root1 = scratch1 / ".telemetry" / "backend" / "scion-backend-pod-1"
    assert (mirrored_root1 / "agent.log").read_text() == "2026-11-10T22:00:00Z INFO agent started\n"
    assert (
        mirrored_root1 / ".gemini" / "jetski" / "brain" / "conv-abc" / ".system_generated" / "logs" / "transcript_full.jsonl"
    ).read_text() == '{"type":"USER_INPUT","content":"hello"}\n'

    # 2. factory-cd.sh periodic + EXIT trap sync when scratchpad exists
    factory_cd = ROOT / "config" / "harness" / "home" / ".local" / "bin" / "factory-cd.sh"
    home2 = tmp_path / "home2"
    home2.mkdir()
    scratch2 = tmp_path / "scratch2"
    scratch2.mkdir()
    repo2 = tmp_path / "repo2"
    repo2.mkdir()

    r_cd = subprocess.run(
        [
            "sh",
            str(factory_cd),
            "python3",
            "-c",
            (
                "import os, pathlib; "
                "h = pathlib.Path(os.environ['HOME']); "
                "(h / '.scion').mkdir(parents=True, exist_ok=True); "
                "(h / '.scion' / 'agent.log').write_text('logged before pod deletion\\n'); "
                "tp = h / '.gemini' / 'jetski' / 'brain' / 'conv-xyz' / '.system_generated' / 'logs' / 'transcript_full.jsonl'; "
                "tp.parent.mkdir(parents=True, exist_ok=True); "
                "tp.write_text('{\"type\":\"PLANNER_RESPONSE\",\"output_tokens\":42}\\n')"
            ),
        ],
        cwd=repo2,
        env={
            **os.environ,
            "HOME": str(home2),
            "SCION_SCRATCHPAD_DIR": str(scratch2),
            "SCION_AGENT_NAME": "lead",
            "HOSTNAME": "scion-lead-pod-0",
            "FACTORY_CD_TELEMETRY_SYNC_SEC": "1",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert r_cd.returncode == 0, r_cd.stderr
    mirrored_root2 = scratch2 / ".telemetry" / "lead" / "scion-lead-pod-0"
    assert (mirrored_root2 / "agent.log").read_text() == "logged before pod deletion\n"
    assert (
        mirrored_root2 / ".gemini" / "jetski" / "brain" / "conv-xyz" / ".system_generated" / "logs" / "transcript_full.jsonl"
    ).read_text() == '{"type":"PLANNER_RESPONSE","output_tokens":42}\n'

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
                tok = state.get("token", "tok-direct-iap-ok")
                self.wfile.write(json.dumps({"access_token": tok, "expires_in": 3599}).encode())
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


def test_persist_telemetry_hook_mirrors_agent_log_from_home_root(tmp_path):
    # Current Scion writes the agent log to ~/agent.log, not ~/.scion/agent.log. In hillclimb Run 1
    # the mirror copied transcripts but no agent log, so the scorecard's error counts had no input.
    hook = ROOT / "config" / "harness" / "home" / ".scion" / "hooks" / "pre-stop.d" / "10-persist-telemetry"
    home = tmp_path / "home"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (home / ".scion").mkdir(parents=True)
    (home / "agent.log").write_text("current location\n")
    (home / ".scion" / "agent.log").write_text("stale legacy location\n")
    r = subprocess.run(
        ["sh", str(hook)],
        env={**os.environ, "HOME": str(home), "SCION_SCRATCHPAD_DIR": str(scratch),
             "SCION_AGENT_SLUG": "lead", "HOSTNAME": "bananacast-hc1--lead"},
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    assert (scratch / ".telemetry" / "lead" / "bananacast-hc1--lead" / "agent.log").read_text() == "current location\n"
    launcher = (ROOT / "config" / "harness" / "home" / ".local" / "bin" / "factory-cd.sh").read_text()
    assert '_cp_telem "${HOME}/agent.log" "${_dst_root}/agent.log"' in launcher
    assert "cat /home/scion/agent.log" in (ROOT / "scripts" / "gke_lifecycle.py").read_text()


def test_direct_iap_refresh_cadence_hits_gke_metadata_5m_window_without_redundant_rewrites(fake_metadata_server, tmp_path):
    launcher = (ROOT / "config" / "harness" / "home" / ".local" / "bin" / "factory-cd.sh").read_text()
    assert 'FACTORY_CD_IAP_REFRESH_SEC:-180' in launcher
    # gke-metadata-server caches Workload Identity access tokens until 300s before 60m expiry ([T+55m, T+60m)).
    # A 180s interval guarantees at least one poll inside [3300s, 3600s) regardless of initial phase offset.
    for offset in range(0, 180):
        ticks = [offset + 180 * k for k in range(1, 25)]
        assert any(3300 <= t < 3600 for t in ticks)

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
    assert subprocess.run(["sh", str(HOOK_PATH)], env=env, capture_output=True, text=True).returncode == 0
    t_path = home / ".scion" / "transport-token"
    i_path = home / ".scion" / "iap-access-token"
    ino_before = (t_path.stat().st_ino, i_path.stat().st_ino)

    # Polling while gke-metadata-server still returns the cached token skips atomic rename churn.
    assert subprocess.run(["sh", str(HOOK_PATH)], env=env, capture_output=True, text=True).returncode == 0
    assert (t_path.stat().st_ino, i_path.stat().st_ino) == ino_before

    # Once gke-metadata-server rotates the cached token inside [T+55m, T+60m), both files update atomically (0600).
    fake_metadata_server["token"] = "tok-rotated-at-57m"
    assert subprocess.run(["sh", str(HOOK_PATH)], env=env, capture_output=True, text=True).returncode == 0
    assert t_path.read_text() == "tok-rotated-at-57m"
    assert i_path.read_text() == "tok-rotated-at-57m"
    assert (t_path.stat().st_mode & 0o777) == 0o600

