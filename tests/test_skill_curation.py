import json
import os
from pathlib import Path
import subprocess
import sys

import yaml

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))
import scion_factory
import skill_curation

ROOT = Path(__file__).resolve().parent.parent


def _starter():
    with open(ROOT / "presets" / "starter-service.factory.yaml", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def test_audit_bank_detects_conflicts_links_and_allows_negations(tmp_path):
    bank = tmp_path / "bank"
    bank.mkdir()
    skill_dir = bank / "ts-testing"
    skill_dir.mkdir()

    report = skill_curation.audit_bank(bank)
    assert report["ok"] is False
    assert any("missing SKILL.md" in e for e in report["errors"])

    # Positive conflicting command fails when stack uses npm.
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        "---\nname: ts-testing\ndescription: >-\n  Folded YAML description for unit testing.\n---\n"
        "Run `pnpm install --frozen-lockfile` and see [missing](references/missing.md).\n",
        encoding="utf-8",
    )
    spec = {"stack": {"build_command": "npm run build", "test_command": "npm test"}}
    report = skill_curation.audit_bank(bank, spec)
    assert report["ok"] is False
    assert any("stack conflict" in e for e in report["errors"])
    assert any("broken relative link" in e for e in report["errors"])

    # Explicit negation ("Never use pnpm") + valid relative link passes cleanly.
    refs = skill_dir / "references"
    refs.mkdir()
    (refs / "guide.md").write_text("# Guide\nUse `npm test`.\n", encoding="utf-8")
    skill_md.write_text(
        "---\nname: ts-testing\ndescription: >-\n  Folded YAML description for unit testing.\n---\n"
        "Use `npm ci` and `npm test`. Never use pnpm or yarn. See [guide](references/guide.md).\n",
        encoding="utf-8",
    )
    report = skill_curation.audit_bank(bank, spec)
    assert report["ok"] is True, report


def test_compile_injects_role_skills_and_claude_only_strict_delegation(tmp_path):
    bank = tmp_path / "skill-bank"
    (bank / "api-design").mkdir(parents=True)
    (bank / "api-design" / "SKILL.md").write_text(
        "---\nname: api-design\ndescription: REST API patterns.\n---\n# API Design\n",
        encoding="utf-8",
    )

    spec = _starter()
    spec["team"]["strict_scion_delegation"] = True
    for role_entry in spec["team"]["build_roles"]:
        if role_entry["role"] == "lead":
            role_entry["harness"] = "claude"
            role_entry["skills"] = ["api-design"]
    for role_entry in spec["team"]["eval_roles"]:
        if role_entry["role"] == "eval-lead":
            role_entry["harness"] = "antigravity"
            role_entry["model"] = "Gemini 4 Argon (High)"

    spec_path = tmp_path / "factory.yaml"
    out_dir = tmp_path / "compiled"
    scion_factory.save_spec(spec, spec_path)

    res = subprocess.run(
        [
            sys.executable,
            "scripts/scion_factory.py",
            "compile",
            str(spec_path),
            "--out",
            str(out_dir),
            "--skill-bank",
            str(bank),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    assert (out_dir / "templates" / "lead" / "skills" / "api-design" / "SKILL.md").is_file()

    claude_settings = out_dir / "templates" / "lead" / "home" / ".claude" / "settings.json"
    assert claude_settings.is_file()
    deny = json.loads(claude_settings.read_text(encoding="utf-8"))["permissions"]["deny"]
    assert "Agent" in deny and "Task" in deny
    # Non-Claude eval-lead does not receive .claude/settings.json
    assert not (out_dir / "templates" / "eval-lead" / "home" / ".claude" / "settings.json").exists()

    audit_cli = subprocess.run(
        [sys.executable, "scripts/scion_factory.py", "audit-skills", str(bank), "--spec", str(spec_path), "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert audit_cli.returncode == 0, audit_cli.stderr
    assert json.loads(audit_cli.stdout)["ok"] is True

    # Reject path traversal in skill_name before any rmtree/copytree executes.
    bad_spec = _starter()
    bad_spec["team"]["build_roles"][0]["skills"] = ["../escape"]
    import pytest
    with pytest.raises(ValueError, match="Unsafe skill"):
        skill_curation.inject_skills(bad_spec, bank, tmp_path / "escape_out")
