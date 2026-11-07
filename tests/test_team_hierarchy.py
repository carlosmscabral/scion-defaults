"""Chain of command: the PO starts only the two leads; each lead starts its own team as its Scion children.

Scion records the creator of every agent (ancestry root -> parent -> child) and the Hub's tree view shows it; the
Hub also sends an agent's notifications to its creator. A lead that starts its team is therefore on top of the tree
and hears about its team, and every team member reports to it (Kubernetes monorepo run, 2026-10-23).
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import scion_factory  # noqa: E402

PRESET = ROOT / "presets" / "starter-service.factory.yaml"


def _compile(tmp_path, **monorepo):
    spec = scion_factory.load_spec(PRESET)
    if monorepo:
        spec["monorepo"] = monorepo
    out = tmp_path / "kit"
    scion_factory.compile_factory_boilerplate(spec, out)
    return spec, out


def _prompt(out, role):
    return (out / "templates" / role / "system-prompt.md").read_text()


@pytest.fixture
def mono(tmp_path):
    return _compile(tmp_path, subdir="pkg/kubelet", agent_prefix="kubelet")


def test_build_lead_starts_each_teammate_as_its_child(mono):
    spec, out = mono
    lead = _prompt(out, "lead")
    assert "## Chain of command" in lead
    for r in spec["team"]["build_roles"]:
        if r["role"] == "lead":
            continue
        assert f'scion start kubelet-{r["role"]} -t {r["role"]} --harness-auth vertex-ai' in lead
    assert "scion start kubelet-lead" not in lead
    assert "scion resume" in lead


def test_eval_lead_starts_the_evaluation_team(mono):
    spec, out = mono
    eval_lead = _prompt(out, "eval-lead")
    for r in spec["team"]["eval_roles"]:
        if r["role"] == "eval-lead":
            continue
        assert f'scion start kubelet-{r["role"]} -t {r["role"]} --harness-auth vertex-ai' in eval_lead
    # The evaluation team never starts or commands build agents.
    assert "scion start kubelet-backend" not in eval_lead


def test_team_members_report_to_their_lead_and_start_no_agents(mono):
    spec, out = mono
    for r in spec["team"]["build_roles"]:
        if r["role"] == "lead":
            continue
        text = _prompt(out, r["role"])
        assert "You report to `kubelet-lead`" in text
        assert "scion start" not in text
    for r in spec["team"]["eval_roles"]:
        if r["role"] == "eval-lead":
            continue
        assert "You report to `kubelet-eval-lead`" in _prompt(out, r["role"])


def test_single_repo_kit_uses_plain_role_names(tmp_path):
    spec, out = _compile(tmp_path)
    lead = _prompt(out, "lead")
    mates = [r["role"] for r in spec["team"]["build_roles"] if r["role"] != "lead"]
    assert mates
    assert f"scion start {mates[0]} -t {mates[0]} --harness-auth vertex-ai" in lead
    assert "You report to `lead`" in _prompt(out, mates[0])


def test_readme_starts_only_the_two_leads():
    readme = (ROOT / "README.md").read_text()
    step8 = readme.split("### Step 8", 1)[1].split("\n### ", 1)[0]
    assert 'echo "$BUILD_AGENTS"' not in step8
    assert '-t lead --project="$BUILD_PROJECT"' in step8
    assert '-t eval-lead --project="$EVAL_PROJECT"' in step8


def test_repo_lead_templates_start_their_team():
    for role in ("lead", "eval-lead"):
        text = (ROOT / "templates" / role / "system-prompt.md").read_text()
        assert "scion start" in text, role
        assert "If the admin created your team" not in text, role


def test_only_the_po_closes_issues(mono):
    """kubelet-lead closed #1 itself while the other leads left theirs open: the kit must say who accepts."""
    _, out = mono
    shared = (out / "templates" / "_shared" / "agents.md").read_text()
    assert "The PO accepts work by closing the issue" in shared
    assert "never close an issue" in shared


def test_renamed_build_lead_is_detected_by_kind_when_not_first(tmp_path):
    spec = scion_factory.load_spec(PRESET)
    backend_role = dict(scion_factory.role_library.library_role_entry("backend"), default=True)
    tech_lead_role = dict(scion_factory.role_library.library_role_entry("lead"), default=True)
    tech_lead_role["role"] = "tech-lead"
    tech_lead_role["default_kind"] = "lead"
    reviewer_role = dict(scion_factory.role_library.library_role_entry("reviewer"), default=True)
    spec["team"]["build_roles"] = [backend_role, tech_lead_role, reviewer_role]
    for issue in spec.get("issues") or []:
        if issue.get("owner_role") == "lead":
            issue["owner_role"] = "tech-lead"

    out = tmp_path / "kit"
    scion_factory.compile_factory_boilerplate(spec, out)

    assert not (out / "templates" / "lead").exists()
    tech_lead_prompt = (out / "templates" / "tech-lead" / "system-prompt.md").read_text()
    assert "## Chain of command" in tech_lead_prompt
    assert "You lead this team and you start it" in tech_lead_prompt
    assert "| Teammate | Start it with |" in tech_lead_prompt
    assert "scion start backend -t backend" in tech_lead_prompt

    backend_prompt = (out / "templates" / "backend" / "system-prompt.md").read_text()
    assert "You report to `tech-lead`" in backend_prompt


def test_renamed_eval_lead_is_detected_by_kind_when_not_first(tmp_path):
    spec = scion_factory.load_spec(PRESET)
    evaluator_role = dict(scion_factory.role_library.library_role_entry("domain-evaluator"), default=True)
    eval_principal_role = dict(scion_factory.role_library.library_role_entry("eval-lead"), default=True)
    eval_principal_role["role"] = "eval-principal"
    eval_principal_role["default_kind"] = "eval-lead"
    spec["team"]["eval_roles"] = [evaluator_role, eval_principal_role]

    out = tmp_path / "kit"
    scion_factory.compile_factory_boilerplate(spec, out)

    assert not (out / "templates" / "eval-lead").exists()
    eval_principal_prompt = (out / "templates" / "eval-principal" / "system-prompt.md").read_text()
    assert "## Chain of command" in eval_principal_prompt
    assert "You lead this team and you start it" in eval_principal_prompt
    assert "| Teammate | Start it with |" in eval_principal_prompt
    assert "scion start domain-evaluator -t domain-evaluator" in eval_principal_prompt

    evaluator_prompt = (out / "templates" / "domain-evaluator" / "system-prompt.md").read_text()
    assert "You report to `eval-principal`" in evaluator_prompt


def test_deployer_start_uses_injected_sa_env_var_and_docker_assign_omits_vertex_harness_auth(tmp_path):
    """Verify compiled leads pass ${SCION_DEPLOYER_SA_ID} and omit --harness-auth vertex-ai on Docker assign mode."""
    spec = scion_factory.load_spec(PRESET)
    out_default = tmp_path / "kit-default"
    scion_factory.compile_factory_boilerplate(spec, out_default)
    lead_default = _prompt(out_default, "lead")
    eval_lead_default = _prompt(out_default, "eval-lead")
    assert '${SCION_DEPLOYER_SA_ID:+--service-account "$SCION_DEPLOYER_SA_ID"}' in lead_default
    assert '${SCION_DEPLOYER_SA_ID:+--service-account "$SCION_DEPLOYER_SA_ID"}' in eval_lead_default
    assert "--service-account <ID>" not in lead_default
    assert "--harness-auth vertex-ai" in lead_default

    # Single-VM Docker with GCP_IDENTITY_DEFAULT=assign via --env file omits --harness-auth vertex-ai
    spec["infrastructure"]["runtime"] = "docker"
    env_file = tmp_path / "factory.env"
    env_file.write_text("GCP_IDENTITY_DEFAULT=assign\n")
    out_assign = tmp_path / "kit-assign"
    scion_factory.compile_factory_boilerplate(spec, out_assign, env_file=env_file)
    lead_assign = _prompt(out_assign, "lead")
    assert "--harness-auth vertex-ai" not in lead_assign
    assert (
        'scion start deployer -t deployer ${SCION_DEPLOYER_SA_ID:+--service-account "$SCION_DEPLOYER_SA_ID"}'
        in lead_assign
    )

