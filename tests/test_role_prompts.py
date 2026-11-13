"""Role prompts: each role's protocol from the library, its repo focus, file briefs and issue sizing (spec §5.4, §6).

The lead sizes every issue against the roles present in its roster and briefs teammates through files in the
scratchpad (never inline: shell metacharacters and long prompts break). A team member reads the brief file named in
the message, and a task is done only when its result is pushed or sent in a message.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import scion_factory  # noqa: E402

PRESET = ROOT / "presets" / "starter-service.factory.yaml"


def _prompt(out, role):
    return (out / "templates" / role / "system-prompt.md").read_text()


@pytest.fixture
def spec():
    return scion_factory.load_spec(PRESET)


@pytest.fixture
def kit(tmp_path, spec):
    out = tmp_path / "kit"
    scion_factory.compile_factory_boilerplate(spec, out)
    return out


def test_specialist_prompts_carry_their_protocol(kit):
    assert "## Role protocol" in _prompt(kit, "investigator")
    assert "Reproduce first" in _prompt(kit, "investigator")
    assert "Docs follow code" in _prompt(kit, "doc-writer")


def test_focus_lists_commands_and_docs(kit, spec):
    investigator = _prompt(kit, "investigator")
    assert "## Focus" in investigator
    assert spec["stack"]["test_command"] in investigator.split("## Focus", 1)[1]
    assert "knowledge-plane/INDEX.md" in _prompt(kit, "doc-writer").split("## Focus", 1)[1]


def _section(text, header):
    return text.split(f"## {header}\n", 1)[1].split("\n## ", 1)[0]


def test_lead_sizes_with_only_the_roles_present(kit):
    lead = _prompt(kit, "lead")
    assert "## Sizing an issue" in lead
    assert "investigator" in _section(lead, "Sizing an issue")
    assert "security-auditor" not in lead          # not in this roster


def test_briefs_are_files_in_the_scratchpad(kit):
    lead = _prompt(kit, "lead")
    assert "## Briefs" in lead
    assert "/scion-volumes/scratchpad/briefs/" in lead
    assert "Read the brief file named in the message" in _prompt(kit, "backend")


def test_every_team_member_has_the_durability_rule(kit, spec):
    for r in spec["team"]["build_roles"] + spec["team"]["eval_roles"]:
        if r["role"] in ("lead", "eval-lead"):
            continue
        assert "done only when its result is pushed or sent in a message" in _prompt(kit, r["role"]), r["role"]
    assert "done only when its result is pushed or sent in a message" in _prompt(kit, "researcher")


def test_unknown_role_gets_a_generic_prompt(tmp_path, spec):
    spec["team"]["build_roles"].append(
        {"role": "data-wrangler", "harness": "antigravity", "model": "m", "responsibility": "Cleans CSVs"})
    out = tmp_path / "kit"
    scion_factory.compile_factory_boilerplate(spec, out)
    text = _prompt(out, "data-wrangler")
    assert "Cleans CSVs" in text
    assert "## Chain of command" in text
    assert "## Role protocol" not in text


def test_role_focus_defaults_and_explicit_focus_wins(spec):
    stack = spec["stack"]
    backend = scion_factory.role_focus(spec, {"role": "backend"})
    assert backend["paths"] == ["."]
    assert backend["commands"][:2] == [stack["build_command"], stack["test_command"]]
    researcher = scion_factory.role_focus(spec, {"role": "researcher"})
    assert researcher["docs"][-1] == "/workspace/knowledge-plane/INDEX.md"
    assert researcher["commands"] == []
    assert scion_factory.role_focus(spec, {"role": "reviewer"}) == {"paths": ["."], "commands": [], "docs": []}

    explicit = scion_factory.role_focus(spec, {"role": "backend", "focus": {"paths": ["api/"]}})
    assert explicit == {"paths": ["api/"], "commands": [], "docs": []}


def test_role_focus_in_a_monorepo_uses_the_product_folder(spec):
    spec["monorepo"] = {"subdir": "pkg/kubelet", "agent_prefix": "kubelet"}
    spec["knowledge_plane"]["sources"]["design"] = ["ARCHITECTURE.md"]
    doc = scion_factory.role_focus(spec, {"role": "doc-writer"})
    assert doc["paths"] == ["pkg/kubelet"]
    assert doc["docs"] == ["/workspace/ARCHITECTURE.md", "/workspace/pkg/kubelet/knowledge-plane/INDEX.md"]


def test_monorepo_lead_sizes_with_prefixed_agent_names(tmp_path, spec):
    spec["monorepo"] = {"subdir": "pkg/kubelet", "agent_prefix": "kubelet"}
    out = tmp_path / "kit"
    scion_factory.compile_factory_boilerplate(spec, out)
    sizing = _prompt(out, "lead").split("## Sizing an issue", 1)[1].split("\n## ", 1)[0]
    assert "bug → `kubelet-investigator` first" in sizing
    assert "UI → `kubelet-frontend`" in sizing
    assert "release-manager" not in sizing


def test_eval_lead_briefs_by_file_and_sizes_only_with_its_own_team(kit):
    eval_lead = _prompt(kit, "eval-lead")
    assert "/scion-volumes/scratchpad/briefs/" in eval_lead
    assert "investigator" not in eval_lead


def test_deterministic_wake_sla_reviewer_focus_and_clean_agent_yaml(kit, tmp_path):
    lead_prompt = _prompt(kit, "lead")
    reviewer_prompt = _prompt(kit, "reviewer")
    assert "more than 5 minutes" in lead_prompt
    assert "scion message --interrupt" in lead_prompt
    assert "Inspect the delivered files and brief acceptance checks first" in reviewer_prompt
    assert "honoring" in lead_prompt and "`agents.md`" in lead_prompt
    assert "honoring `agents.md`" in reviewer_prompt
    assert "templates/_shared/agents.md" not in lead_prompt
    assert "templates/_shared/agents.md" not in reviewer_prompt

    lead_yaml = (kit / "templates" / "lead" / "scion-agent.yaml").read_text()
    deployer_yaml = (kit / "templates" / "deployer" / "scion-agent.yaml").read_text()
    assert "telemetry:" not in lead_yaml
    assert "max_duration:" not in lead_yaml
    assert "telemetry:" not in (kit / "templates" / "reviewer" / "scion-agent.yaml").read_text()
    assert 'FACTORY_ROLE: "lead"' in lead_yaml
    assert 'CLOUDSDK_CORE_DISABLE_PROMPTS: "1"' in deployer_yaml

    psearch_kit = tmp_path / "psearch-kit"
    scion_factory.compile_factory_boilerplate(
        scion_factory.load_spec(ROOT / "presets" / "psearch.factory.yaml"), psearch_kit
    )
    opencode_launcher = psearch_kit / "templates" / "backend" / "home" / ".local" / "bin" / "opencode-launch.sh"
    hermes_launcher = psearch_kit / "templates" / "reviewer" / "home" / ".local" / "bin" / "hermes-launch.sh"
    assert opencode_launcher.is_file() and "google-vertex/gemini-4-argon" in opencode_launcher.read_text()
    assert hermes_launcher.is_file() and "google/gemini-4-argon" in hermes_launcher.read_text()



# --- Fix round 1: roster-aware reviewers, role-aware Operating Loop, kinds, pre-work reports -----------------------

def _compile(tmp_path, spec):
    out = tmp_path / "kit"
    scion_factory.compile_factory_boilerplate(spec, out)
    return out


def _add(spec, role, **extra):
    entry = dict(scion_factory.role_library.library_role_entry(role), **extra) if role in \
        scion_factory.role_library.ROLES else dict({"role": role, "harness": "antigravity", "model": "m",
                                                    "responsibility": "r"}, **extra)
    spec["team"]["build_roles"].append(entry)


def test_starter_lead_names_no_absent_parallel_reviewers(kit):
    lead = _prompt(kit, "lead")
    for absent in ("security auditor", "security-auditor", "performance analyst", "performance-analyst"):
        assert absent not in lead, absent
    assert "Review verdicts from `reviewer` before giving the commit slot." in lead


def test_lead_with_a_security_auditor_waits_for_its_verdict(tmp_path, spec):
    spec["monorepo"] = {"subdir": "pkg/kubelet", "agent_prefix": "kubelet"}
    _add(spec, "security-auditor")
    lead = _prompt(_compile(tmp_path, spec), "lead")
    assert "Review verdicts from `kubelet-reviewer`, `kubelet-security-auditor` before giving the commit slot." in lead
    assert "auth/crypto/secrets → `kubelet-security-auditor` in parallel with `kubelet-reviewer`" in lead


def test_reviewer_takes_work_only_from_its_lead(kit):
    reviewer = _prompt(kit, "reviewer")
    assert "messaging downstream teammates" not in reviewer
    assert "status:ready" not in reviewer and "gh issue list" not in reviewer
    loop = _section(reviewer, "Operating Loop")
    assert "Take work only from the brief file named in `lead`'s message" in loop
    assert "push" not in loop and "commit" not in loop


@pytest.mark.parametrize("role", ["investigator", "researcher"])
def test_read_only_specialists_never_push_product_code(kit, role):
    text = _prompt(kit, role)
    loop = _section(text, "Operating Loop")
    assert "push" not in loop and "commit" not in loop
    assert "Execute your role responsibility" not in text
    assert "never change product code" in loop


def test_writers_commit_only_with_the_commit_slot(kit):
    for role in ("backend", "doc-writer", "frontend"):
        loop = _section(_prompt(kit, role), "Operating Loop")
        assert "commit and push on branch `main` only when `lead` gives you the commit slot" in loop, role


def test_lead_operating_loop_keeps_intake_and_never_implements(kit):
    loop = _section(_prompt(kit, "lead"), "Operating Loop")
    assert "gh issue list -R example-org/pulse-ledger --label status:ready" in loop
    assert "Never implement it yourself" in loop
    assert "messaging downstream teammates" not in loop


def test_renamed_implementer_gets_the_implementer_focus_and_protocol(tmp_path, spec):
    roles = spec["team"]["build_roles"]
    for r in roles:
        if r["role"] == "backend":
            r["role"] = "go-dev"
            r["default_kind"] = "implementer"
    for issue in spec.get("issues") or []:
        if issue.get("owner_role") == "backend":
            issue["owner_role"] = "go-dev"
    out = _compile(tmp_path, spec)
    assert not (out / "templates" / "backend").exists()
    text = _prompt(out, "go-dev")
    assert scion_factory.role_library.role_protocol("backend").splitlines()[0] in _section(text, "Role protocol")
    assert spec["stack"]["build_command"] in _section(text, "Focus")
    assert "commit and push" in _section(text, "Operating Loop")
    assert "only `go-dev` and `reviewer`" in _section(_prompt(out, "lead"), "Sizing an issue")


def test_renamed_reviewer_is_named_in_the_security_line(tmp_path, spec):
    for r in spec["team"]["build_roles"]:
        if r["role"] == "reviewer":
            r["role"] = "code-critic"
            r["default_kind"] = "reviewer"
    _add(spec, "security-auditor")
    out = _compile(tmp_path, spec)
    sizing = _section(_prompt(out, "lead"), "Sizing an issue")
    assert "auth/crypto/secrets → `security-auditor` in parallel with `code-critic`" in sizing
    assert "Review verdicts from `code-critic`, `security-auditor` before giving the commit slot." in sizing
    assert "push" not in _section(_prompt(out, "code-critic"), "Operating Loop")


def test_sizing_line_needs_every_role_it_names(tmp_path, spec):
    spec["team"]["build_roles"] = [r for r in spec["team"]["build_roles"] if r["role"] != "reviewer"]
    spec.setdefault("team", {})["dropped_defaults"] = [{"role": "reviewer", "reason": "test"}]
    _add(spec, "security-auditor")
    sizing = _section(_prompt(_compile(tmp_path, spec), "lead"), "Sizing an issue")
    assert "auth/crypto/secrets" not in sizing


def test_lead_reads_pre_work_reports_before_briefing_the_implementer(kit):
    lead = _prompt(kit, "lead")
    assert "/scion-volumes/scratchpad/reports/<issue>-investigation.md" in lead
    assert "/scion-volumes/scratchpad/reports/<issue>-research.md" in lead
    assert "before you brief `backend`, and put the report's path in its brief" in lead
