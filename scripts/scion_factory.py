#!/usr/bin/env python3
"""Scion Factory Compiler & Socratic Intent Elicitation ("Grill-Me") Tool.

Transforms a repository + user intent into a complete, domain-agnostic Scion
autonomous software factory boilerplate (build team + isolated adversarial eval
team + L1-L5 Evaluation Pyramid + Firestore control plane + Wave 1-N backlog).

This tool is designed to be driven by an agentic coding agent that follows
skills/scion-factory-bootstrap/SKILL.md. The agent conducts the interview; this
script only inspects, drafts, validates, and compiles. It never blocks on stdin.

Usage:
  python3 scripts/scion_factory.py inspect <repo_path> [--json]
  python3 scripts/scion_factory.py grill [--repo <repo_path>] [--out factory.yaml] [--force] [--json]
  python3 scripts/scion_factory.py check <factory.yaml> [--json]
  python3 scripts/scion_factory.py compile <factory.yaml> --out <out_dir>
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shlex
import stat
import sys
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discovery  # noqa: E402
import kaizen_policy  # noqa: E402
import knowledge_plane as kp  # noqa: E402
import role_library  # noqa: E402
import sub_coordinator_prompts  # noqa: E402
import sub_coordinators  # noqa: E402
import verification_policy  # noqa: E402


# ---------------------------------------------------------------------------
# Socratic "Grill-Me" Question Bank (5 Pillars, grounded in Lessons L1-L27)
# ---------------------------------------------------------------------------

GRILL_QUESTIONS: list[dict[str, Any]] = [
    {
        "id": "mission_and_non_goals",
        "pillar": "1_mission_and_boundaries",
        "field_path": "project.mission / project.non_goals",
        "question": (
            "In one sentence, what is the concrete production mission of this system, "
            "and what are at least two explicit NON-GOALS agents must never build?"
        ),
        "why_we_ask": (
            "Without explicit non-goals and narrow escalation boundaries, autonomous "
            "agents either hallucinate scope creep or stall asking the human PO for "
            "product decisions they should refuse (Lesson L4, L15)."
        ),
        "lessons": ["L4", "L15", "L25"],
        "challenge_prompt": (
            "Your mission or non-goals are too broad. Name the exact API/UI surface "
            "that defines 'done' and list 2 concrete adjacent features agents must "
            "reject if tempted to build."
        ),
    },
    {
        "id": "branch_isolation_topology",
        "pillar": "1_mission_and_boundaries",
        "field_path": "vcs.enabled / vcs.repo / vcs.build_branch / vcs.eval_branch",
        "question": (
            "Do you want Git and GitHub now, or start without Git and add it later? "
            "With Git: which repository, which branch does the Build Team push to "
            "(default: `main`), and which disjoint orphan branch does the Evaluation "
            "Team use (default: `eval-dashboard`)? Without Git: the factory uses a file "
            "backlog and snapshot deploys, and `scion-factory enable-git` adds Git later."
        ),
        "why_we_ask": (
            "If build and eval agents share the same branch or workspace, eval agents "
            "can overwrite application code or grade their own unmerged files instead "
            "of the live deployment (Lesson L18, L23, L25)."
        ),
        "lessons": ["L18", "L23", "L25"],
        "challenge_prompt": (
            "If you choose Git, confirm that the evaluation branch is an isolated orphan "
            "branch with zero shared history with the build branch. If you skip Git, "
            "confirm that the evaluation team gets its own Hub project workspace."
        ),
    },
    {
        "id": "stack_verification_commands",
        "pillar": "2_runtime_data_and_iam",
        "field_path": "stack.build_command / stack.test_command / stack.lint_command",
        "question": (
            "What are the exact, deterministic CLI commands for build, unit test, "
            "and lint inside the container before any agent may acquire the commit slot?"
        ),
        "why_we_ask": (
            "In a shared `/workspace` with a commit slot, any broken commit blocks "
            "every specialist agent in the project (Lesson L18, L23)."
        ),
        "lessons": ["L18", "L23"],
        "challenge_prompt": (
            "Give the exact shell commands (e.g., `go test -race ./...`, `pytest -q`) "
            "that exit non-zero on regression and run in under 60 seconds."
        ),
    },
    {
        "id": "deploy_target",
        "pillar": "2_runtime_data_and_iam",
        "field_path": "infrastructure.deploy",
        "question": (
            "Does this factory deploy a service (`service`), or is there nothing to deploy (`none`): a library, "
            "a CLI, or a package of a monorepo?"
        ),
        "why_we_ask": (
            "Factories with nothing to deploy used placeholder `none` services, so their deployers and live checks "
            "had nothing to work on. With `none`, the factory has no deployer, no service and no live checks, and "
            "the evaluation team grades the code itself: build and tests, API compatibility, a quality metric, "
            "dependency and vulnerability checks, and benchmarks (Lesson L29)."
        ),
        "lessons": ["L29"],
        "challenge_prompt": "Is there a Cloud Run service to deploy? A library, CLI or monorepo package is `none`",
    },
    {
        "id": "cloud_stores_and_deployer_iam",
        "pillar": "2_runtime_data_and_iam",
        "field_path": "infrastructure.cloud_stores / infrastructure.deployer_iam_roles",
        "question": (
            "Which cloud backing stores does the system require, and what exact IAM roles "
            "must be pre-granted to `deployer-sa` before Wave 1 starts? Default to serverless "
            "services: Cloud Run, Firestore, BigQuery, Cloud Storage, and Vertex AI."
        ),
        "why_we_ask": (
            "In the reference factory, the deployer agent repeatedly blocked in Wave 1 because "
            "`deployer-sa` lacked data-store roles such as `roles/bigquery.dataEditor`, "
            "forcing human intervention mid-sprint (Lesson L5, L13). Serverless stores also "
            "remove instance sizing and idle cost from the factory's blast radius."
        ),
        "lessons": ["L5", "L13", "L23"],
        "challenge_prompt": (
            "Don't say 'standard GCP permissions'. List the exact `roles/...` strings the "
            "`deployer-sa` service account needs to create indexes or datasets and deploy "
            "Cloud Run. If you want a provisioned store (Spanner, AlloyDB, Cloud SQL) instead "
            "of a serverless one such as Firestore, state the requirement that Firestore or "
            "BigQuery can't meet."
        ),
    },
    {
        "id": "team_roster_and_harnesses",
        "pillar": "3_roster_models_and_cost",
        "field_path": "team.build_roles / team.eval_roles",
        "question": (
            "Confirm the default roster (lead, backend, reviewer, plus the three default specialists: "
            "`researcher`, `doc-writer`, `investigator` on build, and `eval-lead`, `domain-evaluator` on eval). "
            "Review each proposed specialist from discovery with its evidence, add confirmed specialists "
            "with `./scripts/scion-factory add-role`, record drops under `team.dropped_defaults` with a reason, "
            "confirm cross-family harness/model pairs (`team.verification_policy: cross-family`) plus "
            "`team.reviewer_replicas` when concurrent WIP is 4 or higher, and decide whether the repository stays "
            "flat (`lead -> specialists`, default for `< 8` non-lead roles or Single-VM `runtime: docker`) or groups "
            "`3+` disjoint domains (`8+` active build roles) under optional `sub-coordinator` roles "
            "(`--role sub-coordinator --as <domain>-sub-coordinator` and `--reports-to`)."
        ),
        "why_we_ask": (
            "Each harness has distinct runtime requirements: OpenCode Gemini 4 Argon "
            "requires `GOOGLE_CLOUD_LOCATION=global` (L6), Hermes requires an OpenRouter "
            "wrapper (L7), and Antigravity hooks must set `SCION_TELEMETRY_ENABLED=false` "
            "to avoid 60s OTLP timeouts (L22)."
        ),
        "lessons": ["L6", "L7", "L8", "L16", "L22"],
        "challenge_prompt": (
            "Confirm the default roster with three default specialists (`researcher`, `doc-writer`, `investigator`). "
            "Review each proposed specialist with its evidence, drop a default under `team.dropped_defaults` only with a reason, and add a "
            "`deployer` or `eval-deployer` only when `infrastructure.deploy` is `service`."
        ),
    },
    {
        "id": "heartbeat_and_budget_guardrails",
        "pillar": "3_roster_models_and_cost",
        "field_path": "team.heartbeat_mode / team.max_wip / infrastructure.budget_usd",
        "question": (
            "Should the 15-minute schedule wake `lead` directly (`direct-lead`, lower agent count) "
            "or wake a cheap Flash sensor agent (`dedicated-sensor`, lower Opus token burn when idle), "
            "what is your max concurrent WIP per wave, and what is your GCP budget cap (USD)?"
        ),
        "why_we_ask": (
            "Waking an Opus lead every 15 minutes when nothing changed burns tokens; "
            "conversely, unlimited WIP causes merge contention in shared `/workspace` (Lesson L9, L10, L17, L21)."
        ),
        "lessons": ["L9", "L10", "L17", "L21", "L24"],
        "challenge_prompt": (
            "Specify `direct-lead` or `dedicated-sensor`, a numeric `max_wip` (recommended: 2), "
            "and a hard `budget_usd` ceiling."
        ),
    },
    {
        "id": "eval_l1_l2_contracts",
        "pillar": "4_evaluation_pyramid_oracle",
        "field_path": "evaluation_pyramid.l1_health / evaluation_pyramid.l2_contract",
        "question": (
            "What is the L1 liveness endpoint and what are the L2 API/schema invariants "
            "that must hold on every single HTTP response?"
        ),
        "why_we_ask": (
            "Before running expensive domain or LLM evaluations, the eval team must "
            "verify that the live Cloud Run service is healthy and honors its structural "
            "API contracts (Lesson L15, L23)."
        ),
        "lessons": ["L15", "L23"],
        "challenge_prompt": (
            "Name the exact endpoints (e.g., `GET /health`, `POST /api/v1/...`) and "
            "at least two falsifiable structural invariants."
        ),
    },
    {
        "id": "eval_l3_metric",
        "pillar": "4_evaluation_pyramid_oracle",
        "field_path": "evaluation_pyramid.l3_domain_quality",
        "question": (
            "What is the primary quantitative L3 domain quality metric (and numeric "
            "pass threshold) that replaces `nDCG@10` for this project, and what synthetic "
            "probe or golden dataset computes it?"
        ),
        "why_we_ask": (
            "In `psearch`, L3 was `ndcg_at_10 >= 0.80`; in `acme-airlines`, L3 was "
            "`constraint_satisfaction_rate >= 0.95`. Without a numeric L3 oracle, the "
            "Evaluation Team cannot grade waves or file regression issues (Lesson L15, L25)."
        ),
        "lessons": ["L15", "L25"],
        "challenge_prompt": (
            "'Works well' or 'passes tests' is not an L3 metric. Name a specific metric "
            "slug (e.g. `reconciliation_accuracy`, `schedule_recovery_score`), a numeric "
            "target threshold, and how the `domain-evaluator` computes it against the live URL."
        ),
    },
    {
        "id": "eval_l5_slo",
        "pillar": "4_evaluation_pyramid_oracle",
        "field_path": "evaluation_pyramid.l4_safety_grounding / evaluation_pyramid.l5_latency_slo",
        "question": (
            "What are the L4 safety/grounding/fallback invariants (when an upstream dependency "
            "or LLM fails), and what are the exact L5 latency SLOs (`p50_ms`, `p95_ms`, `p99_ms`)?"
        ),
        "why_we_ask": (
            "Autonomous agents often optimize accuracy by adding slow sequential LLM calls "
            "or brittle external calls without fallback. L4 and L5 gates catch latency "
            "regressions and ungrounded outputs automatically (Lesson L15, L27)."
        ),
        "lessons": ["L15", "L27"],
        "challenge_prompt": (
            "Provide concrete millisecond numbers for `p50_ms`, `p95_ms`, and `p99_ms`, "
            "plus at least one fail-closed or graceful degradation invariant."
        ),
    },
    {
        "id": "wave_1_backlog",
        "pillar": "5_backlog_waves_and_autonomy",
        "field_path": "waves",
        "question": (
            "What are the initial Wave 1 and Wave 2 slices (issue titles, owner role, "
            "falsifiable acceptance criteria, and the REQ- IDs each issue delivers) that "
            "seed the backlog (GitHub issues with Git, backlog/issues.json without)?"
        ),
        "why_we_ask": (
            "The Lead agent drives work exclusively from GitHub issues (`status:ready` -> "
            "`status:in-progress` -> `status:in-review` -> `status:verified`). Without "
            "well-scoped Wave 1 issues and acceptance criteria, the first heartbeat "
            "has nothing actionable to dispatch (Lesson L1, L2, L18)."
        ),
        "lessons": ["L1", "L2", "L18"],
        "challenge_prompt": (
            "Break Wave 1 into 2-4 vertical slices, assign each to a build role, "
            "and write at least one verifiable acceptance criterion per issue."
        ),
    },
]


GRILL_QUESTIONS.extend([
    {
        "id": "knowledge_sources",
        "pillar": "6_knowledge_plane",
        "field_path": "knowledge_plane.sources",
        "question": (
            "Which existing documents define this product: requirements or PRD, "
            "architecture decisions, API or data contracts, design docs, and contributor "
            "guides? Confirm the sources detected in the repository, or name the ones "
            "that live elsewhere."
        ),
        "why_we_ask": (
            "Agents are only as good as the knowledge they can open. When the reviewer "
            "couldn't find the real ADR and migration paths, it guessed file names, "
            "issued a wrong FAIL, and the factory deadlocked for about 60 hours (Lesson L4)."
        ),
        "lessons": ["L4", "L15"],
        "challenge_prompt": (
            "'It's all in people's heads' means the agents start blind. Point to at least "
            "one file per kind, or agree that we write the missing pieces now in this interview."
        ),
    },
    {
        "id": "knowledge_requirements_invariants",
        "pillar": "6_knowledge_plane",
        "field_path": "knowledge_plane.requirements / knowledge_plane.invariants / knowledge_plane.decisions",
        "question": (
            "From those sources (or from scratch), what are the requirements with stable "
            "IDs and acceptance criteria, which MUST rules every change keeps, and which "
            "architecture decisions are already made?"
        ),
        "why_we_ask": (
            "Requirement IDs connect backlog items, ADRs, reviews, and evaluation results. "
            "Invariants are the reviewer's checklist; without them reviews are opinions "
            "and regressions ship (Lesson L15, L18, L25)."
        ),
        "lessons": ["L15", "L18", "L25"],
        "challenge_prompt": (
            "Each requirement needs an ID like REQ-001, a title, and at least one testable "
            "acceptance criterion. Each invariant must be something a reviewer can verify "
            "in a diff or with a command."
        ),
    },
])


def get_grill_questions() -> list[dict[str, Any]]:
    """Return the 5-pillar Socratic question bank grounded in Lessons L1-L27."""
    return [dict(q) for q in GRILL_QUESTIONS]


# ---------------------------------------------------------------------------
# Spec Loading / Saving
# ---------------------------------------------------------------------------

def load_spec(path: str | Path) -> dict[str, Any]:
    """Load a FactorySpec from YAML or JSON."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".json":
        return json.loads(text)
    if yaml is not None:
        data = yaml.safe_load(text)
        if not isinstance(data, dict):
            raise ValueError(f"Expected mapping at root of {p}")
        return data
    return json.loads(text)


def save_spec(spec: dict[str, Any], path: str | Path) -> None:
    """Save a FactorySpec to YAML (preferred) or JSON."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.suffix.lower() == ".json" or yaml is None:
        p.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    else:
        p.write_text(yaml.safe_dump(spec, sort_keys=False, width=100), encoding="utf-8")


# ---------------------------------------------------------------------------
# Static Repository Introspection (`inspect_repository`)
# ---------------------------------------------------------------------------

# Manifests that mark a directory as an app with a known stack.
_STACK_MANIFESTS = ("go.mod", "pyproject.toml", "requirements.txt", "package.json", "Cargo.toml")
# Conventional folders that hold several apps (each child is an app) and single-app folders at the root.
_APP_CONTAINERS = ("src", "apps", "services", "packages")
_APP_DIRS = ("backend", "frontend", "server", "api")
_APP_KEYS = ("path", "language", "framework", "build_command", "test_command", "lint_command")
_APP_WALK_SKIP = {".venv", "venv", "env", "__pycache__", "dist", "build", "target", ".next", "coverage"}
_APP_WALK_MAX_FILES = 20000
_LANG_SOURCE_EXTS = {"python": (".py",), "typescript": (".ts", ".tsx", ".js", ".jsx"), "go": (".go",)}


def _inspect_stack_dir(directory: Path, cloud_stores: set[str]) -> dict[str, str]:
    """Detect the stack of one directory from its manifests; adds cloud stores it finds to `cloud_stores`.

    Returns {language, framework, build_command, test_command, lint_command}; language is "unknown" when the
    directory has no Go, Python or Node manifest. Commands run from `directory`.
    """
    stack = {"language": "unknown", "framework": "", "build_command": "", "test_command": "", "lint_command": ""}

    # Detect Go
    go_mod = directory / "go.mod"
    if go_mod.exists():
        stack["language"] = "go"
        stack["build_command"] = "go build -buildvcs=false ./..."
        stack["test_command"] = "go test ./..."
        stack["lint_command"] = "go vet ./..."
        mod_text = go_mod.read_text(errors="ignore").lower()
        if "spanner" in mod_text:
            cloud_stores.add("spanner")
        if "bigquery" in mod_text:
            cloud_stores.add("bigquery")
        if "aiplatform" in mod_text or "genai" in mod_text or "vertex" in mod_text:
            cloud_stores.add("vertex-ai")
        if "storage" in mod_text:
            cloud_stores.add("gcs")
        if "firestore" in mod_text:
            cloud_stores.add("firestore")

    # Detect Python (pyproject.toml and/or requirements.txt)
    py_files = [p for p in (directory / "pyproject.toml", directory / "requirements.txt") if p.exists()]
    if py_files:
        if stack["language"] == "unknown":
            stack["language"] = "python"
            stack["build_command"] = "python3 -m compileall ."
            stack["test_command"] = "pytest -q"
            stack["lint_command"] = "ruff check ."
        py_text = "\n".join(p.read_text(errors="ignore") for p in py_files).lower()
        if "spanner" in py_text:
            cloud_stores.add("spanner")
        if "bigquery" in py_text:
            cloud_stores.add("bigquery")
        if "aiplatform" in py_text or "genai" in py_text or "vertex" in py_text:
            cloud_stores.add("vertex-ai")
        if "firestore" in py_text:
            cloud_stores.add("firestore")
        if "google-cloud-storage" in py_text:
            cloud_stores.add("gcs")
        if "fastapi" in py_text:
            stack["framework"] = "FastAPI"
        elif re.search(r"(?<![a-z0-9_-])flask(?![a-z0-9_])", py_text):
            stack["framework"] = "Flask"

    # Detect Node/TypeScript
    pkg_json = directory / "package.json"
    if pkg_json.exists():
        if stack["language"] == "unknown":
            stack["language"] = "typescript"
            stack["build_command"] = "npm run build"
            stack["test_command"] = "npm test"
            stack["lint_command"] = "npm run lint"
        pkg_text = pkg_json.read_text(errors="ignore").lower()
        if "spanner" in pkg_text:
            cloud_stores.add("spanner")
        if "bigquery" in pkg_text:
            cloud_stores.add("bigquery")

    # Override test/build commands if Makefile defines them
    makefile = directory / "Makefile"
    if makefile.exists():
        mk_text = makefile.read_text(errors="ignore")
        m_test = re.search(r"^test:\s*\n\t(.+)$", mk_text, flags=re.MULTILINE)
        if m_test:
            stack["test_command"] = m_test.group(1).strip()

    return stack


def _is_test_file(name: str, language: str) -> bool:
    if language == "python":
        return (name.startswith("test_") or name.endswith("_test.py")) and name.endswith(".py")
    if language == "typescript":
        return bool(re.search(r"\.(test|spec)\.[jt]sx?$", name))
    if language == "go":
        return name.endswith("_test.go")
    return False


def _count_app_files(directory: Path, language: str) -> tuple[int, bool]:
    """Return (source files, has tests) for one app, skipping vendored, virtualenv and build output dirs."""
    exts = _LANG_SOURCE_EXTS.get(language, ())
    sources, has_tests, seen = 0, False, 0
    for dirpath, dirnames, filenames in os.walk(directory):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in discovery.SKIP_DIRS and d not in _APP_WALK_SKIP and not d.startswith(".")
        )
        for name in sorted(filenames):
            seen += 1
            if seen > _APP_WALK_MAX_FILES:
                return sources, has_tests
            if _is_test_file(name, language):
                has_tests = True
            elif name.endswith(exts):
                sources += 1
    return sources, has_tests


def _app_candidates(root: Path) -> list[Path]:
    """Conventional app folders: src/, apps/, services/, packages/ and their children, then backend/ and co."""
    found: list[Path] = []
    for container in _APP_CONTAINERS:
        base = root / container
        if not base.is_dir() or base.is_symlink():
            continue
        found.append(base)
        found.extend(
            child for child in sorted(base.iterdir())
            if child.is_dir() and not child.is_symlink() and not child.name.startswith(".")
            and child.name not in discovery.SKIP_DIRS
        )
    found.extend(root / d for d in _APP_DIRS if (root / d).is_dir() and not (root / d).is_symlink())
    return found


def _find_apps(root: Path, cloud_stores: set[str]) -> list[dict[str, Any]]:
    """Inspect conventional app folders; returns apps with a known stack, in path order, commands `cd`-prefixed."""
    apps: list[dict[str, Any]] = []
    for directory in _app_candidates(root):
        if not any((directory / m).exists() for m in _STACK_MANIFESTS):
            continue
        stack = _inspect_stack_dir(directory, cloud_stores)
        if stack["language"] == "unknown":
            continue
        rel = directory.relative_to(root).as_posix()
        app: dict[str, Any] = {"path": rel, "language": stack["language"], "framework": stack["framework"]}
        for key in ("build_command", "test_command", "lint_command"):
            app[key] = f"cd {shlex.quote(rel)} && {stack[key]}" if stack[key] else ""
        app["source_files"], app["has_tests"] = _count_app_files(directory, stack["language"])
        apps.append(app)
    apps.sort(key=lambda a: a["path"])
    return apps


def inspect_repository(repo_path: str | Path) -> dict[str, Any]:
    """Inspect an existing repository to auto-detect stack, commands, ports, and stores."""
    root = Path(repo_path)
    detected: dict[str, Any] = {
        "primary_language": "unknown",
        "framework": "",
        "build_command": "",
        "test_command": "",
        "lint_command": "",
        "port": 8080,
        "cloud_stores": [],
        "existing_files": [],
    }

    cloud_stores: set[str] = set()
    for candidate in (
        "go.mod",
        "package.json",
        "pyproject.toml",
        "requirements.txt",
        "Cargo.toml",
        "Dockerfile",
        "Makefile",
        "Justfile",
        "AGENTS.md",
    ):
        if (root / candidate).exists():
            detected["existing_files"].append(candidate)

    root_stack = _inspect_stack_dir(root, cloud_stores)
    for key in ("framework", "build_command", "test_command", "lint_command"):
        detected[key] = root_stack[key]
    detected["primary_language"] = root_stack["language"]
    detected["apps"] = []

    # Multi-app repos (src/backend, apps/web, ...) carry no stack manifest at the root: look one or two levels
    # down in conventional app folders and take the top-level stack from the best app.
    if not any((root / m).exists() for m in _STACK_MANIFESTS):
        apps = _find_apps(root, cloud_stores)
        detected["apps"] = [{k: a[k] for k in _APP_KEYS} for a in apps]
        if apps:
            best = min(apps, key=lambda a: (not a["has_tests"], -a["source_files"], a["path"]))
            detected["primary_language"] = best["language"]
            for key in ("framework", "build_command", "test_command", "lint_command"):
                # A root Makefile `test:` target still wins over the app's test command.
                if key == "test_command" and detected["test_command"]:
                    continue
                detected[key] = best[key]
            detected["_primary_app_dir"] = best["path"]

    # Detect exposed port from Dockerfile
    dockerfile = root / "Dockerfile"
    if not dockerfile.exists() and detected.get("_primary_app_dir"):
        dockerfile = root / detected["_primary_app_dir"] / "Dockerfile"
    detected.pop("_primary_app_dir", None)
    if dockerfile.exists():
        df_text = dockerfile.read_text(errors="ignore")
        m_port = re.search(r"^\s*EXPOSE\s+(\d+)", df_text, flags=re.MULTILINE | re.IGNORECASE)
        if m_port:
            detected["port"] = int(m_port.group(1))

    detected["cloud_stores"] = sorted(cloud_stores)
    detected["knowledge_sources"] = kp.detect_knowledge_sources(root)
    detected["vcs"] = kp.detect_vcs(root)
    detected["proposed_roles"] = discovery.discover_roles(root)

    # Filter which grill questions remain unresolved even after static inspection
    unresolved = []
    for q in get_grill_questions():
        if q["id"] == "stack_verification_commands" and detected["primary_language"] != "unknown" and detected["test_command"]:
            continue
        unresolved.append(q)

    return {
        "repo_path": str(root),
        "detected": detected,
        "unresolved_questions": unresolved,
    }


# ---------------------------------------------------------------------------
# Team Model: default specialists, explicit drops, autonomy, deploy
# ---------------------------------------------------------------------------
#
# Every roster carries the library's always-roles (role_library.ALWAYS_BUILD / ALWAYS_EVAL). New drafts get them with
# `default: true`; older specs and presets get the missing ones at compile time (`normalize_team`), with a note in the
# kit's AGENTS.md. The PO drops a default only explicitly: `team.dropped_defaults: [{role, reason}]`. A renamed role
# with the same `default_kind` (for example `go-dev` with `default_kind: implementer`) counts as the default.

DEPLOY_MODES = ("service", "none")
AUTONOMY_LEVELS = ("supervised", "standard", "autonomous")
WAKE_MODES = ("direct-lead", "dedicated-sensor", "delta")


def has_eval_project(spec: dict[str, Any]) -> bool:
    """True when `project.eval_slug` names a separate evaluation Hub project; False for single-project factories."""
    return bool(str((spec.get("project") or {}).get("eval_slug") or "").strip())


def wake_mode(spec: dict[str, Any]) -> str:
    """`team.heartbeat_mode`: `direct-lead` when absent, or `dedicated-sensor` / `delta`."""
    return str((spec.get("team") or {}).get("heartbeat_mode") or "direct-lead")


def deploy_mode(spec: dict[str, Any]) -> str:
    """`infrastructure.deploy`: `service` when absent (older specs), otherwise the raw value (the draft's `""` too)."""
    infra = spec.get("infrastructure") or {}
    if "deploy" not in infra:
        return "service"
    value = infra["deploy"]
    return value if isinstance(value, str) else str(value)

def runtime_mode(spec: dict) -> str:
    rt = (spec.get("infrastructure") or {}).get("runtime", "")
    if not rt:
        return "docker"
    return rt



def autonomy(spec: dict[str, Any]) -> str:
    """`team.autonomy`, defaulting to `standard` (spec §7.4)."""
    value = (spec.get("team") or {}).get("autonomy")
    if value is None:
        return "standard"
    return value if isinstance(value, str) else str(value)


# Factories with nothing to deploy (spec §8): the evaluation team grades the code of the build branch's commits, not
# a live service. The interview's L2/L3/L4 answers fill in the invariants and the metric; the L2 and L4 tools come
# from the stack (role_library.CODE_PYRAMID_TOOLS).
ASK_PO_TOOL = "ask the PO for the tool"
NOTHING_TO_DEPLOY = "none (nothing to deploy)"


def _code_tool(spec: dict[str, Any], layer: str) -> str | None:
    lang = str((spec.get("stack") or {}).get("primary_language") or "").strip().lower()
    tools = role_library.CODE_PYRAMID_TOOLS.get({"golang": "go"}.get(lang, lang))
    return tools[layer] if tools else None


def render_code_pyramid(spec: dict[str, Any]) -> str:
    """The five L1-L5 lines of the code evaluation pyramid (spec §8), with the stack's tools."""
    stack = spec["stack"]
    pyramid = spec.get("evaluation_pyramid") or {}
    l3 = pyramid.get("l3_domain_quality") or {}
    vcs = spec.get("vcs") or {}
    lang = stack.get("primary_language", "")
    head = f"the head of `{vcs['build_branch']}`" if _vcs_enabled(spec) else "the build workspace"

    def tool(layer: str) -> str:
        found = _code_tool(spec, layer)
        return f"`{found}`" if found else f"no standard tool for `{lang}` ({ASK_PO_TOOL})"

    def invariants(key: str) -> str:
        items = (pyramid.get(key) or {}).get("invariants") or []
        return f" Invariants: {'; '.join(items)}" if items else ""

    probe = f" ({l3['probe_description']})" if l3.get("probe_description") else ""
    return "\n".join([
        f"1. **L1 Builds and tests:** `{stack['build_command']}` and `{stack['test_command']}` pass at {head}.",
        f"2. **L2 API compatibility:** {tool('l2')} against the last release finds no incompatible change to the "
        f"public API.{invariants('l2_contract')}",
        f"3. **L3 Quality metric:** `{l3.get('primary_metric')} >= {l3.get('target_threshold')}`{probe}",
        f"4. **L4 Safety:** {tool('l4')} finds no known vulnerability in the dependencies.{invariants('l4_safety_grounding')}",
        "5. **L5 Performance:** when the repository has benchmarks, they stay within the recorded baseline; without "
        "benchmarks, L5 passes with a note.",
    ])


def _dropped_defaults(spec: dict[str, Any]) -> set[str]:
    drops = (spec.get("team") or {}).get("dropped_defaults") or []
    return {str(d.get("role")) for d in drops if isinstance(d, dict) and d.get("role")}


def _role_kind(role_spec: dict[str, Any]) -> str:
    """A role's `default_kind`: explicit, else the library's for a library role, else sub-coordinator suffix or role name."""
    return role_library.resolve_role_kind(role_spec)


def normalize_team(spec: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return a deep copy of `spec` with every missing always-role appended, plus notes on what was added.

    A role in `team.dropped_defaults` is never added. A roster role with the same `default_kind` as a missing
    always-role counts as that role, so a renamed implementer doesn't get a second `backend`.
    When `has_eval_project(spec)` is False, `ALWAYS_EVAL` is skipped and `evaluator` is appended to `build_roles`
    unless `evaluator` is in `team.dropped_defaults`.
    """
    out = copy.deepcopy(spec)
    team = out.setdefault("team", {})
    dropped = _dropped_defaults(out)
    notes: list[str] = []
    if has_eval_project(out):
        groups: tuple[tuple[str, tuple[str, ...]], ...] = (
            ("build_roles", role_library.ALWAYS_BUILD),
            ("eval_roles", role_library.ALWAYS_EVAL),
        )
    else:
        groups = (("build_roles", role_library.ALWAYS_BUILD + ("evaluator",)),)
    for key, always in groups:
        roster = team.get(key)
        if not isinstance(roster, list):
            roster = []
            team[key] = roster
        names = {str(r.get("role")) for r in roster if isinstance(r, dict)}
        kinds = {
            _role_kind(r) for r in roster
            if isinstance(r, dict) and not str(r.get("reports_to") or "").strip()
        }
        for role in always:
            if role in dropped or role in names or role_library.ROLES[role]["default_kind"] in kinds:
                continue
            entry = dict(role_library.library_role_entry(role), default=True)
            roster.append(entry)
            names.add(role)
            kinds.add(role_library.ROLES[role]["default_kind"])
            notes.append(f"added default role {role}")
    if verification_policy.verification_policy_mode(out) == "cross-family":
        builders = verification_policy._builder_entries(out)
        ref = verification_policy._reference_builders(builders) if builders else []
        ref_families = {verification_policy.infer_model_family(str(b.get("harness", "")), str(b.get("model", ""))) for b in ref}
        ref_harnesses = {str(b.get("harness", "")).strip().lower() for b in ref}
        for key in ("build_roles", "eval_roles"):
            for entry in team.get(key) or []:
                if isinstance(entry, dict) and entry.get("default") and _role_kind(entry) in verification_policy.VERIFIER_KINDS:
                    v_harness = str(entry.get("harness", "")).strip().lower()
                    v_family = verification_policy.infer_model_family(v_harness, str(entry.get("model", "")))
                    if v_family in ref_families or v_harness in ref_harnesses or not v_harness:
                        opp_h, opp_m = verification_policy.opposing_verifier_pair(out)
                        entry["harness"] = opp_h
                        entry["model"] = opp_m
                        notes.append(f"overrode {entry.get('role')} with opposing verifier {opp_h}/{opp_m}")
    notes.extend(verification_policy.expand_reviewer_replicas(out))
    return out, notes


def add_role(
    spec_path: str | Path,
    role: str,
    team: str | None = None,
    evidence: list[str] | tuple[str, ...] = (),
    *,
    as_role: str | None = None,
    reports_to: str | None = None,
    paths: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Add a library role (optionally aliased via `as_role` or bound via `reports_to`) to a FactorySpec in place.

    Idempotent: a role slug already in either roster is left as is. Raises ValueError for an unknown library role,
    an invalid role slug, a bad team, or a role that needs a deployed service when `infrastructure.deploy: none`.
    """
    lib = role_library.ROLES.get(role)
    if lib is None:
        raise ValueError(f"unknown role {role!r}; library roles: {', '.join(sorted(role_library.ROLES))}")
    target_role = (as_role or role).strip()
    if not _SLUG_RE.match(target_role):
        raise ValueError(f"role name {target_role!r} must be a lowercase slug (e.g. backend-sub-coordinator)")
    team_name = team or lib["team"]
    if team_name not in ("build", "eval"):
        raise ValueError(f"--team must be build or eval, not {team_name!r}")
    spec = load_spec(spec_path)
    if lib["needs_service"] and deploy_mode(spec) == "none":
        raise ValueError(f"{role} needs a deployed service, but this factory has infrastructure.deploy: none")
    roster_team = spec.setdefault("team", {})
    existing_by_name = {
        str(r.get("role")): r
        for key in ("build_roles", "eval_roles")
        for r in (roster_team.get(key) or [])
        if isinstance(r, dict) and r.get("role")
    }
    if target_role in existing_by_name:
        prev = existing_by_name[target_role]
        want_parent = (reports_to or "").strip()
        prev_parent = str(prev.get("reports_to") or "").strip()
        if _role_kind(prev) != lib["default_kind"]:
            raise ValueError(
                f"role {target_role!r} already exists with kind {_role_kind(prev)!r} (cannot alias {role!r})"
            )
        if want_parent and want_parent != prev_parent:
            raise ValueError(
                f"role {target_role!r} already exists with reports_to={prev_parent!r} (got {want_parent!r})"
            )
        return spec
    entry = dict(role_library.library_role_entry(role), default=(lib["kind"] == "always" and target_role == role))
    entry["role"] = target_role
    if target_role != role or role == "sub-coordinator":
        entry["default_kind"] = lib["default_kind"]
    if verification_policy.verification_policy_mode(spec) == "cross-family" and lib["default_kind"] in verification_policy.VERIFIER_KINDS:
        opp_h, opp_m = verification_policy.opposing_verifier_pair(spec)
        entry["harness"] = opp_h
        entry["model"] = opp_m
    if reports_to and reports_to.strip():
        parent = reports_to.strip()
        target_list = [r for r in (roster_team.get(f"{team_name}_roles") or []) if isinstance(r, dict)]
        parent_entry = next((r for r in target_list if str(r.get("role") or "") == parent), None)
        if parent_entry is None or not sub_coordinators.is_sub_coordinator(parent_entry):
            raise ValueError(
                f"--reports-to {parent!r} must reference an existing sub-coordinator role in {team_name}_roles"
            )
        if lib["default_kind"] in sub_coordinators.ROOT_ONLY_KINDS or lib["default_kind"] == "sub-coordinator":
            raise ValueError(f"role {target_role!r} ({lib['default_kind']}) cannot report to a sub-coordinator")
        entry["reports_to"] = parent
    if paths:
        clean_paths = [str(p).strip() for p in paths if str(p).strip()]
        if clean_paths:
            default_focus = role_focus(spec, entry)
            merged_focus: dict[str, list[str]] = {"paths": clean_paths}
            for fkey in ("commands", "docs"):
                if default_focus.get(fkey):
                    merged_focus[fkey] = default_focus[fkey]
            entry["focus"] = merged_focus
    if evidence:
        entry["evidence"] = [str(e) for e in evidence]
    roster_team.setdefault(f"{team_name}_roles", []).append(entry)
    # Adding a dropped default back undoes the drop.
    if roster_team.get("dropped_defaults"):
        roster_team["dropped_defaults"] = [d for d in roster_team["dropped_defaults"]
                                           if not (isinstance(d, dict) and d.get("role") in (role, target_role))]
    save_spec(spec, spec_path)
    return spec


def diversify_team(spec_path: str | Path, reviewer_replicas: int | None = None) -> tuple[dict[str, Any], list[str]]:
    """Enable `cross-family` verification on a FactorySpec and assign opposing verifier models in place."""
    spec = load_spec(spec_path)
    team = spec.setdefault("team", {})
    team["verification_policy"] = "cross-family"
    team.pop("single_harness_reason", None)
    notes = verification_policy.apply_cross_family_defaults(spec)
    if reviewer_replicas is not None:
        if reviewer_replicas < 1 or reviewer_replicas > verification_policy.MAX_REVIEWER_REPLICAS:
            raise ValueError(
                f"--reviewer-replicas must be between 1 and {verification_policy.MAX_REVIEWER_REPLICAS}"
            )
        team["reviewer_replicas"] = reviewer_replicas
    save_spec(spec, spec_path)
    return spec, notes


def _team_readiness(spec: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Errors and warnings for the deploy / autonomy / heartbeat enums, explicit drops, and missing default roles."""
    errors: list[str] = []
    mode = deploy_mode(spec)
    if mode not in DEPLOY_MODES:
        errors.append(f"infrastructure.deploy must be `service` or `none` (got {mode!r}): "
                      "does this factory deploy a service?")
    level = autonomy(spec)
    if level not in AUTONOMY_LEVELS:
        errors.append(f"team.autonomy must be one of {', '.join(AUTONOMY_LEVELS)} (got {level!r})")
    team_cfg = spec.get("team") or {}
    wmode = wake_mode(spec)
    if wmode not in WAKE_MODES:
        errors.append(f"team.heartbeat_mode must be one of {', '.join(WAKE_MODES)} (got {wmode!r})")
    elif wmode != "delta" and team_cfg and _is_vague(team_cfg.get("heartbeat_cron")):
        errors.append("team.heartbeat_cron is required unless team.heartbeat_mode is delta")
    drops = team_cfg.get("dropped_defaults")
    if drops is not None and not isinstance(drops, list):
        errors.append("team.dropped_defaults must be a list of {role, reason}")
        drops = []
    for drop in drops or []:
        if not isinstance(drop, dict) or not drop.get("role"):
            errors.append("team.dropped_defaults entries need a role and a reason")
        elif not str(drop.get("reason") or "").strip():
            errors.append(f"dropped_defaults: {drop['role']} needs a reason")
    warnings = [
        f"team is missing default role {note.removeprefix('added default role ')}: compile adds it "
        "(drop it in team.dropped_defaults with a reason to leave it out)"
        for note in normalize_team(spec)[1]
        if note.startswith("added default role ")
    ]
    div_errors, div_warnings = verification_policy.audit_verification_diversity(spec)
    errors.extend(div_errors)
    warnings.extend(div_warnings)
    hier_errors, _hier_warnings = sub_coordinators.validate_hierarchy(spec, _team_lead)
    errors.extend(hier_errors)
    return errors, warnings


# ---------------------------------------------------------------------------
# Readiness Scorer (`evaluate_spec_readiness`)
# ---------------------------------------------------------------------------

VAGUE_PHRASES = {"tbd", "todo", "works well", "fast", "good", "standard", "n/a", "none"}


def _is_vague(val: Any) -> bool:
    if val is None:
        return True
    if isinstance(val, str):
        s = val.strip().lower()
        return len(s) < 2 or s in VAGUE_PHRASES
    if isinstance(val, (list, dict)):
        return len(val) == 0
    return False


def evaluate_spec_readiness(spec: dict[str, Any]) -> dict[str, Any]:
    """Validate whether a FactorySpec is concrete enough to compile boilerplate."""
    missing: list[str] = []
    has_eval = has_eval_project(spec)

    project = spec.get("project") or {}
    proj_keys = ("name", "slug", "eval_slug", "mission") if has_eval else ("name", "slug", "mission")
    for key in proj_keys:
        if _is_vague(project.get(key)):
            missing.append(f"project.{key} is missing or vague")
    if not isinstance(project.get("non_goals"), list) or len(project.get("non_goals", [])) < 1:
        missing.append("project.non_goals must list at least 1 explicit non-goal (Lesson L4)")

    vcs = spec.get("vcs") or {}
    if vcs.get("enabled"):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", str(vcs.get("repo") or "")):
            missing.append("vcs.repo must be OWNER/NAME when vcs.enabled is true")
        vcs_keys = ("build_branch", "eval_branch") if has_eval else ("build_branch",)
        for key in vcs_keys:
            if _is_vague(vcs.get(key)):
                missing.append(f"vcs.{key} is missing")
        if has_eval and vcs.get("build_branch") and vcs.get("build_branch") == vcs.get("eval_branch"):
            missing.append("vcs.eval_branch must differ from vcs.build_branch (orphan branch isolation, Lesson L25)")

    stack = spec.get("stack") or {}
    for key in ("primary_language", "build_command", "test_command"):
        if _is_vague(stack.get(key)):
            missing.append(f"stack.{key} is missing or vague (Lesson L18)")

    # With nothing to deploy (spec §8) there is no service, deployer, health endpoint or latency SLO to ask for.
    serves = deploy_mode(spec) != "none"
    infra = spec.get("infrastructure") or {}
    if serves:
        svc_keys = ("serving_service", "eval_service") if has_eval else ("serving_service",)
        for key in svc_keys:
            if _is_vague(infra.get(key)):
                missing.append(f"infrastructure.{key} is missing")
        if not isinstance(infra.get("deployer_iam_roles"), list) or len(infra.get("deployer_iam_roles", [])) < 1:
            missing.append("infrastructure.deployer_iam_roles must list explicit GCP IAM roles for deployer-sa (Lesson L5)")

    team = spec.get("team") or {}
    build_roles = team.get("build_roles") or []
    eval_roles = team.get("eval_roles") or []
    if len(build_roles) < 3:
        missing.append("team.build_roles must define at least 3 build roles (e.g. lead, implementer, reviewer)")
    if has_eval and len(eval_roles) < 2:
        missing.append("team.eval_roles must define at least 2 independent evaluation roles (Lesson L25)")
    for role_spec in list(build_roles) + list(eval_roles):
        if _is_vague(role_spec.get("responsibility")):
            missing.append(f"team role '{role_spec.get('role', '?')}' is missing a concrete responsibility")
        if serves:
            continue
        name = role_spec.get("role", "?")
        # By kind, so a renamed deployer ({role: shipper, default_kind: deployer}) is caught too.
        lib_role = _library_role(role_spec)
        lib = role_library.ROLES.get(lib_role) if lib_role else None
        if lib and lib["needs_service"]:
            missing.append(f"team role '{name}' needs a deployed service, but the factory has deploy: none "
                           "(remove the role, or answer deploy: service if the factory deploys one)")
        if role_spec.get("gcp_identity"):
            missing.append(f"team role '{name}' has gcp_identity `{role_spec['gcp_identity']}`, but the factory has "
                           "deploy: none (nothing to deploy needs a deployer identity; remove it)")

    pyramid = spec.get("evaluation_pyramid") or {}
    l1 = pyramid.get("l1_health") or {}
    if serves and _is_vague(l1.get("endpoint")):
        missing.append("evaluation_pyramid.l1_health.endpoint is missing")
    l2 = pyramid.get("l2_contract") or {}
    if not l2.get("invariants"):
        missing.append("evaluation_pyramid.l2_contract.invariants must list structural API invariants")
    l3 = pyramid.get("l3_domain_quality") or {}
    if _is_vague(l3.get("primary_metric")):
        missing.append("evaluation_pyramid.l3_domain_quality.primary_metric is missing")
    if not isinstance(l3.get("target_threshold"), (int, float)):
        missing.append("evaluation_pyramid.l3_domain_quality.target_threshold must be numeric")
    l4 = pyramid.get("l4_safety_grounding") or {}
    if not l4.get("invariants"):
        missing.append("evaluation_pyramid.l4_safety_grounding.invariants is missing")
    l5 = pyramid.get("l5_latency_slo") or {}
    if serves and not isinstance(l5.get("p95_ms"), (int, float)):
        missing.append("evaluation_pyramid.l5_latency_slo.p95_ms must be a numeric millisecond SLO")

    waves = spec.get("waves") or []
    if not isinstance(waves, list) or len(waves) < 1:
        missing.append("waves must define at least Wave 1 issues to seed the backlog")
    else:
        first_issues = waves[0].get("issues") or []
        if len(first_issues) < 1:
            missing.append("waves[0].issues must contain at least 1 issue")
        for idx, issue in enumerate(first_issues):
            if not issue.get("acceptance_criteria"):
                missing.append(f"waves[0].issues[{idx}] is missing acceptance_criteria")

    if spec.get("monorepo") is not None:
        prefix, subdir = _monorepo(spec)
        parts = subdir.split("/")
        if not subdir or str((spec.get("monorepo") or {}).get("subdir", "")).startswith("/") or ".." in parts:
            missing.append("monorepo.subdir must be the product folder, relative to the repository root (e.g. pkg/kubelet)")
        if not _SLUG_RE.match(prefix):
            missing.append("monorepo.agent_prefix must be a slug (lowercase letters, digits, single dashes), "
                           "normally the build project slug")

    rt = spec.get("infrastructure", {}).get("runtime", "")
    if rt not in ("", "kubernetes", "docker"):
        missing.append(f"infrastructure.runtime is {rt!r} (must be kubernetes or docker)")
    id_mode = spec.get("infrastructure", {}).get("gcp_identity_mode", "")
    if id_mode not in ("", "assign", "passthrough", "block"):
        missing.append(f"infrastructure.gcp_identity_mode is {id_mode!r} (must be assign, passthrough, or block)")

    missing.extend(kp.validate_knowledge_plane(spec))
    team_errors, warnings = _team_readiness(spec)
    missing.extend(team_errors)
    import topology_audit
    warnings.extend(topology_audit.audit(spec)["warnings"])

    total_checks = 17
    passed = max(0, total_checks - len(missing))
    return {
        "ready": len(missing) == 0,
        "missing_or_weak": missing,
        "score": f"{passed}/{total_checks}",
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Boilerplate Compiler (`compile_factory_boilerplate`)
# ---------------------------------------------------------------------------

def _vcs_enabled(spec: dict[str, Any]) -> bool:
    return bool((spec.get("vcs") or {}).get("enabled"))


# Monorepo factories (docs/plans/2026-10-22-monorepo-factories.md): `monorepo: {subdir, agent_prefix}` makes the kit
# start every agent in `subdir` (factory-cd.sh in the <harness>-factory harness-configs), read the knowledge plane at
# `subdir/knowledge-plane`, and name agents `<agent_prefix>-<role>`.
_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def _monorepo(spec: dict[str, Any]) -> tuple[str, str]:
    mono = spec.get("monorepo") or {}
    return str(mono.get("agent_prefix") or ""), str(mono.get("subdir") or "").strip().strip("/")


def _ws(spec: dict[str, Any]) -> str:
    """The factory's root inside the checkout: /workspace, or /workspace/<subdir> for a monorepo factory."""
    subdir = _monorepo(spec)[1]
    return f"/workspace/{subdir}" if subdir else "/workspace"


def _agent(spec: dict[str, Any], role: str) -> str:
    prefix = _monorepo(spec)[0]
    return f"{prefix}-{role}" if prefix else role


def _factory_label(spec: dict[str, Any]) -> str:
    """GitHub label that scopes a factory's issues: `factory:<agent_prefix or slug>`, for every factory (spec §7.2).

    Factories in one repository share its issues, so one query (`--label status:po-decision`) covers all of them and
    `--label factory:<slug>` picks one factory's backlog.
    """
    return f"factory:{_monorepo(spec)[0] or spec['project']['slug']}"


def _digest_title(spec: dict[str, Any]) -> str:
    """Title of the factory's pinned digest issue (spec §7.3)."""
    return f"{_factory_label(spec).removeprefix('factory:')} digest"


def _monorepo_paths(spec: dict[str, Any], text: str) -> str:
    """Point knowledge-plane and eval-tool paths at the product folder, and name deployers with the prefix."""
    if not _monorepo(spec)[1]:
        return text
    ws = _ws(spec)
    text = text.replace("/workspace/knowledge-plane", f"{ws}/knowledge-plane")
    text = text.replace("/workspace/tools/", f"{ws}/tools/")
    return text.replace("(`deployer`, `eval-deployer`)",
                        f"(`{_agent(spec, 'deployer')}`, `{_agent(spec, 'eval-deployer')}`)")


def _render_shared_agents_md(spec: dict[str, Any]) -> str:
    text = _render_shared_agents_md_base(spec)
    prefix, subdir = _monorepo(spec)
    if not subdir:
        return text
    team = ", ".join(f"`{_agent(spec, r['role'])}`" for r in spec["team"]["build_roles"])
    eval_roles = spec["team"].get("eval_roles") or []
    evals = ", ".join(f"`{_agent(spec, r['role'])}`" for r in eval_roles)
    roster_line = (
        f"- Agent names carry the factory prefix: build team {team}; evaluation team {evals}. Message them with these names."
        if has_eval_project(spec)
        else f"- Agent names carry the factory prefix: build team {team}. Message them with these names."
    )
    return _monorepo_paths(spec, text) + f"""
## 8. Monorepo Product Folder
- This factory owns `{subdir}` in the repository. Your terminal starts there; the rest of the repository is read-only
  context unless the lead assigns work outside the folder. Use `git rev-parse --show-toplevel` for the root.
{roster_line}
- Build and test only what the work touches, inside `{subdir}`; a whole-repository build is not a verification step.
- Other factories share this repository's issues. Your backlog is the issues labeled `{_factory_label(spec)}`: add
  `--label {_factory_label(spec)}` to every `gh issue list` and `gh issue create`, and leave other issues alone.
"""


def _render_shared_agents_md_base(spec: dict[str, Any]) -> str:
    proj = spec["project"]
    stack = spec["stack"]
    infra = spec["infrastructure"]
    vcs = spec.get("vcs") or {}
    l3 = spec["evaluation_pyramid"]["l3_domain_quality"]
    lint = stack.get("lint_command") or "true"
    git = _vcs_enabled(spec)
    serves = deploy_mode(spec) != "none"
    has_eval = has_eval_project(spec)
    is_delta = wake_mode(spec) == "delta"

    if git:
        if has_eval:
            if serves:
                topology = (
                    f"- **Build Project (`{proj['slug']}`):** Pushes application code to `{vcs['build_branch']}` in "
                    f"`{vcs['repo']}` and deploys `{infra['serving_service']}`.\n"
                    f"- **Eval Project (`{proj['eval_slug']}`):** Pushes the independent evaluation control tower to the "
                    f"disjoint orphan branch `{vcs['eval_branch']}` and deploys `{infra['eval_service']}` (Lesson L25)."
                )
            else:
                topology = (
                    f"- **Build Project (`{proj['slug']}`):** Pushes code to `{vcs['build_branch']}` in `{vcs['repo']}`. "
                    "Nothing to deploy (`infrastructure.deploy: none`): a verified commit is the result.\n"
                    f"- **Eval Project (`{proj['eval_slug']}`):** Pushes the independent evaluation control tower to the "
                    f"disjoint orphan branch `{vcs['eval_branch']}` and grades the code of `{vcs['build_branch']}`'s "
                    "commits (Lesson L25)."
                )
        elif serves:
            topology = (
                f"- **Build Project (`{proj['slug']}`):** Single-project factory pushing application code to "
                f"`{vcs['build_branch']}` in `{vcs['repo']}`, deploying `{infra['serving_service']}`, and running "
                "on-demand L1–L5 verification in-project."
            )
        else:
            topology = (
                f"- **Build Project (`{proj['slug']}`):** Single-project factory pushing verified code to "
                f"`{vcs['build_branch']}` in `{vcs['repo']}` and running on-demand L1–L5 code verification in-project. "
                "Nothing to deploy (`infrastructure.deploy: none`): a verified commit is the result."
            )
        flabel = _factory_label(spec)
        delta_backlog_note = (
            "- **Shared-PAT comment attribution (`L14`):** Every GitHub issue body or comment posted by an agent MUST "
            "begin with `<!-- scion-agent: <agent-slug> -->` on line 1. When escalating to `status:po-decision`, post "
            "your `<!-- scion-agent: <agent-slug> -->` comment *before* adding `status:po-decision` so the delta "
            "controller distinguishes agent comments from human PO replies.\n"
            if is_delta
            else ""
        )
        backlog = f"""## 3. Backlog & Status (Lessons L1, L18)
- The backlog is GitHub issues in `{vcs['repo']}`. Status labels move `status:ready` -> `status:in-progress` -> `status:in-review` -> `status:verified` (or `status:blocked`).
- This factory's issues carry `{flabel}`: add `--label {flabel}` to every `gh issue list` and `gh issue create`.
  An issue without it belongs to another factory, or waits for the PO to label it.
- The lead assigns work and moves labels. Every issue cites the `REQ-` IDs it delivers.
- Evaluation findings enter the backlog as issues labeled `eval-finding` and `status:ready` (plus `{flabel}`); the
  build lead works them directly and never files a duplicate.
- The PO accepts work by closing the issue: at `status:verified`, the lead comments the evidence (commit, review,
  checks) on the issue and lists it in the daily digest, and never close an issue yourself.
- **PO inbox:** a decision that waits for the PO is an issue labeled `status:po-decision` and `{flabel}` (the
  question, the options, the lead's recommendation). Only the lead files one.
  `gh issue list -R {vcs['repo']} --label status:po-decision` is the PO's whole inbox.
- **Tell, don't page:** progress and "tell" notices are comments on the issue concerned and on the pinned digest
  issue `{_digest_title(spec)}` (labels `digest` and `{flabel}`), never a Scion message to the PO.
{delta_backlog_note}"""
        asks = "; ".join(a[:1].lower() + a[1:] for a in _po_asks(spec))
        escalation = (
            f"- **Narrow PO escalation (L4):** Only the lead asks the PO, and only for a decision in the \"Waits for the "
            f"PO\" column of its Decision rights (autonomy `{autonomy(spec)}`: {asks}): it files a `status:po-decision` "
            "issue, then sends `scion message user:<PO_USER> \"<one line> <issue URL>\"`. Every message to the PO raises "
            "an ERROR alert (watchdog), so nothing else goes by message. Everyone else escalates to their lead."
        )
        if is_delta:
            build_roles = spec["team"]["build_roles"]
            lead_slug = _agent(spec, _team_lead(build_roles, "lead"))
            reviewer_slug = _kind_agent_slug(spec, build_roles, "reviewer", "reviewer")
            bbranch = vcs["build_branch"]
            integrate = f"""## 4. Issue Branches & Direct Peer Review Handoff (Delta Mode)
Each agent runs in an isolated `/workspace` clone sharing `origin` and `/scion-volumes/scratchpad` (never write `.patch` files to `/scion-volumes/scratchpad/patches/`, and never wait on a commit slot before pushing an issue branch):
1. **Writers (`factory/issue-<N>`):** Fetch `origin/{bbranch}`, create/reset branch `factory/issue-<N>` from `origin/{bbranch}` (`git fetch origin {bbranch} && git checkout -B factory/issue-<N> origin/{bbranch}`), implement with TDD, and run local verification: `{stack['build_command']}`, `{stack['test_command']}`, and `{lint}`.
2. **Clean staging & direct push:** Stage **only** explicit paths (`git add -- <files>`), verify `git diff --cached --name-only` contains no `.scion/`, `.agents/`, `.env`, or secrets, commit, and push directly to `origin/factory/issue-<N>` (`git push -u origin HEAD:factory/issue-<N>`). If a push is rejected by GitHub token scope limits (for example `.github/workflows/*` without `workflow` scope), save restricted files under `/scion-volumes/scratchpad/pending-<issue>/`, push permitted paths on `factory/issue-<N>`, and report the pending patch path.
3. **Direct writer -> `{reviewer_slug}` handoff:** Message `{reviewer_slug}` directly (`scion message {reviewer_slug} "<your-slug> -> {reviewer_slug}: issue #<N> ready on origin/factory/issue-<N> (<sha>)"`) without waking `{lead_slug}`.
4. **Reviewer audit (`origin/{bbranch}...origin/factory/issue-<N>`):** `{reviewer_slug}` fetches `origin/{bbranch}` and `origin/factory/issue-<N>`, audits `git diff origin/{bbranch}...origin/factory/issue-<N>`, and runs `{stack['test_command']}` independently. On the first `FAIL`, `{reviewer_slug}` messages the writer directly (`{lead_slug}` is NOT woken); on `PASS` (or a second `FAIL`), `{reviewer_slug}` writes `/scion-volumes/scratchpad/reports/<N>-review.md` and wakes `{lead_slug}` once.
5. **Trunk merge, cleanup, & auto-stop (`{lead_slug}`):** `{lead_slug}` starts **only** specialists required by active issues (never pre-starting unused roles, and never building virtual environments or running product test suites before delegating), merges verified `origin/factory/issue-<N>` onto `{bbranch}`, pushes `origin/{bbranch}`, deletes remote `factory/issue-<N>` (`git push origin --delete factory/issue-<N>`), persists `/scion-volumes/scratchpad/STATE.json`, and stops idle specialists (`scion stop <agent>`).
6. **Container-local tool environments (`/tmp/.venv`):** Keep any ephemeral Python `venv`, `pip` cache, or `node_modules` inside container-local `/tmp/` (for example `/tmp/.venv`), never on Filestore NFS `/scion-volumes/scratchpad`.
"""
        else:
            k8s_clone_note = (
                "\n- **Per-Pod Clones (`runtime: kubernetes`):** Each agent pod owns an isolated `/workspace` clone and shares "
                "`/scion-volumes/scratchpad` plus `origin` (never uncommitted local files). Before requesting review on uncommitted "
                "work, write `git diff HEAD > /scion-volumes/scratchpad/patches/issue-<N>.patch` (or push `factory/issue-<N>`) so "
                "your reviewer pod can apply, test, and audit the exact diff before trunk merge."
                if runtime_mode(spec) == "kubernetes"
                else ""
            )
            trunks = (
                f"`{vcs['build_branch']}` in `{proj['slug']}`, `{vcs['eval_branch']}` in `{proj['eval_slug']}`"
                if has_eval
                else f"`{vcs['build_branch']}` in `{proj['slug']}`"
            )
            integrate = f"""## 4. Pre-Commit Verification & Commit Slot (Lesson L18)
Before committing or pushing to your team's trunk ({trunks}), ask your lead for the commit slot, then:{k8s_clone_note}
1. Run build, test, and lint verification: `{stack['build_command']}`, `{stack['test_command']}`, and `{lint}`.
2. Check whether your remote trunk moved (`git fetch origin <branch> && git rev-list --count HEAD..origin/<branch>`). When `0`, commit and push directly (`git push origin HEAD:<branch>`) without `git pull --rebase` or `git stash`. When `> 0`, stash **only** your own paths (`git stash push -m "pre-rebase" -- <files>`, never `git stash -u`), rebase onto `origin/<branch>`, pop, and re-verify (or ask your lead if shared files conflict).
3. Stage **only** explicit paths (`git add -- <files>`) and verify `git diff --cached --name-only` contains only your assigned paths (or coupled paths your lead explicitly authorized for a co-commit). Never run `git add -A`, `git add .`, `git add -u`, or `git stash -u`, and never stage or stash `.scion/` or `.agents/`.
4. If `git push origin HEAD:<branch>` is rejected by GitHub token scope limits (for example `.github/workflows/*` without `workflow` scope), save the reviewed restricted files as a patch under `/scion-volumes/scratchpad/pending-<issue>/`, commit and push the permitted files, and report the patch path so your lead opens a `status:po-decision` issue while keeping other backlog items moving.
5. Never commit secrets, tokens, `.env` files, or agent state files.
"""
        deploy = """## 5. Clean Deployment Packaging & Traffic Unpinning (Lesson L23)
Deployer agents (`deployer`, `eval-deployer`) MUST NEVER run `gcloud run deploy --source=/workspace` directly on a shared workspace. Always export the verified commit with `git archive` to a clean temporary directory and pass `--to-latest`:
```bash
sha=$(git -C /workspace rev-parse HEAD)
d=/tmp/deploy-$sha
rm -rf "$d" && mkdir -p "$d"
git -C /workspace archive "$sha" | tar -x -C "$d"
gcloud run deploy <SERVICE_NAME> --source="$d" --to-latest
```
After the deploy, record the revision, the rollback revision, and the commit in `/workspace/knowledge-plane/state/LIVE.md`.
"""
        turn_ref = "issue # and commit SHA"
    else:
        if has_eval:
            if serves:
                topology = (
                    f"- **Build Project (`{proj['slug']}`):** Works in the shared `/workspace` (no version control yet) and "
                    f"deploys `{infra['serving_service']}`.\n"
                    f"- **Eval Project (`{proj['eval_slug']}`):** Works in its own Hub project workspace, isolated from the "
                    f"build workspace, and deploys `{infra['eval_service']}` (Lesson L25)."
                )
            else:
                topology = (
                    f"- **Build Project (`{proj['slug']}`):** Works in the shared `/workspace` (no version control yet). "
                    "Nothing to deploy (`infrastructure.deploy: none`): verified code in the workspace is the result.\n"
                    f"- **Eval Project (`{proj['eval_slug']}`):** Works in its own Hub project workspace, isolated from the "
                    "build workspace, and grades the build workspace's code (Lesson L25)."
                )
        elif serves:
            topology = (
                f"- **Build Project (`{proj['slug']}`):** Single-project factory working in `/workspace` (no version "
                f"control yet) and deploying `{infra['serving_service']}`."
            )
        else:
            topology = (
                f"- **Build Project (`{proj['slug']}`):** Single-project factory working in `/workspace` (no version "
                "control yet). Nothing to deploy (`infrastructure.deploy: none`): verified code in the workspace is the result."
            )
        backlog = """## 3. Backlog & Status (Lessons L1, L18)
- The backlog is `/workspace/backlog/issues.json`. Each item has an `id`, `title`, `owner_role`, `requirements`, `acceptance_criteria`, and `status` (`ready` -> `in-progress` -> `in-review` -> `verified`, or `blocked`).
- The lead is the only writer of `backlog/issues.json`. Workers never edit it; they report progress and results to the lead with `scion message`.
- Every item cites the `REQ-` IDs it delivers.
"""
        integrate = f"""## 4. Edit Slot & Verification (Lesson L18)
The shared `/workspace` has no version control, so a bad edit can't be reverted with Git.
1. Ask the lead for the edit slot before you change files outside the paths assigned to you.
2. Run build verification: `{stack['build_command']}`.
3. Run test verification: `{stack['test_command']}`.
4. Run lint verification: `{lint}`.
5. Report the changed paths to the lead, then release the slot. Never write secrets, tokens, or `.env` files.
"""
        deploy = """## 5. Snapshot Deployment Packaging & Traffic Unpinning (Lesson L23)
Deployer agents (`deployer`, `eval-deployer`) MUST NEVER run `gcloud run deploy --source=/workspace` directly. After the reviewer's PASS, take a snapshot while no edit slot is open, deploy the snapshot, and pass `--to-latest`:
```bash
ts=$(date -u +%Y%m%dT%H%M%SZ)
snap=/tmp/deploy-$ts.tgz
tar -C /workspace --exclude=./.scion --exclude=./node_modules --exclude=./.venv -czf "$snap" .
sum=$(sha256sum "$snap" | cut -d' ' -f1)
d=/tmp/deploy-$ts && mkdir -p "$d" && tar -xzf "$snap" -C "$d"
gcloud run deploy <SERVICE_NAME> --source="$d" --to-latest
```
After the deploy, record the revision, the rollback revision, the snapshot path, and its SHA-256 in `/workspace/knowledge-plane/state/LIVE.md`. Keep the last three snapshots for rollback.
"""
        turn_ref = "backlog item ID and snapshot SHA-256"
        escalation = (
            "- **Narrow PO escalation (L4):** Escalate to the human PO (`scion message user:<PO_USER> \"...\"`) ONLY for "
            "missing IAM roles, missing secrets/quotas, or genuine spec contradictions."
        )

    live_rule = "Only the deployer writes `knowledge-plane/state/LIVE.md`."
    if serves:
        l5 = spec["evaluation_pyramid"]["l5_latency_slo"]
        pyramid = f"""## 7. L1–L5 Evaluation Pyramid Contract
- **L1 Health:** `GET {spec['evaluation_pyramid']['l1_health']['endpoint']}`
- **L2 Contract Invariants:** {'; '.join(spec['evaluation_pyramid']['l2_contract']['invariants'])}
- **L3 Primary Domain Metric:** `{l3['primary_metric']} >= {l3['target_threshold']}` ({l3.get('probe_description', '')})
- **L4 Safety & Grounding:** {'; '.join(spec['evaluation_pyramid']['l4_safety_grounding']['invariants'])}
- **L5 Latency SLO:** `p50 <= {l5.get('p50_ms', 100)}ms`, `p95 <= {l5['p95_ms']}ms`, `p99 <= {l5.get('p99_ms', 500)}ms`
"""
    else:
        # Nothing to deploy (spec §8): no deploy section, no deployer, and the pyramid grades the code.
        live_rule = ("The doc-writer keeps `knowledge-plane/state/LIVE.md` current (nothing is deployed: it records the "
                     "latest release and its source).")
        grader = "The evaluation team grades the code itself" if has_eval else "The `evaluator` specialist grades the code itself in-project"
        pyramid_intro = (
            "The evaluation team grades the code of the build's commits, not a live service:"
            if has_eval
            else "The `evaluator` specialist grades the code of the build's commits in-project, not a live service:"
        )
        deploy = f"""## 5. Nothing to Deploy
This factory has `infrastructure.deploy: none` (a library, a CLI or a package): there is no service and no
deployer, and no agent deploys anything. {grader} (section 7).
"""
        pyramid = f"""## 7. L1–L5 Code Evaluation Pyramid
{pyramid_intro}
{render_code_pyramid(spec)}
"""

    header_slugs = f"`{proj['slug']}` & `{proj['eval_slug']}`" if has_eval else f"`{proj['slug']}`"
    use_shared_kp = runtime_mode(spec) == "kubernetes" or is_delta
    if use_shared_kp:
        kp_reader_line = (
            f"- Build agents check `{kp.EVAL_KP_MOUNT}/INDEX.md` first (with fallback `/workspace/knowledge-plane/INDEX.md`) before planning, implementing, reviewing, or deploying. Evaluation agents read the same knowledge plane read-only at `{kp.EVAL_KP_MOUNT}/INDEX.md`."
            if has_eval
            else f"- Every agent checks `{kp.EVAL_KP_MOUNT}/INDEX.md` first (with fallback `/workspace/knowledge-plane/INDEX.md`) before planning, implementing, reviewing, evaluating, or deploying."
        )
        kp_check_line = (
            "- Run `python3 /workspace/knowledge-plane/kp-check.py` only when `/workspace/knowledge-plane/kp-check.py` is writable in Git (skip when reading the mounted `/scion-volumes/knowledge-plane` shared-dir), and fix every problem it reports."
        )
    else:
        kp_reader_line = (
            f"- Build agents read `/workspace/knowledge-plane/INDEX.md` before planning, implementing, reviewing, or deploying. Evaluation agents read the same knowledge plane read-only at `{kp.EVAL_KP_MOUNT}/INDEX.md`."
            if has_eval
            else "- Every agent reads `/workspace/knowledge-plane/INDEX.md` before planning, implementing, reviewing, evaluating, or deploying."
        )
        kp_check_line = "- After you change the knowledge plane, run `python3 /workspace/knowledge-plane/kp-check.py` and fix every problem it reports."
    delta_comm_note = (
        "\n- **Ignore bare `state-change` (`COMPLETED`) notifications:** If woken solely by a bare system `state-change` "
        "(`COMPLETED`) event with no `DELTA:` payload or actionable peer message, run zero shell commands and immediately "
        "call `sciontool status task_completed \"ignored bare state-change\"`."
        if is_delta
        else ""
    )

    return f"""# Shared Factory Agent Protocol — {proj['name']} ({header_slugs})

Every agent in this Scion software factory MUST follow these operational rules (derived from Lessons L1–L27).

## 1. Mission & Non-Goals
- **Mission:** {proj['mission']}
{topology}
- **Explicit Non-Goals (Never Build):**
{chr(10).join(f'  - {ng}' for ng in proj.get('non_goals', []))}

## 2. Knowledge Plane (Lesson L4)
{kp_reader_line}
- Open files through the paths in the index. Never guess a file name.
- Cite `REQ-` and `ADR-` IDs in work items, reviews, and evaluation results. Record new decisions with `knowledge-plane/adr/_template.md`.
- {live_rule} Evaluation golden datasets never go into the build knowledge plane.
{kp_check_line}

{backlog}
{integrate}
{deploy}
## 6. Communication & Turn Completion (Lessons L1, L2, L4, L5)
- **Verify message delivery (L1, L4):** Always check the exit code and `Message delivered` confirmation of `scion message <recipient> "<msg>"` (printed text in your own output reaches nobody), confirm recipient names reside in the same Scion project, and use `scion message <recipient> --body-file <file>` for long or multi-line messages to avoid shell quoting bugs.
- **Signal turn completion (L2, L5):** End every turn by running `sciontool status task_completed "<concise summary with {turn_ref}>"`, `sciontool status blocked "waiting: <what, from whom>"` when waiting on a teammate, or `sciontool status ask_user "<blocker>"` when genuinely blocked on a human.{delta_comm_note}
{escalation}

{pyramid}"""


def _render_opencode_launcher(model: str) -> str:
    oc_model = model if "/" in model else "google-vertex/gemini-4-argon"
    return f"""#!/bin/sh
# Pinned OpenCode Vertex AI launcher generated by scion_factory.py (Lessons L3, L26).
export GOOGLE_CLOUD_PROJECT="${{GOOGLE_CLOUD_PROJECT:-${{HUB_PROJECT:-}}}}"
export GOOGLE_CLOUD_LOCATION="${{GOOGLE_CLOUD_LOCATION:-global}}"
export GOOGLE_CLOUD_REGION="${{GOOGLE_CLOUD_REGION:-global}}"
export GOOGLE_VERTEX_PROJECT="$GOOGLE_CLOUD_PROJECT"
export GOOGLE_VERTEX_LOCATION="$GOOGLE_CLOUD_LOCATION"
export VERTEXAI_PROJECT="$GOOGLE_CLOUD_PROJECT"
export VERTEX_LOCATION="$GOOGLE_CLOUD_LOCATION"

mkdir -p "$HOME/.config/opencode" "$HOME/.local/state/opencode" "$HOME/.cache/opencode"
rm -f "$HOME/.config/opencode/.opencode.json"

cat << 'JSON' > "$HOME/.cache/opencode/models.json"
{{
  "google-vertex": {{
    "id": "google-vertex",
    "name": "Vertex",
    "env": ["GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"],
    "npm": "@ai-sdk/google-vertex",
    "models": {{
      "gemini-4-argon": {{
        "id": "gemini-4-argon",
        "name": "Gemini 4 Argon",
        "tool_call": true,
        "reasoning": true,
        "attachment": true,
        "temperature": true,
        "cost": {{"input": 1.25, "output": 10}},
        "limit": {{"context": 1048576, "output": 65536}}
      }},
      "gemini-3.8-flash": {{
        "id": "gemini-3.8-flash",
        "name": "Gemini 3.8 Flash",
        "tool_call": true,
        "reasoning": true,
        "attachment": true,
        "temperature": true,
        "cost": {{"input": 0.15, "output": 0.6}},
        "limit": {{"context": 1048576, "output": 65536}}
      }}
    }}
  }}
}}
JSON

cat << 'JSON' > "$HOME/.config/opencode/opencode.json"
{{
  "$schema": "https://opencode.ai/config.json",
  "model": "{oc_model}",
  "small_model": "google-vertex/gemini-3.8-flash",
  "disabled_providers": ["google-vertex-anthropic", "opencode"]
}}
JSON

cat << 'JSON' > "$HOME/.local/state/opencode/model.json"
{{"recent": [{{"providerID": "google-vertex", "modelID": "gemini-4-argon"}}], "favorite": [], "variant": {{}}}}
JSON

# OpenCode treats GITHUB_TOKEN as a Copilot token; Scion's git credential helper reads SCION_GIT_TOKEN.
if [ -n "${{GITHUB_TOKEN:-}}" ]; then
  export SCION_GIT_TOKEN="$GITHUB_TOKEN"
  unset GITHUB_TOKEN
fi
exec /usr/local/bin/opencode --auto -m "{oc_model}" "$@"
"""


def _render_hermes_launcher(model: str) -> str:
    hm_model = model if "/" in model else "google/gemini-4-argon"
    return f"""#!/bin/sh
# Pinned Hermes Vertex AI launcher generated by scion_factory.py (Lessons L4, L26).
mkdir -p "$HOME/.hermes"
touch "$HOME/.hermes/.env"
grep -v '^VERTEX_REGION=' "$HOME/.hermes/.env" > "$HOME/.hermes/.env.tmp" 2>/dev/null || true
echo "VERTEX_REGION=global" >> "$HOME/.hermes/.env.tmp"
mv "$HOME/.hermes/.env.tmp" "$HOME/.hermes/.env"

export VERTEX_REGION=global
export GOOGLE_CLOUD_LOCATION="${{GOOGLE_CLOUD_LOCATION:-global}}"
PROJ="${{GOOGLE_CLOUD_PROJECT:-${{HUB_PROJECT:-}}}}"

cat << YAML > "$HOME/.hermes/config.yaml"
provider: vertex
model: {hm_model}
hooks_auto_accept: true
vertex:
  project_id: $PROJ
  region: global
YAML

TASK=""
while [ $# -gt 0 ]; do
  case "$1" in
    --prompt)
      shift
      TASK="${{1:-}}"
      ;;
    --continue)
      ;;
  esac
  shift 2>/dev/null || break
done

if [ -n "$TASK" ]; then
  (sleep 4 && tmux send-keys -t scion -l "$TASK" && tmux send-keys -t scion Enter) &
fi

exec /usr/local/bin/hermes chat --yolo --accept-hooks --provider vertex -m "{hm_model}"
"""


def _render_scion_agent_yaml(role_spec: dict[str, Any], spec: dict[str, Any] | None = None) -> str:
    role = role_spec["role"]
    harness = role_spec["harness"]
    model = role_spec["model"]
    subdir = _monorepo(spec)[1] if spec else ""
    use_factory_hc = bool(subdir) or (spec is not None and runtime_mode(spec) == "kubernetes")
    lines = [
        "schema_version: \"1\"",
        f"default_harness_config: {harness}-factory" if use_factory_hc else f"default_harness_config: {harness}",
        f"model: \"{model}\"",
        "agent_instructions: agents.md",
        "system_prompt: system-prompt.md",
    ]
    env = [f"  FACTORY_ROLE: \"{role}\""]
    parent_role = str(role_spec.get("reports_to") or "").strip()
    if parent_role:
        env.append(f"  FACTORY_PARENT_ROLE: \"{parent_role}\"")
    if (harness == "opencode" and "vertex" in model.lower()) or (
        harness in ("antigravity", "gemini", "gemini-cli") and "gemini" in model.lower()
    ):
        # Lesson L6: Gemini 4 Argon / 3.8 Flash on Vertex requires global endpoint
        env.append("  GOOGLE_CLOUD_LOCATION: \"global\"")
    if subdir:
        env.append(f"  FACTORY_SUBDIR: \"{subdir}\"   # product folder the harness starts in (factory-cd.sh)")
    is_deployer = bool(role_spec.get("gcp_identity")) or _role_kind(role_spec) in ("deployer", "eval-deployer")
    if is_deployer and (spec is None or deploy_mode(spec) != "none"):
        env.append("  CLOUDSDK_CORE_DISABLE_PROMPTS: \"1\"")
    if env:
        lines.extend(["env:", *env])
    return "\n".join(lines) + "\n"


def _team_lead(roles: list[dict[str, Any]], preferred: str) -> str:
    """The role that leads a team: `preferred` by name, then by kind, else the first role."""
    if not roles:
        return preferred
    names = [r["role"] for r in roles]
    if preferred in names:
        return preferred
    target_kind = role_library.ROLES.get(preferred, {}).get("default_kind", preferred)
    for r in roles:
        if _role_kind(r) == target_kind:
            return r["role"]
    return names[0]


# Role prompts (spec §5.4, §6): each role carries its library protocol and a repo focus; the lead sizes every issue
# against the roles present in its roster and briefs teammates through files in the scratchpad, never inline (shell
# metacharacters and long prompts break). Roles resolve by kind (`_role_kind`), so a renamed role (for example `go-dev`
# with `default_kind: implementer`) gets the library role's protocol, focus and sizing lines.
BRIEFS_DIR = "/scion-volumes/scratchpad/briefs"
REPORTS_DIR = "/scion-volumes/scratchpad/reports"
DURABILITY_RULE = "A task is done only when its result is pushed or sent in a message."

_FOCUS_COMMAND_KINDS = ("implementer", "test-engineer", "investigator", "evaluator")
_FOCUS_DOC_KINDS = ("doc-writer", "researcher", "kaizen-analyst")
_FOCUS_KEYS = ("paths", "commands", "docs")

# Kinds that never change product code, commit or push: they deliver reports, verdicts or measurements to their lead.
# Every other kind writes (implementer, doc-writer, test-engineer, frontend, migration-specialist, release-manager,
# deployers, domain-evaluator, and roles outside the library).
_READ_ONLY_KINDS = frozenset({
    "sub-coordinator", "reviewer", "researcher", "investigator", "security-auditor", "performance-analyst", "kaizen-analyst",
    "contract-tester", "latency-prober", "ui-auditor", "safety-evaluator", "factory-analyst",
    "benchmark-runner", "api-compat-checker", "evaluator",
})
# Reviewers whose verdicts the lead collects before it gives the commit slot (they never message each other).
_PARALLEL_REVIEWER_KINDS = ("reviewer", "security-auditor", "performance-analyst")

# (kinds the line names, sizing line). A line appears only when the roster has every kind it names; each `{kind}`
# placeholder becomes the agent name of the roster role with that kind.
_SIZING_LINES = (
    (("investigator",), "bug → {investigator} first"),
    (("researcher",), "unfamiliar ground → {researcher} first"),
    (("doc-writer",), "user-visible change → {doc-writer} after the commit"),
    (("security-auditor", "reviewer"), "auth/crypto/secrets → {security-auditor} in parallel with {reviewer}"),
    (("frontend",), "UI → {frontend}"),
    (("migration-specialist",), "schema/IaC → {migration-specialist}"),
    (("performance-analyst",), "perf-sensitive → {performance-analyst}"),
    (("release-manager",), "release → {release-manager}"),
    (("evaluator",), "wave or L1–L5 gate → {evaluator}"),
)


def _library_role(role_spec: dict[str, Any]) -> str | None:
    """The library role a roster entry stands for, by kind: itself, or the library role with its `default_kind`."""
    role = str(role_spec.get("role", ""))
    kind = _role_kind(role_spec)
    lib = role_library.ROLES.get(role)
    if lib and lib["default_kind"] == kind:
        return role
    return next((name for name, d in role_library.ROLES.items() if d["default_kind"] == kind), None)


def _role_writes(role_spec: dict[str, Any]) -> bool:
    return _role_kind(role_spec) not in _READ_ONLY_KINDS


def _kind_agents(spec: dict[str, Any], team: list[dict[str, Any]]) -> dict[str, str]:
    """{kind: `agent name`} for a team; the first roster role of a kind wins."""
    agents: dict[str, str] = {}
    for r in team:
        agents.setdefault(_role_kind(r), f"`{_agent(spec, r['role'])}`")
    return agents


def _kind_agent_slug(spec: dict[str, Any], team: list[dict[str, Any]], kind: str, fallback: str) -> str:
    """Plain agent slug (without backticks) for the first root-level role matching `kind`, else any matching role or fallback."""
    for r in team:
        if not str(r.get("reports_to") or "").strip() and _role_kind(r) == kind:
            return _agent(spec, r["role"])
    for r in team:
        if _role_kind(r) == kind:
            return _agent(spec, r["role"])
    return _agent(spec, fallback)


def role_focus(spec: dict[str, Any], role_spec: dict[str, Any]) -> dict[str, list[str]]:
    """The repo scope a role works in: `{paths, commands, docs}`.

    An explicit `focus` in the roster entry wins. Otherwise every role gets `paths=[subdir or "."]`; the implementer,
    test-engineer and investigator kinds get the build, test and lint commands; the doc-writer and researcher kinds get
    the knowledge-plane sources plus the knowledge plane's INDEX.md.
    """
    explicit = role_spec.get("focus")
    if isinstance(explicit, dict):
        return {key: [str(v) for v in (explicit.get(key) or [])] for key in _FOCUS_KEYS}
    kind = _role_kind(role_spec)
    stack = spec.get("stack") or {}
    focus: dict[str, list[str]] = {"paths": [_monorepo(spec)[1] or "."], "commands": [], "docs": []}
    if kind in _FOCUS_COMMAND_KINDS:
        cmds = (stack.get("build_command"), stack.get("test_command"), stack.get("lint_command"))
        focus["commands"] = [str(c) for c in cmds if c]
    if kind in _FOCUS_DOC_KINDS:
        sources = (spec.get("knowledge_plane") or {}).get("sources") or {}
        focus["docs"] = [f"/workspace/{src}" for k in kp.SOURCE_KINDS for src in (sources.get(k) or [])]
        focus["docs"].append(f"{_ws(spec)}/knowledge-plane/INDEX.md")
    return focus


def _render_role_protocol(spec: dict[str, Any], role_spec: dict[str, Any]) -> str:
    lib_role = _library_role(role_spec)
    wmode = wake_mode(spec)
    if lib_role == "kaizen-analyst":
        return f"## Role protocol\n{kaizen_policy.render_kaizen_analyst_protocol(spec).strip()}\n\n"
    protocol = role_library.role_protocol(lib_role, deploy_mode(spec), wake_mode=wmode) if lib_role else None
    if not protocol:
        return ""
    if runtime_mode(spec) == "kubernetes" or wmode == "delta":
        protocol = protocol.replace(
            "Run `python3 <knowledge plane>/kp-check.py`",
            "When `<knowledge plane>/kp-check.py` is writable in Git, run `python3 <knowledge plane>/kp-check.py`",
        )
    protocol = protocol.replace("<knowledge plane>", f"{_ws(spec)}/knowledge-plane")
    protocol = protocol.replace("factory:<slug>", _factory_label(spec))
    if "<build_branch>" in protocol or wmode == "delta":
        vcs = spec.get("vcs") or {}
        bbranch = (vcs.get("build_branch") or "main") if _vcs_enabled(spec) else "the build workspace"
        build_roles = (spec.get("team") or {}).get("build_roles") or []
        lead_role = _team_lead(build_roles, "lead")
        parent_slug = _agent(spec, sub_coordinators.resolve_parent_role(build_roles, role_spec, lead_role))
        reviewer_slug = sub_coordinators.subtree_reviewer_slug(spec, build_roles, role_spec, lead_role, _agent)
        protocol = protocol.replace("<build_branch>", bbranch)
        protocol = protocol.replace("<reviewer>", reviewer_slug)
        protocol = protocol.replace("<parent>", parent_slug)
        protocol = protocol.replace("<agent-slug>", _agent(spec, role_spec["role"]))
    return f"## Role protocol\n{protocol.strip()}\n\n"


def _render_focus(spec: dict[str, Any], role_spec: dict[str, Any]) -> str:
    focus = role_focus(spec, role_spec)
    labels = {"paths": "Paths in scope", "commands": "Commands you run", "docs": "Documents you keep"}
    lines = [f"- **{labels[key]}:** " + ", ".join(f"`{v}`" for v in focus[key]) for key in _FOCUS_KEYS if focus[key]]
    return "## Focus\n" + "\n".join(lines) + "\n\n" if lines else ""


def _render_sizing(spec: dict[str, Any], team: list[dict[str, Any]], is_eval: bool) -> str:
    root_role = _team_lead(team, "eval-lead" if is_eval else "lead")
    root_children = sub_coordinators.direct_children(team, root_role, is_root_lead=True)
    root_agents = _kind_agents(spec, root_children)
    agents = sub_coordinators.root_sizing_agents(spec, team, root_role, _agent)
    lines = [f"- {line.format_map(agents)}" for kinds, line in _SIZING_LINES if all(k in agents for k in kinds)]
    reviewer_pool = [f"`{_agent(spec, r['role'])}`" for r in root_children if _role_kind(r) == "reviewer"]
    if len(reviewer_pool) >= 2:
        lines.append(
            f"- reviewer pool (M/G/k) → check `scion list` and dispatch one idle reviewer ({', '.join(reviewer_pool)}). "
            "Do not review one diff twice. Require two reviewers only on `P0`, auth, or security slices"
        )
    reviewers = [root_agents[k] for k in _PARALLEL_REVIEWER_KINDS if k in root_agents]
    if len(reviewer_pool) >= 2 and reviewers:
        reviewers[0] = f"one idle reviewer ({', '.join(reviewer_pool)})"
    if is_eval and not lines and not reviewers:
        return ""
    small = f"A small change needs only {agents.get('implementer', 'the implementer')} and " \
            f"{agents.get('reviewer', 'the reviewer')}."
    text = ("## Sizing an issue\nRead the issue and the knowledge plane, then decide who is needed.\n"
            f"{small} Add a teammate when its line applies:\n" + "\n".join(lines) + "\n")
    subtrees_note = sub_coordinator_prompts.render_root_lead_subtrees_note(
        spec, team, root_role, _agent, vcs_enabled=_vcs_enabled(spec)
    )
    text += subtrees_note
    relay_clause = " (or a domain sub-coordinator's relayed verdict)" if subtrees_note else ""
    if reviewers:
        if wake_mode(spec) == "delta" and _vcs_enabled(spec):
            bbranch = (spec.get("vcs") or {}).get("build_branch") or "main"
            text += (f"\nRequire PASS review verdicts from {', '.join(reviewers)}{relay_clause} in "
                     f"`{REPORTS_DIR}/<N>-review.md` before merging `origin/factory/issue-<N>` onto `{bbranch}`.\n")
        else:
            text += f"\nReview verdicts from {', '.join(reviewers)}{relay_clause} before giving the commit slot.\n"
        if not is_eval and _vcs_enabled(spec) and reviewer_pool:
            build_branch = (spec.get("vcs") or {}).get("build_branch") or "main"
            text += (
                f"Before feature-branch review, run `git merge-base --is-ancestor origin/{build_branch} <branch>` "
                "to reject stale diffs.\n"
            )
    return text + "\n"


def _render_briefs(spec: dict[str, Any], team: list[dict[str, Any]], lead: str) -> str:
    agents = _kind_agents(spec, team)
    ref = "#N" if _vcs_enabled(spec) else "<item ID>"
    reports = [f"{agents[k]}'s `{REPORTS_DIR}/<issue>-{suffix}.md` ({why})"
               for k, suffix, why in (("investigator", "investigation", "for a bug"),
                                      ("researcher", "research", "for unfamiliar ground")) if k in agents]
    pre_work = ""
    if reports:
        implementer = agents.get("implementer", "the implementer")
        pre_work = ("- Pre-work comes first. Read\n  " + "\n  or ".join(reports) + "\n"
                    f"  before you brief {implementer}, and put the report's path in its brief.\n")
    if wake_mode(spec) == "delta" and _vcs_enabled(spec):
        reviewer_slug = _kind_agent_slug(spec, team, "reviewer", "reviewer")
        return f"""## Briefs
- Write each brief to `{BRIEFS_DIR}/<issue>-<agent>.md` and update `/scion-volumes/scratchpad/STATE.json` **before** launching or messaging `<agent>`.
- **Single-turn launch (never `scion start` + `scion message` back-to-back):** When `<agent>` does not exist yet, pass `{lead} -> <agent>: {ref}: read {BRIEFS_DIR}/<file>` directly as the `scion start` prompt and do **not** send a follow-up `scion message` (sending both queues a wasted duplicate second turn). Use `scion message <agent> "{lead} -> <agent>: {ref}: read {BRIEFS_DIR}/<file>"` **only** when `<agent>` is already `running` (or right after `scion resume <agent>` on a `stopped` pod). Start `{reviewer_slug}` on stand-by (`"{lead} -> {reviewer_slug}: read /scion-volumes/knowledge-plane/INDEX.md and stand by for writer handoffs"`) with no extra message before a writer pushes `origin/factory/issue-<N>`.
- Never inline a brief body in the prompt or message: shell metacharacters and long prompts break.
- For writers, name branch `factory/issue-<N>`, the deliverables (exact paths), the acceptance checks, and the direct
  handoff termination line ("Push `origin/factory/issue-<N>` and message `{reviewer_slug}` directly without waking `{lead}`").
  For reviewers and `evaluator`, name `{REPORTS_DIR}/<N>-review.md` (or `<N>-eval.md`) and report to `{lead}` on `PASS` or second `FAIL`.
{pre_work}
"""
    return f"""## Briefs
- Write each brief to `{BRIEFS_DIR}/<issue>-<agent>.md`, then send
  `scion message <agent> "{lead} -> <agent>: {ref}: read {BRIEFS_DIR}/<file>"`.
- Never inline a brief in the message: shell metacharacters and long prompts break.
- A brief has the key locations, the deliverables (exact paths), the acceptance checks, and a termination line
  ("You MUST … then report to `{lead}`").
{pre_work}
"""


def _load_env_map(env_file: str | Path | None) -> dict[str, str]:
    """Parse key-value pairs from an optional factory.env file without mutating os.environ."""
    if not env_file:
        return {}
    p = Path(env_file)
    if not p.exists():
        return {}
    env_map: dict[str, str] = {}
    for raw_line in p.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env_map[k.strip()] = v.strip().strip("'\"")
    return env_map


def effective_gcp_identity_mode(spec: dict[str, Any], compile_env: dict[str, str] | None = None) -> str:
    """Return the explicit GCP identity mode configured in the spec or factory.env.

    If the mode is 'assign' on Docker, Scion mounts the GCE metadata sidecar.
    Then scion start omits '--harness-auth vertex-ai' so env-gather does not require gcloud-adc.
    """
    infra = spec.get("infrastructure") or {}
    env_map = compile_env or {}
    return (
        env_map.get("GCP_IDENTITY_DEFAULT", "").strip()
        or env_map.get("GCP_IDENTITY_MODE", "").strip()
        or str(infra.get("gcp_identity_mode") or "").strip()
    )


def _render_chain_of_command(
    spec: dict[str, Any],
    role_spec: dict[str, Any],
    is_eval: bool,
    compile_env: dict[str, str] | None = None,
) -> str:
    """The PO starts only the two leads; each lead starts its team, so Scion records the lead as the team's parent.

    Scion keeps each agent's creator (ancestry root -> parent -> child): the Hub's tree view shows the lead on top, and
    the Hub sends an agent's notifications to its creator, so the lead hears when a teammate stops or fails.
    """
    team = (spec["team"].get("eval_roles") or []) if is_eval else spec["team"]["build_roles"]
    lead_role = _team_lead(team, "eval-lead" if is_eval else "lead")
    lead = _agent(spec, lead_role)
    is_delta = wake_mode(spec) == "delta" and _vcs_enabled(spec)
    vcs = spec.get("vcs", {}) or {}
    rt = runtime_mode(spec)
    team_branch = (
        (vcs.get("eval_branch") if (is_eval and has_eval_project(spec)) else vcs.get("build_branch"))
        if rt == "kubernetes" and _vcs_enabled(spec)
        else ""
    )
    id_mode = effective_gcp_identity_mode(spec, compile_env)
    omit_vertex_harness_auth = rt == "docker" and id_mode == "assign"
    if role_spec["role"] != lead_role:
        if sub_coordinators.is_sub_coordinator(role_spec):
            return sub_coordinator_prompts.render_sub_coordinator_chain(
                spec=spec,
                role_spec=role_spec,
                team=team,
                root_lead_slug=lead,
                agent_fn=_agent,
                team_branch=team_branch,
                omit_vertex_harness_auth=omit_vertex_harness_auth,
                is_delta=is_delta,
                is_eval=is_eval,
                vcs_enabled=_vcs_enabled(spec),
                briefs_dir=BRIEFS_DIR,
                reports_dir=REPORTS_DIR,
                durability_rule=DURABILITY_RULE,
            )
        parent_role = sub_coordinators.resolve_parent_role(team, role_spec, lead_role)
        parent_slug = _agent(spec, parent_role)
        if is_delta:
            my_slug = _agent(spec, role_spec["role"])
            reviewer_slug = sub_coordinators.subtree_reviewer_slug(spec, team, role_spec, lead_role, _agent)
            bbranch = vcs.get("build_branch") or "main"
            kind = _role_kind(role_spec)
            if kind == "reviewer":
                return f"""## Chain of command
- `{parent_slug}` started you on demand. Accept review handoffs directly from a writer on `origin/factory/issue-<N>` (and briefs from `{parent_slug}` in `{BRIEFS_DIR}/<issue>-<agent>.md`).
- Audit `git diff origin/{bbranch}...origin/factory/issue-<N>` and run tests independently without modifying product code.
- **First `FAIL`:** Message the writer directly (`scion message <writer> "{my_slug} -> <writer>: FAIL issue #<N>: ..."`) so it can push fixes to `origin/factory/issue-<N>` — do **not** wake `{parent_slug}` on the first `FAIL`.
- **`PASS` (or second `FAIL`):** Write `{REPORTS_DIR}/<N>-review.md` and message `{parent_slug}` once (`scion message {parent_slug} "{my_slug} -> {parent_slug}: <PASS|FAIL> issue #<N> (<sha>): see {REPORTS_DIR}/<N>-review.md"`).
- Prepend `<!-- scion-agent: {my_slug} -->` to any GitHub comment. Never start, stop or delete agents: staffing is `{parent_slug}`'s job.
- {DURABILITY_RULE}

"""
            if _role_writes(role_spec) and kind != "deployer":
                return f"""## Chain of command
- `{parent_slug}` started you on demand for an active issue. Read `{BRIEFS_DIR}/<issue>-<agent>.md` before you start.
- Branch `factory/issue-<N>` from `origin/{bbranch}`, run local tests, commit explicit paths, and push `origin/factory/issue-<N>` directly (never write `.patch` files to `/scion-volumes/scratchpad/patches/`, and never wait on a commit slot).
- **Direct peer handoff:** Once `origin/factory/issue-<N>` is pushed and green, message `{reviewer_slug}` directly (`scion message {reviewer_slug} "{my_slug} -> {reviewer_slug}: issue #<N> ready on origin/factory/issue-<N> (<sha>): brief {BRIEFS_DIR}/<file>"`) without waking `{parent_slug}`. If `{reviewer_slug}` sends a first-attempt `FAIL`, fix it on `factory/issue-<N>`, push, and message `{reviewer_slug}` again. Escalate blockers to `{parent_slug}`.
- Prepend `<!-- scion-agent: {my_slug} -->` to any GitHub comment. Never start, stop or delete agents: staffing is `{parent_slug}`'s job.
- {DURABILITY_RULE}

"""
            return f"""## Chain of command
- You report to `{parent_slug}`, who started you on demand. Read `{BRIEFS_DIR}/<issue>-<agent>.md` before you start, write your structured report under `{REPORTS_DIR}/`, prepend `<!-- scion-agent: {my_slug} -->` to any GitHub comment, and send your `PASS`/`FAIL` verdict or findings to `{parent_slug}` with `scion message {parent_slug} "{my_slug} -> {parent_slug}: ..."`.
- Never start, stop or delete agents: staffing is `{parent_slug}`'s job.
- {DURABILITY_RULE}

"""
        return f"""## Chain of command
- You report to `{parent_slug}`, who started you. Take work from `{parent_slug}` (or the PO), and send every result, blocker and
  verdict to `{parent_slug}` with `scion message {parent_slug} "<your name> -> {parent_slug}: ..."`.
- Read the brief file named in the message (`{BRIEFS_DIR}/<issue>-<agent>.md`) before you start:
  it has the key locations, the deliverables, the acceptance checks and when you are done.
- When a brief tells you to hand work straight to a teammate, do it and copy `{parent_slug}`.
- Never start, stop or delete agents: staffing is `{parent_slug}`'s job.
- {DURABILITY_RULE}
  The scratchpad lasts as long as the Hub project, but anything the next issue needs goes to the knowledge plane or
  the repository.

"""
    rows = []
    for r in sub_coordinators.direct_children(team, lead_role, is_root_lead=True):
        cmd = f'scion start {_agent(spec, r["role"])} -t {r["role"]}'
        if team_branch:
            cmd += f" -b {team_branch}"
        if not omit_vertex_harness_auth:
            cmd += " --harness-auth vertex-ai"
        if r.get("gcp_identity"):
            cmd += ' ${FACTORY_DEPLOYER_SA_ID:+--service-account "$FACTORY_DEPLOYER_SA_ID"}'
        rows.append(f'| `{_agent(spec, r["role"])}` | `{cmd} "Read your instructions, then run your wake-up steps."` |')
    table = "\n".join(rows)
    sa_note = (
        '\n- `FACTORY_DEPLOYER_SA_ID` is injected into your container environment by `scion-factory provision` '
        "when a dedicated or `assign`-mode deployer service account is registered on the Hub. Before starting "
        'that agent, check `echo "${FACTORY_DEPLOYER_SA_ID:-}"`. In `assign` mode, if it is empty, try '
        "`scion project service-accounts list --json` (when supported by your `scion` CLI) and open a PO decision "
        "issue before starting the deployer if neither resolves the UUID."
        if any(r.get("gcp_identity") for r in team if r["role"] != lead_role) else ""
    )
    if is_delta:
        bbranch = vcs.get("build_branch") or "main"
        reviewer_slug = _kind_agent_slug(spec, team, "reviewer", "reviewer")
        return f"""## Chain of command
You lead this team in ephemeral delta mode: the K8s delta controller (`scion-factory-delta`) wakes you only on actionable GitHub or child-pod state deltas, and every specialist you start is your Scion child.

| Teammate | Start it with |
|---|---|
{table}

- **On-demand staffing only (never pre-start unused roles):** Start **only** the specialists required by active issues (for example the assigned writer and `{reviewer_slug}`, or `evaluator` when running L1–L5 verification). Never pre-start unused roles when no active issue needs them; `scion resume <name>` a stopped specialist when assigning new work, and `scion resume --force <name>` one in `error`/`halted`.{sa_note}
- **Merge verified branches & retire idle pods:** After `{reviewer_slug}` (or a domain sub-coordinator relaying `{REPORTS_DIR}/<N>-review.md`) reports `PASS`, merge or fast-forward `origin/factory/issue-<N>` onto `{bbranch}`, push `origin/{bbranch}`, delete remote `factory/issue-<N>` (`git push origin --delete factory/issue-<N>`), persist state in `/scion-volumes/scratchpad/STATE.json`, and stop idle specialists immediately with `scion stop <agent>` (never stop a sub-coordinator while any of its subtree children is still running).
- **Ignore bare `state-change` (`COMPLETED`) events:** If woken solely by a bare system `state-change` (`COMPLETED`) notification with no `DELTA:` payload or actionable peer message, run **zero** shell commands and exit immediately via `sciontool status task_completed "ignored bare state-change"`.

{_render_sizing(spec, team, is_eval)}{_render_briefs(spec, team, lead)}{_render_human_on_the_loop(spec, lead, is_eval)}"""
    return f"""## Chain of command
You lead this team and you start it: the PO starts only you. Every agent you start is your child in Scion, so the
Hub shows you on top of your team and sends you its notifications (stopped, failed, waiting for input).

| Teammate | Start it with |
|---|---|
{table}

- At every wake-up, run `scion list` first. Start each teammate that is missing; use `scion message --wake <name> "..."`
  only when `<name>` is `suspended`; `scion resume <name>` a `stopped` one and re-send its full brief (`--body-file`)
  because a stopped agent starts a fresh session; `scion resume --force <name>` one in `error`; and nudge a specialist
  silent for more than 5 minutes with `scion message --interrupt <name> "..."` after checking expected deliverables.{sa_note}
- Start only the agents in this table. Your team reports to you: you assign every task and accept every result.

{_render_sizing(spec, team, is_eval)}{_render_briefs(spec, team, lead)}{_render_human_on_the_loop(spec, lead, is_eval)}"""


# Human on the loop (spec §7): what agents decide alone, decide and tell, or leave to the PO, per autonomy level.
# With Git, every PO decision is one `status:po-decision` issue and the digest and "tell" notices are GitHub comments:
# the watchdog turns every `scion message user:<PO>` into an ERROR alert, so only PO decisions go by message. Without
# Git (spec §3) the lead keeps escalating by Scion message and the eval-lead reports findings to the PO by message.
_DECIDE_ALONE = ["how to implement", "which specialist to brief", "test strategy", "doc updates", "retries and re-briefs"]


def _cell(items: list[str]) -> str:
    text = "; ".join(items)
    return text[:1].upper() + text[1:]


def _po_asks(spec: dict[str, Any]) -> list[str]:
    """Decisions the lead asks the PO for (a `status:po-decision` issue with Git), at the factory's autonomy level.

    The single source for the "Waits for the PO" column and the shared protocol's escalation line.
    """
    cost = "new cost, IAM roles, secrets or quotas"
    if autonomy(spec) == "autonomous":
        return [cost]
    return ["Scope changes and genuine spec contradictions", cost, "decisions that create an ADR"]


def render_decision_rights(spec: dict[str, Any]) -> str:
    """The decision-rights table from spec §7.1, adjusted for the factory's autonomy level (§7.4)."""
    git = _vcs_enabled(spec)
    level = autonomy(spec)
    accept = ("accepting work (only the PO closes the issue)" if git
              else "accepting work (only the PO accepts a verified item)")
    tell = [
        "each verified issue (evidence comment)" if git else "each verified item (in your turn summary)",
        "new issues filed from findings" if git else "new backlog items from findings",
        "specialists restarted",
    ]
    waits = _po_asks(spec) + [accept]
    if level == "autonomous":
        tell.insert(0, "Scope changes, genuine spec contradictions and decisions that create an ADR ("
                       + ("comment on the issue, " if git else "") + "ADR in the knowledge plane)")
    elif level == "supervised":
        waits.append("the plan of each issue before work starts (`status:po-review` until the PO sets "
                     "`status:approved`)" if git else "the plan of each item before work starts")
    return ("| Agents decide alone | Agents decide and tell the PO | Waits for the PO |\n"
            "|---|---|---|\n"
            f"| {_cell(_DECIDE_ALONE)} | {_cell(tell)} | {_cell(waits)} |\n")


def _render_human_on_the_loop(spec: dict[str, Any], lead: str, is_eval: bool) -> str:
    """Decision rights, PO inbox, digest, and "tell" notices (build lead) or evaluation findings (eval-lead)."""
    level = autonomy(spec)
    git = _vcs_enabled(spec)
    rights = f"""## Decision rights
Autonomy level: `{level}` (`team.autonomy` in factory.yaml; changing it means recompiling and re-pushing templates).

{render_decision_rights(spec)}
"""
    if not git:
        rights += """- **Decide and tell:** do it, then say so in your turn summary (`sciontool status task_completed`).
- **Waits for the PO:** ask with `scion message user:<PO_USER> "<question, options, your recommendation>"` and keep
  working on other items meanwhile. Every message to the PO raises an alert, so send nothing else to the PO.
"""
        if level == "supervised":
            rights += ("- **Supervised:** before work starts on an item, send your plan the same way and brief nobody "
                       "on it until the PO approves; work on other approved items meanwhile.\n")
        if is_eval:
            rights += """
## Evaluation findings
- Without Git there is no issue tracker: report each finding (level, evidence, `REQ-` IDs) to the PO with
  `scion message user:<PO_USER> "<finding>"`.
- Never message build agents; the PO decides which findings become backlog items.
"""
        return rights + "\n"

    repo = spec["vcs"]["repo"]
    flabel = _factory_label(spec)
    digest = _digest_title(spec)
    rights += """- **Decide and tell:** do it, then leave the notice on GitHub (see Tell notices). Never `scion message` the PO
  for it.
- **Waits for the PO:** open a PO inbox issue (see PO inbox) and keep working on other issues meanwhile.
"""
    if level == "supervised":
        rights += """- **Supervised:** before work starts on an issue, comment your plan on it and add `status:po-review`. Brief
  nobody on it until the PO replaces that label with `status:approved`; work on other approved issues meanwhile.
  There is no per-commit approval. Acceptance stays the PO closing the issue.
"""
    shipped = ("evaluation runs and the findings you filed" if is_eval
               else "what shipped (one line per issue at `status:verified`, waiting for the PO to close it)")
    is_delta = wake_mode(spec) == "delta"
    marker_po_note = (
        f"\n- **Shared-PAT marker & ordering (`L14`):** Prepend `<!-- scion-agent: {lead} -->` on line 1 of every "
        f"GitHub issue body or comment you write. Always post your `<!-- scion-agent: {lead} -->` comment **before** "
        f"adding `status:po-decision` (`gh issue comment <N> -R {repo} --body-file <file> && gh issue edit <N> -R {repo} "
        "--add-label status:po-decision`) so `scion-factory-delta` never misreads an older unmarked comment as a PO reply."
        if is_delta
        else ""
    )
    missing_digest_bullet = (
        f'- If `$n` is empty, create the digest issue yourself rather than opening a `status:po-decision` blocker:\n'
        f'  `gh issue create -R {repo} --title "{digest}" --label digest --label {flabel} --body "<!-- scion-agent: {lead} -->\\nDaily digest for {flabel}."`'
        if is_delta
        else (
            "- If `$n` is empty, the digest issue is missing: open a PO inbox issue asking the PO to run\n"
            "  `DRY_RUN=0 scripts/seed-github-waves.sh` from the kit, and skip the digest until it exists."
        )
    )
    text = rights + f"""
## PO inbox
A decision in the "Waits for the PO" column becomes one GitHub issue, and the PO answers on it.
1. Write the question, the options and your recommendation to `/scion-volumes/scratchpad/po/<topic>.md`, then
   `gh issue create -R {repo} --title "PO decision: <question>" --label status:po-decision --label {flabel} --body-file <file>`.
2. Send `scion message user:<PO_USER> "{lead}: PO decision needed: <issue URL>"`. Messages to the PO are only for
   `status:po-decision` issues, and each one links its issue: every message raises an ERROR alert.
3. Keep working on other issues meanwhile. When the PO answers, act on the answer and remove `status:po-decision`.
- `gh issue list -R {repo} --label status:po-decision` is the PO's whole inbox, across every factory on the repository.{marker_po_note}

## Daily digest
Once a day (UTC), add one comment to the pinned issue "{digest}" (labels `digest` and `{flabel}`):
- `n=$(gh issue list -R {repo} --label digest --label {flabel} --state open --search "\\"{digest}\\" in:title" --json number --jq '.[0].number // empty')`
{missing_digest_bullet}
- Start the comment with the line `[{lead} digest <YYYY-MM-DD>]` (UTC date; the brackets keep one lead's line from
  matching another's). Post only when today's line isn't there yet, so frequent wake-ups don't repeat it:
  `gh issue view "$n" -R {repo} --comments | grep -qF "[{lead} digest $(date -u +%F)]" || gh issue comment "$n" -R {repo} --body-file /scion-volumes/scratchpad/digest-$(date -u +%F).md`
The comment covers {shipped}, what is blocked and on whom, and what waits in the PO inbox.
The digest is never a Scion message: a daily message would page the PO every day.

"""
    if is_eval:
        return text + f"""## Evaluation findings
- File each finding (a failed level, a regression, a missed SLO) as one issue for the build team:
  `gh issue create -R {repo} --title "<level>: <finding>" --label eval-finding --label {flabel} --label status:ready --body-file <file>`.
  The body cites the evaluation run (`run_id`), the level (L1–L5), the evidence, and the `REQ-` IDs.
- Search first (`gh issue list -R {repo} --label eval-finding --label {flabel} --search "<finding>"`) and comment on
  the open issue instead of filing a duplicate.
- When an open `kaizen-proposal` issue carries `kaizen-eval` or `[eval]`-tagged items, adopt those probe/evaluation
  improvements on your evaluation trunk and comment your `commit` or `run_id` evidence on that issue.
- Never message build agents: the build lead takes the issue from the backlog. When a later run confirms the fix,
  comment the run on the issue; only the PO closes it.

"""
    marker_tell_note = (
        f"- Prepend `<!-- scion-agent: {lead} -->` on line 1 of every `gh issue comment` (including `status:verified` "
        "evidence comments, digest comments, and PO escalations).\n"
        if is_delta
        else ""
    )
    return text + f"""## Tell notices
{marker_tell_note}- At `status:verified`, comment the evidence on the issue (`gh issue comment`): commit, review verdicts, checks.
  The digest lists the issue; the PO accepts it by closing it.
- An `eval-finding` issue (filed by the eval-lead at `status:ready`) is work like any other: work it directly and
  never file a duplicate issue for it.
- A `kaizen-proposal` issue (filed at `status:ready`) is triaged autonomously against active WIP: fold `[build]` process,
  test, review, and knowledge-plane gates into briefs with mutation proof, escalate to `status:po-decision` only on cost,
  IAM/secrets, quotas, or scope changes, and summarize outcomes under `### Auto-improvement (Kaizen)` in the daily digest.
- A new issue you file from any other finding links the finding (issue, evaluation run or report) in its body.
- When you restart a specialist, comment on the issue it works on.
- Never `scion message` the PO for a tell notice or the digest.

""" + (kaizen_policy.render_coordinator_kaizen_rubric(spec) + "\n" if any(_library_role(r) == "kaizen-analyst" for r in spec.get("team", {}).get("build_roles", [])) else "")


def _render_role_system_prompt(
    spec: dict[str, Any],
    role_spec: dict[str, Any],
    is_eval: bool,
    compile_env: dict[str, str] | None = None,
) -> str:
    text = _monorepo_paths(spec, _render_role_system_prompt_base(spec, role_spec, is_eval))
    # Every factory scopes its backlog with its `factory:<slug>` label (spec §7.2), not only monorepo factories.
    text = text.replace("--label status:ready`", f"--label status:ready --label {_factory_label(spec)}`")
    sections = (_monorepo_paths(spec, _render_role_protocol(spec, role_spec)) + _render_focus(spec, role_spec)
                + _render_chain_of_command(spec, role_spec, is_eval, compile_env=compile_env))
    text = text.replace("## Operating Loop\n", sections + "## Operating Loop\n", 1)
    prefix, subdir = _monorepo(spec)
    if not subdir:
        return text
    team = (spec["team"].get("eval_roles") or []) if is_eval else spec["team"]["build_roles"]
    mates = ", ".join(f"`{_agent(spec, r['role'])}`" for r in team if r["role"] != role_spec["role"])
    return text.replace("## Identity & Responsibility\n", f"""## Identity & Responsibility
- **Your agent name is `{_agent(spec, role_spec['role'])}`.** Teammates: {mates}.
- **Product folder:** `{subdir}` (you start in `/workspace/{subdir}`).
""", 1)


def _render_role_system_prompt_base(spec: dict[str, Any], role_spec: dict[str, Any], is_eval: bool) -> str:
    proj = spec["project"]
    stack = spec["stack"]
    infra = spec["infrastructure"]
    pyramid = spec["evaluation_pyramid"]
    l3 = pyramid["l3_domain_quality"]
    role = role_spec["role"]
    has_eval = has_eval_project(spec)
    is_delta = wake_mode(spec) == "delta"
    project_slug = proj["eval_slug"] if (is_eval and has_eval) else proj["slug"]
    if deploy_mode(spec) == "none":
        # Nothing to deploy (spec §8): no target service, and the pyramid grades the code.
        grader_phrase = "the evaluation team grades the code" if has_eval else "the evaluator specialist grades the code in-project"
        target = f"- **Deploy Target:** {NOTHING_TO_DEPLOY}; {grader_phrase}"
        oracle = f"## Evaluation Oracle (L1–L5 Code Pyramid)\n{render_code_pyramid(spec)}"
    else:
        l5 = pyramid["l5_latency_slo"]
        service = infra["eval_service"] if (is_eval and has_eval) else infra["serving_service"]
        target = f"- **Target Cloud Run Service:** `{service}`"
        oracle = f"""## Evaluation Oracle (L1–L5 Pyramid)
1. **L1 Health:** `{pyramid['l1_health']['endpoint']}` -> {', '.join(pyramid['l1_health'].get('checks', []))}
2. **L2 Contract:** {', '.join(pyramid['l2_contract'].get('endpoints', []))} — Invariants: {'; '.join(pyramid['l2_contract']['invariants'])}
3. **L3 Domain Quality:** `{l3['primary_metric']} >= {l3['target_threshold']}` (Secondary: `{l3.get('secondary_metric', 'n/a')} >= {l3.get('secondary_threshold', 0)}`). Probe: {l3.get('probe_description', '')}
4. **L4 Safety & Grounding:** {'; '.join(pyramid['l4_safety_grounding']['invariants'])}
5. **L5 Latency SLO:** `p50 <= {l5.get('p50_ms', 100)}ms`, `p95 <= {l5['p95_ms']}ms`, `p99 <= {l5.get('p99_ms', 500)}ms`"""
    vcs = spec.get("vcs") or {}
    use_shared_kp = runtime_mode(spec) == "kubernetes" or is_delta
    if is_eval:
        kp_ref = f"`{kp.EVAL_KP_MOUNT}/INDEX.md`"
    elif use_shared_kp:
        kp_ref = f"`{kp.EVAL_KP_MOUNT}/INDEX.md` (fallback `/workspace/knowledge-plane/INDEX.md`)"
    else:
        kp_ref = "`/workspace/knowledge-plane/INDEX.md`"
    team = (spec["team"].get("eval_roles") or []) if is_eval else spec["team"]["build_roles"]
    lead_role = _team_lead(team, "eval-lead" if is_eval else "lead")
    lead = _agent(spec, lead_role)
    parent_role = sub_coordinators.resolve_parent_role(team, role_spec, lead_role)
    parent_slug = _agent(spec, parent_role)
    slot_via = f" (requested on your behalf by `{parent_slug}`)" if parent_role != lead_role else ""
    has_firestore = "firestore" in [str(db).lower() for db in spec.get("infrastructure", {}).get("databases", [])]

    if _vcs_enabled(spec):
        branch = vcs["eval_branch"] if (is_eval and has_eval) else vcs["build_branch"]
        location = f"Repository `{vcs['repo']}`, Branch `{branch}`"
        if is_eval:
            if role == "eval-lead" and (deploy_mode(spec) != "none" or has_firestore):
                intake = f"check `{kp.EVAL_KP_MOUNT}/state/LIVE.md` and `list-eval-runs` for newly shipped revisions since the last evaluation run, plus open `eval-finding` issues in `{vcs['repo']}`"
            else:
                intake = f"check `{kp.EVAL_KP_MOUNT}/state/LIVE.md` for newly shipped revisions, plus open `eval-finding` issues in `{vcs['repo']}`"
        else:
            intake = f"inspect GitHub issues in `{vcs['repo']}` (`gh issue list -R {vcs['repo']} --label status:ready`)"
        slot = "commit slot"
        write_step = (f"Verify your work with `{stack['test_command']}`,\n   then commit and push on branch `{branch}` "
                      f"only when `{lead}` gives you the commit slot{slot_via}.")
    else:
        location = "Shared Hub project workspace `/workspace`, no version control"
        if is_eval:
            if role == "eval-lead" and (deploy_mode(spec) != "none" or has_firestore):
                intake = f"check `{kp.EVAL_KP_MOUNT}/state/LIVE.md` and `list-eval-runs` for newly shipped revisions since the last evaluation run"
            else:
                intake = f"check `{kp.EVAL_KP_MOUNT}/state/LIVE.md` for newly shipped revisions"
        else:
            intake = "read `/workspace/backlog/issues.json` (only the lead edits it)"
        slot = "edit slot"
        write_step = (f"Verify your work with `{stack['test_command']}`,\n   and change files in `/workspace` only while "
                      f"`{lead}` gives you the edit slot{slot_via}.")
    # Leads keep backlog intake and orchestration and never implement; team members take work only from the brief
    # named in their lead's message and report only to their lead (or direct writer -> reviewer handoff in delta mode).
    if is_delta and _vcs_enabled(spec):
        repo = vcs["repo"]
        my_slug = _agent(spec, role)
        reviewer_slug = sub_coordinators.subtree_reviewer_slug(spec, team, role_spec, lead_role, _agent)
        kind = _role_kind(role_spec)
        how = ("as your Role protocol says" if _render_role_protocol(spec, role_spec)
               else "as your Primary Responsibility says")
        if role == lead_role:
            steps = [
                "If woken solely by a bare system `state-change` (`COMPLETED`) notification or an initial stand-by prompt (`stand by`) with no `DELTA:` payload "
                "and no actionable peer message, read `/scion-volumes/knowledge-plane/INDEX.md` once (on stand-by only) and exit immediately via `sciontool status task_completed \"standing by\"` without polling GitHub or starting specialists.",
                f"Read `/scion-volumes/scratchpad/STATE.json`, {kp_ref}, and the incoming `DELTA:` or peer message "
                f"(reconcile against {intake} or `--label status:po-decision --label {_factory_label(spec)}` when needed).",
                "Size active issues, write briefs to `/scion-volumes/scratchpad/briefs/<N>-<agent>.md`, update "
                "`/scion-volumes/scratchpad/STATE.json`, and start **only** the specialists required by active issues "
                "(never pre-starting unused roles, and never implementing product code yourself).",
                f"When `{reviewer_slug}` (or a domain sub-coordinator relaying `{REPORTS_DIR}/<N>-review.md`) reports `PASS`, merge `origin/factory/issue-<N>` "
                f"onto `{branch}`, verify `{stack['test_command']}`, push `origin/{branch}`, delete remote "
                "`factory/issue-<N>` (`git push origin --delete factory/issue-<N>`), update `/scion-volumes/scratchpad/STATE.json`, "
                "and stop idle specialists with `scion stop <agent>` (never stop a sub-coordinator while any of its subtree children is running).",
                f"Prepend `<!-- scion-agent: {lead} -->` to every GitHub comment (always commenting *before* adding "
                "`status:po-decision`).",
            ]
            loop = "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
        elif sub_coordinators.is_sub_coordinator(role_spec):
            loop = sub_coordinator_prompts.render_sub_coordinator_loop(
                root_lead_slug=lead,
                my_slug=my_slug,
                kp_ref=kp_ref,
                test_command=stack["test_command"],
                briefs_dir=BRIEFS_DIR,
                reports_dir=REPORTS_DIR,
                is_delta=True,
                vcs_enabled=True,
            )
        elif kind == "reviewer":
            loop = f"""1. Accept review handoffs directly from a writer on `origin/factory/issue-<N>` (or a brief in `{BRIEFS_DIR}/<N>-{my_slug}.md`); if woken on a fresh pod with no inline message, check `/scion-volumes/scratchpad/STATE.json` and `/scion-volumes/scratchpad/briefs/*` for an active in-progress review assignment on `origin/factory/issue-<N>` before going idle.
2. Read {kp_ref}, fetch `origin/{branch}` and `origin/factory/issue-<N>`, inspect `git diff origin/{branch}...origin/factory/issue-<N>`, and run `{stack['test_command']}` independently (never change product code).
3. On the first `FAIL`, message the writer directly with concrete `file:line` fixes (`{parent_slug}` is NOT woken).
4. On `PASS` (or second `FAIL`), write `{REPORTS_DIR}/<N>-review.md` and wake `{parent_slug}` once with the verdict and commit SHA."""
        elif _role_writes(role_spec) and kind != "deployer":
            loop = f"""1. Take work from the brief file named in `{parent_slug}`'s message (or a first-attempt `FAIL` fix request from `{reviewer_slug}`); if woken on a fresh pod with no inline message, check `/scion-volumes/scratchpad/STATE.json` and `/scion-volumes/scratchpad/briefs/*` for an active in-progress assignment on `origin/factory/issue-<N>` before going idle (never pick unassigned work from the backlog yourself).
2. Read {kp_ref} and the requirements and ADRs that the brief cites.
3. Branch `factory/issue-<N>` from `origin/{branch}` (`git fetch origin {branch} && git checkout -B factory/issue-<N> origin/{branch}`) and implement your changes {how}, honoring `agents.md`.
4. Verify locally with `{stack['build_command']}` and `{stack['test_command']}`, stage explicit paths (`git add -- <files>`), commit, and push `origin/factory/issue-<N>` directly (no scratchpad `.patch` files, no commit-slot wait).
5. Message `{reviewer_slug}` directly (`scion message {reviewer_slug} "{my_slug} -> {reviewer_slug}: issue #<N> ready on origin/factory/issue-<N> (<sha>)"`) without waking `{parent_slug}`."""
        else:
            loop = f"""1. Take work from the brief file named in `{parent_slug}`'s message; if woken on a fresh pod with no inline message, check `/scion-volumes/scratchpad/STATE.json` and `/scion-volumes/scratchpad/briefs/*` for an active in-progress assignment on `origin/factory/issue-<N>` before going idle (never pick unassigned work from the backlog yourself).
2. Read {kp_ref} and the requirements and ADRs that the brief cites.
3. Do your role {how} on `{branch}`, honoring `agents.md` (never change product code).
4. Write structured verification output to `{REPORTS_DIR}/<N>-eval.md` (or the report path in your brief) and prepend `<!-- scion-agent: {my_slug} -->` to any GitHub evidence comment.
5. Report `PASS` or `FAIL` with the report path to `{parent_slug}`."""
    elif role == lead_role:
        steps = [f"Read incoming `scion message` instructions and {intake}."]
        if _vcs_enabled(spec):
            repo = vcs["repo"]
            steps.append(
                f"Check the PO inbox: `gh issue list -R {repo} --label status:po-decision --label {_factory_label(spec)}`.\n"
                "   Act on each answered issue. For one without a PO answer for 24 hours, comment a reminder on it and\n"
                "   re-send `scion message user:<PO_USER>` with the issue URL. Once a day, post the digest (see Daily digest).")
        steps += [
            f"Read {kp_ref} and the requirements and ADRs that the work item cites.",
            "Size the work item, brief your team by file and accept their results (see Chain of command) while honoring\n"
            "   `agents.md`. Never implement it yourself.",
            f"Before you give the {slot} or mark the work verified, check the evidence: `{stack['test_command']}` passes and\n"
            "   every verdict is in.",
        ]
        if not is_eval and "doc-writer" in _dropped_defaults(spec):
            steps.append(
                f"Because `doc-writer` is omitted from this roster, update `{_ws(spec)}/knowledge-plane/state/LIVE.md` "
                f"and `{_ws(spec)}/knowledge-plane/INDEX.md` at wave close."
            )
        loop = "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
    elif sub_coordinators.is_sub_coordinator(role_spec):
        loop = sub_coordinator_prompts.render_sub_coordinator_loop(
            root_lead_slug=lead,
            my_slug=_agent(spec, role),
            kp_ref=kp_ref,
            test_command=stack["test_command"],
            briefs_dir=BRIEFS_DIR,
            reports_dir=REPORTS_DIR,
            is_delta=False,
            vcs_enabled=_vcs_enabled(spec),
        )
    else:
        how = ("as your Role protocol says" if _render_role_protocol(spec, role_spec)
               else "as your Primary Responsibility says")
        result = write_step if _role_writes(role_spec) else (
            "You never change product code: write your result (report, verdict or measurement) where the brief says.")
        loop = f"""1. Take work only from the brief file named in `{parent_slug}`'s message; never pick work from the backlog yourself.
2. Read {kp_ref} and the requirements and ADRs that the brief cites.
3. Do your role {how}, honoring `agents.md`.
4. {result}
5. Report the result's path and a one-line result only to `{parent_slug}`."""
    last_step = len(re.findall(r"^\d+\. ", loop, re.MULTILINE)) + 1
    factory_data = ""
    if is_eval:
        measured = ("`p50_ms`/`p95_ms`/`p99_ms`" if deploy_mode(spec) != "none"
                    else "`layer_results` with each tool's output and the L5 benchmark deltas")
        factory_data = f"""## Factory Data (Firestore)
The control plane is serverless Firestore; see `config/firestore/README.md` in the kit for the data contract.
- Agent status of both teams: `python3 /workspace/tools/factory_store.py list-agents --project-slug {proj['slug']}` (Hub `current_turns` and `model_calls` are `NULL` when unreported; display them as `"n/a"`, never `0`).
- Recent evaluation runs: `python3 /workspace/tools/factory_store.py list-eval-runs --project-slug {proj['slug']}`
- Record each evaluation run as one JSON object (`run_id`, `project_slug`, `source`, `overall_status`, `l1_passed`…`l5_passed`, `primary_metric_name` = `{l3['primary_metric']}`, `primary_metric_value`, {measured}, `requirements`): `python3 /workspace/tools/factory_store.py put-eval-run --file run.json`. Use `--dry-run` first when you change the payload shape.

"""

    return f"""# Role: `{role}` ({'Evaluation Team' if is_eval else 'Build Team'} — Project `{project_slug}`)

## Identity & Responsibility
- **Project:** `{project_slug}` ({location})
- **Harness / Model:** `{role_spec['harness']}` / `{role_spec['model']}`
- **Primary Responsibility:** {role_spec['responsibility']}
{target}

## System Mission
{proj['mission']}

## Stack & Verification Commands
- **Language / Framework:** `{stack['primary_language']}` ({stack.get('framework', '')})
- **Build Command:** `{stack['build_command']}`
- **Test Command:** `{stack['test_command']}`
- **Lint Command:** `{stack.get('lint_command', 'true')}`
- **Cloud Backing Stores:** {', '.join(infra.get('cloud_stores', [])) or 'none'}

{oracle}

{factory_data}## Operating Loop
{loop}
{last_step}. Always conclude your turn with `sciontool status task_completed "<summary>"`.
"""


FIRESTORE_INDEXES = {
    "indexes": [
        {
            "collectionGroup": "factory_agent_status",
            "queryScope": "COLLECTION",
            "fields": [
                {"fieldPath": "project_slug", "order": "ASCENDING"},
                {"fieldPath": "agent_name", "order": "ASCENDING"},
            ],
        },
        {
            "collectionGroup": "eval_runs",
            "queryScope": "COLLECTION",
            "fields": [
                {"fieldPath": "project_slug", "order": "ASCENDING"},
                {"fieldPath": "evaluated_at", "order": "DESCENDING"},
            ],
        },
    ],
    "fieldOverrides": [],
}

FIRESTORE_RULES = """rules_version = '2';
// The factory control plane is server-side only: the Hub sync, eval agents, and the dashboard
// use IAM service accounts, which bypass these rules. Deny every client SDK request.
service cloud.firestore {
  match /databases/{database}/documents {
    match /factory_fleet/{doc} {
      allow read, write: if false;
    }
    match /factory_fleet_snapshots/{doc} {
      allow read, write: if false;
    }
    match /{document=**} {
      allow read, write: if false;
    }
  }
}
"""


def _render_firestore_contract(spec: dict[str, Any]) -> str:
    proj = spec["project"]
    l3 = spec["evaluation_pyramid"]["l3_domain_quality"]
    slugs = f"`{proj['slug']}` and `{proj['eval_slug']}`" if has_eval_project(spec) else f"`{proj['slug']}`"
    return f"""# Factory control plane on Firestore

{proj['name']} ({slugs}) stores agent status and evaluation runs in Firestore
(Native mode). Firestore is serverless: there is no instance to size, and idle cost is zero.

- **Project:** `FIRESTORE_PROJECT` (default: the application project)
- **Database:** `FIRESTORE_DATABASE` (default: `(default)`)
- **Access:** server side only, through IAM. [`firestore.rules`](firestore.rules) denies every client SDK request.
- **Indexes:** [`firestore.indexes.json`](firestore.indexes.json)

## `factory_agent_status`

One document per live agent, upserted every minute by the Hub sync (`scripts/sync-factory-agents.py` in
scion-defaults). Document ID: `<project_slug>__<agent_name>`. Documents not refreshed for `PRUNE_MINUTES` are deleted.

| Field | Type | Notes |
|---|---|---|
| `project_slug`, `agent_name`, `agent_id` | string | Identity |
| `template`, `harness`, `phase`, `activity` | string | Hub state |
| `parent_agent` | string or null | The agent that created this agent |
| `current_turns`, `model_calls` | integer or null | Null means unknown, not zero (L9) |
| `last_summary` | string | Up to 500 characters |
| `last_activity_at` | timestamp or null | Last Hub activity event |
| `synced_at` | timestamp | Server time of the last sync |

## `eval_runs`

One document per evaluation run, written by the evaluation team with `tools/factory_store.py put-eval-run`.
Document ID: `run_id`.

| Field | Type | Notes |
|---|---|---|
| `run_id`, `project_slug` | string | Required |
| `source` | string | Commit SHA (with Git) or snapshot SHA-256 (without Git) of the deployed service |
| `wave` | integer | Backlog wave under evaluation |
| `overall_status` | string | Required: `pass`, `fail`, or `error` |
| `l1_passed` … `l5_passed` | boolean | One per pyramid layer |
| `primary_metric_name`, `primary_metric_value` | string, number | Required. For this project: `{l3['primary_metric']}`, target `{l3['target_threshold']}` |
| `primary_metric_target` | number | The threshold used for this run |
| `secondary_metric_name`, `secondary_metric_value` | string, number | Optional |
| `p50_ms`, `p95_ms`, `p99_ms` | number | Measured latency |
| `layer_results` | map | Per-layer details |
| `requirements` | array of `{{id, status}}` | One entry per `REQ-` ID evaluated |
| `diagnostics` | map | Free-form evidence |
| `evaluated_at` | timestamp | Server time of the write |

## Set up

```bash
gcloud firestore databases create --database="$FIRESTORE_DATABASE" --location=REGION --type=firestore-native \
  --project="$FIRESTORE_PROJECT"
gcloud firestore indexes composite create --project="$FIRESTORE_PROJECT" --database="$FIRESTORE_DATABASE" \
  --collection-group=factory_agent_status --field-config=field-path=project_slug,order=ascending \
  --field-config=field-path=agent_name,order=ascending
gcloud firestore indexes composite create --project="$FIRESTORE_PROJECT" --database="$FIRESTORE_DATABASE" \
  --collection-group=eval_runs --field-config=field-path=project_slug,order=ascending \
  --field-config=field-path=evaluated_at,order=descending
```

Grant `roles/datastore.user` to the identities that write (the Hub VM service account) and `roles/datastore.viewer`
to the dashboard's runtime service account.
"""


def _render_factory_env_example(spec: dict[str, Any]) -> str:

    proj = spec["project"]
    infra = spec["infrastructure"]
    team = spec["team"]
    has_eval = has_eval_project(spec)
    wmode = wake_mode(spec)
    build_names = " ".join(r["role"] for r in team["build_roles"])
    eval_names = " ".join(r["role"] for r in (team.get("eval_roles") or []))
    iam_roles = " ".join(infra.get("deployer_iam_roles", []))
    vcs = spec.get("vcs") or {}
    if deploy_mode(spec) == "none":
        # Nothing to deploy (spec §8): no service, runtime or deployer variables.
        service_vars = "# Nothing to deploy (infrastructure.deploy: none): no service, runtime or deployer variables.\n"
    else:
        dashboard_line = f"DASHBOARD_SERVICE={infra['eval_service']}\n" if has_eval and infra.get("eval_service") else ""
        service_vars = f"""DEPLOYER_SA_NAME=deployer-sa
RUNTIME_SA_NAME={infra['serving_service']}
DEPLOYER_IAM_ROLES="{iam_roles}"
SERVING_SERVICE={infra['serving_service']}
{dashboard_line}"""

    prefix, subdir = _monorepo(spec)
    monorepo = (
        f"""
# --- Monorepo factory (agents named <AGENT_PREFIX>-<role>, starting in FACTORY_SUBDIR) ---
AGENT_PREFIX={prefix}
FACTORY_SUBDIR={subdir}
KNOWLEDGE_PLANE_SOURCES={proj['slug']}={subdir}/knowledge-plane
""" if subdir else ""
    )
    eval_slug = str(proj.get("eval_slug") or "") if has_eval else ""
    eval_branch = vcs.get("eval_branch", "eval-dashboard") if has_eval else ""
    factory_projects = f"{proj['slug']},{eval_slug}" if has_eval else proj["slug"]
    hcron = team.get("heartbeat_cron", "" if wmode == "delta" else "*/15 * * * *")
    return f"""# Scion software factory configuration — {proj['name']}
# Copy this file to config/factory.env in your scion-defaults checkout (git-ignored), replace the your-* values, and
# deploy with docs/setup-runbook.md. The names match what scripts/run-on-hub.sh, render-templates.py and
# push-templates.py read (KEY=value lines, no `export`).

# --- Scion Hub (self-hosted on a GCE VM) ---
HUB_PROJECT=your-hub-gcp-project
HUB_ZONE=us-central1-c
HUB_VM=scion-hub
HUB_OS_USER=scion
HUB_URL=
HUB_LOCAL_URL=http://localhost:8080

# --- GCP sandbox the agents build and deploy into ---
SANDBOX_PROJECT=your-sandbox-gcp-project
SANDBOX_REGION=us-central1
FIRESTORE_PROJECT=your-sandbox-gcp-project   # factory control plane: agent status and eval runs
FIRESTORE_DATABASE='(default)'
DATA_BUCKET=your-sandbox-gcp-project-factory-data   # factory data bucket (scripts/setup-sandbox.sh creates it)
BQ_DATASET={proj['slug'].replace('-', '_')}_dataset          # only with scripts/setup-sandbox.sh --with-bigquery
{service_vars}
# --- Agent Runtime (kubernetes or docker) ---
AGENT_RUNTIME={runtime_mode(spec)}
RUNTIME_PROFILE={infra.get('runtime_profile', '')}
HUB_K8S_NAMESPACE=
HUB_GKE_CLUSTER=
HUB_GKE_LOCATION=
IMAGE_REGISTRY=
WORKER_SA_NAME={infra.get('worker_sa_name', 'agent-worker')}
HUB_SA=

# --- Factory ---
VCS_ENABLED={'true' if vcs.get('enabled') else 'false'}
GITHUB_REPO={vcs.get('repo', '')}
BUILD_PROJECT={proj['slug']}
EVAL_PROJECT={eval_slug}
BUILD_BRANCH={vcs.get('build_branch', 'main')}
EVAL_BRANCH={eval_branch}
PO_EMAIL=po@example.com                      # Hub admin account that acts as the product owner
FACTORY_PROJECTS={factory_projects}
KNOWLEDGE_PLANE_SOURCE_PROJECT={proj['slug']}
MONTHLY_BUDGET_USD={infra.get('budget_usd', 200)}
AUTONOMY={autonomy(spec)}   # team.autonomy: supervised | standard | autonomous (recompile and re-push to change)

# --- Roster (agent names = role names; start one agent per role) ---
BUILD_AGENTS="{build_names}"
EVAL_AGENTS="{eval_names}"
HEARTBEAT_MODE={wmode}
HEARTBEAT_CRON="{hcron}"
WATCHDOG_CRON="{team.get('watchdog_cron', '*/5 * * * *')}"
MAX_WIP={team.get('max_wip', 2)}
{monorepo}"""


def _render_verify_prelude(spec: dict[str, Any], env_vars: str) -> str:
    """verify-factory.sh up to the generated-artifact checks; `env_vars` are the script's optional inputs."""
    proj = spec["project"]
    backlog_check = "" if _vcs_enabled(spec) else 'test -f "$ROOT_DIR/backlog/issues.json"'
    if not has_eval_project(spec):
        return f"""#!/usr/bin/env bash
# End-to-End Infrastructure & L1-L5 Evaluation Oracle Verification Script
# Generated by scion_factory.py for {proj['name']} ({proj['slug']})
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ -f "$ROOT_DIR/config/factory.env" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT_DIR/config/factory.env"
fi

BUILD_PROJECT_SLUG="${{BUILD_PROJECT:-{proj['slug']}}}"
{env_vars}
echo "[1/4] Verifying generated factory artifacts for $BUILD_PROJECT_SLUG..."
test -f "$ROOT_DIR/factory.yaml"
test -f "$ROOT_DIR/config/firestore/README.md"
test -f "$ROOT_DIR/config/firestore/firestore.indexes.json"
test -f "$ROOT_DIR/waves/issues.json"
test -f "$ROOT_DIR/templates/_shared/agents.md"
test -f "$ROOT_DIR/knowledge-plane/INDEX.md"
test -f "$ROOT_DIR/knowledge-plane/kp-check.py"
{backlog_check}
echo "  Knowledge plane present. In the build workspace, run: python3 knowledge-plane/kp-check.py"
"""

    return f"""#!/usr/bin/env bash
# End-to-End Infrastructure & L1-L5 Evaluation Oracle Verification Script
# Generated by scion_factory.py for {proj['name']} ({proj['slug']} + {proj['eval_slug']})
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ -f "$ROOT_DIR/config/factory.env" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT_DIR/config/factory.env"
fi

BUILD_PROJECT_SLUG="${{BUILD_PROJECT:-{proj['slug']}}}"
EVAL_PROJECT_SLUG="${{EVAL_PROJECT:-{proj['eval_slug']}}}"
{env_vars}
echo "[1/4] Verifying generated factory artifacts for $BUILD_PROJECT_SLUG and $EVAL_PROJECT_SLUG..."
test -f "$ROOT_DIR/factory.yaml"
test -f "$ROOT_DIR/config/firestore/README.md"
test -f "$ROOT_DIR/config/firestore/firestore.indexes.json"
test -f "$ROOT_DIR/eval-workspace/tools/factory_store.py"
test -f "$ROOT_DIR/waves/issues.json"
test -f "$ROOT_DIR/templates/_shared/agents.md"
test -f "$ROOT_DIR/knowledge-plane/INDEX.md"
test -f "$ROOT_DIR/knowledge-plane/kp-check.py"
{backlog_check}
echo "  Knowledge plane present. In the build workspace, run: python3 knowledge-plane/kp-check.py"
"""


def _render_verify_factory_sh(spec: dict[str, Any]) -> str:
    if deploy_mode(spec) == "none":
        return _render_verify_code_sh(spec)
    proj = spec["project"]
    l1_ep = spec["evaluation_pyramid"]["l1_health"]["endpoint"]
    l3 = spec["evaluation_pyramid"]["l3_domain_quality"]
    l5 = spec["evaluation_pyramid"]["l5_latency_slo"]

    return _render_verify_prelude(spec, 'SERVING_URL="${SERVING_URL:-}"\nEVAL_URL="${EVAL_URL:-}"\n') + f"""
echo "[2/4] Verifying L1 Health endpoint ({l1_ep}) if SERVING_URL is set..."
if [[ -n "$SERVING_URL" ]]; then
  http_code="$(curl -s -o /dev/null -w '%{{http_code}}' "$SERVING_URL{l1_ep}")"
  if [[ "$http_code" != "200" ]]; then
    echo "ERROR: L1 health check failed on $SERVING_URL{l1_ep} (HTTP $http_code)" >&2
    exit 1
  fi
  echo "  L1 Health OK (HTTP 200)"
else
  echo "  SKIP live L1 curl (set SERVING_URL to verify live Cloud Run deployment)"
fi

echo "[3/4] Verifying L3 Domain Oracle ({l3['primary_metric']} >= {l3['target_threshold']}) & L5 Latency SLO (p95 <= {l5['p95_ms']}ms)..."
if [[ -n "$EVAL_URL" ]]; then
  eval_json="$(curl -fsSL "$EVAL_URL/api/eval")"
  echo "$eval_json" | python3 -c '
import json, sys
data = json.load(sys.stdin)
metric = data.get("primary_metric_value")
if metric is not None and float(metric) < {l3["target_threshold"]}:
    sys.exit(f"L3 threshold failed: {{metric}} < {l3["target_threshold"]}")
print("  Live eval payload verified.")
'
else
  echo "  SKIP live EVAL_URL check (set EVAL_URL to verify live Evaluation Control Tower)"
fi

echo "[4/4] Verification complete for {proj['name']}."
"""


def _render_verify_code_sh(spec: dict[str, Any]) -> str:
    """verify-factory.sh for a factory with nothing to deploy (spec §8): L1 build and tests and the L4 tool, no curl.

    Every spec-derived string reaches bash single-quoted (shlex.quote), so commands with $(...), backticks or double
    quotes are printed literally and run exactly once, through `check`. DRY_RUN=1 prints `+ <command>` instead.
    """
    q = shlex.quote
    proj = spec["project"]
    stack = spec["stack"]
    l3 = spec["evaluation_pyramid"]["l3_domain_quality"]
    subdir = _monorepo(spec)[1]
    workdir = f'"$WORKSPACE_DIR"/{q(subdir)}' if subdir else '"$WORKSPACE_DIR"'
    l4 = _code_tool(spec, "l4")
    if l4:
        l4_tool = l4.split()[0]
        l4_step = f"""printf '%s\\n' {q(f"[3/4] Verifying L4 Safety ({l4}) if WORKSPACE_DIR is set...")}
if [[ -n "$WORKSPACE_DIR" ]]; then
  if [[ "$DRY_RUN" != "1" ]] && ! command -v {q(l4_tool)} >/dev/null 2>&1; then
    printf 'ERROR: L4 tool %s is not installed (install it, or ask the PO to change the L4 tool)\\n' {q(l4_tool)} >&2
    exit 1
  fi
  check "L4 check" {q(l4)}
  echo "  L4 Safety OK$ok_note"
else
  echo "  SKIP L4 check (set WORKSPACE_DIR to a checkout of the product repository)"
fi
"""
    else:
        l4_step = f"""printf '%s\\n' {q(f"[3/4] L4 Safety: no standard tool for {stack.get('primary_language', '')} ({ASK_PO_TOOL}).")}
echo "  SKIP L4 check until the PO names the tool"
"""
    done = (f"[4/4] Verification complete for {proj['name']}. The evaluation team grades L2 API compatibility, "
            f"L3 ({l3['primary_metric']} >= {l3['target_threshold']}) and L5 benchmarks.")
    env_vars = ('WORKSPACE_DIR="${WORKSPACE_DIR:-}"   # a checkout of the product repository to run the code checks in\n'
                'DRY_RUN="${DRY_RUN:-0}"   # 1: print each check command instead of running it\n')
    return _render_verify_prelude(spec, env_vars) + f"""
# check <label> <command>: run one code check in the workspace (DRY_RUN=1 prints it instead) and stop on failure.
ok_note=""
[[ "$DRY_RUN" == "1" ]] && ok_note=" (dry run: printed, not run)"
check() {{
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '+ %s\\n' "$2"
    return 0
  fi
  if ! ( eval "$2" ); then
    printf 'ERROR: %s failed: %s\\n' "$1" "$2" >&2
    exit 1
  fi
}}

echo "[2/4] Verifying L1 Builds and tests if WORKSPACE_DIR is set (nothing to deploy: no live checks)..."
if [[ -n "$WORKSPACE_DIR" ]]; then
  cd {workdir}
  check "L1 build" {q(stack['build_command'])}
  check "L1 tests" {q(stack['test_command'])}
  echo "  L1 Builds and tests OK$ok_note"
else
  echo "  SKIP L1 build and tests (set WORKSPACE_DIR to a checkout of the product repository)"
fi

{l4_step}
printf '%s\\n' {q(done)}
"""


def _render_seed_waves_sh(spec: dict[str, Any]) -> str:
    proj = spec["project"]
    repo = spec["vcs"]["repo"]
    flabel = _factory_label(spec)
    digest = _digest_title(spec)
    labels = ["status:ready", "status:in-progress", "status:in-review", "status:verified", "status:blocked",
              "status:po-decision", "status:po-review", "status:approved", "digest", "eval-finding",
              "kaizen-proposal", "kaizen-eval", flabel]
    create_labels = f'"status:ready", "--label", "{flabel}"'
    return f"""#!/usr/bin/env bash
# Seed Wave 1..N GitHub Issues for {proj['name']} ({repo})
# Creates the status, PO inbox, digest, eval-finding and factory labels, the pinned digest issue "{digest}" (only if
# absent), and the wave issues. DRY_RUN=1 (the default) only prints what it would do.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
ISSUES_FILE="$SCRIPT_DIR/../waves/issues.json"
REPO="${{GITHUB_REPO:-{repo}}}"
FACTORY_LABEL="{flabel}"
DIGEST_TITLE="{digest}"

if [[ ! -f "$ISSUES_FILE" ]]; then
  echo "Missing $ISSUES_FILE" >&2
  exit 1
fi

for lbl in {" ".join(f'"{l}"' for l in labels)}; do
  if [[ "${{DRY_RUN:-1}}" == "0" ]]; then
    gh label create "$lbl" --repo "$REPO" --force >/dev/null 2>&1 || true
  else
    echo "[dry-run] gh label create $lbl --repo $REPO"
  fi
done

# One pinned digest issue per factory: each lead adds one comment a day (spec §7.3). GitHub pins at most 3 issues
# per repository, so a failed pin only warns: it must never stop the waves from being seeded.
pin_digest() {{
  if ! gh issue pin "$1" --repo "$REPO" >/dev/null 2>&1; then
    echo "WARNING: could not pin the digest issue $1 (already pinned, or the repository has 3 pinned issues); pin it by hand" >&2
  fi
}}

if [[ "${{DRY_RUN:-1}}" == "0" ]]; then
  existing="$(gh issue list --repo "$REPO" --state open --label digest --label "$FACTORY_LABEL" \\
    --search "\\"$DIGEST_TITLE\\" in:title" --json number --jq '.[0].number // empty')"
  if [[ -z "$existing" ]]; then
    url="$(gh issue create --repo "$REPO" --title "$DIGEST_TITLE" --label digest --label "$FACTORY_LABEL" \\
      --body "Daily digest: each lead adds one comment a day (what shipped, what is blocked and on whom, the PO inbox).")"
    pin_digest "$url"
  else
    echo "Digest issue #$existing already exists"
    pin_digest "$existing"
  fi
else
  echo "[dry-run] gh issue create --repo $REPO --title \\"$DIGEST_TITLE\\" --label digest --label $FACTORY_LABEL (only if absent), then gh issue pin"
fi

# DRY_RUN goes in as an argument: the quoted heredoc below is not expanded by the shell.
python3 - "$ISSUES_FILE" "$REPO" "${{DRY_RUN:-1}}" "$FACTORY_LABEL" <<'PY'
import json, subprocess, sys
issues_path, repo, dry_run, factory_label = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
waves = json.load(open(issues_path))
existing_titles = set()
if dry_run == "0":
    raw = subprocess.run(
        ["gh", "issue", "list", "--repo", repo, "--state", "all", "--label", factory_label, "--limit", "500", "--json", "title"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    if raw:
        try:
            existing_titles = {{item.get("title", "") for item in json.loads(raw) if isinstance(item, dict)}}
        except Exception:
            existing_titles = set()
for w in waves:
    wave_num = w["wave"]
    for issue in w["issues"]:
        title = issue["title"]
        criteria = "\\n".join(f"- [ ] {{c}}" for c in issue.get("acceptance_criteria", []))
        body = f"## Wave {{wave_num}}: {{w['title']}}\\n\\n**Owner Role:** `{{issue['owner_role']}}`\\n\\n### Acceptance Criteria\\n{{criteria}}\\n"
        if title in existing_titles:
            print(f"[wave-{{wave_num}}] {{title}} (already exists, skipping)")
            continue
        print(f"[wave-{{wave_num}}] {{title}} (owner={{issue['owner_role']}})")
        if dry_run == "0":
            subprocess.run(["gh", "issue", "create", "--repo", repo, "--title", title, "--body", body, "--label", {create_labels}], check=True)
            existing_titles.add(title)
PY
"""


def _render_root_agents_md(spec: dict[str, Any], roster_notes: list[str] | None = None) -> str:
    proj = spec["project"]
    stack = spec["stack"]
    infra = spec["infrastructure"]
    team = spec["team"]
    l3 = spec["evaluation_pyramid"]["l3_domain_quality"]
    serves = deploy_mode(spec) != "none"
    has_eval = has_eval_project(spec)
    wmode = wake_mode(spec)
    # Where each team's work lands: a Cloud Run service, or nothing to deploy (spec §8).
    build_target = f"Cloud Run `{infra['serving_service']}`" if serves else NOTHING_TO_DEPLOY

    build_rows = "\n".join(
        f"| `{r['role']}` | `{proj['slug']}` | `{r['harness']}` | `{r['model']}` | {r['responsibility']} |"
        for r in team["build_roles"]
    )
    eval_section = ""
    if has_eval:
        eval_rows = "\n".join(
            f"| `{r['role']}` | `{proj['eval_slug']}` | `{r['harness']}` | `{r['model']}` | {r['responsibility']} |"
            for r in (team.get("eval_roles") or [])
        )
        eval_section = f"""

### Evaluation Team (`{proj['eval_slug']}`)
| Role | Project | Harness | Model | Responsibility |
|------|---------|---------|-------|----------------|
{eval_rows}"""
    notes_section = ""
    if roster_notes:
        notes_section = "\n### Roster notes\n" + "\n".join(f"- {n}" for n in roster_notes) + (
            "\n\nThe compiler added these default specialists because the spec doesn't list them. To leave one out, "
            "add it to `team.dropped_defaults` with a reason and recompile.\n"
        )
    vcs = spec.get("vcs") or {}
    if vcs.get("enabled"):
        if has_eval:
            eval_target = (f"Cloud Run `{infra['eval_service']}`" if serves
                           else "grades the build's code (the L1–L5 code pyramid)")
            vcs_lines = (
                f"- **Repository:** `{vcs['repo']}`\n"
                f"- **Build Project (`{proj['slug']}`):** Branch `{vcs['build_branch']}` -> {build_target}\n"
                f"- **Evaluation Project (`{proj['eval_slug']}`):** Disjoint orphan branch `{vcs['eval_branch']}` -> {eval_target}\n"
                f"- **PO inbox:** `gh issue list -R {vcs['repo']} --label status:po-decision`; daily digest on the pinned "
                f"issue \"{_digest_title(spec)}\"; issues labeled `{_factory_label(spec)}`"
            )
        else:
            vcs_lines = (
                f"- **Repository:** `{vcs['repo']}`\n"
                f"- **Build Project (`{proj['slug']}`):** Branch `{vcs['build_branch']}` -> {build_target} (single-project factory; on-demand evaluation runs in-project)\n"
                f"- **PO inbox:** `gh issue list -R {vcs['repo']} --label status:po-decision`; daily digest on the pinned "
                f"issue \"{_digest_title(spec)}\"; issues labeled `{_factory_label(spec)}`"
            )
    elif has_eval:
        eval_target = (f"Cloud Run `{infra['eval_service']}`" if serves
                       else "grades the build's code (the L1–L5 code pyramid)")
        vcs_lines = (
            "- **Version control:** Not configured yet. Backlog in `backlog/issues.json`; deploys from workspace snapshots. "
            "Run `scion-factory enable-git` and `scripts/enable-git.sh` to add Git later.\n"
            f"- **Build Project (`{proj['slug']}`):** Shared workspace -> {build_target}\n"
            f"- **Evaluation Project (`{proj['eval_slug']}`):** Separate workspace -> {eval_target}"
        )
    else:
        vcs_lines = (
            "- **Version control:** Not configured yet. Backlog in `backlog/issues.json`; deploys from workspace snapshots. "
            "Run `scion-factory enable-git` and `scripts/enable-git.sh` to add Git later.\n"
            f"- **Build Project (`{proj['slug']}`):** Shared workspace -> {build_target} (single-project factory)"
        )

    if serves:
        l5 = spec["evaluation_pyramid"]["l5_latency_slo"]
        pyramid = f"""## 4. L1–L5 Evaluation Pyramid Oracle
- **L1 Health:** `{spec['evaluation_pyramid']['l1_health']['endpoint']}`
- **L2 Contract:** {'; '.join(spec['evaluation_pyramid']['l2_contract']['invariants'])}
- **L3 Domain Metric:** `{l3['primary_metric']} >= {l3['target_threshold']}`
- **L4 Safety & Grounding:** {'; '.join(spec['evaluation_pyramid']['l4_safety_grounding']['invariants'])}
- **L5 Latency SLO:** `p50 <= {l5.get('p50_ms', 100)}ms`, `p95 <= {l5['p95_ms']}ms`, `p99 <= {l5.get('p99_ms', 500)}ms`
"""
    else:
        pyramid = f"## 4. L1–L5 Code Evaluation Pyramid\n{render_code_pyramid(spec)}\n"

    heartbeat_desc = (
        f"`delta` (zero-LLM K8s delta controller + auto-sleep) + Watchdog (`{team.get('watchdog_cron', '*/5 * * * *')}`)"
        if wmode == "delta"
        else f"`{team.get('heartbeat_mode', 'direct-lead')}` (`{team.get('heartbeat_cron', '*/15 * * * *')}`) + Watchdog (`{team.get('watchdog_cron', '*/5 * * * *')}`)"
    )

    return f"""# {proj['name']} — Scion Autonomous Software Factory (`AGENTS.md`)

## 1. Mission & Architecture
- **Mission:** {proj['mission']}
{vcs_lines}
- **Knowledge Plane:** [`knowledge-plane/INDEX.md`](knowledge-plane/INDEX.md) (copy `knowledge-plane/` to the root of the build workspace)
- **Heartbeat Mode:** {heartbeat_desc}
- **Autonomy:** `{autonomy(spec)}` (what agents decide alone and what waits for the PO: the leads' Decision rights)

## 2. Build & Verification Commands
- **Build:** `{stack['build_command']}`
- **Test:** `{stack['test_command']}`
- **Lint:** `{stack.get('lint_command', 'true')}`

## 3. Agent Roster

### Build Team (`{proj['slug']}`)
| Role | Project | Harness | Model | Responsibility |
|------|---------|---------|-------|----------------|
{build_rows}{eval_section}
{notes_section}
{pyramid}"""


def _render_backlog(spec: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for wave in spec["waves"]:
        for n, issue in enumerate(wave.get("issues") or [], start=1):
            items.append({
                "id": f"W{wave['wave']}-{n}",
                "wave": wave["wave"],
                "title": issue["title"],
                "owner_role": issue.get("owner_role", "lead"),
                "requirements": list(issue.get("requirements") or []),
                "acceptance_criteria": list(issue.get("acceptance_criteria") or []),
                "status": "ready",
            })
    return items


ENABLE_GIT_SH = r'''#!/usr/bin/env bash
# Add Git and GitHub to a __NAME__ factory that started without version control.
#
# Run on the Hub VM as the Hub OS user, from this kit directory. Dry run unless DRY_RUN=0.
# Before you run it with DRY_RUN=0:
#   1. Create the empty GitHub repository OWNER/NAME.
#   2. Make sure the Hub OS user can push to it (for example, `gh auth setup-git`) and has a Git identity.
#   3. Run `scion-factory enable-git factory.yaml --repo OWNER/NAME`, recompile the kit, and push the new templates.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
if [[ -f "$ROOT_DIR/config/factory.env" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT_DIR/config/factory.env"
fi

DRY_RUN="${DRY_RUN:-1}"
REPO="${GITHUB_REPO:-${1:-}}"
BUILD_BRANCH="${BUILD_BRANCH:-main}"
EVAL_BRANCH="${EVAL_BRANCH:-eval-dashboard}"
BUILD_WORKSPACE="${BUILD_WORKSPACE:-$HOME/.scion/projects/__SLUG__}"
EVAL_WORKSPACE="${EVAL_WORKSPACE:-$HOME/.scion/projects/__EVAL_SLUG__}"

if [[ -z "$REPO" ]]; then
  echo "usage: GITHUB_REPO=OWNER/NAME $0   (or: $0 OWNER/NAME)" >&2
  exit 2
fi
if [[ "$BUILD_BRANCH" == "$EVAL_BRANCH" ]]; then
  echo "BUILD_BRANCH and EVAL_BRANCH must differ (orphan branch isolation, L25)" >&2
  exit 2
fi

run() {
  if [[ "$DRY_RUN" == "0" ]]; then
    "$@"
  else
    printf '[dry-run]'
    printf ' %q' "$@"
    printf '\n'
  fi
}

ensure_gitignore() {
  local ws="$1"
  local f="$ws/.gitignore"
  for pattern in ".env" ".env.*" ".envrc" ".scion/" "node_modules/" ".venv/"; do
    if ! grep -qxF "$pattern" "$f" 2>/dev/null; then
      run sh -c "printf '%s\n' '$pattern' >> '$f'"
    fi
  done
}

import_workspace() {
  local ws="$1" branch="$2" message="$3"
  if [[ ! -d "$ws" ]]; then
    echo "workspace not found: $ws" >&2
    exit 1
  fi
  if [[ -d "$ws/.git" ]]; then
    echo "  $ws is already a Git repository; skipping init"
  else
    run git -C "$ws" init -q -b "$branch"
  fi
  ensure_gitignore "$ws"
  run git -C "$ws" add -A
  run git -C "$ws" commit -q -m "$message"
  if ! git -C "$ws" remote get-url origin >/dev/null 2>&1; then
    run git -C "$ws" remote add origin "https://github.com/$REPO.git"
  fi
  run git -C "$ws" push -u origin "$branch"
}

echo "[1/3] Migrate backlog/issues.json to GitHub issues and repoint the knowledge plane"
BACKLOG="$BUILD_WORKSPACE/backlog/issues.json"
if [[ ! -f "$BACKLOG" ]]; then
  echo "  no backlog at $BACKLOG; skipping"
else
  python3 - "$BACKLOG" "$REPO" "$DRY_RUN" <<'PY'
import json, subprocess, sys
path, repo, dry_run = sys.argv[1], sys.argv[2], sys.argv[3] != "0"
items = json.load(open(path))

def run(cmd):
    if dry_run:
        print("[dry-run] " + " ".join(cmd))
    else:
        subprocess.run(cmd, check=True)

for status in ("ready", "in-progress", "in-review", "verified", "blocked"):
    run(["gh", "label", "create", f"status:{status}", "--repo", repo, "--force"])
for item in items:
    criteria = "\n".join(f"- [ ] {c}" for c in item.get("acceptance_criteria", []))
    reqs = ", ".join(item.get("requirements", [])) or "none"
    body = (f"Migrated from backlog item `{item['id']}`.\n\n**Owner role:** `{item['owner_role']}`\n"
            f"**Requirements:** {reqs}\n\n### Acceptance criteria\n{criteria}\n")
    run(["gh", "issue", "create", "--repo", repo, "--title", f"[{item['id']}] {item['title']}",
         "--body", body, "--label", f"status:{item.get('status', 'ready')}"])
print(f"{len(items)} backlog item(s) processed")
PY
  run mv "$BACKLOG" "$BUILD_WORKSPACE/backlog/issues.migrated.json"
fi

INDEX="$BUILD_WORKSPACE/knowledge-plane/INDEX.md"
if [[ -f "$INDEX" ]]; then
  run python3 - "$INDEX" "$REPO" <<'PY'
import sys
path, repo = sys.argv[1], sys.argv[2]
lines = open(path).read().splitlines()
row = f"| Find the backlog | GitHub issues in `{repo}` (`status:*` labels) |"
lines = [row if line.startswith("| Find the backlog |") else line for line in lines]
open(path, "w").write("\n".join(lines) + "\n")
PY
fi

echo "[2/3] Import the build workspace into $REPO on branch $BUILD_BRANCH"
import_workspace "$BUILD_WORKSPACE" "$BUILD_BRANCH" "chore: import factory build workspace into Git"

if [[ -n "__EVAL_SLUG__" ]]; then
  echo "[3/3] Import the evaluation workspace as the orphan branch $EVAL_BRANCH (no shared history)"
  import_workspace "$EVAL_WORKSPACE" "$EVAL_BRANCH" "chore: import factory evaluation workspace into Git"
else
  echo "[3/3] Single-project factory: no separate evaluation workspace to import"
fi

echo "Done. Next: recompile the kit after 'scion-factory enable-git', push the templates, and restart the agents."
'''


def _render_enable_git_sh(spec: dict[str, Any]) -> str:
    proj = spec["project"]
    eval_slug = str(proj.get("eval_slug") or "")
    return (ENABLE_GIT_SH.replace("__NAME__", proj["name"])
            .replace("__SLUG__", proj["slug"]).replace("__EVAL_SLUG__", eval_slug))


def compile_factory_boilerplate(
    spec: dict[str, Any],
    out_dir: str | Path,
    force_knowledge: bool = False,
    env_file: str | Path | None = None,
    skill_bank: str | Path | None = None,
) -> dict[str, Any]:
    """Compile a validated FactorySpec into a complete Scion factory directory.

    Files under knowledge-plane/, backlog/, and eval-workspace/ are seeded once and never
    overwritten, because agents edit them after bootstrap. Pass force_knowledge=True to reseed.
    """
    readiness = evaluate_spec_readiness(spec)
    if not readiness["ready"]:
        raise ValueError(f"FactorySpec is not ready to compile: {readiness['missing_or_weak']}")
    # Older specs and presets get the missing default specialists; the kit's factory.yaml records the full roster.
    spec, roster_notes = normalize_team(spec)
    has_eval = has_eval_project(spec)
    compile_env = _load_env_map(env_file)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written_files: list[str] = []

    skipped_files: list[str] = []

    def _write(rel_path: str, content: str, executable: bool = False, seed_once: bool = False) -> None:
        target = out / rel_path
        if seed_once and target.exists() and not force_knowledge:
            skipped_files.append(rel_path)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        if executable:
            target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        written_files.append(rel_path)

    # 1. Canonical spec & root AGENTS.md
    save_spec(spec, out / "factory.yaml")
    written_files.append("factory.yaml")
    _write("AGENTS.md", _render_root_agents_md(spec, roster_notes))

    git = _vcs_enabled(spec)
    backlog_ref = (
        f"open GitHub issues in {spec['vcs']['repo']}" if git
        else f"/workspace/backlog/issues.json and {_ws(spec)}/knowledge-plane/INDEX.md"
    )

    # 2. Config files
    _write("config/factory.env.example", _render_factory_env_example(spec))
    if any(_library_role(r) == "kaizen-analyst" for r in spec.get("team", {}).get("build_roles", [])):
        _write("config/kaizen-manifest.json", json.dumps(kaizen_policy.build_kaizen_manifest(spec), indent=2) + "\n")
    _write("config/firestore/README.md", _render_firestore_contract(spec))
    _write("config/firestore/firestore.indexes.json", json.dumps(FIRESTORE_INDEXES, indent=2) + "\n")
    _write("config/firestore/firestore.rules", FIRESTORE_RULES)
    if has_eval:
        # The eval team's Firestore client; refreshed on every compile (it's code, not knowledge).
        _write("eval-workspace/tools/factory_store.py",
               (Path(__file__).resolve().parent / "factory_store.py").read_text(), executable=True)
    _write(
        "config/heartbeat-message.txt",
        (
            f"[HEARTBEAT] Inspect {backlog_ref}, "
            f"check active teammates in {spec['project']['slug']} (max WIP={spec['team'].get('max_wip', 2)}), "
            f"advance verified slices, and end turn with sciontool status task_completed.\n"
        ),
    )

    # 3. Shared template protocol
    shared_md = _render_shared_agents_md(spec)
    _write("templates/_shared/agents.md", shared_md)

    # 4. Per-role templates for Build and (when two-project) Eval teams
    role_groups: list[tuple[bool, list[dict[str, Any]]]] = [(False, spec["team"]["build_roles"])]
    if has_eval:
        role_groups.append((True, spec["team"].get("eval_roles") or []))
    for is_eval, role_list in role_groups:
        for role_spec in role_list:
            role = role_spec["role"]
            _write(f"templates/{role}/scion-agent.yaml", _render_scion_agent_yaml(role_spec, spec))
            _write(
                f"templates/{role}/system-prompt.md",
                _render_role_system_prompt(spec, role_spec, is_eval, compile_env=compile_env),
            )
            _write(f"templates/{role}/agents.md", shared_md)

            if role_spec["harness"] == "opencode":
                _write(
                    f"templates/{role}/home/.local/bin/opencode-launch.sh",
                    _render_opencode_launcher(role_spec["model"]),
                    executable=True,
                )
                _write(
                    f"templates/{role}/home/.local/bin/opencode",
                    "#!/usr/bin/env bash\n"
                    "export GOOGLE_CLOUD_LOCATION=\"${GOOGLE_CLOUD_LOCATION:-global}\"\n"
                    "if [ -n \"${GITHUB_TOKEN:-}\" ]; then export SCION_GIT_TOKEN=\"$GITHUB_TOKEN\"; unset GITHUB_TOKEN; fi\n"
                    "exec /usr/local/bin/opencode \"$@\"\n",
                    executable=True,
                )
            elif role_spec["harness"] == "hermes":
                _write(
                    f"templates/{role}/home/.local/bin/hermes-launch.sh",
                    _render_hermes_launcher(role_spec["model"]),
                    executable=True,
                )
                _write(
                    f"templates/{role}/home/.local/bin/hermes",
                    "#!/usr/bin/env bash\n"
                    "export VERTEX_REGION=\"${VERTEX_REGION:-global}\"\n"
                    "export GOOGLE_CLOUD_LOCATION=\"${GOOGLE_CLOUD_LOCATION:-global}\"\n"
                    "exec /usr/local/bin/hermes \"$@\"\n",
                    executable=True,
                )

    # 5. Knowledge plane (seeded once) and evaluation-only golden data notes (two-project kits only)
    for rel, content in kp.render_knowledge_plane(spec, git).items():
        _write(rel, content, executable=rel.endswith(".py"), seed_once=True)
    if has_eval:
        _write("eval-workspace/golden/README.md", kp.render_eval_golden_readme(spec), seed_once=True)

    # 6. Waves, backlog & operational scripts (mode-specific; stale scripts from the other mode are removed)
    _write("waves/issues.json", json.dumps(spec["waves"], indent=2) + "\n")
    if git:
        _write("scripts/seed-github-waves.sh", _render_seed_waves_sh(spec), executable=True)
        stale = out / "scripts" / "enable-git.sh"
    else:
        _write("backlog/issues.json", json.dumps(_render_backlog(spec), indent=2) + "\n", seed_once=True)
        _write("scripts/enable-git.sh", _render_enable_git_sh(spec), executable=True)
        stale = out / "scripts" / "seed-github-waves.sh"
    if stale.exists():
        stale.unlink()
    _write("scripts/verify-factory.sh", _render_verify_factory_sh(spec), executable=True)

    # 7. Optional role-scoped skill injection and strict Scion container delegation
    import skill_curation
    effective_bank = skill_bank or spec.get("skill_bank")
    skill_warnings: list[str] = []
    if effective_bank:
        skill_warnings = skill_curation.inject_skills(spec, effective_bank, out)
    for rel_settings in skill_curation.apply_strict_delegation(spec, out):
        written_files.append(rel_settings)

    return {
        "out_dir": str(out),
        "files_written": len(written_files),
        "files": written_files,
        "preserved": skipped_files,
        "warnings": skill_warnings,
    }


# ---------------------------------------------------------------------------
# Agent-Driven Draft Spec (`draft_spec_from_repo`)
# ---------------------------------------------------------------------------
#
# The grill-me interview is always conducted by an agentic coding agent (see
# skills/scion-factory-bootstrap/SKILL.md). This module never blocks on stdin:
# it writes a draft that contains only STATIC facts plus the proven roster
# defaults, and leaves every INTENT field empty so the readiness gate fails
# until the agent has elicited real answers from the human.

# Claude Code and Antigravity run on a stock Hub without harness patches (full e2e, lessons-learned.md L28): the
# bundled Hermes config crashes on a fresh image and the bundled OpenCode launcher stops at a permission prompt.
# The interview can still choose OpenCode or Hermes per role; see docs/setup-runbook.md step 7 for their patches.
# The draft roster is exactly the role library's always-roles (spec §5.1-§5.3); catalog specialists such as frontend
# or deployer come from discovery (`proposed_roles`) and `scion-factory add-role`.
DEFAULT_BUILD_ROLES: list[dict[str, Any]] = [role_library.library_role_entry(r) for r in role_library.ALWAYS_BUILD]

DEFAULT_EVAL_ROLES: list[dict[str, Any]] = [role_library.library_role_entry(r) for r in role_library.ALWAYS_EVAL]


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "new-project"


def draft_spec_from_repo(repo_path: str | Path | None) -> dict[str, Any]:
    """Build a draft FactorySpec with STATIC facts filled and INTENT fields empty."""
    detected: dict[str, Any] = {}
    slug = "new-project"
    if repo_path:
        detected = inspect_repository(repo_path)["detected"]
        slug = _slugify(Path(repo_path).resolve().name)

    language = detected.get("primary_language", "")
    draft: dict[str, Any] = {
        "project": {
            "name": "",
            "slug": slug,
            "eval_slug": f"{slug}-eval",
            "mission": "",
            "non_goals": [],
        },
        "vcs": {
            "enabled": bool((detected.get("vcs") or {}).get("github_repo")),
            "provider": "github",
            "repo": (detected.get("vcs") or {}).get("github_repo", ""),
            "build_branch": "main",
            "eval_branch": "eval-dashboard",
        },
        "knowledge_plane": kp.empty_knowledge_plane(detected.get("knowledge_sources")),
        "stack": {
            "primary_language": "" if language in ("", "unknown") else language,
            "framework": detected.get("framework", ""),
            "build_command": detected.get("build_command", ""),
            "test_command": detected.get("test_command", ""),
            "lint_command": detected.get("lint_command", ""),
            "port": detected.get("port", 8080),
        },
        "infrastructure": {
            "deploy": "",  # intent: `service` or `none` (spec §8); the interview must answer
            "cloud_stores": detected.get("cloud_stores", []),
            "serving_service": f"{slug}-serving",
            "eval_service": f"{slug}-eval-dashboard",
            "budget_usd": None,
            "deployer_iam_roles": [],
        },
        "team": {
            "heartbeat_mode": "direct-lead",
            "heartbeat_cron": "*/15 * * * *",
            "watchdog_cron": "*/5 * * * *",
            "max_wip": 2,
            "autonomy": "standard",
            "verification_policy": "cross-family",
            "dropped_defaults": [],
            "build_roles": [dict(r, responsibility="", default=True) for r in DEFAULT_BUILD_ROLES],
            "eval_roles": [dict(r, responsibility="", default=True) for r in DEFAULT_EVAL_ROLES],
        },
        "evaluation_pyramid": {
            "l1_health": {"endpoint": "", "checks": []},
            "l2_contract": {"endpoints": [], "invariants": []},
            "l3_domain_quality": {
                "primary_metric": "",
                "target_threshold": None,
                "secondary_metric": "",
                "secondary_threshold": None,
                "probe_description": "",
            },
            "l4_safety_grounding": {"invariants": []},
            "l5_latency_slo": {"p50_ms": None, "p95_ms": None, "p99_ms": None},
        },
        "waves": [],
    }
    verification_policy.apply_cross_family_defaults(draft)
    return draft


def prepare_grill_session(repo_path: str | Path | None, out_path: str | Path, force: bool = False) -> dict[str, Any]:
    """Write (or reuse) the draft spec and return the payload the agent uses to interview the human."""
    out = Path(out_path)
    if out.exists() and not force:
        spec = load_spec(out)
    else:
        spec = draft_spec_from_repo(repo_path)
        save_spec(spec, out)

    if repo_path:
        inspection = inspect_repository(repo_path)
        detected, questions = inspection["detected"], inspection["unresolved_questions"]
        # Until the interview answers infrastructure.deploy, propose as for a service.
        mode = deploy_mode(spec)
        proposed = discovery.discover_roles(Path(repo_path), subdir=_monorepo(spec)[1],
                                            deploy=mode if mode in DEPLOY_MODES else "service")
    else:
        detected, questions, proposed = {}, get_grill_questions(), []

    return {
        "draft_path": str(out),
        "detected": detected,
        "readiness": evaluate_spec_readiness(spec),
        "questions": questions,
        "proposed_roles": proposed,
    }


def enable_git(spec_path: str | Path, repo: str, build_branch: str = "main",
               eval_branch: str = "eval-dashboard") -> dict[str, Any]:
    """Switch a FactorySpec to Git mode in place. Recompile the kit afterwards."""
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise ValueError("--repo must be OWNER/NAME")
    spec = load_spec(spec_path)
    has_eval = has_eval_project(spec)
    if has_eval and build_branch == eval_branch:
        raise ValueError("--eval-branch must differ from --build-branch (orphan branch isolation, L25)")
    vcs_cfg: dict[str, Any] = {
        "enabled": True,
        "provider": "github",
        "repo": repo,
        "build_branch": build_branch,
    }
    if has_eval:
        vcs_cfg["eval_branch"] = eval_branch
    spec["vcs"] = vcs_cfg
    save_spec(spec, spec_path)
    return spec


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scion-factory",
        description="Scion Factory Socratic Intent Elicitation ('Grill-Me') & Boilerplate Compiler",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_inspect = sub.add_parser("inspect", help="Inspect a repository to auto-detect stack and remaining intent questions")
    p_inspect.add_argument("repo_path", help="Path to target repository")
    p_inspect.add_argument("--json", action="store_true", help="Output JSON")

    p_grill = sub.add_parser(
        "grill",
        help="Write a draft factory.yaml and emit the grill-me question bank for your coding agent (never blocks)",
    )
    p_grill.add_argument("--repo", default=None, help="Optional repository path to inspect first")
    p_grill.add_argument("--out", default="factory.yaml", help="Draft FactorySpec YAML path")
    p_grill.add_argument("--force", action="store_true", help="Overwrite an existing draft instead of reusing it")
    p_grill.add_argument("--json", action="store_true", help="Output JSON (recommended for agents)")

    p_check = sub.add_parser("check", help="Evaluate readiness of a FactorySpec YAML/JSON file")
    p_check.add_argument("spec_path", help="Path to factory.yaml")
    p_check.add_argument("--json", action="store_true", help="Output JSON")

    p_compile = sub.add_parser("compile", help="Compile a validated FactorySpec into a complete boilerplate directory")
    p_compile.add_argument("spec_path", help="Path to factory.yaml")
    p_compile.add_argument("--out", required=True, help="Output directory for generated factory boilerplate")
    p_compile.add_argument("--env", default=None,
                           help="Optional factory.env path to align GCP_IDENTITY_DEFAULT / GCP_IDENTITY_MODE at compile time")
    p_compile.add_argument("--skill-bank", default=None,
                           help="Optional directory of curated skills to inject per role (overrides spec.skill_bank)")
    p_compile.add_argument("--force-knowledge", action="store_true",
                           help="Reseed knowledge-plane/, backlog/, and eval-workspace/ even if they exist")

    p_skills = sub.add_parser("audit-skills", help="Audit a skill bank directory for bloat, broken links, and stack conflicts")
    p_skills.add_argument("skill_bank_dir", help="Path to skill bank directory")
    p_skills.add_argument("--spec", default=None, help="Optional factory.yaml to check stack package-manager conflicts")
    p_skills.add_argument("--json", action="store_true", help="Output JSON")

    p_topo = sub.add_parser("audit-topology", help="Audit FactorySpec workload balance, VM role sizing, and container checklist")
    p_topo.add_argument("spec_path", help="Path to factory.yaml")
    p_topo.add_argument("--json", action="store_true", help="Output JSON")

    p_git = sub.add_parser("enable-git", help="Switch a factory that started without Git to Git mode")
    p_git.add_argument("spec_path", help="Path to factory.yaml")
    p_git.add_argument("--repo", required=True, help="GitHub repository OWNER/NAME")
    p_git.add_argument("--build-branch", default="main")
    p_git.add_argument("--eval-branch", default="eval-dashboard")

    p_prov = sub.add_parser("provision", help="Deterministically provision projects, infra, and templates")
    p_prov.add_argument("spec_path", type=str)
    p_prov.add_argument("--kit", type=str, required=True)
    p_prov.add_argument("--env", type=str, required=True)
    p_prov.add_argument("--seed-repo", type=str)
    p_prov.add_argument("--apply", action="store_true")
    p_prov.add_argument("--go", action="store_true")
    p_prov.add_argument("--json", action="store_true")

    p_gke_up = sub.add_parser("gke-up", help="One-command cold-start GKE Hub setup, kit compile, and factory provisioning")
    p_gke_up.add_argument("spec_path", type=str, help="Path to factory.yaml")
    p_gke_up.add_argument("--env", type=str, required=True, help="Path to factory.env (updated in place on --apply)")
    p_gke_up.add_argument("--kit", type=str, default=None, help="Output directory for compiled kit (default: build/kit-<slug>)")
    p_gke_up.add_argument("--create-project", action="store_true", help="Create HUB_PROJECT, link billing, and bootstrap VPC/APIs")
    p_gke_up.add_argument("--scion-repo", type=str, default=None, help="Optional local Scion source checkout")
    p_gke_up.add_argument("--apply", action="store_true", help="Execute live provisioning (default: dry-run)")
    p_gke_up.add_argument("--dry-run", action="store_true", help="Run in dry-run mode without mutating cloud state")
    p_gke_up.add_argument("--go", action="store_true", help="Send initial GO message after provisioning")
    p_gke_up.add_argument("--json", action="store_true", help="Output JSON")

    p_gke_down = sub.add_parser(
        "gke-down",
        help="Archive GKE pod logs/transcripts, write scorecard.json, clean GitHub test artifacts, and optionally delete project",
    )
    p_gke_down.add_argument("spec_path", type=str, help="Path to factory.yaml")
    p_gke_down.add_argument("--env", type=str, required=True, help="Path to factory.env")
    p_gke_down.add_argument("--backup-root", type=str, default=None, help="Root directory for <project>-<ts> archive")
    p_gke_down.add_argument("--cleanup-issue-label", type=str, default="", help="Close open GitHub test issues carrying this label")
    p_gke_down.add_argument("--cleanup-branch", type=str, default="", help="Delete this remote GitHub test branch")
    p_gke_down.add_argument("--delete-project", action="store_true", help="Delete HUB_PROJECT via gcloud projects delete --quiet")
    p_gke_down.add_argument("--confirm-project", type=str, default="", help="Confirm deletion of a non-ephemeral HUB_PROJECT")
    p_gke_down.add_argument("--apply", action="store_true", help="Execute archive, cleanup, and optional project deletion")
    p_gke_down.add_argument("--dry-run", action="store_true", help="Preview teardown actions without mutating state")
    p_gke_down.add_argument("--json", action="store_true", help="Output JSON")

    p_role = sub.add_parser("add-role", help="Add a library specialist (for example one discovery proposed) to a FactorySpec")
    p_role.add_argument("spec_path", help="Path to factory.yaml")
    p_role.add_argument("--role", required=True, help="Library role, e.g. security-auditor or sub-coordinator")
    p_role.add_argument("--as", dest="as_role", default=None,
                        help="Optional custom role slug, e.g. backend-sub-coordinator or api-builder")
    p_role.add_argument("--reports-to", default=None,
                        help="Optional parent sub-coordinator role slug in the same team")
    p_role.add_argument("--paths", action="append", default=[],
                        help="Optional repo-relative focus path for this role (repeatable)")
    p_role.add_argument("--team", choices=("build", "eval"), default=None,
                        help="Roster to add it to (default: the role's team in the library)")
    p_role.add_argument("--evidence", action="append", default=[],
                        help="Why the role is needed (repeatable), e.g. a path discovery found")

    p_hot = sub.add_parser(
        "hot-upgrade",
        help="Create missing templates, broadcast live rule deltas, and ask lead to spawn child specialists",
    )
    p_hot.add_argument("--project", required=True, help="Target Scion Hub project slug")
    p_hot.add_argument("--kit", required=True, help="Compiled kit directory containing templates/")
    p_hot.add_argument("--env", default="", help="Optional factory.env path")
    p_hot.add_argument("--roles", default="", help="Comma-separated template roles to push")
    p_hot.add_argument("--create-only", action="store_true", help="Default mode: create only missing Hub templates")
    p_hot.add_argument("--overwrite-templates", action="store_true", help="Overwrite existing Hub templates in --roles")
    p_hot.add_argument("--rule-delta", default="", help="Live prompt rule delta sent via Hub message")
    p_hot.add_argument("--rule-delta-file", default="", help="File containing a live prompt rule delta")
    p_hot.add_argument("--notify-agents", default="", help="Explicit comma-separated running agents to message")
    p_hot.add_argument("--spawn", action="append", default=[], help="Specialist to start via lead: template[:agent_name]")
    p_hot.add_argument("--lead", default="", help="Explicit lead agent slug (auto-detected by default)")
    p_hot.add_argument("--branch", default="", help="Optional Git branch flag (-b) for per-agent specialist starts")
    p_hot.add_argument("--harness-auth", default="vertex-ai", help="Harness auth flag passed to scion start")
    p_hot.add_argument("--apply", action="store_true", help="Execute mutations (dry-run by default)")
    p_hot.add_argument("--json", action="store_true", help="Output JSON")

    p_div = sub.add_parser(
        "diversify-team",
        help="Enforce cross-family verification on a FactorySpec and optionally set standing reviewer replicas",
    )
    p_div.add_argument("spec_path", help="Path to factory.yaml")
    p_div.add_argument("--reviewer-replicas", type=int, default=None,
                       help="Optional standing reviewer replica count (1-8)")

    args = parser.parse_args(argv)

    if args.cmd == "inspect":
        res = inspect_repository(args.repo_path)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            det = res["detected"]
            print(f"Repository: {res['repo_path']}")
            print(f"  Detected Language: {det['primary_language']}")
            print(f"  Build Command:     {det['build_command']}")
            print(f"  Test Command:      {det['test_command']}")
            print(f"  Cloud Stores:      {', '.join(det['cloud_stores']) or 'none detected'}")
            print(f"  Unresolved Intent Questions ({len(res['unresolved_questions'])}):")
            for q in res["unresolved_questions"]:
                print(f"    - [{q['pillar']}] ({', '.join(q['lessons'])}) {q['id']}: {q['question']}")
        return 0

    if args.cmd == "grill":
        payload = prepare_grill_session(args.repo, args.out, force=args.force)
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"Draft: {payload['draft_path']} (readiness {payload['readiness']['score']})")
            for q in payload["questions"]:
                print(f"\n[{q['pillar']}] {q['id']} (Lessons {', '.join(q['lessons'])})")
                print(f"  Q: {q['question']}")
                print(f"  Why: {q['why_we_ask']}")
                print(f"  Challenge: {q['challenge_prompt']}")
        return 0

    if args.cmd == "check":
        spec = load_spec(args.spec_path)
        report = evaluate_spec_readiness(spec)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"Readiness Score: {report['score']} (ready={report['ready']})")
            for item in report["missing_or_weak"]:
                print(f"  - MISSING/WEAK: {item}")
            for item in report["warnings"]:
                print(f"  - WARNING: {item}")
        return 0 if report["ready"] else 1

    if args.cmd == "compile":
        spec = load_spec(args.spec_path)
        res = compile_factory_boilerplate(
            spec,
            args.out,
            force_knowledge=args.force_knowledge,
            env_file=args.env,
            skill_bank=args.skill_bank,
        )
        print(f"Compiled {res['files_written']} boilerplate files into {res['out_dir']}")
        if res.get("preserved"):
            print(f"Preserved {len(res['preserved'])} existing knowledge files (use --force-knowledge to reseed)")
        for w in res.get("warnings") or []:
            print(f"  - WARNING: {w}")
        return 0

    if args.cmd == "audit-skills":
        import skill_curation
        spec_obj = load_spec(args.spec) if args.spec else None
        report = skill_curation.audit_bank(args.skill_bank_dir, spec_obj)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"Audited {report['skills_checked']} skills (ok={report['ok']})")
            for err in report["errors"]:
                print(f"  - ERROR: {err}")
            for warn in report["warnings"]:
                print(f"  - WARNING: {warn}")
        return 0 if report["ok"] else 1

    if args.cmd == "audit-topology":
        import topology_audit
        spec_obj = load_spec(args.spec_path)
        report = topology_audit.audit(spec_obj)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"Topology audit (warnings={len(report['warnings'])})")
            for warn in report["warnings"]:
                print(f"  - WARNING: {warn}")
            print("Container & runtime readiness checklist:")
            for item in report["checklist"]:
                print(f"  [ ] {item}")
        return 0 if report["ok"] else 1

    if args.cmd == "enable-git":
        try:
            spec = enable_git(args.spec_path, args.repo, args.build_branch, args.eval_branch)
        except ValueError as err:
            print(f"error: {err}", file=sys.stderr)
            return 2
        report = evaluate_spec_readiness(spec)
        print(f"Enabled Git for {args.spec_path}: {args.repo} ({args.build_branch} / {args.eval_branch}); ready={report['ready']}")
        print("Next: run scripts/enable-git.sh from the current kit (dry run first), then recompile the kit and push templates.")
        return 0 if report["ready"] else 1

    if args.cmd == "provision":
        spec = load_spec(args.spec_path)
        import provision_factory
        report = provision_factory.provision_factory(
            spec,
            Path(args.kit),
            Path(args.env),
            seed_repo=Path(args.seed_repo) if args.seed_repo else None,
            apply=args.apply,
            send_go=args.go,
        )
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            for p in report["phases"]:
                print(f"Phase {p['phase']}: {'OK' if p.get('ok') else 'SKIP'}")
        return 0 if report["ok"] else 1

    if args.cmd == "gke-up":
        import gke_lifecycle
        eff_apply = bool(args.apply) and not bool(args.dry_run)
        report = gke_lifecycle.gke_up(
            args.spec_path,
            env_file=args.env,
            kit_dir=args.kit,
            create_project=args.create_project,
            scion_repo=args.scion_repo,
            apply=eff_apply,
            send_go=args.go,
        )
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(
                f"gke-up ({'apply' if report.get('apply') else 'dry-run'}): ok={report.get('ok')} "
                f"total={report.get('total_sec')}s (setup={report.get('setup_gke_sec')}s, "
                f"compile={report.get('compile_sec')}s, provision={report.get('provision_sec')}s)"
            )
        return 0 if report.get("ok") else 1

    if args.cmd == "gke-down":
        import gke_lifecycle
        eff_apply = bool(args.apply) and not bool(args.dry_run)
        try:
            report = gke_lifecycle.gke_down(
                args.spec_path,
                env_file=args.env,
                backup_root=args.backup_root,
                cleanup_issue_label=args.cleanup_issue_label,
                cleanup_branch=args.cleanup_branch,
                delete_project=args.delete_project,
                confirm_project=args.confirm_project,
                apply=eff_apply,
            )
        except ValueError as err:
            if args.json:
                print(json.dumps({"ok": False, "error": str(err)}, indent=2))
            else:
                print(f"error: {err}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(
                f"gke-down ({'apply' if report.get('apply') else 'dry-run'}): ok={report.get('ok')} "
                f"backup_dir={report.get('backup_dir')}"
            )
        return 0 if report.get("ok") else 1

    if args.cmd == "add-role":
        before = load_spec(args.spec_path)
        present = {r.get("role") for key in ("build_roles", "eval_roles")
                   for r in ((before.get("team") or {}).get(key) or []) if isinstance(r, dict)}
        target_slug = (args.as_role or args.role).strip()
        try:
            spec = add_role(
                args.spec_path,
                args.role,
                team=args.team,
                evidence=args.evidence,
                as_role=args.as_role,
                reports_to=args.reports_to,
                paths=args.paths,
            )
        except ValueError as err:
            print(f"error: {err}", file=sys.stderr)
            return 2
        team_name = next(t for t in ("build", "eval")
                         if any(r.get("role") == target_slug for r in spec["team"].get(f"{t}_roles") or []))
        report = evaluate_spec_readiness(spec)
        print(json.dumps({
            "spec_path": str(args.spec_path),
            "role": target_slug,
            "team": team_name,
            "added": target_slug not in present,
            "ready": report["ready"],
            "next": "recompile the kit and push the new template",
        }, indent=2))
        return 0

    if args.cmd == "hot-upgrade":
        import hot_upgrade
        delta = args.rule_delta
        if args.rule_delta_file:
            delta = Path(args.rule_delta_file).read_text(encoding="utf-8")
        try:
            report = hot_upgrade.run_hot_upgrade(
                project_slug=args.project,
                templates_dir=Path(args.kit) / "templates",
                env_file=Path(args.env) if args.env else None,
                roles=[r for r in args.roles.split(",") if r.strip()],
                overwrite_templates=bool(args.overwrite_templates and not args.create_only),
                rule_delta=delta,
                notify_agents=[a for a in args.notify_agents.split(",") if a.strip()],
                spawn_specs=hot_upgrade.parse_spawn_specs(args.spawn),
                lead_slug=args.lead,
                branch=args.branch,
                harness_auth=args.harness_auth,
                apply=args.apply,
            )
        except (ValueError, RuntimeError) as err:
            print(f"error: {err}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            mode = "APPLY" if report["apply"] else "DRY-RUN"
            print(f"[{mode}] project={report['project']} templates={report['templates_pushed']} notified={report['notified_agents']} spawns={len(report['spawn_commands'])}")
        return 0

    if args.cmd == "diversify-team":
        try:
            spec, notes = diversify_team(args.spec_path, reviewer_replicas=args.reviewer_replicas)
        except ValueError as err:
            print(f"error: {err}", file=sys.stderr)
            return 2
        report = evaluate_spec_readiness(spec)
        print(json.dumps({
            "spec_path": str(args.spec_path),
            "verification_policy": spec["team"].get("verification_policy"),
            "reviewer_replicas": spec["team"].get("reviewer_replicas"),
            "changes": notes,
            "ready": report["ready"],
        }, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
