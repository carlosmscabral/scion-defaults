"""Tests for blocked-WIP delta recovery, subprocess hygiene, and anti-contagion brief templates."""

from __future__ import annotations

import copy
import datetime
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import factory_delta  # noqa: E402
import hot_upgrade  # noqa: E402
import hub_client  # noqa: E402
import kaizen_policy  # noqa: E402
import role_library  # noqa: E402
import scion_factory  # noqa: E402
from test_factory_delta import FakeDeltaStore, delta_hub  # noqa: E402

__all__ = ["delta_hub"]


def test_delta_wakes_lead_on_blocked_wip_and_caps_per_fingerprint(delta_hub):
    """Verify run_delta_tick recovers stranded blocked WIP, ignores updatedAt bumps, and caps wakes at 2."""
    store = FakeDeltaStore()
    base_ts = datetime.datetime(2026, 11, 13, 14, 26, 5, tzinfo=datetime.timezone.utc).timestamp()
    delta_hub.agents["uuid-bc"] = {
        "lead": {
            "id": "id-lead",
            "slug": "lead",
            "name": "lead",
            "template": "lead",
            "phase": "running",
            "activity": "blocked",
            "lastActivityEvent": "2026-11-13T14:26:00Z",
        },
        "reviewer-claude": {
            "id": "id-rc",
            "slug": "reviewer-claude",
            "name": "reviewer-claude",
            "template": "reviewer-claude",
            "phase": "running",
            "activity": "blocked",
            "lastActivityEvent": "2026-11-13T14:26:05Z",
            "updatedAt": "2026-11-13T14:40:55Z",
        },
    }
    client = hub_client.HubClient(base_url=delta_hub.url, token="tok-owner")

    issues_ready = [{"number": 91, "title": "Layout fix", "labels": [{"name": "status:ready"}], "comments": 0}]
    r1 = factory_delta.run_delta_tick(
        client,
        store,
        project_slug="bananacast",
        lead_slug="lead",
        apply=True,
        now=base_ts,
        github_fetcher=lambda *_: (issues_ready, {}),
    )
    assert r1["woke_lead"] is True

    # 1. Transition to status:in-review 120s later (<900s) is suppressed as in-flight echo
    issues_review = [{"number": 91, "title": "Layout fix", "labels": [{"name": "status:in-review"}], "comments": 1}]
    r2 = factory_delta.run_delta_tick(
        client,
        store,
        project_slug="bananacast",
        lead_slug="lead",
        apply=True,
        now=base_ts + 120,
        blocked_wip_sec=900,
        github_fetcher=lambda *_: (issues_review, {}),
    )
    assert r2["woke_lead"] is False
    assert r2["action"] == "suppressed_in_flight_echo"

    # 2. Past 900s (even with fresh updatedAt on reviewer-claude) -> wakes #1 with STALLED_WIP!
    r3 = factory_delta.run_delta_tick(
        client,
        store,
        project_slug="bananacast",
        lead_slug="lead",
        apply=True,
        now=base_ts + 905,
        blocked_wip_sec=900,
        github_fetcher=lambda *_: (issues_review, {}),
    )
    assert r3["woke_lead"] is True
    assert r3["stalled_wip"] is True
    assert "reviewer-claude(blocked)" in delta_hub.messages[-1]["body"]["message"]

    # 3. Cooldown blocks wake at +965s; second wake fires at +1820s, then caps at 2 wakes!
    assert not factory_delta.run_delta_tick(
        client, store, project_slug="bananacast", lead_slug="lead", apply=True,
        now=base_ts + 965, blocked_wip_sec=900, github_fetcher=lambda *_: (issues_review, {}),
    )["woke_lead"]
    r4 = factory_delta.run_delta_tick(
        client, store, project_slug="bananacast", lead_slug="lead", apply=True,
        now=base_ts + 1820, blocked_wip_sec=900, github_fetcher=lambda *_: (issues_review, {}),
    )
    assert r4["woke_lead"] is True
    # Third attempt for the same fingerprint is capped (0 wakes even 30m later)
    r5 = factory_delta.run_delta_tick(
        client, store, project_slug="bananacast", lead_slug="lead", apply=True,
        now=base_ts + 3600, blocked_wip_sec=900, github_fetcher=lambda *_: (issues_review, {}),
    )
    assert r5["woke_lead"] is False


def test_delta_ignores_idle_teammates_on_held_ready_issue_and_reports_mixed_stopped_plus_blocked(delta_hub):
    """Verify idle reviewers do not cause STALLED_WIP wakes and stopped rows do not mask blocked reviewers."""
    store = FakeDeltaStore()
    base_ts = datetime.datetime(2026, 11, 13, 15, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
    delta_hub.agents["uuid-bc"] = {
        "lead": {"id": "l", "slug": "lead", "name": "lead", "template": "lead", "phase": "running", "activity": "idle", "lastActivityEvent": "2026-11-13T15:00:00Z"},
        "reviewer": {"id": "r", "slug": "reviewer", "name": "reviewer", "template": "reviewer", "phase": "running", "activity": "idle", "lastActivityEvent": "2026-11-13T15:00:00Z"},
    }
    client = hub_client.HubClient(base_url=delta_hub.url, token="tok-owner")
    held_ready = [{"number": 95, "title": "Held slice", "labels": [{"name": "status:ready"}], "comments": 0}]

    # Initial dispatch wake
    assert factory_delta.run_delta_tick(
        client, store, project_slug="bananacast", lead_slug="lead", apply=True, now=base_ts, github_fetcher=lambda *_: (held_ready, {})
    )["woke_lead"]

    # Over 3 hours of 2-minute ticks with reviewer=idle: ZERO STALLED_WIP wakes!
    extra_wakes = sum(
        1 for step in range(1, 91)
        if factory_delta.run_delta_tick(
            client, store, project_slug="bananacast", lead_slug="lead", apply=True,
            now=base_ts + step * 120, blocked_wip_sec=900, github_fetcher=lambda *_: (held_ready, {}),
        )["woke_lead"]
    )
    assert extra_wakes == 0

    # Mixed stopped leftover + blocked reviewer -> reports BOTH stopped and blocked!
    delta_hub.agents["uuid-bc"]["backend"] = {"id": "b", "slug": "backend", "name": "backend", "template": "backend", "phase": "stopped", "created": "2026-11-13T15:00:00Z"}
    delta_hub.agents["uuid-bc"]["reviewer"]["activity"] = "blocked"
    res_mixed = factory_delta.run_delta_tick(
        client, store, project_slug="bananacast", lead_slug="lead", apply=True,
        now=base_ts + 12_000, blocked_wip_sec=900, github_fetcher=lambda *_: (held_ready, {}),
    )
    assert res_mixed["woke_lead"] is True
    msg = delta_hub.messages[-1]["body"]["message"]
    assert "stopped: backend" in msg
    assert "blocked/stalled: reviewer(blocked)" in msg


def test_compiler_emits_home_review_brief_template_and_executes_compound_gates(tmp_path: Path):
    """Verify compound `cd && CI=1` gates execute cleanly under timeout sh -c and land in home/."""
    base_spec = scion_factory.load_spec(ROOT / "presets" / "starter-service.factory.yaml")
    spec = copy.deepcopy(base_spec)
    spec["infrastructure"]["deploy"] = "none"
    spec["vcs"]["enabled"] = False
    sub = tmp_path / "subpkg"
    sub.mkdir()
    spec["stack"]["build_command"] = f"cd {sub} && CI=1 python3 -c 'import os; assert os.environ[\"CI\"] == \"1\"'"
    spec["stack"]["test_command"] = "python3 -c 'assert 1 + 1 == 2'"
    spec["stack"]["lint_command"] = "true"
    spec["stack"]["gate_timeout_sec"] = 45
    spec["team"]["build_roles"] = [
        r for r in spec["team"]["build_roles"] if r.get("role") != "deployer"
    ] + [role_library.library_role_entry("kaizen-analyst")]
    spec["team"]["eval_roles"] = [
        r for r in (spec["team"].get("eval_roles") or []) if r.get("role") != "eval-deployer"
    ]

    out = tmp_path / "kit-nongit"
    scion_factory.compile_factory_boilerplate(spec, out)

    home_tpl = (out / "templates/lead/home/review-brief-template.md").read_text(encoding="utf-8")
    shared_tpl = (out / "templates/_shared/review-brief-template.md").read_text(encoding="utf-8")
    assert home_tpl == shared_tpl
    assert "timeout 45s sh -c " in home_tpl
    assert "~/review-brief-template.md" in home_tpl

    # Extract rendered compound command from step 3 and execute it via /bin/sh (proving zero exit-127!)
    cmd_line = next(ln for ln in home_tpl.splitlines() if "Single-Pass Executable Gate" in ln)
    rendered_chain = cmd_line.split("Run `", 1)[1].split("` once", 1)[0]
    # Replace final kp-check path with compiled kit kp-check path so the entire chain runs end-to-end
    runnable = rendered_chain.replace("knowledge-plane/kp-check.py", str(out / "knowledge-plane/kp-check.py"))
    proc = subprocess.run(["sh", "-c", runnable], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr

    lead_prompt = (out / "templates/lead/system-prompt.md").read_text(encoding="utf-8")
    assert "~/review-brief-template.md" in lead_prompt
    assert "### Kaizen 3-Axis Triage Rubric" in lead_prompt

    git_spec = copy.deepcopy(base_spec)
    git_spec["vcs"] = {"enabled": True, "repo": "acme/py-svc", "build_branch": "trunk", "eval_branch": "eval"}
    git_tpl = kaizen_policy.render_verification_brief_template(git_spec)
    assert "origin/trunk" in git_tpl and "acme/py-svc" in git_tpl


def test_hot_upgrade_flush_stale_examples_works_standalone_and_with_rule_delta(tmp_path: Path, monkeypatch):
    """Verify --flush-stale-examples works standalone (defaulting to lead) and embeds inline rules."""
    tpl_dir = tmp_path / "templates"
    (tpl_dir / "lead").mkdir(parents=True)
    (tpl_dir / "lead" / "scion-agent.yaml").write_text("harness: claude\n", encoding="utf-8")

    sent: list[dict] = []

    class DummyClient:
        def project_id(self, slug: str) -> str:
            return f"pid-{slug}"

        def list_agents(self, _pid: str):
            return [{"slug": "lead", "template": "lead", "phase": "running"}]

        def request(self, method: str, path: str, body=None):
            sent.append({"method": method, "path": path, "body": body})
            return 200, {"templates": [{"slug": "lead"}]}

    monkeypatch.setattr(hot_upgrade.HubClient, "from_env", staticmethod(lambda _e=None: DummyClient()))

    rep = hot_upgrade.run_hot_upgrade(
        project_slug="demo-proj",
        templates_dir=tpl_dir,
        flush_stale_examples=True,
        apply=True,
    )
    assert rep["flush_stale_examples"] is True
    assert rep["notified_agents"] == ["lead"]
    body_msg = json.loads(json.dumps(sent[-1]["body"]))["message"]
    assert "[ANTI-CONTAGION BRIEF ANCHOR]" in body_msg
    assert "timeout 300s sh -c" in body_msg
