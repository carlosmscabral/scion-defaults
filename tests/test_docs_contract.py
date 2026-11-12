import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_skill_and_readme_document_the_team_step():
    skill = (ROOT / "skills/scion-factory-bootstrap/SKILL.md").read_text()
    for needle in ("add-role", "proposed_roles", "dropped_defaults", "team.autonomy", "infrastructure.deploy"):
        assert needle in skill
    readme = (ROOT / "README.md").read_text()
    assert "autonomy" in readme and "deploy: none" in readme
    assert subprocess.run([sys.executable, "scripts/scion_factory.py", "add-role", "--help"], cwd=ROOT).returncode == 0


def test_grill_question_team_roster_describes_specialists_and_deploy():
    sys.path.insert(0, str(ROOT / "scripts"))
    import scion_factory

    question = next(q for q in scion_factory.GRILL_QUESTIONS if q["id"] == "team_roster_and_harnesses")
    prompt = question["challenge_prompt"]
    q_text = question["question"]

    # Must mention the 3 default specialists, dropped_defaults/reason, and deployer conditional on deploy: service
    for needle in ("researcher", "doc-writer", "investigator"):
        assert needle in prompt or needle in q_text
    assert "deploy" in prompt or "deploy" in q_text
    assert "add-role" in prompt or "add-role" in q_text or "proposed" in prompt or "proposed" in q_text
    for needle in ("dropped_defaults", "reason"):
        assert needle in prompt or needle in q_text


def test_grill_me_playbook_covers_deploy_and_team_topics():
    playbook = (ROOT / "docs/grill-me-playbook.md").read_text()
    assert "deploy_target" in playbook or "infrastructure.deploy" in playbook
    assert "autonomy" in playbook


def test_templates_readme_references_role_library():
    templates_readme = (ROOT / "templates/README.md").read_text()
    assert "role_library.py" in templates_readme


def test_setup_runbook_documents_deploy_none_sandbox_skips():
    runbook = (ROOT / "docs/setup-runbook.md").read_text()
    assert "deploy: none" in runbook or "infrastructure.deploy: none" in runbook
    assert "DEPLOYER_SA_NAME" in runbook


def test_readme_includes_copy_paste_starting_prompts():
    readme = (ROOT / "README.md").read_text()
    assert "## Starting prompts" in readme
    for needle in (
        "scion-factory-bootstrap",
        "existing Scion Hub",
        "new Scion Hub",
        "monorepo",
        "factory/main",
        "factory/eval",
    ):
        assert needle in readme, f"missing {needle!r} in README starting prompts"


def test_docs_cover_github_workflow_scope_and_lesson_l31():
    readme = (ROOT / "README.md").read_text()
    runbook = (ROOT / "docs/setup-runbook.md").read_text()
    lessons = (ROOT / "docs/lessons-learned.md").read_text()

    assert "workflow" in readme and ".github/workflows" in readme
    assert "workflow" in runbook and ".github/workflows" in runbook
    assert "## L31." in lessons
    for needle in (
        "eval-sweep",
        "pytest.importorskip",
        "migration-specialist",
        "/scion-volumes/scratchpad/pending-",
        "status:po-decision",
    ):
        assert needle in lessons, f"missing {needle!r} in docs/lessons-learned.md"



def test_docs_and_skill_cover_gke_runtime_and_auto_provision():
    skill = (ROOT / "skills/scion-factory-bootstrap/SKILL.md").read_text()
    for needle in ("setup-gke-hub.sh", "provision", "infrastructure.runtime", "kubernetes"):
        assert needle in skill, f"missing {needle!r} in SKILL.md"

    gke_runbook = (ROOT / "docs/gke-setup-runbook.md").read_text()
    for needle in (
        "Workload Identity",
        "scion-worker",
        "scion-deployer",
        "install-k8s-control-plane.py",
        "/health",
        "setup-gke-hub.sh",
        "HUB_IMPERSONATE_SA",
        "per-agent",
    ):
        assert needle in gke_runbook, f"missing {needle!r} in docs/gke-setup-runbook.md"

    readme = (ROOT / "README.md").read_text()
    assert "GKE" in readme and "scion-factory provision" in readme and "L33" in readme

    lessons = (ROOT / "docs/lessons-learned.md").read_text()
    assert "## L32." in lessons and "## L33." in lessons


def _strip_fenced_code_blocks(markdown: str) -> str:
    lines = []
    in_fence = False
    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            lines.append(line)
    return "\n".join(lines)


def _github_heading_slug(heading_text: str) -> str:
    cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", heading_text.strip())
    cleaned = re.sub(r"`([^`]*)`", r"\1", cleaned).lower()
    cleaned = re.sub(r"[^\w\s-]", "", cleaned)
    cleaned = re.sub(r" ", "-", cleaned)
    return cleaned


def _markdown_headings(markdown: str) -> set[str]:
    stripped = _strip_fenced_code_blocks(markdown)
    return {
        _github_heading_slug(m.group(1))
        for m in re.finditer(r"^#{1,6}\s+(.+)$", stripped, flags=re.MULTILINE)
    }


def test_readme_follows_google_dev_style_and_ste100_without_content_loss():
    readme = (ROOT / "README.md").read_text()
    prose = _strip_fenced_code_blocks(readme)

    # 1. Google Developer Style + ASD-STE100: no first-person plural or contractions in prose
    assert not re.search(r"\b(we|our|ourselves)\b", prose, flags=re.IGNORECASE), (
        "README prose must use second-person ('you'), not first-person plural"
    )
    normalized_apostrophes = prose.replace("’", "'")
    contractions = re.findall(
        r"\b(?:\w+'(?:t|re|ve|ll|d|m)|(?:it|that|what|there|here|who|let)'s)\b",
        normalized_apostrophes,
        flags=NG_FLAGS if (NG_FLAGS := re.IGNORECASE) else 0,
    )
    assert not contractions, f"ASD-STE100 forbids contractions in prose; found: {sorted(set(contractions))}"

    # 2. ASD-STE100 sentence length: descriptive/procedural prose & callout sentences stay <= 25 words
    # (unwrapping [label](url) so visible link text words are counted, while inline `code` counts as a single token)
    paragraphs = []
    buf = []
    for raw_line in prose.splitlines():
        s = raw_line.strip()
        if s.startswith(">"):
            s = re.sub(r"^>\s*", "", s)
            if re.match(r"^\[![A-Z]+\]$", s):
                if buf:
                    paragraphs.append(" ".join(buf))
                    buf = []
                continue
        if not s or s.startswith(("#", "|", "├──", "└──", "│")):
            if buf:
                paragraphs.append(" ".join(buf))
                buf = []
            continue
        if re.match(r"^(?:[-*]|\d+\.)\s+", s):
            if buf:
                paragraphs.append(" ".join(buf))
            buf = [re.sub(r"^(?:[-*]|\d+\.)\s+", "", s)]
        else:
            buf.append(s)
    if buf:
        paragraphs.append(" ".join(buf))

    overlong = []
    for para in paragraphs:
        normalized = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", para)
        normalized = re.sub(r"`[^`]+`", "CODE_TOKEN", normalized)
        for sent in re.split(r"(?<=[.!?])\s+", normalized):
            words = re.findall(r"[A-Za-z0-9_'-]+", sent)
            if len(words) > 25:
                overlong.append((len(words), sent))
    assert not overlong, f"ASD-STE100 sentence length (>25 words) exceeded: {overlong[:5]}"

    # 3. Zero content loss + schema/CLI accuracy
    required_items = (
        "docs/gke-setup-runbook.md",
        "docs/setup-runbook.md",
        "docs/operations-runbook.md",
        "docs/grill-me-playbook.md",
        "docs/lessons-learned.md",
        "docs/verification-and-eval.md",
        "docs/telemetry/README.md",
        "skills/scion-factory-bootstrap/SKILL.md",
        "scripts/hub_client.py",
        "scripts/provision_factory.py",
        "scripts/setup-gke-hub.sh",
        "scripts/install-k8s-control-plane.py",
        "starter-service.factory.yaml",
        "psearch.factory.yaml",
        "acme-airlines.factory.yaml",
        "knowledge-plane/INDEX.md",
        "kp-check.py",
        "state/LIVE.md",
        "backlog/issues.json",
        "vcs.enabled",
        "FACTORY_SUBDIR",
        "--via-hub",
        "enable-git",
        "add-role",
        "L32",
        "## Next steps",
    )
    for item in required_items:
        assert item in readme, f"README lost or omitted required reference: {item!r}"

    # 4. Link and anchor integrity: every local path and #anchor (internal and cross-doc) resolves
    readme_headings = _markdown_headings(readme)
    for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", prose):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        if target.startswith("#"):
            anchor = target[1:]
            assert anchor in readme_headings, (
                f"Broken internal anchor #{anchor} in README.md (available: {sorted(readme_headings)})"
            )
        else:
            rel_path, _, anchor = target.partition("#")
            target_file = ROOT / rel_path
            assert target_file.exists(), f"Broken relative path {rel_path!r} in README.md"
            if anchor and target_file.suffix.lower() == ".md":
                doc_headings = _markdown_headings(target_file.read_text())
                assert anchor in doc_headings, (
                    f"Broken cross-doc anchor {target!r} in README.md (available in {rel_path}: {sorted(doc_headings)})"
                )


def test_docs_cover_gke_hillclimb_delta_orchestration_and_l34():
    report_path = ROOT / "docs/gke-hillclimb-report.md"
    assert report_path.exists(), "docs/gke-hillclimb-report.md must exist"
    report = report_path.read_text()
    for needle in (
        "gke-up",
        "gke-down",
        "factory_delta.py",
        "scion-gke-hc1-",
        "scion-gke-hc2-",
        "scion-gke-hc3-",
        "status:po-decision",
        "kubectl delete pod",
    ):
        assert needle in report, f"missing {needle!r} in docs/gke-hillclimb-report.md"

    lessons = (ROOT / "docs/lessons-learned.md").read_text()
    assert "## L34." in lessons
    for needle in ("factory_delta.py", "gke-up", "telemetry.cloud", "10-direct-iap-token"):
        assert needle in lessons, f"missing {needle!r} in docs/lessons-learned.md"

    gke_runbook = (ROOT / "docs/gke-setup-runbook.md").read_text()
    for needle in ("gke-up", "gke-down", "factory_delta.py", "SCION_REF", "gke-hillclimb-report.md"):
        assert needle in gke_runbook, f"missing {needle!r} in docs/gke-setup-runbook.md"

    readme = (ROOT / "README.md").read_text()
    for needle in (
        "gke-up",
        "gke-down",
        "factory_delta.py",
        "bananacast-gke.factory.yaml",
        "docs/gke-hillclimb-report.md",
        "L34",
    ):
        assert needle in readme, f"missing {needle!r} in README.md"

    agents_md = (ROOT / "AGENTS.md").read_text()
    for needle in ("gke-hillclimb-report.md", "bananacast-gke.factory.yaml", "factory_delta.py", "34 lessons learned"):
        assert needle in agents_md, f"missing {needle!r} in AGENTS.md"

    leak_re = re.compile(
        r"scion-gke-hc[123]-(?!a1b2c3|fail00)[0-9a-f]{6}\b|altostrat\.com|\.a\.run\.app",
        flags=re.IGNORECASE,
    )
    for rel in (
        "docs/gke-hillclimb-report.md",
        "docs/lessons-learned.md",
        "docs/gke-setup-runbook.md",
        "README.md",
        "AGENTS.md",
    ):
        text = (ROOT / rel).read_text()
        leaks = leak_re.findall(text)
        assert not leaks, f"found unredacted real identifier in {rel}: {leaks}"


def test_public_repo_skills_cover_gcp_gke_reproducibility_without_corp_leaks():
    corp_re = re.compile(r"\b(cloudtop|jetski|agcis|google3|citc|piper|moma|buganizer|altostrat)\b", flags=re.IGNORECASE)
    for rel in (
        "skills/scion-factory-bootstrap/SKILL.md",
        "skills/clean-room-reproducibility/SKILL.md",
        "skills/gke-hillclimb-verification/SKILL.md",
    ):
        path = ROOT / rel
        assert path.is_file(), f"missing public repo skill {rel}"
        text = path.read_text()
        assert text.startswith("---\nname: "), f"{rel} missing YAML frontmatter"
        assert "\ndescription:" in text, f"{rel} missing description in frontmatter"
        corp_hits = corp_re.findall(text)
        assert not corp_hits, f"public skill {rel} contains Google-corp internal terms: {corp_hits}"
        assert "\u2014" not in text, f"em-dash found in {rel}"



