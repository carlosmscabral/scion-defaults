"""Cross-family verification policy and reviewer pool expansion for Scion factories.

Why this module exists:
Builders and verifiers on the same model family share blind spots on auth checks, imports, and green-mock tests.
This module classifies model families, assigns opposing verifier models, and expands standing reviewer pools (`M/G/k`).
It also validates builder-verifier separation during `scion-factory check` and `compile`.
"""
from __future__ import annotations

import copy
import re
from typing import Any

import role_library

BUILDER_KINDS: frozenset[str] = frozenset({
    "implementer",
    "frontend",
    "migration-specialist",
})

VERIFIER_KINDS: frozenset[str] = frozenset({
    "reviewer",
    "test-engineer",
    "security-auditor",
    "domain-evaluator",
    "evaluator",
    "safety-evaluator",
    "contract-tester",
})

VERIFICATION_POLICIES: tuple[str, ...] = (
    "advisory",
    "cross-family",
    "single-harness",
)

MAX_REVIEWER_REPLICAS = 8

# Stock pairs run on unpatched Scion Hub images without interactive auth prompts (Lesson L28).
_STOCK_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("claude", "opus", "anthropic"),
    ("antigravity", "Gemini 4 Argon (High)", "google"),
)


def role_kind(role_spec: dict[str, Any]) -> str:
    """Return the canonical `default_kind` for a roster entry."""
    return role_library.resolve_role_kind(role_spec)


def verification_policy_mode(spec: dict[str, Any]) -> str:
    """Return `team.verification_policy`, defaulting to `advisory` for older specs."""
    raw = (spec.get("team") or {}).get("verification_policy")
    if raw is None or raw == "":
        return "advisory"
    return str(raw).strip()


def infer_model_family(harness: str, model: str) -> str:
    """Classify a `(harness, model)` pair into a canonical model family slug.

    The model identifier takes precedence over the harness name because multi-provider
    harnesses (`opencode`, `hermes`) can route to Google, Anthropic, or OpenAI models.
    """
    h = (harness or "").strip().lower()
    m = (model or "").strip().lower()

    if any(tok in m for tok in ("claude", "opus", "sonnet", "haiku", "anthropic")):
        return "anthropic"
    if any(tok in m for tok in ("gemini", "argon", "gemma")) or m.startswith(("google/", "google-vertex/")):
        return "google"
    if any(tok in m for tok in ("gpt-", "gpt4", "gpt5", "codex")) or re.search(r"(?:^|[-_/])o[134](?:$|[-_.])", m) or m.startswith("openai/"):
        return "openai"
    if "/" in m:
        prefix = m.split("/", 1)[0].strip()
        if prefix:
            return prefix

    if h == "claude":
        return "anthropic"
    if h in ("antigravity", "gemini"):
        return "google"
    if h == "codex":
        return "openai"
    return h or "unknown"


def _builder_entries(spec: dict[str, Any]) -> list[dict[str, Any]]:
    team = spec.get("team") or {}
    return [
        r for r in (team.get("build_roles") or [])
        if isinstance(r, dict) and role_kind(r) in BUILDER_KINDS
    ]


def _verifier_entries(spec: dict[str, Any]) -> list[dict[str, Any]]:
    team = spec.get("team") or {}
    entries: list[dict[str, Any]] = []
    for key in ("build_roles", "eval_roles"):
        for r in (team.get(key) or []):
            if isinstance(r, dict) and role_kind(r) in VERIFIER_KINDS:
                entries.append(r)
    return entries


def _reference_builders(builders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select the builder baseline for diversity checks.

    If builders span multiple model families on a two-harness Hub, compare verifiers against
    the primary `implementer` roles so validation remains satisfiable with stock harnesses.
    """
    families = {infer_model_family(str(b.get("harness", "")), str(b.get("model", ""))) for b in builders}
    if len(families) <= 1:
        return builders
    implementers = [b for b in builders if role_kind(b) == "implementer"]
    return implementers or builders[:1]


def opposing_verifier_pair(spec: dict[str, Any]) -> tuple[str, str]:
    """Choose a stock `(harness, model)` pair whose model family opposes the active builders."""
    ref = _reference_builders(_builder_entries(spec))
    b_families = {infer_model_family(str(b.get("harness", "")), str(b.get("model", ""))) for b in ref}
    b_harnesses = {str(b.get("harness", "")).strip().lower() for b in ref}

    for harness, model, family in _STOCK_PAIRS:
        if family not in b_families and harness not in b_harnesses:
            return harness, model
    for harness, model, family in _STOCK_PAIRS:
        if family not in b_families:
            return harness, model
    return _STOCK_PAIRS[0][0], _STOCK_PAIRS[0][1]


def apply_cross_family_defaults(spec: dict[str, Any]) -> list[str]:
    """Assign opposing model families to verifier roles in `spec` in place and return change notes."""
    builders = _builder_entries(spec)
    if not builders:
        return []
    target_harness, target_model = opposing_verifier_pair(spec)
    target_family = infer_model_family(target_harness, target_model)
    ref = _reference_builders(builders)
    ref_families = {infer_model_family(str(b.get("harness", "")), str(b.get("model", ""))) for b in ref}
    ref_harnesses = {str(b.get("harness", "")).strip().lower() for b in ref}

    notes: list[str] = []
    for entry in _verifier_entries(spec):
        cur_h = str(entry.get("harness", "")).strip().lower()
        cur_f = infer_model_family(cur_h, str(entry.get("model", "")))
        if cur_f in ref_families or cur_h in ref_harnesses:
            entry["harness"] = target_harness
            entry["model"] = target_model
            notes.append(
                f"assigned cross-family verifier {entry.get('role')} -> {target_harness}/{target_model} ({target_family})"
            )
    return notes


def expand_reviewer_replicas(spec: dict[str, Any]) -> list[str]:
    """Materialize `reviewer-2..reviewer-k` in `team.build_roles` when `team.reviewer_replicas >= 2`."""
    team = spec.get("team") or {}
    raw_replicas = team.get("reviewer_replicas")
    if not isinstance(raw_replicas, int) or isinstance(raw_replicas, bool) or raw_replicas < 2:
        return []
    replicas = min(raw_replicas, MAX_REVIEWER_REPLICAS)
    build_roles = team.get("build_roles")
    if not isinstance(build_roles, list):
        return []

    reviewers = [
        r for r in build_roles
        if isinstance(r, dict) and role_kind(r) == "reviewer" and not str(r.get("reports_to") or "").strip()
    ]
    if not reviewers or len(reviewers) >= replicas:
        return []

    base = reviewers[0]
    base_name = str(base.get("role") or "reviewer")
    existing_names = {str(r.get("role")) for r in build_roles if isinstance(r, dict)}
    opp_h, opp_m = opposing_verifier_pair(spec)
    policy = verification_policy_mode(spec)
    ref = _reference_builders(_builder_entries(spec))
    ref_families = {infer_model_family(str(b.get("harness", "")), str(b.get("model", ""))) for b in ref}
    base_family = infer_model_family(str(base.get("harness", "")), str(base.get("model", "")))

    insert_at = max(
        i for i, r in enumerate(build_roles)
        if isinstance(r, dict) and role_kind(r) == "reviewer" and not str(r.get("reports_to") or "").strip()
    ) + 1
    notes: list[str] = []
    idx = 2
    while len(reviewers) < replicas:
        cand = f"{base_name}-{idx}"
        idx += 1
        if cand in existing_names:
            continue
        clone = copy.deepcopy(base)
        clone["role"] = cand
        clone["default_kind"] = "reviewer"
        if policy != "single-harness" and (base_family in ref_families or len({
            infer_model_family(str(b.get("harness", "")), str(b.get("model", ""))) for b in _builder_entries(spec)
        }) > 1):
            clone["harness"] = opp_h
            clone["model"] = opp_m
        build_roles.insert(insert_at, clone)
        insert_at += 1
        existing_names.add(cand)
        reviewers.append(clone)
        notes.append(f"added parallel reviewer replica {cand} ({clone['harness']}/{clone['model']})")
    return notes


def audit_verification_diversity(spec: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Validate `team.verification_policy`, `team.reviewer_replicas`, and builder/verifier separation."""
    errors: list[str] = []
    warnings: list[str] = []
    team = spec.get("team") or {}

    policy = verification_policy_mode(spec)
    if policy not in VERIFICATION_POLICIES:
        errors.append(
            f"team.verification_policy must be one of {', '.join(VERIFICATION_POLICIES)} (got {policy!r})"
        )
        return errors, warnings

    if policy == "single-harness" and not str(team.get("single_harness_reason") or "").strip():
        errors.append(
            "team.single_harness_reason is required when team.verification_policy is `single-harness`"
        )

    raw_replicas = team.get("reviewer_replicas")
    if raw_replicas is not None:
        if isinstance(raw_replicas, bool) or not isinstance(raw_replicas, int) or raw_replicas < 1 or raw_replicas > MAX_REVIEWER_REPLICAS:
            errors.append(f"team.reviewer_replicas must be an integer between 1 and {MAX_REVIEWER_REPLICAS}")

    max_wip = team.get("max_wip", 2)
    build_roles = [r for r in (team.get("build_roles") or []) if isinstance(r, dict)]
    standing_reviewers = [
        r for r in build_roles
        if role_kind(r) == "reviewer" and not str(r.get("reports_to") or "").strip()
    ]
    effective_reviewers = max(len(standing_reviewers), raw_replicas if isinstance(raw_replicas, int) and not isinstance(raw_replicas, bool) else 0)
    if isinstance(max_wip, int) and max_wip >= 4 and effective_reviewers < 2:
        warnings.append(
            f"team.max_wip is {max_wip} with only {effective_reviewers or 1} standing reviewer (M/G/1 queue bottleneck): "
            "set team.reviewer_replicas: 2 or add a second reviewer role"
        )

    if policy == "single-harness":
        return errors, warnings

    builders = _builder_entries(spec)
    verifiers = _verifier_entries(spec)
    if not builders or not verifiers:
        return errors, warnings

    ref = _reference_builders(builders)
    ref_families = {infer_model_family(str(b.get("harness", "")), str(b.get("model", ""))) for b in ref}
    ref_harnesses = {str(b.get("harness", "")).strip().lower() for b in ref}
    ref_names = [str(b.get("role", "?")) for b in ref]
    opp_h, opp_m = opposing_verifier_pair(spec)

    for v in verifiers:
        v_name = str(v.get("role", "?"))
        v_harness = str(v.get("harness", "")).strip().lower()
        v_family = infer_model_family(v_harness, str(v.get("model", "")))
        same_family = v_family in ref_families
        same_harness = v_harness in ref_harnesses
        if not (same_family or same_harness):
            continue
        overlap = (
            f"both harness `{v_harness}` and model family `{v_family}`"
            if (same_family and same_harness)
            else (f"model family `{v_family}`" if same_family else f"harness `{v_harness}`")
        )
        msg = (
            f"verifier role '{v_name}' shares {overlap} with builder role(s) {ref_names}: "
            f"assign an independent verifier such as `{opp_h}` / `{opp_m}` (or run `scion-factory diversify-team`)"
        )
        if policy == "cross-family":
            errors.append(msg)
        else:
            warnings.append(msg)

    return errors, warnings
