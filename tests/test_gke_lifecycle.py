"""Tests for GKE factory lifecycle orchestration (`gke-up`, `extract_pod_and_transcript_metrics`, `gke-down`)."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import urllib.parse

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import scion_factory  # noqa: E402
from test_hub_client import fake_hub  # noqa: E402
from test_provision_factory import _single_project_delta_gke_spec  # noqa: E402


def test_gke_up_chains_setup_compile_and_provision_with_phase_timers(fake_hub, tmp_path, monkeypatch):
    import gke_lifecycle

    spec = _single_project_delta_gke_spec()
    spec_path = tmp_path / "bananacast.factory.yaml"
    scion_factory.save_spec(spec, spec_path)

    env_file = tmp_path / "factory.env"
    env_file.write_text(
        "# Hillclimb env\n"
        "HUB_PROJECT=scion-gke-hc1-a1b2c3\n"
        "PO_EMAIL=po@example.com\n"
        "SCION_HUB_TOKEN=fake-pat\n"
        "HUB_TRANSPORT_TOKEN=fake-iap-oidc\n"
        "AGENT_START_TIMEOUT_SEC=5\n"
        "AGENT_START_POLL_SEC=0.05\n"
    )

    bin_dir = tmp_path / "fake_bin"
    bin_dir.mkdir()
    klog = tmp_path / "kubectl.log"
    for b in ("gcloud", "make", "git"):
        p = bin_dir / b
        p.write_text("#!/usr/bin/env bash\nexit 0\n")
        p.chmod(0o755)
    kp = bin_dir / "kubectl"
    kp.write_text(f'#!/usr/bin/env bash\necho "$@" >> "{klog}"\ncat >> "{klog}"\necho "" >> "{klog}"\nexit 0\n')
    kp.chmod(0o755)

    seen_setup_cmds: list[list[str]] = []
    real_run = subprocess.run

    def intercept_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and any(str(part).endswith("setup-gke-hub.sh") for part in cmd):
            seen_setup_cmds.append([str(c) for c in cmd])
            if "--approve-apply" in cmd and "--write-env" in cmd:
                we_idx = cmd.index("--write-env") + 1
                we_path = Path(cmd[we_idx])
                existing = we_path.read_text() if we_path.exists() else ""
                we_path.write_text(
                    existing
                    + f"HUB_URL={fake_hub.url}\n"
                    + "SANDBOX_PROJECT=scion-gke-hc1-a1b2c3\n"
                    + "FIRESTORE_PROJECT=scion-gke-hc1-a1b2c3\n"
                    + "HUB_K8S_NAMESPACE=tfha-prod\n"
                    + "HUB_IAP_AUDIENCE=123-abc.apps.googleusercontent.com\n"
                    + "RUNTIME_PROFILE=remote\n"
                )
                we_path.chmod(0o600)
            return subprocess.CompletedProcess(cmd, 0, stdout="setup ok\n", stderr="")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("GITHUB_TOKEN", "gh_fake_pat_for_test")
    monkeypatch.setattr(subprocess, "run", intercept_run)

    # 1. Dry-run: passes --dry-run, compiles default kit_dir (or passed kit_dir), does not create project on Hub
    dry_kit = tmp_path / "kit-dry"
    res_dry = gke_lifecycle.gke_up(
        spec_path,
        env_file=env_file,
        kit_dir=dry_kit,
        create_project=True,
        scion_repo=tmp_path / "scion-src",
        apply=False,
        send_go=False,
    )
    assert res_dry["ok"] is True, res_dry
    assert res_dry["apply"] is False
    assert "--create-project" in seen_setup_cmds[0]
    assert "--dry-run" in seen_setup_cmds[0]
    assert "--approve-apply" not in seen_setup_cmds[0]
    assert seen_setup_cmds[0][seen_setup_cmds[0].index("--harnesses") + 1] == "antigravity"
    assert seen_setup_cmds[0][seen_setup_cmds[0].index("--write-env") + 1] == str(env_file.resolve())
    assert (dry_kit / "factory.yaml").is_file()
    assert "bananacast-hc1" not in fake_hub.projects_by_slug
    for key in ("setup_gke_sec", "compile_sec", "provision_sec", "total_sec"):
        assert isinstance(res_dry[key], (int, float)) and res_dry[key] >= 0

    # 2. Apply via scion_factory.main(["gke-up", ..., "--json"]): writes env, compiles kit, provisions Hub + sends GO
    apply_kit = tmp_path / "kit-apply"
    capsys_out: list[str] = []
    orig_stdout = sys.stdout
    from io import StringIO

    buf = StringIO()
    sys.stdout = buf
    try:
        rc = scion_factory.main(
            [
                "gke-up",
                str(spec_path),
                "--env",
                str(env_file),
                "--kit",
                str(apply_kit),
                "--create-project",
                "--apply",
                "--go",
                "--json",
            ]
        )
    finally:
        sys.stdout = orig_stdout
    capsys_out.append(buf.getvalue())

    assert rc == 0, capsys_out[0]
    out = json.loads(capsys_out[0])
    assert out["ok"] is True, out
    assert "--approve-apply" in seen_setup_cmds[1]
    assert "--create-project" in seen_setup_cmds[1]
    assert "bananacast-hc1" in fake_hub.projects_by_slug
    build_pid = fake_hub.projects_by_slug["bananacast-hc1"]["id"]
    assert any(
        r["method"] == "POST" and r["path"] == f"/api/v1/projects/{build_pid}/agents/lead/message"
        for r in fake_hub.requests
    )
    for key in ("setup_gke_sec", "compile_sec", "provision_sec", "total_sec"):
        assert isinstance(out[key], (int, float)) and out[key] >= 0


def test_extract_pod_and_transcript_metrics_counts_errors_and_tokens_accurately(tmp_path):
    import gke_lifecycle

    backup_dir = tmp_path / "scion-gke-hc1-a1b2c3-20261110T210000Z"
    lead_pod = backup_dir / "pods" / "lead"
    backend_pod = backup_dir / "pods" / "backend"
    lead_logs = lead_pod / ".gemini" / "jetski" / "brain" / "conv-1" / ".system_generated" / "logs"
    backend_logs = backend_pod / ".gemini" / "jetski" / "brain" / "conv-2" / ".system_generated" / "logs"
    lead_logs.mkdir(parents=True)
    backend_logs.mkdir(parents=True)

    (lead_pod / "agent.log").write_text(
        "2026-11-10T20:00:01Z INFO Starting sciontool init\n"
        "2026-11-10T20:00:02Z ERROR Failed to start telemetry: cloud telemetry enabled but destination is not configured\n"
        "2026-11-10T20:00:10Z ERROR Token refresh failed: status 401: Invalid IAP credentials: empty token\n"
        "2026-11-10T20:00:20Z ERROR Token refresh failed: status 401: Invalid IAP credentials: empty token\n"
        "2026-11-10T20:00:21Z WARN Heartbeat failed: hub status writes paused\n"
        "2026-11-10T20:00:31Z WARN Heartbeat failed: hub status writes paused\n"
        "2026-11-10T20:00:41Z WARN Heartbeat failed: hub status writes paused\n"
    )
    (backend_pod / "agent.log").write_text(
        "2026-11-10T20:01:00Z ERROR Failed to start telemetry: cloud telemetry enabled but destination is not configured\n"
        "2026-11-10T20:01:05Z ERROR Token refresh failed: status 401: Invalid IAP credentials: empty token\n"
    )

    lead_steps = [
        # Turn 1: Active DELTA turn dispatching backend
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "content": "DELTA: ACTIONABLE: #1 'Add endpoint' [factory:bananacast-hc1,status:ready]",
        },
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "input_tokens": 1000,
            "cache_read_tokens": 400,
            "output_tokens": 150,
            "tool_calls": [{"name": "run_command", "args": {"CommandLine": "scion start backend -t backend"}}],
        },
        {
            "step_index": 2,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "input_tokens": 1200,
            "cache_read_tokens": 900,
            "output_tokens": 100,
            "tool_calls": [{"name": "run_command", "args": {"CommandLine": 'sciontool status task_completed "Dispatched #1"'}}],
        },
        # Turn 2: Idle bare state-change turn
        {
            "step_index": 3,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "content": "[system] state-change: agent backend entered COMPLETED",
        },
        {
            "step_index": 4,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "input_tokens": 500,
            "cache_read_tokens": 450,
            "output_tokens": 30,
            "tool_calls": [
                {"name": "run_command", "args": {"CommandLine": 'sciontool status task_completed "ignored bare state-change"'}}
            ],
        },
        # Turn 3: Idle cron heartbeat poll turn
        {
            "step_index": 5,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "content": "[HEARTBEAT] Inspect open GitHub issues in example-org/bananacast",
        },
        {
            "step_index": 6,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "input_tokens": 600,
            "cache_read_tokens": 550,
            "output_tokens": 40,
            "tool_calls": [{"name": "run_command", "args": {"CommandLine": "gh issue list --state open"}}],
        },
    ]
    (lead_logs / "transcript_full.jsonl").write_text("\n".join(json.dumps(s) for s in lead_steps) + "\n")

    backend_steps = [
        {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "Implement #1 on factory/issue-1"},
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "input_tokens": 2000,
            "cache_read_tokens": 0,
            "output_tokens": 300,
        },
        {
            "step_index": 2,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "input_tokens": 2500,
            "cache_read_tokens": 1800,
            "output_tokens": 250,
        },
    ]
    (backend_logs / "transcript_full.jsonl").write_text("\n".join(json.dumps(s) for s in backend_steps) + "\n")

    metrics = gke_lifecycle.extract_pod_and_transcript_metrics(backup_dir)
    assert metrics["iap_401_errors"] == 3
    assert metrics["heartbeat_errors"] == 3
    assert metrics["telemetry_boot_errors"] == 2

    assert metrics["per_agent"]["lead"] == {
        "steps": 7,
        "user_inputs": 3,
        "input_tokens": 3300,
        "cache_read_tokens": 2300,
        "output_tokens": 320,
    }
    assert metrics["per_agent"]["backend"] == {
        "steps": 3,
        "user_inputs": 1,
        "input_tokens": 4500,
        "cache_read_tokens": 1800,
        "output_tokens": 550,
    }

    split = metrics["lead_active_vs_idle"]
    assert split["active"]["turns"] == 1
    assert split["active"]["input_tokens"] == 2200
    assert split["active"]["cache_read_tokens"] == 1300
    assert split["active"]["output_tokens"] == 250

    assert split["idle"]["turns"] == 2
    assert split["idle"]["input_tokens"] == 1100
    assert split["idle"]["cache_read_tokens"] == 1000
    assert split["idle"]["output_tokens"] == 70


def test_gke_down_refuses_non_ephemeral_project_without_confirm_and_cleans_github_when_hub_unreachable(tmp_path):
    import gke_lifecycle

    spec = _single_project_delta_gke_spec()
    spec_path = tmp_path / "bananacast.factory.yaml"
    scion_factory.save_spec(spec, spec_path)

    # 1. Non-ephemeral project without matching confirm_project MUST raise ValueError
    prod_env = tmp_path / "prod.env"
    prod_env.write_text("HUB_PROJECT=demo-long-lived-hub\nHUB_URL=http://127.0.0.1:1\n")
    with pytest.raises(ValueError, match="scion-gke-hc"):
        gke_lifecycle.gke_down(
            spec_path,
            env_file=prod_env,
            backup_root=tmp_path / "backups",
            delete_project=True,
            apply=True,
        )
    with pytest.raises(ValueError):
        gke_lifecycle.gke_down(
            spec_path,
            env_file=prod_env,
            backup_root=tmp_path / "backups",
            delete_project=True,
            confirm_project="wrong-project",
            apply=True,
        )

    # 2. Protected branches MUST raise ValueError
    hc_env = tmp_path / "hc1.env"
    secret_gh = "fake_gh_secret_never_persist_98765"
    secret_hub = "fake_hub_secret_never_persist_54321"
    hc_env.write_text(
        f"HUB_PROJECT=scion-gke-hc1-a1b2c3\n"
        f"SANDBOX_PROJECT=scion-gke-hc1-a1b2c3\n"
        f"HUB_URL=http://127.0.0.1:1\n"
        f"HUB_K8S_NAMESPACE=tfha-prod\n"
        f"GITHUB_TOKEN={secret_gh}\n"
        f"SCION_HUB_TOKEN={secret_hub}\n"
    )
    for protected in ("main", "master", "factory/main", "factory/eval"):
        with pytest.raises(ValueError, match=protected):
            gke_lifecycle.gke_down(
                spec_path,
                env_file=hc_env,
                backup_root=tmp_path / "backups",
                cleanup_branch=protected,
                apply=True,
            )

    # 3. Ephemeral project with unreachable Hub + unreachable K8s still archives scorecard (0700/0600, zero tokens),
    #    closes GitHub test issues (adding hillclimb-archive), deletes test branch, and deletes GCP project.
    gh_state: dict[str, object] = {
        "labels_created": [],
        "patched_issues": {},
        "deleted_refs": [],
    }

    class _FakeGitHubHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _json(self, code: int, data: object):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode("utf-8"))

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/repos/example-org/bananacast/issues":
                qs = urllib.parse.parse_qs(parsed.query)
                assert qs.get("labels") == ["factory:bananacast-hc1"]
                self._json(
                    200,
                    [
                        {
                            "number": 21,
                            "state": "open",
                            "title": "HC1 Slice 1",
                            "labels": [{"name": "factory:bananacast-hc1"}, {"name": "status:verified"}],
                        },
                        {
                            "number": 22,
                            "state": "open",
                            "title": "HC1 Slice 2",
                            "labels": [{"name": "factory:bananacast-hc1"}, {"name": "status:in-progress"}],
                        },
                        {
                            "number": 99,
                            "state": "open",
                            "title": "Open PR ignored",
                            "pull_request": {"url": "https://api.github.com/pulls/99"},
                            "labels": [{"name": "factory:bananacast-hc1"}],
                        },
                    ],
                )
                return
            self._json(404, {"message": "not found"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            if self.path == "/repos/example-org/bananacast/labels":
                gh_state["labels_created"].append(body.get("name"))  # type: ignore[union-attr]
                self._json(201, body)
                return
            self._json(200, {})

        def do_PATCH(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            if self.path.startswith("/repos/example-org/bananacast/issues/"):
                num = int(self.path.rsplit("/", 1)[-1])
                gh_state["patched_issues"][num] = body  # type: ignore[index]
                self._json(200, {"number": num, **body})
                return
            self._json(404, {})

        def do_DELETE(self):
            gh_state["deleted_refs"].append(self.path)  # type: ignore[union-attr]
            self.send_response(204)
            self.end_headers()

    gh_srv = HTTPServer(("127.0.0.1", 0), _FakeGitHubHandler)
    th = threading.Thread(target=gh_srv.serve_forever, daemon=True)
    th.start()

    bin_dir = tmp_path / "fake_bin"
    bin_dir.mkdir()
    gcloud_log = tmp_path / "gcloud.log"
    (bin_dir / "gcloud").write_text(f'#!/usr/bin/env bash\necho "$@" >> "{gcloud_log}"\nexit 0\n')
    (bin_dir / "gcloud").chmod(0o755)
    (bin_dir / "kubectl").write_text('#!/usr/bin/env bash\necho "Unable to connect to the server" >&2\nexit 1\n')
    (bin_dir / "kubectl").chmod(0o755)

    backup_root = tmp_path / "scion-backups"
    cli_env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "GITHUB_API_URL": f"http://127.0.0.1:{gh_srv.server_port}",
    }

    try:
        r_down = subprocess.run(
            [
                sys.executable,
                "scripts/scion_factory.py",
                "gke-down",
                str(spec_path),
                "--env",
                str(hc_env),
                "--backup-root",
                str(backup_root),
                "--cleanup-issue-label",
                "factory:bananacast-hc1",
                "--cleanup-branch",
                "factory/gke-hc1",
                "--delete-project",
                "--apply",
                "--json",
            ],
            cwd=ROOT,
            env=cli_env,
            capture_output=True,
            text=True,
        )
    finally:
        gh_srv.shutdown()
        gh_srv.server_close()

    assert r_down.returncode == 0, r_down.stderr + "\n" + r_down.stdout
    out = json.loads(r_down.stdout)
    assert out["ok"] is True, out
    assert out["warnings"], "Expected unreachable Hub/K8s warnings to be recorded cleanly"

    bdir = Path(out["backup_dir"])
    assert bdir.is_dir()
    assert bdir.name.startswith("scion-gke-hc1-a1b2c3-")
    assert (bdir.stat().st_mode & 0o777) == 0o700

    scorecard_file = bdir / "scorecard.json"
    assert scorecard_file.is_file()
    assert (scorecard_file.stat().st_mode & 0o777) == 0o600

    # Zero tokens written anywhere under backup_dir (including scorecard.json)
    for fpath in bdir.rglob("*"):
        if fpath.is_file():
            txt = fpath.read_text()
            assert secret_gh not in txt
            assert secret_hub not in txt

    # GitHub cleanup: issues #21 and #22 closed with hillclimb-archive, PR #99 untouched, branch deleted
    patched = gh_state["patched_issues"]
    assert set(patched.keys()) == {21, 22}  # type: ignore[union-attr]
    for num in (21, 22):
        assert patched[num]["state"] == "closed"  # type: ignore[index]
        assert "hillclimb-archive" in patched[num]["labels"]  # type: ignore[index]
    assert "/repos/example-org/bananacast/git/refs/heads/factory/gke-hc1" in gh_state["deleted_refs"]  # type: ignore[operator]

    # GCP project deleted via gcloud projects delete <hub_project> --quiet
    glog_lines = gcloud_log.read_text().splitlines()
    assert any("projects delete scion-gke-hc1-a1b2c3 --quiet" in line for line in glog_lines), glog_lines

    # 4. Explicit confirm_project allows intentional non-ephemeral deletion, while DRY_RUN=1 suppresses mutations
    r_dry = subprocess.run(
        [
            sys.executable,
            "scripts/scion_factory.py",
            "gke-down",
            str(spec_path),
            "--env",
            str(prod_env),
            "--backup-root",
            str(backup_root),
            "--delete-project",
            "--confirm-project",
            "demo-long-lived-hub",
            "--apply",
            "--json",
        ],
        cwd=ROOT,
        env={**cli_env, "DRY_RUN": "1"},
        capture_output=True,
        text=True,
    )
    assert r_dry.returncode == 0, r_dry.stderr
    assert json.loads(r_dry.stdout)["apply"] is False
    assert not any("demo-long-lived-hub" in line for line in gcloud_log.read_text().splitlines())

