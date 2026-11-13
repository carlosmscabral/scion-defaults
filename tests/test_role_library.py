"""Tests for scripts/role_library.py."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import role_library  # noqa: E402


def test_every_always_role_and_catalog_role_has_a_protocol():
    for role in role_library.ALWAYS_BUILD + role_library.ALWAYS_EVAL:
        assert role_library.ROLES[role]["kind"] == "always"
    for role, d in role_library.ROLES.items():
        assert d["protocol"].strip(), role
        assert d["team"] in ("build", "eval")


def test_specialist_protocols_carry_their_guardrails():
    assert "never invent a citation" in role_library.role_protocol("researcher").lower()
    inv = role_library.role_protocol("investigator").lower()
    assert "reproduce first" in inv and "file:line" in inv and "don't fix" in inv
    assert "docs follow code" in role_library.role_protocol("doc-writer").lower()
    assert "kp-check.py" in role_library.role_protocol("doc-writer")
    for r in ("reviewer", "security-auditor", "performance-analyst"):
        assert "never start or message other agents" in role_library.role_protocol(r).lower()


def test_default_harnesses():
    assert role_library.library_role_entry("lead")["harness"] == "claude"
    assert role_library.library_role_entry("kaizen-analyst")["harness"] == "claude"
    assert all(
        role_library.library_role_entry(r)["harness"] == "antigravity"
        for r in role_library.ROLES
        if r not in ("lead", "kaizen-analyst")
    )


def test_service_roles_are_marked():
    assert {r for r, d in role_library.ROLES.items() if d["needs_service"]} == {
        "deployer",
        "eval-deployer",
        "contract-tester",
        "latency-prober",
        "ui-auditor",
    }


def test_unknown_role():
    assert role_library.role_protocol("chef") is None
    with pytest.raises(KeyError):
        role_library.library_role_entry("chef")


def test_code_pyramid_tools():
    tools = role_library.CODE_PYRAMID_TOOLS
    assert tools["go"] == {"l2": "apidiff", "l4": "govulncheck ./..."}
    assert tools["python"] == {"l2": "griffe check", "l4": "pip-audit"}
    assert tools["typescript"] == {"l2": "api-extractor run", "l4": "npm audit --omit=dev"}
    assert tools["rust"] == {"l2": "cargo semver-checks", "l4": "cargo audit"}


def test_library_role_entry_contents():
    entry = role_library.library_role_entry("researcher")
    assert entry == {
        "role": "researcher",
        "harness": "antigravity",
        "model": "Gemini 4 Argon (High)",
        "responsibility": role_library.ROLES["researcher"]["responsibility"],
    }
    lead_entry = role_library.library_role_entry("lead")
    assert lead_entry["model"] == "opus"


def test_role_def_structure_and_counts():
    assert len(role_library.ALWAYS_BUILD) == 6
    assert len(role_library.ALWAYS_EVAL) == 2
    assert len(role_library.ROLES) == 27
    for r, d in role_library.ROLES.items():
        assert "team" in d
        assert "kind" in d
        assert "default_kind" in d
        assert "harness" in d
        assert "model" in d
        assert "responsibility" in d
        assert "protocol" in d
        assert "needs_service" in d


def test_legacy_template_operational_details_ported_to_role_library():
    ui = role_library.ROLES["ui-craftsman"]
    assert ui["team"] == "eval"
    assert ui["kind"] == "catalog"
    assert "impeccable" in ui["protocol"].lower()
    assert "n/a" in ui["protocol"].lower()

    for r in ("deployer", "eval-deployer"):
        proto = role_library.role_protocol(r)
        assert proto is not None
        for needle in ("latestReadyRevisionName", "archive", "/tmp/deploy-", "--to-latest", "--update-secrets", "--to-revisions"):
            assert needle in proto, (r, needle)

    eval_lead_proto = role_library.role_protocol("eval-lead")
    assert eval_lead_proto is not None
    assert "NULL" in eval_lead_proto and '"n/a"' in eval_lead_proto


def test_investigator_and_auditor_protocols_forbid_untimed_server_daemons():
    for r in ("investigator", "security-auditor", "test-engineer", "domain-evaluator", "safety-evaluator"):
        proto = role_library.role_protocol(r, deploy="none").lower()
        assert "timeout" in proto or "testclient" in proto or "test_client" in proto, f"{r} protocol missing timeout/TestClient guardrail"


def test_migration_specialist_covers_database_and_terraform_iac_and_ci_workflows():
    resp = role_library.ROLES["migration-specialist"]["responsibility"].lower()
    assert "migration" in resp and ("terraform" in resp or "infrastructure" in resp or "iac" in resp)
    proto = role_library.role_protocol("migration-specialist")
    assert proto is not None
    low = proto.lower()
    assert "terraform fmt" in low
    assert "terraform validate" in low
    assert "tfstate" in low
    assert "default = false" in low or "opt-in" in low
    assert "sha" in low and "workflow" in low
    assert "never run `terraform apply`" in low or "never run terraform apply" in low


def test_python_and_multi_app_test_protocols_guard_optional_cross_app_imports():
    for proto in (
        role_library.role_protocol("backend"),
        role_library.role_protocol("backend", deploy="none"),
        role_library.role_protocol("test-engineer"),
        role_library.role_protocol("reviewer"),
    ):
        assert proto is not None
        assert "importorskip" in proto, f"missing pytest.importorskip guidance in: {proto[:80]}"


def test_lead_protocol_covers_coupled_co_commits_and_pending_patch_escalation():
    proto = role_library.role_protocol("lead")
    assert proto is not None
    assert "co-commit" in proto.lower()
    assert "/scion-volumes/scratchpad/pending-" in proto
    assert "decision rights" in proto.lower()
    assert "status:po-decision" not in proto  # VCS-agnostic: non-Git kits escalate via message, Git kits via PO inbox


def test_evaluator_role_in_library_supports_service_and_deploy_none():
    entry = role_library.library_role_entry("evaluator")
    assert entry["role"] == "evaluator"
    assert entry["harness"] == "antigravity"
    assert entry["model"] == "Gemini 4 Argon"
    assert entry["responsibility"].strip()

    role_def = role_library.ROLES["evaluator"]
    assert role_def["team"] == "build"
    assert role_def["kind"] == "catalog"
    assert role_def["default_kind"] == "evaluator"
    assert role_def["needs_service"] is False

    proto_service = role_library.role_protocol("evaluator", deploy="service")
    proto_none = role_library.role_protocol("evaluator", deploy="none")
    assert proto_service is not None and proto_none is not None
    assert proto_service != proto_none

    for proto in (proto_service, proto_none):
        low = proto.lower()
        assert "l1" in low and "l5" in low
        assert "evidence" in low and "issue" in low
        assert "pass" in low and "fail" in low
        assert "lead" in low
        assert "timeout" in low or "testclient" in low or "test_client" in low

    assert "<build_branch>" in proto_none
    for forbidden in ("user journeys", "payload validation", "cloud run", "health endpoint", "latency", "for the service"):
        assert forbidden not in proto_none.lower(), forbidden

