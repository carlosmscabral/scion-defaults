"""Tests for cross-family verification policy, parallel reviewer pools, and release-checkpoint eval triggers."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import role_library  # noqa: E402
import scion_factory  # noqa: E402
import verification_policy  # noqa: E402
from test_hub_client import fake_hub  # noqa: E402,F401


def _load_script(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _starter() -> dict:
    return scion_factory.load_spec(ROOT / "presets" / "starter-service.factory.yaml")


def test_infer_model_family_detects_cross_harness_same_provider():
    assert verification_policy.infer_model_family("opencode", "google-vertex/gemini-4-argon") == "google"
    assert verification_policy.infer_model_family("antigravity", "Gemini 4 Argon (High)") == "google"
    assert verification_policy.infer_model_family("hermes", "google/gemini-4-argon") == "google"
    assert verification_policy.infer_model_family("claude", "opus") == "anthropic"
    assert verification_policy.infer_model_family("hermes", "anthropic/claude-sonnet-4") == "anthropic"
    assert verification_policy.infer_model_family("codex", "o3") == "openai"
    assert verification_policy.infer_model_family("hermes", "deepseek/deepseek-r1") == "deepseek"


def test_new_grill_draft_enforces_cross_family_verifiers_with_stock_harnesses():
    draft = scion_factory.draft_spec_from_repo(None)
    assert draft["team"]["verification_policy"] == "cross-family"
    by_role = {r["role"]: r for r in draft["team"]["build_roles"] + draft["team"]["eval_roles"]}
    assert by_role["backend"]["harness"] == "antigravity"
    assert by_role["reviewer"] == {
        **by_role["reviewer"],
        "harness": "claude",
        "model": "opus",
    }
    assert by_role["domain-evaluator"]["harness"] == "claude"
    assert by_role["domain-evaluator"]["model"] == "opus"


def test_cross_family_policy_blocks_monoculture_while_legacy_advisory_warns():
    legacy = _starter()
    # starter-service uses Gemini across opencode, antigravity, and hermes and legacy specs warn without failing.
    legacy_report = scion_factory.evaluate_spec_readiness(legacy)
    assert legacy_report["ready"] is True
    assert any("verifier role 'reviewer' shares model family `google`" in w for w in legacy_report["warnings"])

    enforced = _starter()
    enforced["team"]["verification_policy"] = "cross-family"
    enforced_report = scion_factory.evaluate_spec_readiness(enforced)
    assert enforced_report["ready"] is False
    assert any("verifier role 'reviewer'" in err for err in enforced_report["missing_or_weak"])

    # Single-harness opt-out requires an explicit reason.
    single = _starter()
    single["team"]["verification_policy"] = "single-harness"
    assert scion_factory.evaluate_spec_readiness(single)["ready"] is False
    single["team"]["single_harness_reason"] = "Sandbox Hub exposes only the antigravity harness."
    assert scion_factory.evaluate_spec_readiness(single)["ready"] is True


def test_add_role_and_diversify_team_assign_opposing_family_and_reviewer_pool(tmp_path):
    spec_path = tmp_path / "factory.yaml"
    scion_factory.save_spec(_starter(), spec_path)

    res = subprocess.run(
        [
            sys.executable,
            "scripts/scion_factory.py",
            "diversify-team",
            str(spec_path),
            "--reviewer-replicas",
            "2",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout)
    assert payload["ready"] is True
    assert payload["verification_policy"] == "cross-family"
    assert payload["reviewer_replicas"] == 2

    scion_factory.add_role(spec_path, "test-engineer", evidence=["tests/"])
    spec = scion_factory.load_spec(spec_path)
    te = next(r for r in spec["team"]["build_roles"] if r["role"] == "test-engineer")
    assert te["harness"] == "claude"
    assert te["model"] == "opus"

    kit = tmp_path / "kit"
    scion_factory.compile_factory_boilerplate(spec, kit)
    assert (kit / "templates" / "reviewer" / "system-prompt.md").exists()
    assert (kit / "templates" / "reviewer-2" / "system-prompt.md").exists()
    lead_md = (kit / "templates" / "lead" / "system-prompt.md").read_text()
    assert "reviewer pool (M/G/k)" in lead_md
    assert "`reviewer`, `reviewer-2`" in lead_md
    assert "git merge-base --is-ancestor origin/main" in lead_md


def test_high_wip_single_reviewer_emits_bottleneck_warning():
    spec = _starter()
    spec["team"]["max_wip"] = 4
    report = scion_factory.evaluate_spec_readiness(spec)
    assert any("M/G/1 queue bottleneck" in w for w in report["warnings"])


def test_reviewer_guardrails_eval_intake_and_dropped_doc_writer_sync(tmp_path):
    spec = _starter()
    spec["team"]["dropped_defaults"] = [{"role": "doc-writer", "reason": "Lead updates LIVE.md at wave close."}]
    kit = tmp_path / "kit"
    scion_factory.compile_factory_boilerplate(spec, kit)

    reviewer_md = (kit / "templates" / "reviewer" / "system-prompt.md").read_text()
    assert "git grep" in reviewer_md
    assert "mutating endpoints" in reviewer_md
    assert "green-mock" in reviewer_md
    assert "git grep" in role_library.role_protocol("reviewer")

    lead_md = (kit / "templates" / "lead" / "system-prompt.md").read_text()
    assert "Because `doc-writer` is omitted from this roster, update `/workspace/knowledge-plane/state/LIVE.md`" in lead_md

    eval_lead_md = (kit / "templates" / "eval-lead" / "system-prompt.md").read_text()
    eval_loop = eval_lead_md.split("## Operating Loop\n", 1)[1]
    assert "/scion-volumes/knowledge-plane/state/LIVE.md" in eval_loop
    assert "--label status:ready" not in eval_loop


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")
def test_watchdog_per_agent_kp_sync_and_live_md_release_wake(tmp_path, monkeypatch):
    home = tmp_path / ".scion"
    home.mkdir()
    os.environ["SCION_HOME"] = str(home)
    watchdog = _load_script("factory_watchdog_release_test", ROOT / "scripts" / "factory-watchdog.py")

    conn = sqlite3.connect(home / "hub.db")
    conn.execute("CREATE TABLE projects (id TEXT, slug TEXT)")
    conn.execute("CREATE TABLE agents (id TEXT, project_id TEXT, slug TEXT, template TEXT, phase TEXT)")
    conn.execute("INSERT INTO projects VALUES ('11111111-aaaa', 'demo-app')")
    conn.execute("INSERT INTO projects VALUES ('22222222-bbbb', 'demo-app-eval')")
    conn.execute("INSERT INTO agents VALUES ('a1', '22222222-bbbb', 'eval-lead', 'eval-lead', 'running')")
    conn.commit()
    conn.row_factory = sqlite3.Row

    # per-agent layout: no ~/.scion/projects/demo-app, only ~/.scion/agents/demo-app--lead/workspace/knowledge-plane
    lead_kp = home / "agents" / "demo-app--lead" / "workspace" / "knowledge-plane"
    (lead_kp / "state").mkdir(parents=True)
    (lead_kp / "INDEX.md").write_text("# Index\n")
    (lead_kp / "state" / "LIVE.md").write_text("revision: demo-app-serving-00001\n")

    (home / "project-configs" / "demo-app__11111111" / "shared-dirs").mkdir(parents=True)
    (home / "project-configs" / "demo-app-eval__22222222" / "shared-dirs").mkdir(parents=True)

    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(watchdog, "SCION_HOME", str(home))
    monkeypatch.setattr(watchdog, "FACTORY_PROJECTS", ["demo-app", "demo-app-eval"])
    monkeypatch.setattr(watchdog, "api", lambda m, p, b=None: (calls.append((m, p, b or {})) or (200, {"ok": True})))

    watchdog.check_shared_knowledge_plane(conn, apply=True)
    eval_live = home / "project-configs" / "demo-app-eval__22222222" / "shared-dirs" / "knowledge-plane" / "state" / "LIVE.md"
    assert eval_live.read_text() == "revision: demo-app-serving-00001\n"
    assert len(calls) == 1
    assert calls[0][1] == "/api/v1/projects/22222222-bbbb/agents/eval-lead/message"
    assert "[RELEASE CHECKPOINT]" in calls[0][2]["message"]

    # Second run without a LIVE.md change does NOT wake eval-lead again (zero idle token burn).
    calls.clear()
    watchdog.check_shared_knowledge_plane(conn, apply=True)
    assert calls == []

    # Unrelated file with LIVE.md substring does NOT wake eval-lead.
    (lead_kp / "notes-state").mkdir(exist_ok=True)
    (lead_kp / "notes-state" / "LIVE.md.txt").write_text("unrelated\n")
    watchdog.check_shared_knowledge_plane(conn, apply=True)
    assert calls == []

    # DRY_RUN=1 prevents any API POST even if apply=True is passed, and single-project evaluator (no -eval suffix) works.
    conn.execute("INSERT INTO agents VALUES ('a2', '11111111-aaaa', 'evaluator', 'evaluator', 'running')")
    conn.commit()
    (lead_kp / "state" / "LIVE.md").write_text("revision: demo-app-serving-00002\n")
    monkeypatch.setenv("DRY_RUN", "true")
    watchdog.check_shared_knowledge_plane(conn, apply=True)
    assert calls == []

    monkeypatch.delenv("DRY_RUN", raising=False)
    watchdog.check_shared_knowledge_plane(conn, apply=True)
    woken_paths = {path for _m, path, _b in calls}
    assert "/api/v1/projects/11111111-aaaa/agents/evaluator/message" in woken_paths
    assert "/api/v1/projects/22222222-bbbb/agents/eval-lead/message" in woken_paths


def test_provision_creates_optional_eval_heartbeat_when_configured(fake_hub, tmp_path):
    from test_provision_factory import fake_cli_env, write_gke_factory_env

    spec = _starter()
    spec["project"]["slug"] = "starter-service"
    spec["project"]["eval_slug"] = "starter-service-eval"
    spec["infrastructure"]["runtime"] = "kubernetes"
    spec["infrastructure"]["runtime_profile"] = "gke"
    spec["team"]["eval_heartbeat_cron"] = "0 */2 * * *"

    spec_path = tmp_path / "factory.yaml"
    kit_dir = tmp_path / "kit"
    scion_factory.save_spec(spec, spec_path)
    scion_factory.compile_factory_boilerplate(spec, kit_dir)
    env_file = write_gke_factory_env(tmp_path, fake_hub.url)

    res = subprocess.run(
        [
            sys.executable,
            "scripts/scion_factory.py",
            "provision",
            str(spec_path),
            "--kit",
            str(kit_dir),
            "--env",
            str(env_file),
            "--apply",
            "--json",
        ],
        cwd=ROOT,
        env=fake_cli_env(tmp_path, fake_hub.url),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr + "\n" + res.stdout
    eval_pid = fake_hub.projects_by_slug["starter-service-eval"]["id"]
    eval_sched = next(s for s in fake_hub.schedules[eval_pid] if s["name"] == "starter-service-eval-lead-heartbeat")
    assert eval_sched["agentName"] == "eval-lead"
    assert eval_sched["cronExpr"] == "0 */2 * * *"

def test_agent_kp_sync_wakes_suspended_eval_lead(tmp_path, monkeypatch):
    home = tmp_path / ".scion"
    home.mkdir()
    os.environ["SCION_HOME"] = str(home)
    watchdog = _load_script("factory_watchdog_release_test", ROOT / "scripts" / "factory-watchdog.py")

    conn = sqlite3.connect(home / "hub.db")
    conn.execute("CREATE TABLE projects (id TEXT, slug TEXT)")
    conn.execute("CREATE TABLE agents (id TEXT, project_id TEXT, slug TEXT, template TEXT, phase TEXT)")
    conn.execute("INSERT INTO projects VALUES ('22222222-bbbb', 'demo-app-eval')")
    conn.execute("INSERT INTO agents VALUES ('a1', '22222222-bbbb', 'eval-lead', 'eval-lead', 'suspended')")
    conn.commit()
    conn.row_factory = sqlite3.Row

    lead_kp = home / "agents" / "demo-app--lead" / "workspace" / "knowledge-plane"
    (lead_kp / "state").mkdir(parents=True)
    (lead_kp / "INDEX.md").write_text("# Index\n")
    (lead_kp / "state" / "LIVE.md").write_text("revision: demo-app-serving-00001\n")

    (home / "project-configs" / "demo-app-eval__22222222" / "shared-dirs").mkdir(parents=True)

    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(watchdog, "SCION_HOME", str(home))
    monkeypatch.setattr(watchdog, "FACTORY_PROJECTS", ["demo-app-eval"])
    monkeypatch.setattr(watchdog, "api", lambda m, p, b=None: (calls.append((m, p, b or {})) or (200, {"ok": True})))

    watchdog.check_shared_knowledge_plane(conn, apply=True)
    
    assert len(calls) == 1
    assert calls[0][1] == "/api/v1/projects/22222222-bbbb/agents/eval-lead/message"
    assert calls[0][2].get("wake") is True

def test_normalize_team_respects_explicit_diverse_default_verifier():
    spec = _starter()
    spec["team"]["verification_policy"] = "cross-family"
    # Provide an explicit, diverse model for the reviewer (which has default: true)
    rev = next(r for r in spec["team"]["build_roles"] if r["role"] == "reviewer")
    rev["default"] = True
    rev["harness"] = "codex"
    rev["model"] = "gpt-5"

    normalized, notes = scion_factory.normalize_team(spec)
    n_rev = next(r for r in normalized["team"]["build_roles"] if r["role"] == "reviewer")
    assert n_rev["harness"] == "codex"
    assert n_rev["model"] == "gpt-5"
    assert not any("overrode reviewer" in note for note in notes)

    # Now make it non-diverse and see it overridden
    rev["harness"] = "antigravity"
    rev["model"] = "gemini"
    normalized2, notes2 = scion_factory.normalize_team(spec)
    n_rev2 = next(r for r in normalized2["team"]["build_roles"] if r["role"] == "reviewer")
    assert n_rev2["harness"] == "claude"
    assert "overrode reviewer with opposing verifier claude/opus" in "".join(notes2)
