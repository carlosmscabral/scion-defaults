"""Tests for scripts/hot_upgrade.py and `scion-factory hot-upgrade`."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import hot_upgrade  # noqa: E402
import scion_factory  # noqa: E402
from test_hub_client import fake_hub  # noqa: E402,F401
from test_provision_factory import fake_cli_env, write_gke_factory_env  # noqa: E402


def _starter() -> dict:
    return scion_factory.load_spec(ROOT / "presets" / "starter-service.factory.yaml")


def test_parse_spawn_specs_and_slug_validation():
    assert hot_upgrade.parse_spawn_specs(["reviewer-2", "security-auditor:sec-1,reviewer-2"]) == [
        ("reviewer-2", "reviewer-2"),
        ("security-auditor", "sec-1"),
    ]
    with pytest.raises(ValueError, match="invalid spawn"):
        hot_upgrade.parse_spawn_specs(["reviewer;rm -rf /"])
    with pytest.raises(ValueError, match="invalid project slug"):
        hot_upgrade.run_hot_upgrade(project_slug="bad slug!", templates_dir=ROOT / "templates")


def test_hot_upgrade_dry_run_and_apply_preserve_ancestry_and_broadcast_delta(fake_hub, tmp_path):
    spec = _starter()
    spec["project"]["slug"] = "starter-service"
    spec["project"]["eval_slug"] = "starter-service-eval"
    kit_dir = tmp_path / "kit"
    scion_factory.compile_factory_boilerplate(spec, kit_dir)
    # Add a new specialist template (reviewer-claude) in the compiled kit.
    extra_tpl = kit_dir / "templates" / "reviewer-claude"
    extra_tpl.mkdir(parents=True)
    for f in (kit_dir / "templates" / "reviewer").iterdir():
        if f.is_file():
            (extra_tpl / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")

    # Seed project and running lead + reviewer on fake_hub.
    pid = "pid-starter-service"
    fake_hub.projects_by_slug["starter-service"] = {"id": pid, "slug": "starter-service"}
    fake_hub.project_settings[pid] = {}
    fake_hub.agents_by_project[pid] = ["lead", "reviewer"]
    fake_hub.seed_agent(pid, slug="lead", phase="running")
    fake_hub.seed_agent(pid, slug="reviewer", phase="running")

    env_file = write_gke_factory_env(tmp_path, fake_hub.url)
    cli_env = fake_cli_env(tmp_path, fake_hub.url)

    # 1. Dry-run performs zero POST/PUT mutations on agents or templates.
    fake_hub.requests.clear()
    r_dry = subprocess.run(
        [
            sys.executable,
            "scripts/scion_factory.py",
            "hot-upgrade",
            "--project",
            "starter-service",
            "--kit",
            str(kit_dir),
            "--env",
            str(env_file),
            "--roles",
            "reviewer,reviewer-claude",
            "--rule-delta",
            "Run git grep before claiming a route is missing.",
            "--spawn",
            "reviewer-claude",
            "--spawn",
            "reviewer",
            "--json",
        ],
        cwd=ROOT,
        env=cli_env,
        capture_output=True,
        text=True,
    )
    assert r_dry.returncode == 0, r_dry.stderr
    dry_out = json.loads(r_dry.stdout)
    assert dry_out["apply"] is False
    assert dry_out["notified_agents"] == ["reviewer"]
    assert dry_out["skipped_existing_agents"] == ["reviewer"]
    assert len(dry_out["spawn_commands"]) == 1
    assert "scion start reviewer-claude -t reviewer-claude" in dry_out["spawn_commands"][0]
    assert not any(r["method"] in ("POST", "PUT", "PATCH", "DELETE") for r in fake_hub.requests)

    # 2. Apply pushes templates, sends rule delta to running reviewer, and delegates child spawn to lead.
    fake_hub.requests.clear()
    r_apply = subprocess.run(
        [
            sys.executable,
            "scripts/scion_factory.py",
            "hot-upgrade",
            "--project",
            "starter-service",
            "--kit",
            str(kit_dir),
            "--env",
            str(env_file),
            "--roles",
            "reviewer,reviewer-claude",
            "--rule-delta",
            "Run git grep before claiming a route is missing.",
            "--spawn",
            "reviewer-claude",
            "--branch",
            "main",
            "--apply",
            "--json",
        ],
        cwd=ROOT,
        env=cli_env,
        capture_output=True,
        text=True,
    )
    assert r_apply.returncode == 0, r_apply.stderr + "\n" + r_apply.stdout
    apply_out = json.loads(r_apply.stdout)
    assert apply_out["apply"] is True
    assert apply_out["overwrite_templates"] is False
    assert apply_out["lead_agent"] == "lead"
    assert apply_out["lead_notified"] is True

    msg_reqs = [
        (r["path"], json.loads(r["body"].decode("utf-8"))["message"])
        for r in fake_hub.requests
        if r["method"] == "POST" and r["path"].endswith("/message")
    ]
    assert any(
        path == f"/api/v1/projects/{pid}/agents/reviewer/message" and "[HOT-UPGRADE RULE DELTA]" in msg
        for path, msg in msg_reqs
    )
    assert any(
        path == f"/api/v1/projects/{pid}/agents/lead/message"
        and "scion start reviewer-claude -t reviewer-claude -b main --harness-auth vertex-ai" in msg
        for path, msg in msg_reqs
    )

    # 3. DRY_RUN=true overrides --apply, and unknown --notify-agents fails fast instead of dropping typos.
    fake_hub.requests.clear()
    env_dry_true = dict(cli_env, DRY_RUN="true")
    r_override = subprocess.run(
        [
            sys.executable,
            "scripts/scion_factory.py",
            "hot-upgrade",
            "--project",
            "starter-service",
            "--kit",
            str(kit_dir),
            "--env",
            str(env_file),
            "--rule-delta",
            "Check auth.",
            "--notify-agents",
            "reviewer",
            "--apply",
            "--json",
        ],
        cwd=ROOT,
        env=env_dry_true,
        capture_output=True,
        text=True,
    )
    assert r_override.returncode == 0, r_override.stderr
    assert json.loads(r_override.stdout)["apply"] is False
    assert not any(r["method"] == "POST" for r in fake_hub.requests)

    r_typo = subprocess.run(
        [
            sys.executable,
            "scripts/scion_factory.py",
            "hot-upgrade",
            "--project",
            "starter-service",
            "--kit",
            str(kit_dir),
            "--env",
            str(env_file),
            "--rule-delta",
            "Check auth.",
            "--notify-agents",
            "revieewr-typo",
            "--json",
        ],
        cwd=ROOT,
        env=cli_env,
        capture_output=True,
        text=True,
    )
    assert r_typo.returncode == 2
    assert "not running" in r_typo.stderr


def test_hot_upgrade_fails_cleanly_when_lead_is_not_running_for_spawn(fake_hub, tmp_path):
    spec = _starter()
    kit_dir = tmp_path / "kit"
    scion_factory.compile_factory_boilerplate(spec, kit_dir)

    pid = "pid-pulse-ledger"
    fake_hub.projects_by_slug["pulse-ledger"] = {"id": pid, "slug": "pulse-ledger"}
    fake_hub.project_settings[pid] = {}
    fake_hub.agents_by_project[pid] = []

    env_file = write_gke_factory_env(tmp_path, fake_hub.url)
    res = subprocess.run(
        [
            sys.executable,
            "scripts/hot_upgrade.py",
            "--project",
            "pulse-ledger",
            "--templates",
            str(kit_dir / "templates"),
            "--env",
            str(env_file),
            "--spawn",
            "reviewer",
        ],
        cwd=ROOT,
        env=fake_cli_env(tmp_path, fake_hub.url),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 2
    assert "lead agent 'lead' is not running" in res.stderr
