"""Tests for optional 3-tier domain sub-coordinators (hierarchy validation, compilation, hot-upgrade, and CLI)."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import hot_upgrade  # noqa: E402
import kaizen_policy  # noqa: E402
import role_library  # noqa: E402
import scion_factory  # noqa: E402
import skill_curation  # noqa: E402
import topology_audit  # noqa: E402
import verification_policy  # noqa: E402
from test_provision_factory import write_gke_factory_env  # noqa: E402

pytest_plugins = ["test_hub_client"]

PRESET = ROOT / "presets" / "starter-service.factory.yaml"


def _section(prompt: str, heading: str) -> str:
    """Extract a markdown `## <heading>` section from a compiled system prompt."""
    body = prompt.split(f"## {heading}\n", 1)[1]
    return body.split("\n## ", 1)[0]


def _build_three_domain_spec() -> dict:
    """Construct a valid 3-tier multi-domain spec (root lead -> 3 sub-coordinators -> domain specialists)."""
    spec = copy.deepcopy(scion_factory.load_spec(PRESET))
    spec["infrastructure"]["runtime"] = "kubernetes"
    spec["team"]["strict_scion_delegation"] = True
    spec["team"]["build_roles"] = [
        dict(role_library.library_role_entry("lead"), default=True),
        {
            **role_library.library_role_entry("sub-coordinator"),
            "role": "backend-sub-coordinator",
            "focus": {"paths": ["apps/platform/services/backend"]},
        },
        {
            **role_library.library_role_entry("sub-coordinator"),
            "role": "data-api-sub-coordinator",
            "harness": "claude",
            "model": "opus",
            "focus": {"paths": ["apps/platform/services/data-api"]},
        },
        {
            **role_library.library_role_entry("sub-coordinator"),
            "role": "frontend-sub-coordinator",
            "focus": {"paths": ["apps/platform/services/frontend"]},
        },
        {
            **role_library.library_role_entry("backend"),
            "role": "span-builder",
            "default_kind": "implementer",
            "reports_to": "backend-sub-coordinator",
            "focus": {"paths": ["apps/platform/services/backend/span"]},
        },
        {
            **role_library.library_role_entry("backend"),
            "role": "api-builder",
            "default_kind": "implementer",
            "reports_to": "data-api-sub-coordinator",
            "focus": {"paths": ["apps/platform/services/data-api/api"]},
        },
        {
            **role_library.library_role_entry("reviewer"),
            "role": "data-reviewer",
            "default_kind": "reviewer",
            "harness": "claude",
            "model": "opus",
            "reports_to": "data-api-sub-coordinator",
            "focus": {"paths": ["apps/platform/services/data-api"]},
        },
        {
            **role_library.library_role_entry("security-auditor"),
            "role": "iap-security",
            "default_kind": "security-auditor",
            "harness": "claude",
            "model": "opus",
            "reports_to": "data-api-sub-coordinator",
            "focus": {"paths": ["apps/platform/services/data-api/auth"]},
        },
        {
            **role_library.library_role_entry("frontend"),
            "role": "ui-builder",
            "default_kind": "frontend",
            "reports_to": "frontend-sub-coordinator",
            "focus": {"paths": ["apps/platform/services/frontend/ui"]},
        },
        {
            **role_library.library_role_entry("reviewer"),
            "harness": "claude",
            "model": "opus",
            "default": True,
        },
        dict(role_library.library_role_entry("researcher"), default=True),
        dict(role_library.library_role_entry("doc-writer"), default=True),
        dict(role_library.library_role_entry("investigator"), default=True),
        role_library.library_role_entry("deployer"),
        role_library.library_role_entry("kaizen-analyst"),
    ]
    return spec


def test_default_flat_factory_remains_unchanged(tmp_path: Path):
    """Ensure standard specs without sub-coordinators compile flat 2-tier chains with zero regressions."""
    spec = scion_factory.load_spec(PRESET)
    out = tmp_path / "flat-kit"
    scion_factory.compile_factory_boilerplate(spec, out)
    lead = (out / "templates/lead/system-prompt.md").read_text(encoding="utf-8")
    assert "### Domain sub-coordinators" not in lead
    assert "scion start backend -t backend" in lead


def test_three_tier_sub_coordinator_tree_aligns_all_prompt_sections(tmp_path: Path):
    """Verify Role protocol, Chain of command, Sizing, and Operating Loop agree across all 3 tiers."""
    for mode in ("direct-lead", "delta"):
        spec = _build_three_domain_spec()
        spec["team"]["heartbeat_mode"] = mode
        spec["team"]["reviewer_replicas"] = 2
        if mode == "direct-lead":
            spec["team"]["heartbeat_cron"] = "0 */6 * * *"
        spec["monorepo"] = {"subdir": "apps/platform", "agent_prefix": "acme"}

        ready = scion_factory.evaluate_spec_readiness(spec)
        assert ready["ready"] is True, ready["missing_or_weak"]

        out = tmp_path / f"kit-{mode}"
        scion_factory.compile_factory_boilerplate(spec, out)
        skill_curation.apply_strict_delegation(spec, out)

        lead_prompt = (out / "templates/lead/system-prompt.md").read_text(encoding="utf-8")
        data_sub_prompt = (out / "templates/data-api-sub-coordinator/system-prompt.md").read_text(encoding="utf-8")
        api_builder_prompt = (out / "templates/api-builder/system-prompt.md").read_text(encoding="utf-8")
        data_rev_prompt = (out / "templates/data-reviewer/system-prompt.md").read_text(encoding="utf-8")
        deployer_prompt = (out / "templates/deployer/system-prompt.md").read_text(encoding="utf-8")

        # 1. Root lead Sizing never dispatches subtree leaves directly and routes UI -> frontend-sub-coordinator
        sizing = _section(lead_prompt, "Sizing an issue")
        sizing_body = sizing.split("### Domain sub-coordinators", 1)[0]
        assert "UI → `acme-frontend-sub-coordinator`" in sizing_body
        for leaf_slug in ("acme-api-builder", "acme-ui-builder", "acme-iap-security", "acme-data-reviewer"):
            assert leaf_slug not in sizing_body
        assert "`acme-reviewer-2`" in sizing_body
        assert "scion start acme-data-api-sub-coordinator -t data-api-sub-coordinator" in lead_prompt
        assert "scion start acme-api-builder" not in lead_prompt

        # 2. Sub-coordinator Operating Loop is dedicated orchestration (never <N>-eval.md)
        sub_loop = _section(data_sub_prompt, "Operating Loop")
        assert "<N>-eval.md" not in sub_loop
        assert "`acme-lead`" in sub_loop
        assert "scion start acme-api-builder -t api-builder" in data_sub_prompt
        assert "scion start acme-ui-builder" not in data_sub_prompt

        # 3. Leaf Operating Loop & Role protocol point to sub-coordinator + subtree reviewer, never root lead as manager
        leaf_loop = _section(api_builder_prompt, "Operating Loop")
        leaf_proto = _section(api_builder_prompt, "Role protocol")
        assert "`acme-data-api-sub-coordinator`'s message" in leaf_loop
        assert "only to `acme-lead`" not in leaf_loop
        assert "without waking `acme-lead`" not in leaf_loop
        assert "without waking `acme-lead`" not in leaf_proto
        if mode == "delta":
            assert "`acme-data-reviewer`" in leaf_proto
            assert "`acme-data-reviewer`" in leaf_loop
            rev_proto = _section(data_rev_prompt, "Role protocol")
            rev_loop = _section(data_rev_prompt, "Operating Loop")
            assert "message `acme-data-api-sub-coordinator` once" in rev_proto
            assert "wake `acme-data-api-sub-coordinator` once" in rev_loop
        else:
            assert "requested on your behalf by `acme-data-api-sub-coordinator`" in leaf_loop
            assert "only to `acme-data-api-sub-coordinator`" in leaf_loop

        # 4. Deployer stays root-scoped, Claude sub-coordinator denies Agent/Task, Kaizen excludes sub-coordinators
        assert "`acme-lead`" in deployer_prompt
        sub_claude_settings = json.loads(
            (out / "templates/data-api-sub-coordinator/home/.claude/settings.json").read_text(encoding="utf-8")
        )
        assert "Agent" in sub_claude_settings["permissions"]["deny"]
        kz = kaizen_policy.build_kaizen_manifest(spec)
        assert "acme-data-api-sub-coordinator" in kz["sub_coordinator_roles"]
        assert "acme-data-api-sub-coordinator" not in kz["producer_roles"]


def test_normalize_team_and_replicas_preserve_root_roles_when_subtrees_exist():
    """Ensure subtree reviewers/implementers never suppress root always-roles or root replica expansion."""
    spec = _build_three_domain_spec()
    spec["team"]["build_roles"] = [r for r in spec["team"]["build_roles"] if r["role"] != "reviewer"]
    spec["team"]["reviewer_replicas"] = 2
    norm, notes = scion_factory.normalize_team(spec)
    root_revs = [
        r["role"] for r in norm["team"]["build_roles"]
        if verification_policy.role_kind(r) == "reviewer" and not r.get("reports_to")
    ]
    assert root_revs == ["reviewer", "reviewer-2"], (root_revs, notes)


def test_hierarchy_validation_rejects_depth_overflow_empty_subtrees_and_root_only_kinds():
    """Verify readiness and topology_audit reject 4-tier nesting, empty sub-coordinators, and nested leads/evaluators."""
    spec_empty = _build_three_domain_spec()
    spec_empty["team"]["build_roles"].append({
        **role_library.library_role_entry("sub-coordinator"),
        "role": "empty-sub",
        "focus": {"paths": ["apps/platform/services/empty"]},
    })
    rep_empty = scion_factory.evaluate_spec_readiness(spec_empty)
    assert not rep_empty["ready"]
    assert any("empty-sub" in e and "zero specialists" in e for e in rep_empty["missing_or_weak"])
    aud_empty = topology_audit.audit(spec_empty)
    assert aud_empty["ok"] is False and aud_empty["errors"]

    for bad_kind, bad_role in (("lead", "shadow-lead"), ("evaluator", "wave-gate")):
        spec_bad = _build_three_domain_spec()
        spec_bad["team"]["build_roles"].append({
            **role_library.library_role_entry(bad_kind),
            "role": bad_role,
            "default_kind": bad_kind,
            "reports_to": "backend-sub-coordinator",
        })
        rep_bad = scion_factory.evaluate_spec_readiness(spec_bad)
        assert not rep_bad["ready"], bad_kind

    spec_docker = _build_three_domain_spec()
    spec_docker["infrastructure"]["runtime"] = "docker"
    aud_docker = topology_audit.audit(spec_docker)
    assert any("runtime: docker" in w for w in aud_docker["warnings"])


def test_hot_upgrade_routes_subtree_child_spawn_to_sub_coordinator(fake_hub, tmp_path: Path):
    """Verify hot_upgrade sends [HOT-UPGRADE STAFFING] for a reports_to child to its running sub-coordinator."""
    spec = _build_three_domain_spec()
    spec["project"]["slug"] = "starter-service"
    kit_dir = tmp_path / "kit"
    scion_factory.compile_factory_boilerplate(spec, kit_dir)

    pid = "pid-subcoord"
    fake_hub.projects_by_slug["starter-service"] = {"id": pid, "slug": "starter-service"}
    fake_hub.project_settings[pid] = {}
    fake_hub.agents_by_project[pid] = ["lead", "data-api-sub-coordinator"]
    fake_hub.seed_agent(pid, slug="lead", phase="running")
    fake_hub.seed_agent(pid, slug="data-api-sub-coordinator", phase="running")
    env_file = write_gke_factory_env(tmp_path, fake_hub.url)
    fake_hub.requests.clear()

    rep = hot_upgrade.run_hot_upgrade(
        project_slug="starter-service",
        templates_dir=kit_dir / "templates",
        env_file=env_file,
        roles=["api-builder"],
        spawn_specs=[("api-builder", "api-builder")],
        apply=True,
    )
    assert rep["lead_notified"] is True
    msg_posts = [r for r in fake_hub.requests if r["method"] == "POST" and r["path"].endswith("/message")]
    assert len(msg_posts) == 1
    assert msg_posts[0]["path"] == f"/api/v1/projects/{pid}/agents/data-api-sub-coordinator/message"
    body_obj = json.loads(msg_posts[0]["body"].decode("utf-8")) if isinstance(msg_posts[0]["body"], bytes) else msg_posts[0]["body"]
    assert "scion start api-builder -t api-builder" in body_obj["message"]


def test_add_role_cli_preserves_focus_commands_and_rejects_alias_collisions(tmp_path: Path):
    """Verify add-role --paths preserves stack commands for subtree builders and rejects conflicting --as slugs."""
    spec_file = tmp_path / "factory.yaml"
    spec_file.write_text(PRESET.read_text(encoding="utf-8"), encoding="utf-8")

    subprocess.run(
        [sys.executable, "scripts/scion_factory.py", "add-role", str(spec_file),
         "--role", "sub-coordinator", "--as", "payments-sub-coordinator", "--paths", "services/payments"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    r2 = subprocess.run(
        [sys.executable, "scripts/scion_factory.py", "add-role", str(spec_file),
         "--role", "backend", "--as", "ledger-builder",
         "--reports-to", "payments-sub-coordinator", "--paths", "services/payments/ledger"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    assert json.loads(r2.stdout)["ready"] is True
    saved = scion_factory.load_spec(spec_file)
    ledger = next(r for r in saved["team"]["build_roles"] if r["role"] == "ledger-builder")
    assert ledger["focus"]["paths"] == ["services/payments/ledger"]
    assert saved["stack"]["test_command"] in ledger["focus"]["commands"]

    collision = subprocess.run(
        [sys.executable, "scripts/scion_factory.py", "add-role", str(spec_file),
         "--role", "backend", "--as", "reviewer"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert collision.returncode == 2
    _ = pytest
