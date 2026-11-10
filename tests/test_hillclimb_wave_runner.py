"""Tests for Task 6: `bananacast` Hillclimb Wave Harness (`run_hillclimb_wave.py`) & Preset."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from typing import Any
import urllib.parse

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import scion_factory  # noqa: E402


def test_bananacast_gke_preset_validates_all_roles_antigravity_gemini_4_argon_and_delta_mode(tmp_path: Path):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((ROOT / "config" / "factory.schema.json").read_text(encoding="utf-8"))

    preset_path = ROOT / "presets" / "bananacast-gke.factory.yaml"
    assert preset_path.is_file(), f"Missing preset {preset_path}"

    spec = scion_factory.load_spec(preset_path)
    jsonschema.validate(spec, schema)

    # Single-project topology (eval_slug omitted)
    assert scion_factory.has_eval_project(spec) is False
    assert "eval_slug" not in spec["project"]
    assert spec["project"]["slug"] == "bananacast"

    # VCS & Infrastructure & Stack exact contract (Ruling R1)
    assert spec["vcs"]["enabled"] is True
    assert spec["vcs"]["provider"] == "github"
    assert spec["vcs"]["repo"] == "duboc/bananacast"
    assert spec["vcs"]["build_branch"] == "factory/gke-hc"

    assert spec["infrastructure"]["deploy"] == "none"
    assert spec["infrastructure"]["runtime"] == "kubernetes"
    assert spec["infrastructure"]["runtime_profile"] == "remote"
    assert spec["infrastructure"]["cloud_stores"] == ["firestore", "gcs"]
    assert spec["infrastructure"]["budget_usd"] == 50

    assert spec["stack"]["primary_language"] == "python"
    assert spec["stack"]["framework"] == "flask"
    assert spec["stack"]["build_command"] == "python3 -m compileall -q src/backend"
    assert spec["stack"]["test_command"] == "cd src/backend && pytest -q"
    assert spec["stack"]["port"] == 8080

    # Delta heartbeat & dropped_defaults so normalize_team adds zero non-pinned roles
    team = spec["team"]
    assert team["heartbeat_mode"] == "delta"
    dropped = {d["role"]: d["reason"] for d in team.get("dropped_defaults") or []}
    assert {"researcher", "doc-writer", "investigator"}.issubset(dropped.keys())
    for role_name in ("researcher", "doc-writer", "investigator"):
        assert len(dropped[role_name].strip()) >= 10

    expected_roles = ["lead", "backend", "frontend", "reviewer", "security-auditor", "evaluator"]
    raw_roles = team["build_roles"]
    assert [r["role"] for r in raw_roles] == expected_roles
    for entry in raw_roles:
        assert entry["harness"] == "antigravity"
        assert entry["model"] == "Gemini 4 Argon"

    norm_spec, notes = scion_factory.normalize_team(spec)
    assert notes == []
    norm_roles = norm_spec["team"]["build_roles"]
    assert [r["role"] for r in norm_roles] == expected_roles
    for entry in norm_roles:
        assert entry["harness"] == "antigravity"
        assert entry["model"] == "Gemini 4 Argon"

    readiness = scion_factory.evaluate_spec_readiness(spec)
    assert readiness["ready"] is True, readiness["missing_or_weak"]
    assert readiness["missing_or_weak"] == []
    assert readiness["warnings"] == []

    # Compiled kit factory.yaml must also pass jsonschema.validate (Ruling R1)
    kit_dir = tmp_path / "compiled-bananacast-kit"
    compile_res = scion_factory.compile_factory_boilerplate(spec, kit_dir)
    assert compile_res["files_written"] > 0
    compiled_spec = scion_factory.load_spec(kit_dir / "factory.yaml")
    jsonschema.validate(compiled_spec, schema)

    # Every compiled role template must configure antigravity-factory, Gemini 4 Argon, and GOOGLE_CLOUD_LOCATION=global (Ruling R5)
    for role_name in expected_roles:
        agent_yaml = (kit_dir / "templates" / role_name / "scion-agent.yaml").read_text(encoding="utf-8")
        assert 'GOOGLE_CLOUD_LOCATION: "global"' in agent_yaml, role_name
        assert "default_harness_config: antigravity-factory" in agent_yaml, role_name
        assert 'model: "Gemini 4 Argon"' in agent_yaml, role_name


class _MockGitHubAndHubServer:
    """Deterministic local HTTP double for GitHub REST API and Scion Hub API."""

    def __init__(self) -> None:
        self.branch_shas: dict[str, str] = {
            "main": "1111111111111111111111111111111111111111",
            "factory/main": "2222222222222222222222222222222222222222",
        }
        self.created_refs: list[dict[str, Any]] = []
        self.labels: set[str] = set()
        self.issues: dict[int, dict[str, Any]] = {}
        self.comments: dict[int, list[dict[str, Any]]] = {}
        self.next_issue_num = 101
        self.next_comment_id = 5001
        self.issue_poll_count = 0
        self.po_issue_num: int | None = None
        self.po_reply_posted = False
        self.po_polls_after_seed = 0
        self.po_polls_after_reply = 0
        self.lead_phase = "running"
        self.mutate_main_on_teardown = False

        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: object) -> None:
                pass

            def _send(self, code: int, payload: Any) -> None:
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(raw)

            def _body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0") or 0)
                return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

            def do_GET(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                path = urllib.parse.unquote(parsed.path)

                # GitHub git ref lookup
                m_ref = re.match(r"^/repos/duboc/bananacast/git/ref/heads/(.+)$", path)
                if m_ref:
                    br = m_ref.group(1)
                    if br in parent.branch_shas:
                        self._send(200, {"ref": f"refs/heads/{br}", "object": {"sha": parent.branch_shas[br]}})
                    else:
                        self._send(404, {"message": "Not Found"})
                    return

                # GitHub issues list
                if path == "/repos/duboc/bananacast/issues":
                    qs = urllib.parse.parse_qs(parsed.query)
                    want_label = (qs.get("labels") or [""])[0]
                    parent.issue_poll_count += 1

                    # Advance Wave issues to status:verified on poll >= 2
                    if parent.issue_poll_count >= 2:
                        for num, iss in parent.issues.items():
                            if num != parent.po_issue_num:
                                lbls = [l["name"] for l in iss["labels"] if not l["name"].startswith("status:")]
                                lbls.append("status:verified")
                                iss["labels"] = [{"name": n} for n in lbls]

                    # Advance PO drill states deterministically
                    if parent.po_issue_num is not None and not parent.po_reply_posted:
                        parent.po_polls_after_seed += 1
                        if parent.po_polls_after_seed >= 1:
                            parent.lead_phase = "stopped"
                    elif parent.po_issue_num is not None and parent.po_reply_posted:
                        parent.po_polls_after_reply += 1
                        if parent.po_polls_after_reply == 1:
                            parent.lead_phase = "running"
                        elif parent.po_polls_after_reply >= 2:
                            po_iss = parent.issues[parent.po_issue_num]
                            lbls = [l["name"] for l in po_iss["labels"] if not l["name"].startswith("status:")]
                            lbls.append("status:verified")
                            po_iss["labels"] = [{"name": n} for n in lbls]
                            parent.lead_phase = "stopped"

                    items = []
                    for iss in sorted(parent.issues.values(), key=lambda x: x["number"]):
                        if iss.get("state") != "open":
                            continue
                        names = {l["name"] for l in iss.get("labels") or []}
                        if not want_label or want_label in names:
                            items.append(iss)
                    self._send(200, items)
                    return

                # Hub projects & agents
                if path == "/api/v1/projects":
                    self._send(
                        200,
                        {
                            "projects": [
                                {"id": "pid-hc", "slug": "bananacast-hc1"},
                                {"id": "pid-hc3", "slug": "bananacast-hc3"},
                            ]
                        },
                    )
                    return

                if re.match(r"^/api/v1/projects/[^/]+/agents$", path):
                    self._send(
                        200,
                        {
                            "agents": [
                                {
                                    "id": "ag-lead",
                                    "slug": "lead",
                                    "name": "lead",
                                    "template": "lead",
                                    "phase": parent.lead_phase,
                                    "activity": "idle",
                                }
                            ]
                        },
                    )
                    return

                self._send(404, {"message": f"Unhandled GET {path}"})

            def do_POST(self) -> None:
                path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
                body = self._body()

                if path == "/repos/duboc/bananacast/git/refs":
                    ref = str(body.get("ref") or "")
                    sha = str(body.get("sha") or "")
                    br = ref.removeprefix("refs/heads/")
                    parent.branch_shas[br] = sha
                    parent.created_refs.append({"ref": ref, "sha": sha})
                    self._send(201, {"ref": ref, "object": {"sha": sha}})
                    return

                if path == "/repos/duboc/bananacast/labels":
                    name = str(body.get("name") or "")
                    parent.labels.add(name)
                    self._send(201, {"name": name})
                    return

                if path == "/repos/duboc/bananacast/issues":
                    num = parent.next_issue_num
                    parent.next_issue_num += 1
                    lbl_names = [str(x) for x in (body.get("labels") or [])]
                    iss = {
                        "number": num,
                        "state": "open",
                        "title": body.get("title", ""),
                        "body": body.get("body", ""),
                        "labels": [{"name": n} for n in lbl_names],
                        "comments": 0,
                    }
                    parent.issues[num] = iss
                    parent.comments[num] = []
                    if "status:po-decision" in lbl_names:
                        parent.po_issue_num = num
                    self._send(201, iss)
                    return

                m_comment = re.match(r"^/repos/duboc/bananacast/issues/(\d+)/comments$", path)
                if m_comment:
                    inum = int(m_comment.group(1))
                    cid = parent.next_comment_id
                    parent.next_comment_id += 1
                    c_obj = {"id": cid, "body": str(body.get("body") or "")}
                    parent.comments.setdefault(inum, []).append(c_obj)
                    if inum in parent.issues:
                        parent.issues[inum]["comments"] = len(parent.comments[inum])
                    if inum == parent.po_issue_num and "<!-- scion-agent:" not in c_obj["body"]:
                        parent.po_reply_posted = True
                    self._send(201, c_obj)
                    return

                self._send(404, {"message": f"Unhandled POST {path}"})

            def do_PATCH(self) -> None:
                path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
                body = self._body()
                m_ref = re.match(r"^/repos/duboc/bananacast/git/refs/heads/(.+)$", path)
                if m_ref:
                    br = m_ref.group(1)
                    parent.branch_shas[br] = str(body.get("sha") or "")
                    self._send(200, {"ref": f"refs/heads/{br}", "object": {"sha": parent.branch_shas[br]}})
                    return
                self._send(404, {"message": f"Unhandled PATCH {path}"})

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def test_run_hillclimb_wave_dry_run_and_mocked_lifecycle_preserves_main_and_runs_drills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import run_hillclimb_wave

    # 1. Verify WAVE_CATALOG structure and real duboc/bananacast@main grounding
    assert set(run_hillclimb_wave.WAVE_CATALOG.keys()) == {1, 2, 3}
    all_blob_parts: list[str] = []
    for wave_id in (1, 2, 3):
        tasks = run_hillclimb_wave.WAVE_CATALOG[wave_id]
        assert len(tasks) == 3
        for t in tasks:
            assert t.get("title")
            assert t.get("owner_role") in ("backend", "frontend")
            assert t.get("acceptance_criteria")
            all_blob_parts.extend(
                [
                    str(t.get("title", "")),
                    str(t.get("description", "")),
                    *(str(p) for p in (t.get("paths") or [])),
                    *(str(a) for a in (t.get("acceptance_criteria") or [])),
                ]
            )
    catalog_blob = "\n".join(all_blob_parts)
    for required_ref in (
        "src/backend/utils/admin_auth.py",
        "src/backend/routes/performances.py",
        "src/frontend/proxy.py",
        "openapi_url=None",
        "is_relative_to",
        'pytest.importorskip("fastapi")',
        "src/backend/routes/campaigns.py",
        "src/backend/tests/test_get_config_route.py",
        "cd src/backend && pytest -q",
    ):
        assert required_ref in catalog_blob, f"Missing grounded reference {required_ref!r} in WAVE_CATALOG"

    srv = _MockGitHubAndHubServer()
    try:
        env_file = tmp_path / "factory.env"
        env_file.write_text(
            "PO_EMAIL=po@example.com\n"
            "GITHUB_TOKEN=gh_fake_token_for_wave_test\n"
            "SCION_HUB_TOKEN=hub_fake_token_for_wave_test\n"
            f"HUB_URL={srv.base_url}\n"
            "HUB_K8S_NAMESPACE=tfha-prod\n"
            "IDLE_STOP_GRACE_SEC=10\n",
            encoding="utf-8",
        )

        seen_up_calls: list[dict[str, Any]] = []
        seen_down_calls: list[dict[str, Any]] = []
        deleted_pods: list[str] = []

        def fake_gke_up(spec_path: str | Path, *, env_file: str | Path, **kwargs: Any) -> dict[str, Any]:
            loaded_spec = scion_factory.load_spec(spec_path)
            parsed_env = dict(
                line.split("=", 1)
                for line in Path(env_file).read_text(encoding="utf-8").splitlines()
                if "=" in line and not line.startswith("#")
            )
            seen_up_calls.append(
                {
                    "spec": loaded_spec,
                    "env": parsed_env,
                    "kwargs": kwargs,
                }
            )
            return {
                "ok": True,
                "apply": bool(kwargs.get("apply")),
                "kit_dir": str(tmp_path / "kit"),
                "env_file": str(env_file),
                "total_sec": 0.1,
            }

        def fake_gke_down(spec_path: str | Path, *, env_file: str | Path, **kwargs: Any) -> dict[str, Any]:
            loaded_spec = scion_factory.load_spec(spec_path)
            parsed_env = dict(
                line.split("=", 1)
                for line in Path(env_file).read_text(encoding="utf-8").splitlines()
                if "=" in line and not line.startswith("#")
            )
            seen_down_calls.append(
                {
                    "spec": loaded_spec,
                    "env": parsed_env,
                    "kwargs": kwargs,
                }
            )
            if srv.mutate_main_on_teardown:
                srv.branch_shas["main"] = "9999999999999999999999999999999999999999"
            return {
                "ok": True,
                "apply": bool(kwargs.get("apply")),
                "hub_project": parsed_env.get("HUB_PROJECT", ""),
                "backup_dir": str(tmp_path / "backup-hc"),
                "project_deleted": bool(kwargs.get("apply") and kwargs.get("delete_project")),
            }

        monkeypatch.setattr(run_hillclimb_wave.gke_lifecycle, "gke_up", fake_gke_up)
        monkeypatch.setattr(run_hillclimb_wave.gke_lifecycle, "gke_down", fake_gke_down)
        monkeypatch.setenv("GITHUB_API_URL", srv.base_url)

        # Fake clock so idle_soak_sec=1800 executes deterministically with zero wall-clock wait
        fake_now = [1000.0]
        monkeypatch.setattr(run_hillclimb_wave, "_monotonic", lambda: fake_now[0])
        monkeypatch.setattr(run_hillclimb_wave, "_sleep", lambda sec: fake_now.__setitem__(0, fake_now[0] + max(float(sec), 1.0)))

        real_run = subprocess.run

        def fake_kubectl_run(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if isinstance(cmd, list) and cmd and cmd[0] == "kubectl":
                if "delete" in cmd and "pod" in cmd:
                    ns_val = cmd[cmd.index("-n") + 1] if "-n" in cmd else ""
                    pod_arg = next(
                        a
                        for a in cmd[cmd.index("pod") + 1 :]
                        if not a.startswith("-") and a != ns_val
                    )
                    deleted_pods.append(pod_arg)
                    return subprocess.CompletedProcess(cmd, 0, stdout="pod deleted\n", stderr="")
                if "get" in cmd and "pods" in cmd:
                    # Return 1 running specialist pod only on the very first wave poll before deletion
                    if not deleted_pods and srv.issue_poll_count <= 1:
                        pods = {
                            "items": [
                                {
                                    "metadata": {"name": "scion-lead-0", "labels": {"scion.dev/agent": "lead"}},
                                    "status": {"phase": "Running"},
                                },
                                {
                                    "metadata": {"name": "scion-backend-0", "labels": {"scion.dev/agent": "backend"}},
                                    "status": {"phase": "Running"},
                                },
                            ]
                        }
                    elif srv.lead_phase == "running":
                        pods = {
                            "items": [
                                {
                                    "metadata": {"name": "scion-lead-0", "labels": {"scion.dev/agent": "lead"}},
                                    "status": {"phase": "Running"},
                                }
                            ]
                        }
                    else:
                        pods = {
                            "items": [
                                {
                                    "metadata": {
                                        "name": "scion-factory-delta-2901-abc",
                                        "labels": {"job-name": "scion-factory-delta-2901"},
                                    },
                                    "status": {"phase": "Succeeded"},
                                }
                            ]
                        }
                    return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(pods), stderr="")
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", fake_kubectl_run)

        # 2. Dry-run check: zero GitHub refs/issues created, gke_up & gke_down invoked with apply=False
        dry_res = run_hillclimb_wave.run_wave(
            1,
            env_file=env_file,
            idle_soak_sec=1800,
            drills=False,
            apply=False,
        )
        assert dry_res["ok"] is True
        assert dry_res["apply"] is False
        assert re.fullmatch(r"scion-gke-hc1-[0-9a-f]{6}", dry_res["hub_project"])
        assert srv.created_refs == []
        assert srv.issues == {}
        assert seen_up_calls[-1]["spec"]["project"]["slug"] == "bananacast-hc1"
        assert seen_up_calls[-1]["spec"]["vcs"]["build_branch"] == "factory/gke-hc1"
        assert seen_up_calls[-1]["kwargs"]["apply"] is False
        assert seen_down_calls[-1]["kwargs"]["apply"] is False

        # 3. Full mocked Wave 3 with drills=True & apply=True
        res_w3 = run_hillclimb_wave.run_wave(
            3,
            env_file=env_file,
            idle_soak_sec=1800,
            drills=True,
            poll_interval_sec=5.0,
            apply=True,
        )
        assert res_w3["ok"] is True, res_w3
        assert res_w3["apply"] is True
        assert re.fullmatch(r"scion-gke-hc3-[0-9a-f]{6}", res_w3["hub_project"])

        # Remote branch factory/gke-hc3 created from main SHA + 3 wave issues + 1 PO drill issue
        assert any(
            r["ref"] == "refs/heads/factory/gke-hc3"
            and r["sha"] == "1111111111111111111111111111111111111111"
            for r in srv.created_refs
        )
        assert len(res_w3["seeded_issues"]) == 3
        assert srv.po_issue_num is not None
        po_comments = srv.comments[srv.po_issue_num]
        assert any("<!-- scion-agent:" not in c["body"] for c in po_comments)

        # Specialist eviction drill + PO decision wake drill both passed
        assert deleted_pods == ["scion-backend-0"]
        assert res_w3["drills"]["eviction_recovery"]["ok"] is True
        assert res_w3["drills"]["eviction_recovery"]["deleted_pod"] == "scion-backend-0"
        assert res_w3["drills"]["po_decision_roundtrip"]["ok"] is True
        assert res_w3["drills"]["po_decision_roundtrip"]["woke_within_2m"] is True

        # Idle soak confirmed 0 running agent pods and 0 additional lead steps/tokens after grace
        assert res_w3["idle_soak"]["ok"] is True
        assert res_w3["idle_soak"]["max_running_agent_pods_after_grace"] == 0
        assert res_w3["idle_soak"]["additional_lead_steps_after_grace"] == 0
        assert res_w3["idle_soak"]["additional_lead_tokens_after_grace"] == 0

        # gke_down called with exact teardown flags + main and factory/main SHAs preserved
        down_call = seen_down_calls[-1]
        assert down_call["kwargs"]["apply"] is True
        assert down_call["kwargs"]["delete_project"] is True
        assert down_call["kwargs"]["cleanup_issue_label"] == "factory:bananacast-hc3"
        assert down_call["kwargs"]["cleanup_branch"] == "factory/gke-hc3"
        assert res_w3["branch_guard"]["unchanged"] is True
        assert res_w3["branch_guard"]["main_before"] == res_w3["branch_guard"]["main_after"]
        assert res_w3["branch_guard"]["factory_main_before"] == res_w3["branch_guard"]["factory_main_after"]
    finally:
        srv.close()


def test_run_hillclimb_wave_teardown_runs_in_finally_on_poll_timeout_and_catches_branch_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import run_hillclimb_wave

    srv = _MockGitHubAndHubServer()
    try:
        env_file = tmp_path / "factory.env"
        env_file.write_text(
            "PO_EMAIL=po@example.com\n"
            "GITHUB_TOKEN=gh_fake_token\n"
            f"HUB_URL={srv.base_url}\n"
            "HUB_K8S_NAMESPACE=tfha-prod\n",
            encoding="utf-8",
        )

        # CLI check: DRY_RUN=1 overrides --apply
        cli_proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "run_hillclimb_wave.py"),
                "--wave",
                "2",
                "--env",
                str(env_file),
                "--idle-soak-sec",
                "60",
                "--timeout-sec",
                "120",
                "--apply",
                "--json",
            ],
            cwd=ROOT,
            env={**os.environ, "DRY_RUN": "1", "GITHUB_API_URL": srv.base_url},
            capture_output=True,
            text=True,
        )
        assert cli_proc.returncode == 0, cli_proc.stderr
        cli_out = json.loads(cli_proc.stdout)
        assert cli_out["apply"] is False
        assert srv.created_refs == []

        # Force wave polling to never verify (timeout) and ensure gke_down STILL runs in finally!
        seen_down: list[dict[str, Any]] = []

        def fake_down_for_timeout(spec_path: str | Path, *, env_file: str | Path, **kw: Any) -> dict[str, Any]:
            seen_down.append(kw)
            if srv.mutate_main_on_teardown:
                srv.branch_shas["main"] = "9999999999999999999999999999999999999999"
            return {"ok": True, "apply": True}

        real_sub_run = subprocess.run

        def fast_kubectl_stub(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if isinstance(cmd, list) and cmd and cmd[0] == "kubectl":
                return subprocess.CompletedProcess(cmd, 0, stdout='{"items": []}', stderr="")
            return real_sub_run(cmd, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", fast_kubectl_stub)
        monkeypatch.setattr(
            run_hillclimb_wave.gke_lifecycle,
            "gke_up",
            lambda spec_path, *, env_file, **kw: {"ok": True, "apply": True, "env_file": str(env_file)},
        )
        monkeypatch.setattr(
            run_hillclimb_wave.gke_lifecycle,
            "gke_down",
            fake_down_for_timeout,
        )
        monkeypatch.setenv("GITHUB_API_URL", srv.base_url)

        # Keep issues stuck in status:ready by freezing issue_poll_count increment via timeout_sec=5
        fake_now = [5000.0]
        monkeypatch.setattr(run_hillclimb_wave, "_monotonic", lambda: fake_now[0])
        monkeypatch.setattr(run_hillclimb_wave, "_sleep", lambda sec: fake_now.__setitem__(0, fake_now[0] + 10.0))

        # Override GET issues so they stay status:ready until timeout
        orig_poll = run_hillclimb_wave._poll_issues_until_verified

        def stuck_poll(*args: Any, **kwargs: Any) -> Any:
            raise TimeoutError("Timed out waiting for wave issues to reach status:verified")

        monkeypatch.setattr(run_hillclimb_wave, "_poll_issues_until_verified", stuck_poll)

        timed_out_res = run_hillclimb_wave.run_wave(
            2,
            env_file=env_file,
            idle_soak_sec=60,
            timeout_sec=5,
            apply=True,
        )
        assert timed_out_res["ok"] is False
        assert "Timed out" in timed_out_res.get("error", "")
        assert len(seen_down) == 1
        assert seen_down[0]["apply"] is True
        assert seen_down[0]["delete_project"] is True
        assert seen_down[0]["cleanup_issue_label"] == "factory:bananacast-hc2"
        assert seen_down[0]["cleanup_branch"] == "factory/gke-hc2"

        # Verify protected branch mutation raises RuntimeError AFTER gke_down runs
        monkeypatch.setattr(run_hillclimb_wave, "_poll_issues_until_verified", orig_poll)
        srv.lead_phase = "stopped"
        srv.mutate_main_on_teardown = True
        with pytest.raises(RuntimeError, match="main"):
            run_hillclimb_wave.run_wave(
                1,
                env_file=env_file,
                idle_soak_sec=0,
                poll_interval_sec=1.0,
                apply=True,
            )
        assert len(seen_down) == 2
    finally:
        srv.close()
