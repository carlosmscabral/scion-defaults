"""Tests for single-project FactorySpecs (`eval_slug` optional), `evaluator` role, and ephemeral delta prompts."""
from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import role_library  # noqa: E402
import scion_factory  # noqa: E402


def _single_project_delta_spec() -> dict:
    """Single-project GKE delta FactorySpec without eval_slug, eval_branch, eval_service, eval_roles, or heartbeat_cron."""
    return {
        "project": {
            "name": "BananaCast Studio",
            "slug": "bananacast-hc1",
            "mission": "Deliver verified multi-app audio/video studio slices with zero idle LLM or pod spend.",
            "non_goals": [
                "Never deploy public Cloud Run services during hillclimb code verification runs.",
                "Never mutate main or factory/main branches.",
            ],
        },
        "vcs": {
            "enabled": True,
            "provider": "github",
            "repo": "example-org/bananacast",
            "build_branch": "factory/gke-hc1",
        },
        "knowledge_plane": {
            "requirements": [
                {
                    "id": "REQ-001",
                    "title": "Health & config validation",
                    "description": "Validate studio configuration and proxy routes deterministically.",
                    "acceptance": ["Unit and integration suites pass under pytest."],
                }
            ],
            "invariants": [
                "All API error payloads return structured JSON with an explicit error code.",
            ],
            "decisions": [
                {
                    "id": "ADR-001",
                    "title": "In-process ASGI/WSGI verification",
                    "decision": "Verify Flask and FastAPI endpoints with in-process test clients instead of live daemons.",
                }
            ],
        },
        "stack": {
            "primary_language": "python",
            "framework": "flask-fastapi-react",
            "build_command": "python3 -m compileall -q src",
            "test_command": "pytest -q",
            "lint_command": "ruff check src",
            "port": 8080,
        },
        "infrastructure": {
            "deploy": "none",
            "runtime": "kubernetes",
            "runtime_profile": "remote",
            "cloud_stores": [],
            "budget_usd": 50,
        },
        "team": {
            "heartbeat_mode": "delta",
            "max_wip": 2,
            "autonomy": "standard",
            "dropped_defaults": [
                {"role": "researcher", "reason": "Scoped hillclimb tasks require no external research pod."},
                {"role": "doc-writer", "reason": "Documentation is maintained alongside code slices."},
                {"role": "investigator", "reason": "implementer reproduces defects directly in-process."},
            ],
            "build_roles": [
                {
                    "role": "lead",
                    "harness": "antigravity",
                    "model": "Gemini 4 Argon",
                    "responsibility": "Plans active issues on demand, merges verified issue branches, and stops idle pods.",
                },
                {
                    "role": "backend",
                    "harness": "antigravity",
                    "model": "Gemini 4 Argon",
                    "responsibility": "Implements Python Flask/FastAPI backend features on factory/issue-<N> branches.",
                },
                {
                    "role": "frontend",
                    "harness": "antigravity",
                    "model": "Gemini 4 Argon",
                    "responsibility": "Implements React UI and proxy integration slices with unit tests.",
                },
                {
                    "role": "reviewer",
                    "harness": "antigravity",
                    "model": "Gemini 4 Argon",
                    "responsibility": "Audits origin/<build_branch>...origin/factory/issue-<N> diffs and runs tests.",
                },
            ],
        },
        "evaluation_pyramid": {
            "l2_contract": {
                "endpoints": ["/api/health", "/api/episodes"],
                "invariants": ["JSON responses include status and request_id fields"],
            },
            "l3_domain_quality": {
                "primary_metric": "pytest_pass_rate",
                "target_threshold": 1.0,
                "probe_description": "Full backend and proxy test suite pass rate.",
            },
            "l4_safety_grounding": {
                "invariants": ["Zero hardcoded credentials or unvalidated proxy targets"],
            },
        },
        "waves": [
            {
                "wave": 1,
                "title": "Wave 1 — Core Studio Validation",
                "issues": [
                    {
                        "title": "Add structured config validation endpoint",
                        "owner_role": "backend",
                        "requirements": ["REQ-001"],
                        "acceptance_criteria": ["Returns 200 on valid config and 400 JSON error on invalid payload."],
                    }
                ],
            }
        ],
    }


def test_single_project_delta_spec_passes_schema_and_readiness_without_eval_slug_or_heartbeat_cron(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((ROOT / "config" / "factory.schema.json").read_text())
    spec = _single_project_delta_spec()

    assert scion_factory.has_eval_project(spec) is False
    assert scion_factory.wake_mode(spec) == "delta"

    # Schema accepts omitted eval_slug, eval_branch, eval_service, eval_roles, and heartbeat_cron
    jsonschema.validate(spec, schema)

    report = scion_factory.evaluate_spec_readiness(spec)
    assert report["ready"] is True, report["missing_or_weak"]
    assert report["missing_or_weak"] == []

    # normalize_team skips ALWAYS_EVAL and appends evaluator to build_roles unless dropped
    normalized, notes = scion_factory.normalize_team(spec)
    build_names = [r["role"] for r in normalized["team"]["build_roles"]]
    eval_names = [r["role"] for r in normalized["team"].get("eval_roles") or []]
    assert "evaluator" in build_names
    assert eval_names == []
    assert "eval-lead" not in build_names and "domain-evaluator" not in build_names
    eval_entry = next(r for r in normalized["team"]["build_roles"] if r["role"] == "evaluator")
    assert eval_entry["harness"] == "antigravity"
    assert eval_entry["model"] == "Gemini 4 Argon"
    assert any("evaluator" in n for n in notes)

    # Dropping evaluator explicitly suppresses auto-addition
    spec_dropped = copy.deepcopy(spec)
    spec_dropped["team"]["dropped_defaults"].append(
        {"role": "evaluator", "reason": "Reviewer runs the full check suite directly."}
    )
    norm_dropped, notes_dropped = scion_factory.normalize_team(spec_dropped)
    assert "evaluator" not in [r["role"] for r in norm_dropped["team"]["build_roles"]]
    assert not any("evaluator" in n for n in notes_dropped)

    # Single-project service mode also validates without eval_service
    service_spec = copy.deepcopy(spec)
    service_spec["infrastructure"]["deploy"] = "service"
    service_spec["infrastructure"]["serving_service"] = "bananacast-serving"
    service_spec["infrastructure"]["deployer_iam_roles"] = ["roles/run.admin"]
    service_spec["evaluation_pyramid"]["l1_health"] = {"endpoint": "/health", "checks": ["db"]}
    service_spec["evaluation_pyramid"]["l5_latency_slo"] = {"p50_ms": 50, "p95_ms": 200, "p99_ms": 400}
    jsonschema.validate(service_spec, schema)
    assert scion_factory.evaluate_spec_readiness(service_spec)["ready"] is True

    # Non-delta mode still requires heartbeat_cron; invalid heartbeat_mode fails
    non_delta = copy.deepcopy(spec)
    non_delta["team"]["heartbeat_mode"] = "direct-lead"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(non_delta, schema)
    assert scion_factory.evaluate_spec_readiness(non_delta)["ready"] is False

    bad_mode = copy.deepcopy(spec)
    bad_mode["team"]["heartbeat_mode"] = "polling-forever"
    assert scion_factory.evaluate_spec_readiness(bad_mode)["ready"] is False

    # add_role supports evaluator cleanly
    spec_file = tmp_path / "factory.yaml"
    scion_factory.save_spec(spec_dropped, spec_file)
    updated = scion_factory.add_role(spec_file, "evaluator")
    assert "evaluator" in [r["role"] for r in updated["team"]["build_roles"]]


def test_compiled_delta_kit_uses_issue_branches_direct_reviewer_handoff_agent_comment_marker_and_omits_eval_workspace(
    tmp_path,
):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((ROOT / "config" / "factory.schema.json").read_text())
    spec = _single_project_delta_spec()
    out = tmp_path / "delta-kit"

    res = scion_factory.compile_factory_boilerplate(spec, out)
    assert res["files_written"] >= 15

    # Ruling R1: saved factory.yaml in compiled kit also validates against factory.schema.json
    compiled_spec = scion_factory.load_spec(out / "factory.yaml")
    jsonschema.validate(compiled_spec, schema)

    # Single-project omissions: no eval-workspace/, no eval-lead/domain-evaluator templates, no empty Eval Project ()
    assert not (out / "eval-workspace").exists()
    assert not (out / "templates" / "eval-lead").exists()
    assert not (out / "templates" / "domain-evaluator").exists()
    assert (out / "templates" / "evaluator" / "system-prompt.md").exists()

    root_agents = (out / "AGENTS.md").read_text()
    shared_agents = (out / "templates" / "_shared" / "agents.md").read_text()
    firestore_readme = (out / "config" / "firestore" / "README.md").read_text()
    env_example = (out / "config" / "factory.env.example").read_text()

    for doc in (root_agents, shared_agents, firestore_readme):
        assert "Eval Project ()" not in doc
        assert "Evaluation Project ()" not in doc
        assert "Evaluation Team ()" not in doc
        assert "(`bananacast-hc1` & ``)" not in doc
    assert "HEARTBEAT_MODE=delta" in env_example
    assert "FACTORY_PROJECTS=bananacast-hc1\n" in env_example

    # verify-factory.sh parses cleanly and succeeds in DRY_RUN=1 without eval-workspace
    verify_sh = out / "scripts" / "verify-factory.sh"
    assert subprocess.run(["bash", "-n", str(verify_sh)], capture_output=True).returncode == 0
    verify_run = subprocess.run(
        ["bash", str(verify_sh)],
        env={"PATH": "/usr/bin:/bin", "DRY_RUN": "1"},
        capture_output=True,
        text=True,
    )
    assert verify_run.returncode == 0, verify_run.stdout + verify_run.stderr

    # Zero unrendered ${VAR} placeholders in compiled templates + render-templates.py succeeds
    placeholder_re = re.compile(r"\$\{[A-Z][A-Z0-9_]*\}")
    leftovers = [
        f"{p.relative_to(out)}: {m}"
        for p in (out / "templates").rglob("*")
        if p.is_file()
        for m in placeholder_re.findall(p.read_text())
    ]
    assert leftovers == []
    env_copy = tmp_path / "factory.env"
    env_copy.write_text(env_example)
    render_res = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "render-templates.py"),
            "--env",
            str(env_copy),
            "--src",
            str(out / "templates"),
            "--out",
            str(tmp_path / "rendered"),
        ],
        capture_output=True,
        text=True,
    )
    assert render_res.returncode == 0, render_res.stdout + render_res.stderr

    # Evaluator scion-agent.yaml uses antigravity-factory + Gemini 4 Argon + global endpoint
    eval_yaml = (out / "templates" / "evaluator" / "scion-agent.yaml").read_text()
    assert "default_harness_config: antigravity-factory" in eval_yaml
    assert 'model: "Gemini 4 Argon"' in eval_yaml
    assert 'GOOGLE_CLOUD_LOCATION: "global"' in eval_yaml

    # Ephemeral delta prompt checks across shared, writer, reviewer, evaluator, and lead prompts
    backend_prompt = (out / "templates" / "backend" / "system-prompt.md").read_text()
    reviewer_prompt = (out / "templates" / "reviewer" / "system-prompt.md").read_text()
    evaluator_prompt = (out / "templates" / "evaluator" / "system-prompt.md").read_text()
    lead_prompt = (out / "templates" / "lead" / "system-prompt.md").read_text()

    # 1. Writers: factory/issue-<N> from origin/<build_branch>, direct push, no .patch/commit-slot, direct reviewer msg
    for text in (shared_agents, backend_prompt):
        assert "factory/issue-" in text
        assert "origin/factory/gke-hc1" in text
        assert "origin/factory/issue-" in text
        assert "reviewer" in text
    assert "only when `lead` gives you the commit slot" not in backend_prompt
    assert ".patch" in shared_agents  # explicitly forbids .patch scratchpad handoffs

    # 2. Reviewer: audits origin/<build_branch>...origin/factory/issue-<N>, 1st FAIL -> writer directly, PASS/2nd FAIL -> report + lead
    assert "origin/factory/gke-hc1...origin/factory/issue-" in reviewer_prompt
    assert "/scion-volumes/scratchpad/reports/" in reviewer_prompt
    assert "-review.md" in reviewer_prompt
    assert "first" in reviewer_prompt.lower() and "fail" in reviewer_prompt.lower()
    assert "Never start or message other agents" not in reviewer_prompt

    # 3. Evaluator: L1-L5 code pyramid in-project on factory/gke-hc1
    assert "factory/gke-hc1" in evaluator_prompt
    assert "L1" in evaluator_prompt and "L5" in evaluator_prompt
    assert "PASS" in evaluator_prompt and "FAIL" in evaluator_prompt

    # 4. Lead: on-demand specialists only, merge & delete factory/issue-<N>, STATE.json, scion stop, scion-agent marker before status:po-decision, ignore bare state-change COMPLETED
    assert "Start each teammate that is missing" not in lead_prompt
    assert "unused" in lead_prompt.lower() or "only the specialists" in lead_prompt.lower() or "only the" in lead_prompt.lower()
    assert "/scion-volumes/scratchpad/STATE.json" in lead_prompt
    assert "scion stop" in lead_prompt
    assert "--delete factory/issue-" in lead_prompt or "delete" in lead_prompt.lower()
    assert "<!-- scion-agent:" in lead_prompt
    assert "before" in lead_prompt.lower() and "status:po-decision" in lead_prompt
    assert "state-change" in lead_prompt and "COMPLETED" in lead_prompt

    # 5. Knowledge-plane shared-dir path first + conditional kp-check.py on K8s/delta
    for text in (shared_agents, lead_prompt, backend_prompt, reviewer_prompt, evaluator_prompt):
        assert "/scion-volumes/knowledge-plane/INDEX.md" in text
        assert "/workspace/knowledge-plane/INDEX.md" in text
    assert "writable" in shared_agents and "kp-check.py" in shared_agents

    # 6. Specialist fresh-pod resume fallback checking STATE.json and briefs/* before going idle
    for sp_prompt in (backend_prompt, reviewer_prompt, evaluator_prompt):
        loop_sec = sp_prompt.split("## Operating Loop\n", 1)[1]
        assert "/scion-volumes/scratchpad/STATE.json" in loop_sec
        assert "/scion-volumes/scratchpad/briefs/" in loop_sec
        assert "origin/factory/issue-" in loop_sec

    # 7. Delta daily digest rule creates missing digest issue directly instead of opening status:po-decision
    digest_sec = lead_prompt.split("## Daily digest\n", 1)[1].split("\n## ", 1)[0]
    assert "gh issue create" in digest_sec
    assert "--label digest --label factory:bananacast-hc1" in digest_sec
    assert "scripts/seed-github-waves.sh" not in digest_sec


def test_legacy_two_project_spec_compiles_unchanged(tmp_path):
    starter = scion_factory.load_spec(ROOT / "presets" / "starter-service.factory.yaml")
    assert scion_factory.has_eval_project(starter) is True
    assert scion_factory.wake_mode(starter) == "direct-lead"

    out = tmp_path / "legacy-kit"
    scion_factory.compile_factory_boilerplate(starter, out)

    assert (out / "eval-workspace" / "tools" / "factory_store.py").is_file()
    assert (out / "eval-workspace" / "golden" / "README.md").is_file()
    assert (out / "templates" / "eval-lead" / "system-prompt.md").is_file()
    assert (out / "templates" / "domain-evaluator" / "system-prompt.md").is_file()
    assert not (out / "templates" / "evaluator").exists()

    root_agents = (out / "AGENTS.md").read_text()
    shared_agents = (out / "templates" / "_shared" / "agents.md").read_text()
    lead_prompt = (out / "templates" / "lead" / "system-prompt.md").read_text()
    backend_prompt = (out / "templates" / "backend" / "system-prompt.md").read_text()
    reviewer_prompt = (out / "templates" / "reviewer" / "system-prompt.md").read_text()

    assert "Evaluation Team (`pulse-ledger-eval`)" in root_agents
    assert "Eval Project (`pulse-ledger-eval`)" in shared_agents
    assert "Start each teammate that is missing" in lead_prompt
    assert "commit and push on branch `main` only when `lead` gives you the commit slot" in backend_prompt
    assert "Never start or message other agents" in reviewer_prompt
