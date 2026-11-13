"""Tests for the topology-agnostic closed-loop 24h Kaizen engine (L34)."""
from __future__ import annotations

import copy
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import hub_telemetry_exporter as exporter  # noqa: E402
import kaizen_policy  # noqa: E402
import kaizen_watcher as kw  # noqa: E402
import role_library  # noqa: E402
import scion_factory  # noqa: E402


def _seed_hub_db(db_path: Path) -> None:
    """Create a synthetic Hub SQLite database with flow, verdict, and drift signals."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE projects (id TEXT PRIMARY KEY, slug TEXT);
        CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT, role TEXT);
        CREATE TABLE agents (
            id TEXT PRIMARY KEY, project_id TEXT, slug TEXT, template TEXT,
            phase TEXT, activity TEXT, container_status TEXT,
            current_turns INTEGER, current_model_calls INTEGER,
            generation INTEGER, exit_reason TEXT, applied_config TEXT, harness_config TEXT,
            updated TEXT, deleted_at TEXT
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY, project_id TEXT, sender TEXT, recipient TEXT,
            msg TEXT, dispatch_state TEXT, created TEXT
        );
        CREATE TABLE agent_session_metrics (
            id TEXT PRIMARY KEY, agent_id TEXT, grove_id TEXT, model TEXT,
            turn_count INTEGER, tokens_input INTEGER, tokens_output INTEGER,
            tokens_cached INTEGER, created_at TEXT
        );
        CREATE TABLE templates (id TEXT PRIMARY KEY, scope_id TEXT, slug TEXT);
        """
    )
    conn.execute("INSERT INTO projects VALUES ('p-build', 'svc-build'), ('p-eval', 'svc-eval')")
    conn.execute("INSERT INTO users VALUES ('u1', 'po@example.com', 'admin')")
    conn.execute(
        """INSERT INTO agents VALUES
        ('a1','p-build','lead','lead','running','blocked','Up 2 hours',2,5,1,'','{"model":"opus","SECRET":"nope"}','claude','now',NULL),
        ('a2','p-build','kaizen-analyst','kaizen-analyst','running','completed','Up 2 hours',1,2,1,'','{"model":"opus"}','claude','now',NULL),
        ('a3','p-build','reviewer','reviewer','running','completed','Up 2 hours',1,3,1,'','{"model":"Gemini"}','antigravity','now',NULL),
        ('a4','p-build','security-auditor','security-auditor','stopped','idle','Up 2 hours',1,3,1,'','{"model":"opus"}','claude','now',NULL),
        ('a5','p-build','deployer','deployer','running','completed','Up 2 hours',1,2,1,'','{"model":"Gemini"}','antigravity','now',NULL),
        ('a6','p-eval','eval-lead','eval-lead','running','blocked','Up 2 hours',1,2,1,'','{"model":"Gemini"}','antigravity','now',NULL)
        """
    )
    conn.execute(
        """INSERT INTO messages VALUES
        ('m1','p-build','agent:lead','agent:reviewer','Review slice. COMMIT SLOT queued behind #12','dispatched',datetime('now','-30 minutes')),
        ('m2','p-build','agent:reviewer','agent:lead','VERDICT: FAIL (rebase-only) after 429 rate limit','dispatched',datetime('now','-24 minutes')),
        ('m3','p-build','agent:lead','agent:security-auditor','Audit auth handler','pending',datetime('now','-20 minutes')),
        ('m4','p-build','agent:security-auditor','agent:lead','VERDICT: PASS — clean signature verification','dispatched',datetime('now','-15 minutes')),
        ('m5','p-build','agent:lead','agent:backend','COMMIT SLOT #13 granted — push to main','dispatched',datetime('now','-14 minutes')),
        ('m6','p-build','agent:backend','agent:lead','Slice #13 landed and pushed to origin/main','dispatched',datetime('now','-11 minutes')),
        ('m7','p-build','agent:deployer','agent:lead','Revision 00012 deployed and verified','dispatched',datetime('now','-5 minutes'))
        """
    )
    conn.execute(
        """INSERT INTO agent_session_metrics VALUES
        ('s1','a1','p-build','opus',4,12000,1500,9000,datetime('now'))
        """
    )
    conn.execute("INSERT INTO templates VALUES ('t1','p-build','lead'), ('t2','p-build','kaizen-analyst')")
    conn.commit()
    conn.close()


def test_sqlite_telemetry_exports_flow_verdicts_and_friction_without_secrets(tmp_path: Path) -> None:
    """Verify v2 telemetry matches VERDICT: PASS, reconciles disk reviews, and computes flow metrics."""
    db_path = tmp_path / "hub.db"
    _seed_hub_db(db_path)
    sp = tmp_path / "project-configs" / "svc-build__12345678" / "shared-dirs" / "scratchpad"
    (sp / "skills" / "demo-skill").mkdir(parents=True)
    (sp / "skills" / "demo-skill" / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    reviews = sp / "reviews"
    reviews.mkdir(parents=True)
    for idx in range(4):
        (reviews / f"13-r{idx}.md").write_text("VERDICT: PASS\nAll checks green.\n", encoding="utf-8")
    (reviews / "12-fail.md").write_text("VERDICT: FAIL\nMissing regression test.\n", encoding="utf-8")

    conn = exporter.open_hub_db(str(tmp_path))
    try:
        out = exporter.collect_hub_telemetry(conn, str(tmp_path), ["svc-build", "svc-eval"], window_hours=24)
    finally:
        conn.close()

    assert out["schema_version"] == 2
    assert out["source_mode"] == "sqlite"
    assert out["total_messages_24h"] == 7
    assert out["undispatched_messages_24h"] == 1
    assert out["verifier_inbound_messages_24h"] == {"reviewer": 1, "security-auditor": 1}
    assert out["symptom_counts_24h"]["verdict_approve"] == 1
    assert out["symptom_counts_24h"]["verdict_fail"] == 1
    assert out["symptom_counts_24h"]["rebase_churn"] == 1
    assert out["symptom_counts_24h"]["commit_slot_wait"] == 1
    assert out["symptom_counts_24h"]["rate_limit_or_quota"] == 1
    assert out["verdict_artifacts_24h"] == {
        "pass_files": 4,
        "fail_files": 1,
        "sensor_drift_warning": True,
    }
    assert out["flow_latencies_24h"]["verifier_turnaround_min"]["count"] == 2
    assert out["flow_latencies_24h"]["slot_hold_min"] == {"count": 1, "p50": 3.0, "p95": 3.0, "max": 3.0}
    assert out["flow_latencies_24h"]["trunk_landings_24h"] == 1
    assert out["flow_latencies_24h"]["releases_24h"] == 1
    assert out["operational_friction_24h"]["phase_drift_agents"] == ["security-auditor"]
    assert out["scratchpad_staged_skills"] == ["demo-skill"]
    assert "nope" not in json.dumps(out)

    written = exporter.write_telemetry_file(out, str(tmp_path), "svc-build")
    assert len(written) == 1
    assert json.loads(Path(written[0]).read_text(encoding="utf-8"))["schema_version"] == 2


def test_api_telemetry_mode_supports_remote_hubs() -> None:
    """Verify remote Hub REST mode computes PASS/APPROVE verdicts and verifier queues."""
    class FakeClient:
        def request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
            if path == "/api/v1/projects":
                return 200, {"projects": [{"id": "p1", "slug": "svc-build"}]}
            if path == "/api/v1/projects/p1/agents":
                return 200, {"agents": [{"slug": "lead", "template": "lead", "phase": "running", "harnessConfig": "claude"}]}
            if path.startswith("/api/v1/messages"):
                return 200, {
                    "messages": [
                        {
                            "projectId": "p1",
                            "sender": "agent:lead",
                            "recipient": "agent:reviewer",
                            "dispatchState": "dispatched",
                            "msg": "VERDICT: PASS",
                        }
                    ]
                }
            raise AssertionError(f"unexpected {method} {path}")

    out = exporter.collect_hub_telemetry_api(FakeClient(), ["svc-build"], window_hours=24)  # type: ignore[arg-type]
    assert out["source_mode"] == "api"
    assert out["total_messages_24h"] == 1
    assert out["symptom_counts_24h"]["verdict_approve"] == 1
    assert out["verifier_inbound_messages_24h"] == {"reviewer": 1}


def test_kaizen_watcher_routes_to_both_build_and_eval_leads_once(tmp_path: Path, monkeypatch: Any) -> None:
    """Verify kaizen_watcher wakes kaizen-analyst and deduplicates Build + Eval lead routing."""
    _seed_hub_db(tmp_path / "hub.db")
    (tmp_path / "hub.env").write_text("SCION_SERVER_SESSION_SECRET=test-secret\n", encoding="utf-8")
    monkeypatch.setenv("SCION_HOME", str(tmp_path))
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_dummy")

    sent: list[tuple[str, Any]] = []

    class FakeHubClient:
        def request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
            sent.append((path, body))
            return 200, {"ok": True}

    issues_payload = [
        {
            "number": 42,
            "title": "Kaizen 2026-11-11: harden L2 contract check and commit-slot gate",
            "labels": [{"name": "kaizen-proposal"}, {"name": "status:ready"}, {"name": "kaizen-eval"}],
            "body": "- KZ-2026-11-11-01 [build] reviewer gate\n- KZ-2026-11-11-02 [eval] L2 happy-path check",
        }
    ]

    with (
        mock.patch.object(kw, "_resolve_hub_client", return_value=FakeHubClient()),
        mock.patch.object(kw, "github_get_json", return_value=issues_payload),
    ):
        rc1 = kw.main(
            [
                "--repo", "example-org/svc",
                "--build-project", "svc-build",
                "--eval-project", "svc-eval",
                "--po-email", "po@example.com",
                "--trigger-now",
                "--telemetry-output", str(tmp_path / "hub-24h.json"),
            ]
        )
        assert rc1 == 0
        paths_first = [p for p, _ in sent]
        assert "/api/v1/projects/p-build/agents/kaizen-analyst/message" in paths_first
        assert "/api/v1/projects/p-build/agents/lead/message" in paths_first
        assert "/api/v1/projects/p-eval/agents/eval-lead/message" in paths_first

        lead_msg = next(b["message"] for p, b in sent if p.endswith("/agents/lead/message"))
        eval_msg = next(b["message"] for p, b in sent if p.endswith("/agents/eval-lead/message"))
        assert "without interrupting busy specialists mid-task" in lead_msg
        assert "mutation proof" in lead_msg
        assert "### Auto-improvement (Kaizen)" in lead_msg
        assert "without reading or modifying the build branch" in eval_msg

        sent.clear()
        rc2 = kw.main(
            [
                "--repo", "example-org/svc",
                "--build-project", "svc-build",
                "--eval-project", "svc-eval",
                "--po-email", "po@example.com",
                "--daily-hour-utc", "23",
                "--telemetry-output", str(tmp_path / "hub-24h.json"),
            ]
        )
        assert rc2 == 0
        assert sent == []


def test_post_compile_kaizen_binds_dynamically_across_stochastic_topologies(tmp_path: Path) -> None:
    """Verify post-compile Kaizen adapts cleanly to service, deploy:none library, and non-Git topologies."""
    proto = role_library.role_protocol("kaizen-analyst")
    assert proto is not None
    assert "ADOPTED & MEASURED" in proto
    assert "mutation" in proto.lower()
    assert "never modify product code" in proto.lower()

    base_spec = scion_factory.load_spec(ROOT / "presets" / "starter-service.factory.yaml")

    # Case A: Deployed multi-verifier service factory with Git enabled
    svc_spec = copy.deepcopy(base_spec)
    svc_spec["team"]["build_roles"].append(role_library.library_role_entry("security-auditor"))
    svc_spec["team"]["build_roles"].append(role_library.library_role_entry("kaizen-analyst"))
    svc_kit = tmp_path / "svc_kit"
    scion_factory.compile_factory_boilerplate(svc_spec, svc_kit)

    svc_manifest = json.loads((svc_kit / "config" / "kaizen-manifest.json").read_text(encoding="utf-8"))
    assert svc_manifest["deploy_mode"] == "service"
    assert svc_manifest["workflow_mode"] == "git_commit_slot"
    assert svc_manifest["verifier_roles"] == ["reviewer", "security-auditor"]
    assert svc_manifest["releaser_roles"] == ["deployer"]

    svc_kaizen = (svc_kit / "templates" / "kaizen-analyst" / "system-prompt.md").read_text(encoding="utf-8")
    svc_lead = (svc_kit / "templates" / "lead" / "system-prompt.md").read_text(encoding="utf-8")
    assert "ADOPTED & MEASURED" in svc_kaizen
    assert "VALIDATED" in svc_kaizen and "CONTRADICTED" in svc_kaizen
    assert "[kaizen no-op]" in svc_kaizen
    assert "`reviewer`, `security-auditor`" in svc_kaizen
    assert "solo_release_serialization" in svc_kaizen
    assert "knowledge-plane/INDEX.md" in svc_kaizen
    assert "### Kaizen 3-Axis Triage Rubric" in svc_lead
    assert "solo_release_serialization" in svc_lead

    # Case B: Library / CLI factory (`deploy: "none"`, single verifier, non-Git snapshot mode)
    lib_spec = scion_factory.load_spec(ROOT / "presets" / "go-library.factory.yaml")
    lib_spec["vcs"]["enabled"] = False
    lib_spec["team"]["build_roles"] = [
        r for r in lib_spec["team"]["build_roles"] if r["role"] != "deployer"
    ]
    lib_spec["team"]["build_roles"].append(role_library.library_role_entry("kaizen-analyst"))
    lib_kit = tmp_path / "lib_kit"
    scion_factory.compile_factory_boilerplate(lib_spec, lib_kit)

    lib_manifest = json.loads((lib_kit / "config" / "kaizen-manifest.json").read_text(encoding="utf-8"))
    assert lib_manifest["deploy_mode"] == "none"
    assert lib_manifest["workflow_mode"] == "shared_edit_slot"
    assert lib_manifest["releaser_roles"] == []

    lib_kaizen = (lib_kit / "templates" / "kaizen-analyst" / "system-prompt.md").read_text(encoding="utf-8")
    lib_lead = (lib_kit / "templates" / "lead" / "system-prompt.md").read_text(encoding="utf-8")
    assert "deployer" not in lib_kaizen.lower()
    assert "landings_per_release" not in lib_kaizen
    assert "kaizen-$(date -u +%F).md" in lib_kaizen
    assert "gh issue create" not in lib_kaizen
    assert "batch `>= 2` verified non-urgent slices per deployment" not in lib_lead

    # Case C: Monorepo factory with monorepo.agent_prefix and subdir
    mono_spec = copy.deepcopy(base_spec)
    mono_spec["monorepo"] = {"agent_prefix": "pay", "subdir": "services/pay"}
    mono_spec["team"]["build_roles"].append(role_library.library_role_entry("reviewer"))
    mono_spec["team"]["build_roles"].append(role_library.library_role_entry("kaizen-analyst"))
    mono_kit = tmp_path / "mono_kit"
    scion_factory.compile_factory_boilerplate(mono_spec, mono_kit)
    
    mono_kaizen = (mono_kit / "templates" / "kaizen-analyst" / "system-prompt.md").read_text(encoding="utf-8")
    mono_lead = (mono_kit / "templates" / "lead" / "system-prompt.md").read_text(encoding="utf-8")
    assert "`pay-reviewer`" in mono_kaizen
    assert "services/pay/knowledge-plane/kp-check.py" in mono_kaizen
    assert "pay-lead" in mono_kaizen



def test_kaizen_watcher_bypasses_loop_on_github_rest_api_error(tmp_path: Path, monkeypatch: Any) -> None:
    """Verify kaizen_watcher catches URLError and does not loop on network failures."""
    _seed_hub_db(tmp_path / "hub.db")
    (tmp_path / "hub.env").write_text("SCION_SERVER_SESSION_SECRET=test-secret\n", encoding="utf-8")
    monkeypatch.setenv("SCION_HOME", str(tmp_path))
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_dummy")

    sent: list[tuple[str, Any]] = []

    class FakeHubClient:
        def request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
            sent.append((path, body))
            return 200, {"ok": True}

    import urllib.error
    with (
        mock.patch.object(kw, "_resolve_hub_client", return_value=FakeHubClient()),
        mock.patch.object(kw, "github_get_json", side_effect=urllib.error.URLError("offline")),
    ):
        for i in range(3):
            args = [
                "--repo", "example-org/svc",
                "--build-project", "svc-build",
                "--telemetry-output", str(tmp_path / "hub-24h.json"),
            ]
            if i == 0:
                args.append("--trigger-now")
            else:
                args.append("--daily-hour-utc")
                args.append("23") # Ensure it doesn't trigger by time if i > 0
            rc = kw.main(args)
            assert rc == 0
        
        # Should only wake kaizen-analyst once (first tick), then state is saved.
        # It never hits github routing because github_get_json fails.
        assert len([p for p, _ in sent if "kaizen-analyst" in p]) == 1


def test_kaizen_watcher_supports_empty_repo_non_git_shared_edit_slot(tmp_path: Path, monkeypatch: Any) -> None:
    """Verify kaizen_watcher supports --repo "" for non-Git sweeps."""
    _seed_hub_db(tmp_path / "hub.db")
    monkeypatch.setenv("SCION_HOME", str(tmp_path))
    sent: list[tuple[str, Any]] = []

    class FakeHubClient:
        def request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
            sent.append((path, body))
            return 200, {"ok": True}

    with mock.patch.object(kw, "_resolve_hub_client", return_value=FakeHubClient()):
        rc = kw.main([
            "--repo", "",
            "--build-project", "svc-build",
            "--trigger-now",
            "--telemetry-output", str(tmp_path / "hub-24h.json"),
        ])
        assert rc == 0
        assert len(sent) == 1
        msg = sent[0][1]["message"]
        assert "DAILY KAIZEN SWEEP" in msg
        assert "scratchpad/kaizen-" in msg


def test_hub_telemetry_exporter_tolerates_minimal_sqlite_schema(tmp_path: Path) -> None:
    """Verify the exporter handles old hub.db schemas with missing optional columns/tables."""
    db_path = tmp_path / "hub.db"
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE projects (id TEXT PRIMARY KEY, slug TEXT);
        CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT, role TEXT);
        CREATE TABLE agents (
            id TEXT PRIMARY KEY, project_id TEXT, slug TEXT, template TEXT,
            phase TEXT, activity TEXT, current_turns INTEGER
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY, project_id TEXT, sender TEXT, recipient TEXT,
            msg TEXT, dispatch_state TEXT, created TEXT
        );
        """
    )
    conn.execute("INSERT INTO projects VALUES ('p-build', 'svc-build')")
    conn.execute("INSERT INTO agents VALUES ('a1','p-build','lead','lead','running','blocked',2)")
    conn.commit()
    conn.close()
    
    out = exporter.collect_hub_telemetry(exporter.open_hub_db(str(tmp_path)), str(tmp_path), ["svc-build"], window_hours=24)
    assert out["source_mode"] == "sqlite"
    assert out["roster"][0]["agent"] == "lead"
    assert out["roster"][0]["container_status"] == ""
    assert out["roster"][0]["generation"] == 1
