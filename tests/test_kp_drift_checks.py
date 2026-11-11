"""Tests for bidirectional ADR supersession and value-registry oracle drift checks in kp-check.py."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import scion_factory  # noqa: E402


def _compile_starter(tmp_path: Path) -> Path:
    spec = scion_factory.load_spec(ROOT / "presets" / "starter-service.factory.yaml")
    out = tmp_path / "kit"
    scion_factory.compile_factory_boilerplate(spec, out)
    return out / "knowledge-plane"


def _run_kp_check(kp: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(kp / "kp-check.py"), str(kp)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_default_compiled_knowledge_plane_passes_kp_check(tmp_path: Path) -> None:
    kp = _compile_starter(tmp_path)
    res = _run_kp_check(kp)
    assert res.returncode == 0, res.stdout + res.stderr


def test_asymmetric_adr_supersedes_fails_until_superseded_by_backlink_exists(tmp_path: Path) -> None:
    kp = _compile_starter(tmp_path)
    adr_001 = next((kp / "adr").glob("ADR-001-*.md"))
    adr_099 = kp / "adr" / "ADR-099-raise-batch-limit.md"
    adr_099.write_text(
        "# ADR-099: Raise batch limit\n\n"
        "- **Status:** Accepted\n"
        "- **Requirements:** REQ-001\n"
        "- **Supersedes:** ADR-001\n\n"
        "## Decision\n\nRaise batch limit from 40 items to 80 items.\n",
        encoding="utf-8",
    )

    res_missing = _run_kp_check(kp)
    assert res_missing.returncode == 1
    assert "missing 'Superseded by ADR-099' status back-link" in res_missing.stdout

    old_txt = adr_001.read_text(encoding="utf-8").replace(
        "- **Status:** Accepted",
        "- **Status:** Superseded by ADR-099",
    )
    adr_001.write_text(old_txt, encoding="utf-8")
    res_fixed = _run_kp_check(kp)
    assert res_fixed.returncode == 0, res_fixed.stdout


def test_asymmetric_superseded_by_fails_without_supersedes_header(tmp_path: Path) -> None:
    kp = _compile_starter(tmp_path)
    adr_001 = next((kp / "adr").glob("ADR-001-*.md"))
    adr_099 = kp / "adr" / "ADR-099-new-timeout.md"
    adr_099.write_text(
        "# ADR-099: New timeout\n\n"
        "- **Status:** Accepted\n"
        "- **Requirements:** REQ-001\n\n"
        "## Decision\n\nSet RPC timeout to 3000 ms.\n",
        encoding="utf-8",
    )
    adr_001.write_text(
        adr_001.read_text(encoding="utf-8").replace("- **Status:** Accepted", "- **Status:** Superseded by ADR-099"),
        encoding="utf-8",
    )

    res = _run_kp_check(kp)
    assert res.returncode == 1
    assert "missing 'Supersedes: ADR-001' header" in res.stdout


def test_value_registry_flags_stale_constants_while_exempting_superseded_adrs_and_live_history(tmp_path: Path) -> None:
    kp = _compile_starter(tmp_path)
    repo_root = kp.parent
    adr_001 = next((kp / "adr").glob("ADR-001-*.md"))
    adr_099 = kp / "adr" / "ADR-099-raise-word-cap.md"

    adr_001.write_text(
        "# ADR-001: Initial payload cap\n\n"
        "- **Status:** Superseded by ADR-099\n"
        "- **Requirements:** REQ-001\n\n"
        "## Decision\n\nCap summary length at 40 words.\n",
        encoding="utf-8",
    )
    adr_099.write_text(
        "# ADR-099: Expand payload cap\n\n"
        "- **Status:** Accepted\n"
        "- **Requirements:** REQ-001\n"
        "- **Supersedes:** ADR-001\n\n"
        "## Decision\n\nCap summary length at 80 words.\n",
        encoding="utf-8",
    )
    (kp / "state" / "LIVE.md").write_text(
        (kp / "state" / "LIVE.md").read_text(encoding="utf-8") + "| 2026-11-01 | rev-1 | 40 words cap | #1 |\n",
        encoding="utf-8",
    )

    contracts_md = kp / "contracts" / "README.md"
    contracts_md.write_text(
        contracts_md.read_text(encoding="utf-8") + "\n- Summary cap: 40 words maximum.\n",
        encoding="utf-8",
    )
    agents_md = repo_root / "AGENTS.md"
    agents_md.write_text("# Agents\n\nEnforce the 40 words summary cap.\n", encoding="utf-8")

    (kp / "value-registry.json").write_text(
        json.dumps(
            {
                "values": [
                    {
                        "id": "REQ-001.summary_cap",
                        "current": "80 words",
                        "superseded": ["40 words"],
                        "context_pattern": "summary|cap",
                    }
                ]
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    res_stale = _run_kp_check(kp)
    assert res_stale.returncode == 1
    assert "knowledge-plane/contracts/README.md" in res_stale.stdout
    assert "AGENTS.md" in res_stale.stdout
    assert "ADR-001" not in res_stale.stdout
    assert "LIVE.md" not in res_stale.stdout

    contracts_md.write_text(
        contracts_md.read_text(encoding="utf-8").replace("40 words", "80 words"),
        encoding="utf-8",
    )
    agents_md.write_text("# Agents\n\nEnforce the 80 words summary cap.\n", encoding="utf-8")
    res_clean = _run_kp_check(kp)
    assert res_clean.returncode == 0, res_clean.stdout


def test_duplicate_adr_id_and_invalid_context_regex_reported_without_crash(tmp_path: Path) -> None:
    kp = _compile_starter(tmp_path)
    (kp / "adr" / "ADR-001-duplicate.md").write_text(
        "# ADR-001: Duplicate number\n\n- **Status:** Accepted\n- **Requirements:** REQ-001\n",
        encoding="utf-8",
    )
    (kp / "value-registry.json").write_text(
        json.dumps({"values": [{"id": "bad.re", "current": "10", "superseded": ["5"], "context_pattern": "[unclosed"}]}),
        encoding="utf-8",
    )
    res = _run_kp_check(kp)
    assert res.returncode == 1
    assert "duplicate ADR identifier ADR-001" in res.stdout
    assert "invalid context_pattern regex" in res.stdout
