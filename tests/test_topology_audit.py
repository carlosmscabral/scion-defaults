import json
import os
from pathlib import Path
import subprocess
import sys

import yaml

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))
import scion_factory
import topology_audit

ROOT = Path(__file__).resolve().parent.parent


def _starter():
    with open(ROOT / "presets" / "starter-service.factory.yaml", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def test_topology_audit_detects_top_level_wave_bottleneck_and_docker_bloat(tmp_path):
    spec = _starter()
    spec["infrastructure"]["runtime"] = "docker"
    spec["team"]["max_wip"] = 4
    # Overload >10 total roles on Docker + >60% issues owned by frontend
    for idx in range(5):
        spec["team"]["build_roles"].append(
            {
                "role": f"extra-{idx}",
                "default_kind": "implementer",
                "harness": "antigravity",
                "model": "Gemini 4 Argon (High)",
                "responsibility": "Implements slice modules.",
                "default": False,
                "evidence": ["slice"],
                "skills": [f"s{i}" for i in range(7)] if idx == 0 else [],
            }
        )
    spec["waves"] = [
        {
            "wave": 1,
            "goal": "Initial delivery",
            "issues": [
                {"id": "W1-1", "title": "UI 1", "owner_role": "frontend", "acceptance_criteria": ["ok"]},
                {"id": "W1-2", "title": "UI 2", "owner_role": "frontend", "acceptance_criteria": ["ok"]},
                {"id": "W1-3", "title": "UI 3", "owner_role": "frontend", "acceptance_criteria": ["ok"]},
                {"id": "W1-4", "title": "UI 4", "owner_role": "frontend", "acceptance_criteria": ["ok"]},
                {"id": "W1-5", "title": "API 1", "owner_role": "backend", "acceptance_criteria": ["ok"]},
            ],
        }
    ]
    report = topology_audit.audit(spec)
    assert any("Workload bottleneck" in w and "frontend" in w for w in report["warnings"])
    assert any("Single-VM Docker container bloat" in w for w in report["warnings"])
    assert any("attaches 7 skills" in w for w in report["warnings"])
    assert any("USER root" in item for item in report["checklist"])
    assert any("workspaceMode: per-agent" in item for item in report["checklist"])

    spec_path = tmp_path / "factory.yaml"
    scion_factory.save_spec(spec, spec_path)
    cli = subprocess.run(
        [sys.executable, "scripts/scion_factory.py", "audit-topology", str(spec_path), "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert cli.returncode == 0, cli.stderr
    cli_report = json.loads(cli.stdout)
    assert len(cli_report["warnings"]) >= 3
