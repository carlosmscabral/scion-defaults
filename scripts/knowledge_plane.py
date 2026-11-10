"""Knowledge plane support for the Scion factory bootstrap.

Detects knowledge sources in a target repository, validates the `knowledge_plane`
section of a FactorySpec, and renders the knowledge plane files plus the
reference checker that agents run inside the workspace.

See docs/plans/2026-10-20-knowledge-plane-and-optional-git.md.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

KP_ROOT = "knowledge-plane"
EVAL_KP_MOUNT = "/scion-volumes/knowledge-plane"

SOURCE_KINDS = ("requirements", "adrs", "contracts", "design", "guides")

_DIR_CANDIDATES = {
    "adrs": (
        "knowledge-plane/adr",
        "docs/adr",
        "docs/adrs",
        "docs/decisions",
        "docs/architecture/decisions",
        "adr",
        "adrs",
        "decisions",
    ),
}

_FILE_CANDIDATES = {
    "requirements": (
        "PRODUCT.md",
        "PRD.md",
        "REQUIREMENTS.md",
        "SPEC.md",
        "docs/prd.md",
        "docs/PRD.md",
        "docs/product.md",
        "docs/requirements.md",
        "docs/spec.md",
    ),
    "design": ("DESIGN.md", "ARCHITECTURE.md", "docs/design.md", "docs/architecture.md"),
    "guides": ("README.md", "CONTRIBUTING.md", "AGENTS.md", "CLAUDE.md", "GEMINI.md"),
}

_CONTRACT_PATTERNS = (
    re.compile(r"^(openapi|swagger)[\w.-]*\.(ya?ml|json)$", re.IGNORECASE),
    re.compile(r"\.proto$"),
    re.compile(r"^schema\.graphqls?$", re.IGNORECASE),
)

_SKIP_DIRS = {".git", "node_modules", "vendor", ".venv", "venv", "dist", "build", "__pycache__", ".scion", "target"}
_MAX_DEPTH = 5

_REQ_ID = re.compile(r"\bREQ-\d{3,}\b")


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def detect_knowledge_sources(root: Path) -> dict[str, list[str]]:
    """Return existing knowledge sources in `root`, as repository-relative paths."""
    sources: dict[str, list[str]] = {kind: [] for kind in SOURCE_KINDS}
    for kind, candidates in _DIR_CANDIDATES.items():
        sources[kind] = [c for c in candidates if (root / c).is_dir()]
    for kind, candidates in _FILE_CANDIDATES.items():
        sources[kind] = [c for c in candidates if (root / c).is_file()]

    contracts: list[str] = []
    root_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).parts) - root_depth
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and depth < _MAX_DEPTH]
        for name in filenames:
            if any(p.search(name) for p in _CONTRACT_PATTERNS):
                contracts.append(str((Path(dirpath) / name).relative_to(root)))
    sources["contracts"] = sorted(contracts)
    return sources


def detect_vcs(root: Path) -> dict[str, Any]:
    """Report whether `root` is a Git repository and, if so, its GitHub `owner/name` remote."""
    info: dict[str, Any] = {"is_git_repo": (root / ".git").exists(), "github_repo": ""}
    if not info["is_git_repo"]:
        return info
    try:
        url = subprocess.run(
            ["git", "-C", str(root), "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return info
    match = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", url)
    if match:
        info["github_repo"] = f"{match.group(1)}/{match.group(2)}"
    return info


def empty_knowledge_plane(sources: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """Return a draft `knowledge_plane` section: learned sources filled, intent left empty."""
    return {
        "root": KP_ROOT,
        "sources": {kind: list((sources or {}).get(kind, [])) for kind in SOURCE_KINDS},
        "requirements": [],
        "invariants": [],
        "decisions": [],
        "glossary": [],
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and len(value.strip()) < 2) or (
        isinstance(value, (list, dict)) and not value
    )


def validate_knowledge_plane(spec: dict[str, Any]) -> list[str]:
    """Return readiness problems for the `knowledge_plane` section and REQ citations in waves."""
    problems: list[str] = []
    kp = spec.get("knowledge_plane") or {}

    requirements = kp.get("requirements") or []
    if not requirements:
        problems.append("knowledge_plane.requirements must list at least 1 requirement with a stable REQ- ID")
    known_ids: set[str] = set()
    for idx, req in enumerate(requirements):
        rid = str(req.get("id", ""))
        if not _REQ_ID.fullmatch(rid):
            problems.append(f"knowledge_plane.requirements[{idx}].id must look like REQ-001")
        elif rid in known_ids:
            problems.append(f"knowledge_plane.requirements has duplicate id {rid}")
        known_ids.add(rid)
        if _blank(req.get("title")):
            problems.append(f"knowledge_plane.requirements[{idx}] ({rid or '?'}) is missing a title")
        if _blank(req.get("acceptance")):
            problems.append(f"knowledge_plane.requirements[{idx}] ({rid or '?'}) is missing acceptance criteria")

    if not [i for i in (kp.get("invariants") or []) if not _blank(i)]:
        problems.append("knowledge_plane.invariants must list at least 1 testable MUST rule (reviewer checklist)")

    for idx, dec in enumerate(kp.get("decisions") or []):
        if not re.fullmatch(r"ADR-\d{3,}", str(dec.get("id", ""))) or _blank(dec.get("title")) or _blank(dec.get("decision")):
            problems.append(f"knowledge_plane.decisions[{idx}] needs an ADR-NNN id, a title, and a decision")

    for w_idx, wave in enumerate(spec.get("waves") or []):
        for i_idx, issue in enumerate(wave.get("issues") or []):
            for rid in issue.get("requirements") or []:
                if rid not in known_ids:
                    problems.append(f"waves[{w_idx}].issues[{i_idx}] cites unknown requirement {rid}")
    return problems


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "decision"


def adr_filename(decision: dict[str, Any]) -> str:
    return f"{decision['id']}-{_slug(decision['title'])}.md"


def _issues_citing(spec: dict[str, Any], rid: str) -> list[str]:
    titles = []
    for wave in spec.get("waves") or []:
        for issue in wave.get("issues") or []:
            if rid in (issue.get("requirements") or []):
                titles.append(issue["title"])
    return titles


def render_knowledge_plane(spec: dict[str, Any], vcs_enabled: bool) -> dict[str, str]:
    """Return {path relative to the kit root: content} for every knowledge plane file."""
    proj = spec["project"]
    kp = spec.get("knowledge_plane") or empty_knowledge_plane()
    sources = kp.get("sources") or {}
    vcs = spec.get("vcs") or {}
    decisions = kp.get("decisions") or []
    files: dict[str, str] = {}
    # Learned sources are repo-root paths. A monorepo factory keeps its knowledge plane at
    # <subdir>/knowledge-plane, so each link climbs one extra level per subdir part.
    subdir = str((spec.get("monorepo") or {}).get("subdir") or "").strip().strip("/")
    up = "../" * len([part for part in subdir.split("/") if part])
    # A factory with nothing to deploy (infrastructure.deploy: none, spec §8) has no service and no deployer: the
    # doc-writer keeps state/LIVE.md current with the latest release and verified source.
    nothing_deployed = (spec.get("infrastructure") or {}).get("deploy") == "none"
    live_writer = ("The doc-writer keeps [state/LIVE.md](state/LIVE.md) current (nothing is deployed)."
                   if nothing_deployed else "Only the deployer writes [state/LIVE.md](state/LIVE.md).")

    backlog_row = (
        f"| Find the backlog | GitHub issues in `{vcs.get('repo', '')}` (`status:*` labels) |"
        if vcs_enabled
        else "| Find the backlog | [../backlog/issues.json](../backlog/issues.json) (the lead is the only writer) |"
    )

    learned_lines = []
    labels = {
        "requirements": "Requirements",
        "adrs": "Decisions (ADRs)",
        "contracts": "Contracts",
        "design": "Design",
        "guides": "Guides",
    }
    for kind in SOURCE_KINDS:
        for src in sources.get(kind) or []:
            learned_lines.append(f"- {labels[kind]}: [`{src}`](../{up}{src})")
    learned = "\n".join(learned_lines) or "- None. Every entry in this knowledge plane came from the bootstrap interview."

    adr_rows = "\n".join(
        f"| [{d['id']}](adr/{adr_filename(d)}) | {d['title']} | {d.get('status', 'Accepted')} | "
        f"{', '.join(d.get('requirements') or []) or '—'} |"
        for d in decisions
    ) or "| — | No decisions recorded yet. Copy [adr/_template.md](adr/_template.md) to add one. | — | — |"

    files[f"{KP_ROOT}/INDEX.md"] = f"""# {proj['name']} knowledge plane

Read this index before you plan, implement, review, or deploy any work item. Open files through the paths in this
index; never guess a file name (L4). Cite requirement IDs (`REQ-NNN`) and decision IDs (`ADR-NNN`) in work items,
commits, reviews, and evaluation results.

## Where to look

| When you need to | Read |
|---|---|
| Know what to build and how it's accepted | [product/requirements.md](product/requirements.md) |
| Know why the system is built this way | [Decisions](#decisions) |
| Check an API or data contract | [contracts/README.md](contracts/README.md) |
| Review a change | [invariants.md](invariants.md) |
| Understand a domain term | [glossary.md](glossary.md) |
| Follow a project-specific procedure | [playbooks/README.md](playbooks/README.md) |
| Know what is live right now | [state/LIVE.md](state/LIVE.md) |
{backlog_row}

## Learned sources

These files existed in the repository before the factory started. They stay where they are; this index links to them.

{learned}

## Decisions

| ADR | Title | Status | Requirements |
|---|---|---|---|
{adr_rows}

## Keep this knowledge plane healthy

- Run `python3 {KP_ROOT}/kp-check.py` after you change any file here. It fails on broken links and unknown
  `REQ-`/`ADR-` IDs.
- Add a decision with [adr/_template.md](adr/_template.md), and add a row to the table above.
- Don't put live state in this file. {live_writer}
- Don't put evaluation golden datasets here. They belong to the evaluation team.
"""

    req_sections = []
    for req in kp.get("requirements") or []:
        acceptance = "\n".join(f"- [ ] {a}" for a in req.get("acceptance") or [])
        cited = _issues_citing(spec, req["id"])
        cited_md = "\n".join(f"- {t}" for t in cited) or "- None yet."
        req_sections.append(
            f"## {req['id']}: {req['title']}\n\n{req.get('description', '').strip()}\n\n"
            f"### Acceptance criteria\n\n{acceptance}\n\n### Work items\n\n{cited_md}\n"
        )
    files[f"{KP_ROOT}/product/requirements.md"] = (
        f"# Requirements\n\nMission: {proj['mission']}\n\nNon-goals:\n\n"
        + "\n".join(f"- {ng}" for ng in proj.get("non_goals") or [])
        + "\n\nEach requirement has a stable ID. Never renumber an ID; mark it `Withdrawn` instead.\n\n"
        + "\n".join(req_sections)
    )

    files[f"{KP_ROOT}/adr/_template.md"] = """# ADR-NNN: Title

- **Status:** Proposed | Accepted | Superseded by ADR-NNN
- **Requirements:** REQ-NNN
- **Work items:** issue numbers or backlog IDs
- **Migrations:** paths, if any
- **Supersedes:** ADR-NNN, if any

## Context

What forces are at play, and what problem needs a decision.

## Decision

What we do, stated so a reviewer can verify it in code.

## Consequences

What becomes easier, what becomes harder, and what to watch.
"""

    for dec in decisions:
        files[f"{KP_ROOT}/adr/{adr_filename(dec)}"] = f"""# {dec['id']}: {dec['title']}

- **Status:** {dec.get('status', 'Accepted')}
- **Requirements:** {', '.join(dec.get('requirements') or []) or '—'}
- **Work items:** —
- **Migrations:** —

## Context

{dec.get('context', '').strip() or 'Recorded during the bootstrap interview.'}

## Decision

{dec['decision'].strip()}

## Consequences

{dec.get('consequences', '').strip() or 'To be documented as the decision is implemented.'}
"""

    contract_lines = "\n".join(f"- [`{c}`](../../{up}{c})" for c in sources.get("contracts") or [])
    l2 = spec.get("evaluation_pyramid", {}).get("l2_contract", {})
    endpoints = "\n".join(f"- `{e}`" for e in l2.get("endpoints") or []) or "- None recorded."
    l2_inv = "\n".join(f"- {i}" for i in l2.get("invariants") or []) or "- None recorded."
    files[f"{KP_ROOT}/contracts/README.md"] = f"""# Contracts

Contracts are the source of truth for the L2 contract checks that the evaluation team runs. Change a contract in the
same work item as the code that implements it.

## Contract files

{contract_lines or '- None yet. Add OpenAPI, protobuf, or GraphQL files to this directory.'}

## Endpoints under contract

{endpoints}

## Contract invariants

{l2_inv}
"""

    invariants = "\n".join(f"{n}. {inv}" for n, inv in enumerate(kp.get("invariants") or [], start=1))
    files[f"{KP_ROOT}/invariants.md"] = f"""# Invariants

Every change MUST keep these rules. The reviewer checks each one and cites the rule number in a FAIL verdict.

{invariants}
"""

    glossary_rows = "\n".join(f"| {g['term']} | {g['definition']} |" for g in kp.get("glossary") or [])
    files[f"{KP_ROOT}/glossary.md"] = (
        "# Glossary\n\n| Term | Definition |\n|---|---|\n"
        + (glossary_rows or "| — | Add domain terms as agents meet them. |")
        + "\n"
    )

    files[f"{KP_ROOT}/playbooks/README.md"] = """# Playbooks

Put procedures specific to this project here, such as database migration rules or data backfills. One file per
procedure, named after the task (for example, `DATABASE-MIGRATIONS.md`).

Don't copy the factory protocol here. How agents coordinate lives in `templates/_shared/agents.md`.
"""

    snapshot_row = "| Source commit | — |" if vcs_enabled else "| Source snapshot (SHA-256) | — |"
    if nothing_deployed:
        files[f"{KP_ROOT}/state/LIVE.md"] = f"""# Live state

Nothing is deployed: this factory has `infrastructure.deploy: none` (a library, a CLI or a package). The doc-writer
keeps this file current after every release; other agents read it.

| Field | Value |
|---|---|
| Service | none (nothing to deploy) |
| Latest release | — |
{snapshot_row}
| Released at (UTC) | — |
| Updated by | doc-writer |

## History

| Released at (UTC) | Release | Source | Work items |
|---|---|---|---|
"""
    else:
        files[f"{KP_ROOT}/state/LIVE.md"] = f"""# Live state

Only the deployer writes this file, after every deploy and every rollback. Other agents read it.

| Field | Value |
|---|---|
| Service | `{spec['infrastructure']['serving_service']}` |
| Live revision | — |
| Rollback revision | — |
{snapshot_row}
| Applied migrations | — |
| Deployed at (UTC) | — |
| Deployed by | deployer |

## History

| Deployed at (UTC) | Revision | Source | Work items |
|---|---|---|---|
"""

    files[f"{KP_ROOT}/kp-check.py"] = KP_CHECK_SCRIPT
    return files


def render_eval_golden_readme(spec: dict[str, Any]) -> str:
    l3 = spec["evaluation_pyramid"]["l3_domain_quality"]
    proj = spec.get("project") or {}
    eval_slug = proj.get("eval_slug") or proj.get("slug", "")
    return f"""# Golden data (evaluation team only)

This directory belongs to the evaluation project (`{eval_slug}`). Copy it into the evaluation
workspace, never into the build workspace or the build knowledge plane, so the build team can't tune the product to
the test set.

- **Metric:** `{l3['primary_metric']}` with a target of `{l3['target_threshold']}` or higher.
- **Probe:** {l3.get('probe_description', '')}

Store each dataset as a versioned file (for example, `golden-v1.jsonl`), and record the version in every evaluation
run. The evaluation team reads the build team's knowledge plane read-only at `{EVAL_KP_MOUNT}`.
"""


KP_CHECK_SCRIPT = r'''#!/usr/bin/env python3
"""Check a Scion factory knowledge plane for broken links and unknown REQ-/ADR- IDs.

Usage: python3 knowledge-plane/kp-check.py [KNOWLEDGE_PLANE_DIR]
Exit code 0 when every reference resolves, 1 otherwise.
"""
import re
import sys
from pathlib import Path

LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
REQ = re.compile(r"\bREQ-\d{3,}\b")
ADR = re.compile(r"\bADR-\d{3,}\b")


def main() -> int:
    kp = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent
    if not kp.is_dir():
        print(f"knowledge plane not found: {kp}")
        return 1

    docs = sorted(kp.rglob("*.md"))
    reqs_file = kp / "product" / "requirements.md"
    defined_reqs = set(re.findall(r"^## (REQ-\d{3,})", reqs_file.read_text(), re.M)) if reqs_file.exists() else set()
    defined_adrs = {m.group(0) for p in (kp / "adr").glob("ADR-*.md") for m in [ADR.match(p.name)] if m}

    problems = []
    for doc in docs:
        if doc.name == "_template.md":
            continue
        text = doc.read_text(errors="ignore")
        rel = doc.relative_to(kp)
        for target in LINK.findall(text):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            path = target.split("#", 1)[0]
            if path and not (doc.parent / path).exists():
                problems.append(f"{rel}: broken link -> {target}")
        for rid in sorted(set(REQ.findall(text)) - defined_reqs):
            problems.append(f"{rel}: unknown requirement {rid}")
        for aid in sorted(set(ADR.findall(text)) - defined_adrs):
            problems.append(f"{rel}: unknown decision {aid}")

    for line in problems:
        print(line)
    print(f"kp-check: {len(docs)} files, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
'''
