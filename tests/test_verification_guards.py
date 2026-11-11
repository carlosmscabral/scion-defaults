"""Tests for universal anti-masking verification guardrails across build, deploy, and eval roles."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import role_library  # noqa: E402
import scion_factory  # noqa: E402


def test_lead_requires_complete_root_cause_closure_before_verified() -> None:
    proto = role_library.role_protocol("lead")
    assert proto is not None
    low = proto.lower()
    assert "root cause" in low and "follow-up issue" in low
    assert "status:po-decision" not in proto


def test_backend_deployer_and_reviewer_forbid_silent_fallbacks_and_clamped_metrics() -> None:
    backend_svc = role_library.role_protocol("backend")
    backend_lib = role_library.role_protocol("backend", deploy="none")
    reviewer = role_library.role_protocol("reviewer")
    deployer = role_library.role_protocol("deployer")

    assert backend_svc and "[fallback]" in backend_svc and "clamp" in backend_svc.lower()
    assert backend_lib and "clamp" in backend_lib.lower() and "fallback" in backend_lib.lower()
    assert reviewer and "clamp" in reviewer.lower() and "precondition" in reviewer.lower()
    assert deployer and "[fallback]" in deployer and "LIVE.md" in deployer


def test_test_engineer_and_eval_roles_enforce_negative_thresholds_and_dual_l2_probes() -> None:
    tester = role_library.role_protocol("test-engineer")
    contract = role_library.role_protocol("contract-tester")
    dom_eval = role_library.role_protocol("domain-evaluator")

    assert tester and "negative over-threshold" in tester.lower() and "precondition" in tester.lower()
    assert contract and "401" in contract and "happy-path" in contract.lower()
    assert dom_eval and "401" in dom_eval and "happy-path" in dom_eval.lower()


def test_compiled_kit_and_verification_docs_carry_anti_masking_guardrails(tmp_path: Path) -> None:
    spec = scion_factory.load_spec(ROOT / "presets" / "starter-service.factory.yaml")
    spec["team"]["build_roles"].append(role_library.library_role_entry("test-engineer"))
    spec["team"]["eval_roles"].append(role_library.library_role_entry("contract-tester"))
    out = tmp_path / "kit"
    scion_factory.compile_factory_boilerplate(spec, out)

    assert "[fallback]" in (out / "templates" / "backend" / "system-prompt.md").read_text(encoding="utf-8")
    assert "negative over-threshold" in (out / "templates" / "test-engineer" / "system-prompt.md").read_text(encoding="utf-8")
    assert "happy-path" in (out / "templates" / "contract-tester" / "system-prompt.md").read_text(encoding="utf-8")

    doc = (ROOT / "docs" / "verification-and-eval.md").read_text(encoding="utf-8")
    assert "Universal Anti-Masking Verification Guardrails" in doc
    assert "[fallback]" in doc and "401" in doc
