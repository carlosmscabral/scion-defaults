#!/usr/bin/env python3
"""Automated full-lifecycle hillclimb wave runner for `duboc/bananacast` on GKE.

Executes a complete single-project ephemeral delta factory wave end-to-end:
  1. Snapshots `duboc/bananacast` `main` and `factory/main` commit SHAs.
  2. Creates remote branch `factory/gke-hc<wave>` from `main` and seeds 3 multi-app wave issues.
  3. Provisions an ephemeral `scion-gke-hc<wave>-<6hex>` GKE factory via `gke_lifecycle.gke_up`.
  4. Polls wave issues to `status:verified` (plus optional specialist pod eviction and
     `status:po-decision` human-reply wake drills when `--drills` is enabled).
  5. Runs an `idle_soak_sec` post-wave soak confirming `0` running agent pods and `0`
     additional `lead` steps/tokens after `IDLE_STOP_GRACE_SEC`.
  6. Tears down via `gke_lifecycle.gke_down` (archiving transcripts/logs + `scorecard.json`,
     closing wave issues, deleting `factory/gke-hc<wave>`, and deleting the ephemeral GCP
     project) inside a `try ... finally` guard and asserts `main` and `factory/main` SHAs
     never changed.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any
import urllib.error
import urllib.parse

os.environ.setdefault("CLOUDSDK_CORE_DISABLE_PROMPTS", "1")

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import gke_lifecycle  # noqa: E402
from hub_client import HubClient  # noqa: E402
import provision_factory  # noqa: E402
import scion_factory  # noqa: E402


WAVE_CATALOG: dict[int, list[dict[str, Any]]] = {
    1: [
        {
            "title": "Wave 1.1: Fix is_admin_request NameError and enforce @require_admin on performance routes",
            "owner_role": "backend",
            "requirements": ["REQ-001"],
            "paths": [
                "src/backend/utils/admin_auth.py",
                "src/backend/routes/performances.py",
                "src/backend/tests/test_admin_auth_performances.py",
            ],
            "description": (
                "In `src/backend/utils/admin_auth.py`, `is_admin_request()` references `config.ADMIN_PASSWORD` "
                "and `hmac.compare_digest` without importing `config` or `hmac`, causing a `NameError` when "
                "`GET /api/performances/<id>` checks admin status in `src/backend/routes/performances.py`. "
                "Fix the imports, accept bearer tokens from `X-App-Auth` or `Authorization`, and enforce "
                "`@require_admin` returning HTTP 401 JSON when `config.ADMIN_PASSWORD` is configured."
            ),
            "acceptance_criteria": [
                "Import `hmac` and `config` in `src/backend/utils/admin_auth.py` so `is_admin_request()` never raises `NameError`.",
                "Update `require_admin` in `src/backend/utils/admin_auth.py` to enforce bearer (`X-App-Auth` / `Authorization`) or `X-Goog-Authenticated-User-Email` checks when `config.ADMIN_PASSWORD` is non-empty.",
                "Add regression tests in `src/backend/tests/test_admin_auth_performances.py` covering `GET /api/performances/<id>` user vs admin serialization and `@require_admin` 401/200 responses, passing `cd src/backend && pytest -q`.",
            ],
        },
        {
            "title": "Wave 1.2: Disable /openapi.json and guard DIST_DIR path traversal in src/frontend/proxy.py",
            "owner_role": "frontend",
            "requirements": ["REQ-002"],
            "paths": [
                "src/frontend/proxy.py",
                "src/backend/tests/test_frontend_proxy.py",
            ],
            "description": (
                "Harden `src/frontend/proxy.py` so `FastAPI(docs_url=None, redoc_url=None, openapi_url=None)` "
                "hides `/openapi.json`, and canonicalize static candidate paths in `proxy_assets` and `serve_spa` "
                "with `.resolve().is_relative_to(DIST_DIR.resolve())` so URL-encoded traversal (`/..%2f`) never "
                "serves files outside `DIST_DIR`."
            ),
            "acceptance_criteria": [
                "Configure `FastAPI(docs_url=None, redoc_url=None, openapi_url=None)` in `src/frontend/proxy.py` so `GET /openapi.json` is not exposed.",
                "Canonicalize candidate static paths with `.resolve().is_relative_to(DIST_DIR.resolve())` in `proxy_assets` and `serve_spa` to block `/..%2f` path traversal.",
                "Add unit tests in `src/backend/tests/test_frontend_proxy.py` guarding optional imports with `pytest.importorskip(\"fastapi\")` and passing `cd src/backend && pytest -q`.",
            ],
        },
        {
            "title": "Wave 1.3: Enforce @require_admin on campaign management routes and pin GET /api/config/<slug> whitelist",
            "owner_role": "backend",
            "requirements": ["REQ-003"],
            "paths": [
                "src/backend/routes/campaigns.py",
                "src/backend/tests/test_get_config_route.py",
            ],
            "description": (
                "Decorate management campaign endpoints (`list_campaigns`, `create_campaign`, `update_campaign`) "
                "in `src/backend/routes/campaigns.py` with `@require_admin`, and expand "
                "`src/backend/tests/test_get_config_route.py` to pin public field whitelisting on "
                "`GET /api/config/<slug>` so internal `imagePrompt`, `safetyConfig`, and `couponCode` secrets "
                "are never leaked."
            ),
            "acceptance_criteria": [
                "Apply `@require_admin` to admin campaign endpoints in `src/backend/routes/campaigns.py`.",
                "Extend `src/backend/tests/test_get_config_route.py` to assert `GET /api/config/<slug>` strips `imagePrompt`, `safetyConfig`, and raw `couponCode` while `/api/campaigns` enforces `@require_admin`.",
                "Verify all tests pass via `cd src/backend && pytest -q`.",
            ],
        },
    ],
    2: [
        {
            "title": "Wave 2.1: Fix admin_auth.py imports and protect GET /api/safety_blocks with @require_admin",
            "owner_role": "backend",
            "requirements": ["REQ-001"],
            "paths": [
                "src/backend/utils/admin_auth.py",
                "src/backend/routes/safety.py",
                "src/backend/tests/test_safety_admin_auth.py",
            ],
            "description": (
                "Fix missing `hmac` and `config` imports in `src/backend/utils/admin_auth.py`, enforce "
                "`@require_admin` on `list_safety_blocks` in `src/backend/routes/safety.py`, clamp the `limit` "
                "query parameter to `[1, 500]`, and add unit coverage in `src/backend/tests/test_safety_admin_auth.py`."
            ),
            "acceptance_criteria": [
                "Fix `is_admin_request()` and `require_admin` in `src/backend/utils/admin_auth.py` with constant-time `hmac.compare_digest` checks.",
                "Decorate `list_safety_blocks` in `src/backend/routes/safety.py` with `@require_admin` and clamp `limit` between `1` and `500`.",
                "Add tests in `src/backend/tests/test_safety_admin_auth.py` verified via `cd src/backend && pytest -q`.",
            ],
        },
        {
            "title": "Wave 2.2: Harden src/frontend/proxy.py OpenAPI/path canonicalization and brandingValidation.ts",
            "owner_role": "frontend",
            "requirements": ["REQ-002"],
            "paths": [
                "src/frontend/proxy.py",
                "src/frontend/utils/brandingValidation.ts",
                "src/backend/tests/test_proxy_security.py",
            ],
            "description": (
                "Set `FastAPI(docs_url=None, redoc_url=None, openapi_url=None)`, enforce "
                "`.resolve().is_relative_to(DIST_DIR.resolve())` static path containment and strip spoofed inbound "
                "`X-App-Auth` headers when `Authorization` is absent in `src/frontend/proxy.py`, and reject "
                "percent-encoded `..` segments in `src/frontend/utils/brandingValidation.ts::safeAssetPath`."
            ),
            "acceptance_criteria": [
                "Disable `/openapi.json`, block `/..%2f` traversal via `.resolve().is_relative_to(DIST_DIR.resolve())`, and strip spoofed `X-App-Auth` in `src/frontend/proxy.py`.",
                "Reject `..` and `%2e%2e` traversal segments in `safeAssetPath` in `src/frontend/utils/brandingValidation.ts`.",
                "Add tests in `src/backend/tests/test_proxy_security.py` using `pytest.importorskip(\"fastapi\")` and pass `cd src/backend && pytest -q`.",
            ],
        },
        {
            "title": "Wave 2.3: Remove query-string mobile token fallback and verify GET /api/config/<slug> secret stripping",
            "owner_role": "backend",
            "requirements": ["REQ-003"],
            "paths": [
                "src/backend/routes/mobile.py",
                "src/backend/tests/test_mobile.py",
                "src/backend/tests/test_get_config_route.py",
            ],
            "description": (
                "Remove the deprecated `?token=` query-string fallback in `src/backend/routes/mobile.py::mobile_session_info` "
                "so mobile upload tokens are accepted exclusively via `X-App-Auth` / `Authorization` bearer headers, "
                "and add regression assertions across `test_mobile.py` and `test_get_config_route.py`."
            ),
            "acceptance_criteria": [
                "Reject query-string `?token=` in `mobile_session_info` in `src/backend/routes/mobile.py`, requiring `X-App-Auth` or `Authorization` bearer headers.",
                "Update `src/backend/tests/test_mobile.py` and `src/backend/tests/test_get_config_route.py` to verify zero token, `imagePrompt`, or coupon secret exposure.",
                "Pass `cd src/backend && pytest -q`.",
            ],
        },
    ],
    3: [
        {
            "title": "Wave 3.1: Harden admin_auth.py X-Forwarded-For logging, imports, and performances.py @require_admin gates",
            "owner_role": "backend",
            "requirements": ["REQ-001"],
            "paths": [
                "src/backend/utils/admin_auth.py",
                "src/backend/routes/performances.py",
                "src/backend/tests/test_performances_admin_gate.py",
            ],
            "description": (
                "Import `hmac` and `config` in `src/backend/utils/admin_auth.py`, enforce `@require_admin` when "
                "`config.ADMIN_PASSWORD` is set, log only the sanitized first hop of `X-Forwarded-For` instead of "
                "Cloud Run's edge `request.remote_addr`, and add `GET /api/performances/<id>` serializer tier tests."
            ),
            "acceptance_criteria": [
                "Fix `is_admin_request()` imports, enforce `@require_admin`, and replace raw `request.remote_addr` audit metadata in `src/backend/utils/admin_auth.py`.",
                "Verify `GET /api/performances/<id>` in `src/backend/routes/performances.py` returns `serialize_performance_user` to anonymous callers and `serialize_performance_admin` to authenticated admins.",
                "Add `src/backend/tests/test_performances_admin_gate.py` passing `cd src/backend && pytest -q`.",
            ],
        },
        {
            "title": "Wave 3.2: Lock down src/frontend/proxy.py OpenAPI/static canonicalization and urlValidation.ts",
            "owner_role": "frontend",
            "requirements": ["REQ-002"],
            "paths": [
                "src/frontend/proxy.py",
                "src/frontend/utils/urlValidation.ts",
                "src/backend/tests/test_frontend_proxy_canonicalization.py",
            ],
            "description": (
                "Configure `FastAPI(docs_url=None, redoc_url=None, openapi_url=None)` and `.resolve().is_relative_to(DIST_DIR.resolve())` "
                "containment in `src/frontend/proxy.py`, reject URLs carrying embedded `username`/`password` credentials "
                "in `src/frontend/utils/urlValidation.ts::isSafeUrl`, and add guarded proxy canonicalization tests."
            ),
            "acceptance_criteria": [
                "Hide `/openapi.json` via `openapi_url=None` and block `/..%2f` traversal via `.resolve().is_relative_to(DIST_DIR.resolve())` in `src/frontend/proxy.py`.",
                "Reject URLs with non-empty `parsed.username` or `parsed.password` in `src/frontend/utils/urlValidation.ts::isSafeUrl`.",
                "Add `src/backend/tests/test_frontend_proxy_canonicalization.py` using `pytest.importorskip(\"fastapi\")` and pass `cd src/backend && pytest -q`.",
            ],
        },
        {
            "title": "Wave 3.3: Protect campaign mutation routes and expand GET /api/config/<slug> and config guard tests",
            "owner_role": "backend",
            "requirements": ["REQ-003"],
            "paths": [
                "src/backend/routes/campaigns.py",
                "src/backend/tests/test_get_config_route.py",
                "src/backend/tests/test_config.py",
            ],
            "description": (
                "Enforce `@require_admin` on campaign management routes in `src/backend/routes/campaigns.py`, "
                "extend `src/backend/tests/test_get_config_route.py` for nested scene/coupon/safety whitelist "
                "invariants on `GET /api/config/<slug>`, and verify production `SECRET_KEY` / `FLASK_DEBUG` "
                "startup guards in `src/backend/tests/test_config.py`."
            ),
            "acceptance_criteria": [
                "Decorate admin routes in `src/backend/routes/campaigns.py` with `@require_admin`.",
                "Assert zero `imagePrompt`, `safetyConfig`, or `couponCode` leakage in `src/backend/tests/test_get_config_route.py` and verify production config startup guards in `src/backend/tests/test_config.py`.",
                "Pass `cd src/backend && pytest -q`.",
            ],
        },
    ],
}

_STALE_PROJECT_ENV_KEYS = (
    "SANDBOX_PROJECT",
    "FIRESTORE_PROJECT",
    "DATA_BUCKET",
    "STATE_BUCKET",
    "IMAGE_REGISTRY",
    "HUB_IMPERSONATE_SA",
)


def _monotonic() -> float:
    return time.monotonic()


def _sleep(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


def _api_base(eff_env: dict[str, str]) -> str:
    return (eff_env.get("GITHUB_API_URL") or os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")


def _extract_issue_labels(issue: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for lbl in issue.get("labels") or []:
        name = str(lbl.get("name") if isinstance(lbl, dict) else lbl).strip()
        if name:
            names.add(name)
    return names


def _get_branch_sha(
    repo: str,
    branch: str,
    *,
    github_token: str,
    api_base: str,
    allow_missing: bool = False,
) -> str:
    quoted = urllib.parse.quote(branch.strip(), safe="/")
    url = f"{api_base}/repos/{repo}/git/ref/heads/{quoted}"
    try:
        _, payload = gke_lifecycle._github_request("GET", url, github_token=github_token)
    except urllib.error.HTTPError as err:
        if allow_missing and err.code == 404:
            return ""
        raise RuntimeError(f"GitHub branch ref lookup failed for {repo}@{branch}: HTTP {err.code}") from None
    obj = payload.get("object") if isinstance(payload, dict) else None
    sha = str((obj or {}).get("sha") or "").strip()
    if not sha and not allow_missing:
        raise RuntimeError(f"GitHub branch ref {repo}@{branch} returned empty SHA")
    return sha


def _ensure_remote_branch(
    repo: str,
    branch: str,
    base_sha: str,
    *,
    github_token: str,
    api_base: str,
) -> None:
    clean = branch.strip().removeprefix("refs/heads/")
    if clean in gke_lifecycle.PROTECTED_BRANCHES:
        raise ValueError(f"Refusing to create or overwrite protected branch {clean!r}")
    create_url = f"{api_base}/repos/{repo}/git/refs"
    try:
        gke_lifecycle._github_request(
            "POST",
            create_url,
            github_token=github_token,
            body={"ref": f"refs/heads/{clean}", "sha": base_sha},
        )
        return
    except urllib.error.HTTPError as err:
        if err.code != 422:
            raise RuntimeError(f"Failed creating remote branch {clean}: HTTP {err.code}") from None

    quoted = urllib.parse.quote(clean, safe="/")
    gke_lifecycle._github_request(
        "PATCH",
        f"{api_base}/repos/{repo}/git/refs/heads/{quoted}",
        github_token=github_token,
        body={"sha": base_sha, "force": True},
    )


def _ensure_labels(
    repo: str,
    labels: list[tuple[str, str]],
    *,
    github_token: str,
    api_base: str,
) -> None:
    for name, color in labels:
        try:
            gke_lifecycle._github_request(
                "POST",
                f"{api_base}/repos/{repo}/labels",
                github_token=github_token,
                body={"name": name, "color": color},
            )
        except urllib.error.HTTPError as err:
            if err.code not in (409, 422):
                raise RuntimeError(f"Failed ensuring GitHub label {name!r}: HTTP {err.code}") from None


def _format_task_issue_body(wave_num: int, task: dict[str, Any]) -> str:
    reqs = ", ".join(task.get("requirements") or ["REQ-001"])
    paths_md = "\n".join(f"- `{p}`" for p in (task.get("paths") or []))
    ac_md = "\n".join(f"- [ ] {item}" for item in (task.get("acceptance_criteria") or []))
    return (
        f"## Hillclimb Wave {wave_num} Slice\n\n"
        f"**Owner role:** `{task['owner_role']}`\n"
        f"**Requirements:** {reqs}\n\n"
        f"### Context\n{task.get('description', '').strip()}\n\n"
        f"### Target Paths\n{paths_md}\n\n"
        f"### Acceptance Criteria\n{ac_md}\n"
    )


def _seed_digest_issue(
    repo: str,
    project_slug: str,
    issue_label: str,
    *,
    github_token: str,
    api_base: str,
) -> int:
    digest_title = f"{project_slug} digest"
    existing = _list_open_wave_issues(repo, issue_label, github_token=github_token, api_base=api_base)
    for item in existing:
        if str(item.get("title") or "").strip() == digest_title and "digest" in _extract_issue_labels(item):
            return int(item.get("number") or 0)
    _, created = gke_lifecycle._github_request(
        "POST",
        f"{api_base}/repos/{repo}/issues",
        github_token=github_token,
        body={
            "title": digest_title,
            "body": f"<!-- scion-agent: seed -->\nDaily digest for `{project_slug}`.\n",
            "labels": ["digest", issue_label],
        },
    )
    return int((created or {}).get("number") or 0)


def _seed_wave_issues(
    repo: str,
    wave_num: int,
    issue_label: str,
    tasks: list[dict[str, Any]],
    *,
    github_token: str,
    api_base: str,
) -> list[int]:
    seeded: list[int] = []
    for task in tasks:
        _, created = gke_lifecycle._github_request(
            "POST",
            f"{api_base}/repos/{repo}/issues",
            github_token=github_token,
            body={
                "title": task["title"],
                "body": _format_task_issue_body(wave_num, task),
                "labels": [issue_label, "status:ready"],
            },
        )
        num = int((created or {}).get("number") or 0)
        if not num:
            raise RuntimeError(f"GitHub issue creation did not return an issue number for {task['title']!r}")
        seeded.append(num)
    return seeded


def _list_open_wave_issues(
    repo: str,
    issue_label: str,
    *,
    github_token: str,
    api_base: str,
) -> list[dict[str, Any]]:
    qs = urllib.parse.urlencode({"state": "open", "labels": issue_label, "per_page": "100"})
    _, items = gke_lifecycle._github_request(
        "GET",
        f"{api_base}/repos/{repo}/issues?{qs}",
        github_token=github_token,
    )
    if not isinstance(items, list):
        return []
    return [it for it in items if isinstance(it, dict) and "pull_request" not in it]


def _get_hub_lead_phase(eff_env: dict[str, str], project_slug: str) -> str:
    hub_url = (eff_env.get("HUB_URL") or "").strip()
    if not hub_url:
        return ""
    try:
        client = HubClient.from_env(eff_env)
        _, proj_resp = client.request("GET", "/api/v1/projects", timeout=10)
        proj_items = (
            proj_resp.get("projects", proj_resp.get("items", []))
            if isinstance(proj_resp, dict)
            else (proj_resp if isinstance(proj_resp, list) else [])
        )
        pid = project_slug
        for item in proj_items:
            if isinstance(item, dict) and str(item.get("slug") or "") == project_slug:
                pid = str(item.get("id") or project_slug)
                break
        _, ag_resp = client.request("GET", f"/api/v1/projects/{pid}/agents", timeout=10)
        ag_items = (
            ag_resp.get("agents", ag_resp.get("items", []))
            if isinstance(ag_resp, dict)
            else (ag_resp if isinstance(ag_resp, list) else [])
        )
        for ag in ag_items:
            if not isinstance(ag, dict):
                continue
            slug = str(ag.get("slug") or ag.get("name") or "")
            if gke_lifecycle._is_lead_agent(slug):
                return str(ag.get("phase") or ag.get("status") or "").strip().lower()
    except Exception:
        return ""
    return ""


def _list_running_agent_pods(eff_env: dict[str, str]) -> list[dict[str, Any]]:
    ns = (eff_env.get("HUB_K8S_NAMESPACE") or "").strip()
    if not ns:
        return []
    cmd_env = {**os.environ, "CLOUDSDK_CORE_DISABLE_PROMPTS": "1"}
    try:
        proc = subprocess.run(
            ["kubectl", "get", "pods", "-n", ns, "-o", "json"],
            env=cmd_env,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return []

    running: list[dict[str, Any]] = []
    for item in (payload.get("items") if isinstance(payload, dict) else []) or []:
        if not isinstance(item, dict):
            continue
        meta = item.get("metadata") or {}
        status = item.get("status") or {}
        pod_name = str(meta.get("name") or "").strip()
        if not pod_name:
            continue
        labels = meta.get("labels") if isinstance(meta.get("labels"), dict) else {}
        if pod_name.startswith("scion-factory-") or "job-name" in labels:
            continue
        phase = str(status.get("phase") or "").strip()
        if phase != "Running":
            continue
        if meta.get("deletionTimestamp"):
            continue
        agent_slug = str(
            labels.get("scion.dev/agent")
            or labels.get("scion.name")
            or labels.get("app.kubernetes.io/instance")
            or pod_name
        )
        running.append(
            {
                "pod_name": pod_name,
                "agent_slug": agent_slug,
                "is_lead": gke_lifecycle._is_lead_agent(agent_slug),
            }
        )
    return running


def _read_live_lead_steps_and_tokens(eff_env: dict[str, str], running_pods: list[dict[str, Any]]) -> tuple[int, int]:
    ns = (eff_env.get("HUB_K8S_NAMESPACE") or "").strip()
    lead_pods = [p for p in running_pods if p.get("is_lead")]
    if not ns or not lead_pods:
        return 0, 0
    cmd_env = {**os.environ, "CLOUDSDK_CORE_DISABLE_PROMPTS": "1"}
    steps = 0
    tokens = 0
    for pod in lead_pods:
        pod_name = pod["pod_name"]
        try:
            r_find = subprocess.run(
                [
                    "kubectl",
                    "exec",
                    "-n",
                    ns,
                    pod_name,
                    "--",
                    "sh",
                    "-c",
                    'find /home/scion/.gemini -name "transcript_full.jsonl" 2>/dev/null',
                ],
                env=cmd_env,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if r_find.returncode != 0 or not r_find.stdout.strip():
            continue
        for remote_path in [ln.strip() for ln in r_find.stdout.splitlines() if ln.strip()]:
            try:
                r_cat = subprocess.run(
                    ["kubectl", "exec", "-n", ns, pod_name, "--", "cat", remote_path],
                    env=cmd_env,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if r_cat.returncode != 0 or not r_cat.stdout:
                continue
            for raw in r_cat.stdout.splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    step_obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(step_obj, dict):
                    in_t, cache_t, out_t = gke_lifecycle._extract_step_tokens(step_obj)
                    steps += 1
                    tokens += in_t + cache_t + out_t
    return steps, tokens


def _merge_staged_pods_into_backup(
    stage_dir: Path,
    backup_dir_str: str,
    secret_strings: list[str],
) -> None:
    if not backup_dir_str:
        return
    backup_dir = Path(backup_dir_str)
    staged_pods = stage_dir / "pods"
    if not backup_dir.is_dir() or not staged_pods.is_dir():
        return
    for src_file in staged_pods.rglob("*"):
        if not src_file.is_file():
            continue
        rel = src_file.relative_to(stage_dir)
        dest_file = backup_dir / rel
        if not dest_file.exists() or src_file.stat().st_size > dest_file.stat().st_size:
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            cur = dest_file.parent
            while cur != backup_dir and backup_dir in cur.parents:
                os.chmod(cur, 0o700)
                cur = cur.parent
            content = src_file.read_text(encoding="utf-8", errors="replace")
            gke_lifecycle._write_private_file(
                dest_file,
                gke_lifecycle._redact_secrets(content, secret_strings),
                mode=0o600,
            )
    scorecard_path = backup_dir / "scorecard.json"
    if scorecard_path.is_file():
        try:
            sc = json.loads(scorecard_path.read_text(encoding="utf-8"))
            sc["metrics"] = gke_lifecycle.extract_pod_and_transcript_metrics(backup_dir)
            safe_sc = gke_lifecycle._redact_secrets(sc, secret_strings)
            gke_lifecycle._write_private_file(scorecard_path, json.dumps(safe_sc, indent=2) + "\n", mode=0o600)
        except Exception:
            pass


def _poll_issues_until_verified(
    *,
    repo: str,
    issue_label: str,
    target_numbers: list[int],
    eff_env: dict[str, str],
    github_token: str,
    api_base: str,
    timeout_sec: int,
    poll_interval_sec: float,
    enable_eviction_drill: bool,
    stage_dir: Path,
    secret_strings: list[str],
) -> dict[str, Any]:
    deadline = _monotonic() + max(timeout_sec, 1)
    eviction: dict[str, Any] = {
        "enabled": enable_eviction_drill,
        "triggered": False,
        "deleted_pod": "",
        "agent_slug": "",
        "ok": not enable_eviction_drill,
    }
    ns = (eff_env.get("HUB_K8S_NAMESPACE") or "").strip()
    verified_numbers: set[int] = set()

    while True:
        open_issues = _list_open_wave_issues(repo, issue_label, github_token=github_token, api_base=api_base)
        by_num = {int(it["number"]): it for it in open_issues if it.get("number")}
        for num in target_numbers:
            iss = by_num.get(num)
            if iss is not None and "status:verified" in _extract_issue_labels(iss):
                verified_numbers.add(num)

        running_pods = _list_running_agent_pods(eff_env)
        if running_pods:
            stage_warnings: list[str] = []
            gke_lifecycle._archive_k8s_logs_and_transcripts(eff_env, stage_dir, secret_strings, stage_warnings)

        if enable_eviction_drill and not eviction["triggered"] and len(verified_numbers) < len(target_numbers):
            specialists = [p for p in running_pods if not p.get("is_lead")]
            if specialists and ns:
                victim = specialists[0]
                r_del = subprocess.run(
                    ["kubectl", "delete", "pod", "-n", ns, victim["pod_name"], "--wait=false"],
                    env={**os.environ, "CLOUDSDK_CORE_DISABLE_PROMPTS": "1"},
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                if r_del.returncode == 0:
                    eviction["triggered"] = True
                    eviction["deleted_pod"] = victim["pod_name"]
                    eviction["agent_slug"] = victim["agent_slug"]

        if len(verified_numbers) == len(target_numbers):
            if enable_eviction_drill:
                eviction["ok"] = bool(eviction["triggered"])
            return {
                "verified_issues": sorted(verified_numbers),
                "eviction_recovery": eviction,
            }

        if _monotonic() >= deadline:
            pending = sorted(set(target_numbers) - verified_numbers)
            raise TimeoutError(
                f"Timed out after {timeout_sec}s waiting for wave issues {pending} to reach status:verified"
            )
        _sleep(poll_interval_sec)


def _run_po_decision_drill(
    *,
    wave_num: int,
    project_slug: str,
    repo: str,
    issue_label: str,
    eff_env: dict[str, str],
    github_token: str,
    api_base: str,
    timeout_sec: int,
    poll_interval_sec: float,
    stage_dir: Path | None = None,
    secret_strings: list[str] | None = None,
) -> dict[str, Any]:
    po_wake_sla_sec = float(eff_env.get("PO_WAKE_SLA_SEC", "120"))
    drill_body = (
        "<!-- scion-agent: lead -->\n"
        f"## Hillclimb Wave {wave_num} PO Decision Drill\n\n"
        "Question for Product Owner: For local preview CORS defaults in `src/backend/config.py`, should we:\n"
        "- **Option A (Recommended):** Keep existing `CORS_ORIGINS` behavior unchanged (zero code diff), "
        "record the PO decision on this issue, remove `status:po-decision`, and mark `status:verified`.\n"
        "- **Option B:** Introduce an additional preview env override.\n"
    )
    _, created = gke_lifecycle._github_request(
        "POST",
        f"{api_base}/repos/{repo}/issues",
        github_token=github_token,
        body={
            "title": f"Wave {wave_num} PO Drill: Confirm local preview CORS default",
            "body": drill_body,
            "labels": [issue_label, "status:po-decision"],
        },
    )
    po_issue_num = int((created or {}).get("number") or 0)
    if not po_issue_num:
        raise RuntimeError("Failed to seed status:po-decision drill issue")

    # 1. Verify lead enters stopped and 0 agent pods are running while waiting on PO
    sleep_deadline = _monotonic() + max(timeout_sec, 1)
    entered_sleep = False
    while _monotonic() <= sleep_deadline:
        _list_open_wave_issues(repo, issue_label, github_token=github_token, api_base=api_base)
        lead_phase = _get_hub_lead_phase(eff_env, project_slug)
        running_pods = _list_running_agent_pods(eff_env)
        if running_pods and stage_dir is not None:
            gke_lifecycle._archive_k8s_logs_and_transcripts(eff_env, stage_dir, secret_strings or [], [])
        if lead_phase == "stopped" and len(running_pods) == 0:
            entered_sleep = True
            break
        _sleep(poll_interval_sec)

    if not entered_sleep:
        raise TimeoutError(
            f"PO decision drill timed out waiting for lead to enter stopped / 0 running pods on #{po_issue_num}"
        )

    # 2. Post unmarked human PO reply comment (must NOT contain <!-- scion-agent: marker)
    reply_body = (
        "PO Decision: Approved Option A — keep existing CORS_ORIGINS behavior with zero code changes, "
        "remove status:po-decision, and mark this issue status:verified."
    )
    reply_posted_at = _monotonic()
    gke_lifecycle._github_request(
        "POST",
        f"{api_base}/repos/{repo}/issues/{po_issue_num}/comments",
        github_token=github_token,
        body={"body": reply_body},
    )

    # 3. Verify lead wakes within 2 minutes (po_wake_sla_sec) and finishes the PO issue
    finish_deadline = _monotonic() + max(timeout_sec, 1)
    woke = False
    wake_latency_sec: float | None = None
    finished = False

    while _monotonic() <= finish_deadline:
        _sleep(poll_interval_sec)
        open_issues = _list_open_wave_issues(repo, issue_label, github_token=github_token, api_base=api_base)
        po_iss = next((it for it in open_issues if int(it.get("number") or 0) == po_issue_num), None)
        po_labels = _extract_issue_labels(po_iss) if po_iss else set()

        lead_phase = _get_hub_lead_phase(eff_env, project_slug)
        running_pods = _list_running_agent_pods(eff_env)
        if running_pods and stage_dir is not None:
            gke_lifecycle._archive_k8s_logs_and_transcripts(eff_env, stage_dir, secret_strings or [], [])

        if not woke and (
            lead_phase in ("running", "provisioning", "starting")
            or any(p.get("is_lead") for p in running_pods)
            or "status:verified" in po_labels
        ):
            woke = True
            wake_latency_sec = round(_monotonic() - reply_posted_at, 3)

        if "status:verified" in po_labels:
            finished = True
            break

    woke_within_2m = bool(woke and wake_latency_sec is not None and wake_latency_sec <= po_wake_sla_sec)
    ok = bool(entered_sleep and woke and woke_within_2m and finished)
    if not ok:
        raise RuntimeError(
            f"PO decision drill failed on #{po_issue_num}: entered_sleep={entered_sleep}, "
            f"woke={woke}, wake_latency_sec={wake_latency_sec}, finished={finished}"
        )

    return {
        "ok": True,
        "issue_number": po_issue_num,
        "entered_sleep": entered_sleep,
        "woke": woke,
        "wake_latency_sec": wake_latency_sec,
        "woke_within_2m": woke_within_2m,
        "finished": finished,
    }


def _run_idle_soak(
    *,
    project_slug: str,
    eff_env: dict[str, str],
    idle_soak_sec: int,
    poll_interval_sec: float,
    stage_dir: Path | None = None,
    secret_strings: list[str] | None = None,
) -> dict[str, Any]:
    grace_sec = int(eff_env.get("IDLE_STOP_GRACE_SEC", "600"))
    soak_start = _monotonic()
    soak_deadline = soak_start + max(idle_soak_sec, 0)
    grace_deadline = soak_start + min(max(idle_soak_sec, 0), grace_sec + 180)

    # Wait up to IDLE_STOP_GRACE_SEC (+ CronJob cadence buffer) for scale-to-zero if still winding down
    while _monotonic() < grace_deadline:
        running_pods = _list_running_agent_pods(eff_env)
        if running_pods and stage_dir is not None:
            gke_lifecycle._archive_k8s_logs_and_transcripts(eff_env, stage_dir, secret_strings or [], [])
        lead_phase = _get_hub_lead_phase(eff_env, project_slug)
        if len(running_pods) == 0 and lead_phase in ("stopped", ""):
            break
        _sleep(poll_interval_sec)

    baseline_pods = _list_running_agent_pods(eff_env)
    baseline_steps, baseline_tokens = _read_live_lead_steps_and_tokens(eff_env, baseline_pods)

    max_running_pods = 0
    additional_steps = 0
    additional_tokens = 0
    samples_after_grace = 0

    while True:
        running_pods = _list_running_agent_pods(eff_env)
        lead_phase = _get_hub_lead_phase(eff_env, project_slug)
        pod_count = len(running_pods)
        if lead_phase in ("running", "provisioning", "starting") and pod_count == 0:
            pod_count = 1
        max_running_pods = max(max_running_pods, pod_count)

        cur_steps, cur_tokens = _read_live_lead_steps_and_tokens(eff_env, running_pods)
        if cur_steps > baseline_steps:
            additional_steps = max(additional_steps, cur_steps - baseline_steps)
        if cur_tokens > baseline_tokens:
            additional_tokens = max(additional_tokens, cur_tokens - baseline_tokens)
        samples_after_grace += 1

        if _monotonic() >= soak_deadline:
            break
        _sleep(poll_interval_sec)

    ok = max_running_pods == 0 and additional_steps == 0 and additional_tokens == 0
    if not ok:
        raise RuntimeError(
            f"Idle soak failed: max_running_agent_pods_after_grace={max_running_pods}, "
            f"additional_lead_steps_after_grace={additional_steps}, "
            f"additional_lead_tokens_after_grace={additional_tokens}"
        )

    return {
        "ok": True,
        "idle_soak_sec": idle_soak_sec,
        "idle_stop_grace_sec": grace_sec,
        "samples_after_grace": samples_after_grace,
        "max_running_agent_pods_after_grace": max_running_pods,
        "additional_lead_steps_after_grace": additional_steps,
        "additional_lead_tokens_after_grace": additional_tokens,
    }


def _prepare_wave_files(
    wave_num: int,
    *,
    preset_spec: dict[str, Any],
    env_file: Path,
    work_dir: Path,
    effective_apply: bool,
) -> tuple[Path, Path, str, str, str, str]:
    project_slug = f"bananacast-hc{wave_num}"
    build_branch = f"factory/gke-hc{wave_num}"
    issue_label = f"factory:{project_slug}"

    raw_env_map = gke_lifecycle._parse_env_file(env_file)
    merged_env = gke_lifecycle._load_lifecycle_env(env_file)
    existing_hub = (merged_env.get("HUB_PROJECT") or "").strip()
    if re.fullmatch(rf"scion-gke-hc{wave_num}-[0-9a-f]{{6}}", existing_hub):
        hub_project = existing_hub
        env_out = dict(raw_env_map)
    else:
        hub_project = f"scion-gke-hc{wave_num}-{secrets.token_hex(3)}"
        drop_keys = set(_STALE_PROJECT_ENV_KEYS)
        if existing_hub:
            drop_keys.update({"HUB_URL", "HUB_IAP_AUDIENCE"})
        env_out = {k: v for k, v in raw_env_map.items() if k not in drop_keys}

    if not effective_apply:
        env_out.pop("HUB_URL", None)
        env_out.pop("HUB_IAP_AUDIENCE", None)

    env_out["HUB_PROJECT"] = hub_project
    if not effective_apply and not (env_out.get("PO_EMAIL") or merged_env.get("PO_EMAIL")):
        env_out["PO_EMAIL"] = "po@example.com"

    wave_env_path = work_dir / "factory.env"
    env_lines = [f"{k}={v}" for k, v in env_out.items() if v != ""]
    gke_lifecycle._write_private_file(wave_env_path, "\n".join(env_lines) + "\n", mode=0o600)

    wave_spec = copy.deepcopy(preset_spec)
    wave_spec.setdefault("project", {})["slug"] = project_slug
    wave_spec.setdefault("vcs", {})["build_branch"] = build_branch
    wave_spec["waves"] = [
        {
            "wave": wave_num,
            "title": f"Hillclimb Wave {wave_num}",
            "issues": [
                {
                    "title": t["title"],
                    "owner_role": t["owner_role"],
                    "requirements": list(t.get("requirements") or ["REQ-001"]),
                    "acceptance_criteria": list(t["acceptance_criteria"]),
                }
                for t in WAVE_CATALOG[wave_num]
            ],
        }
    ]
    wave_spec_path = work_dir / f"{project_slug}.factory.yaml"
    scion_factory.save_spec(wave_spec, wave_spec_path)
    return wave_spec_path, wave_env_path, project_slug, build_branch, issue_label, hub_project


def _keep_project_for_debug(
    *,
    wave_spec_path: Path,
    wave_env_path: Path,
    backup_root: Path | str | None,
    hub_project: str,
    issue_label: str,
    build_branch: str,
) -> dict[str, Any]:
    """Copy the wave env and spec out of the temp work dir and describe how to tear down later."""
    root = Path(backup_root).expanduser() if backup_root else Path.home() / "scion-backups"
    kept_dir = root / f"kept-{hub_project}"
    kept_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(kept_dir, 0o700)
    kept_env = kept_dir / "factory.env"
    kept_spec = kept_dir / wave_spec_path.name
    gke_lifecycle._write_private_file(kept_env, wave_env_path.read_text(encoding="utf-8"), mode=0o600)
    gke_lifecycle._write_private_file(kept_spec, wave_spec_path.read_text(encoding="utf-8"), mode=0o600)
    teardown = (
        f"python3 scripts/scion_factory.py gke-down {kept_spec} --env {kept_env} --delete-project "
        f"--cleanup-issue-label {issue_label} --cleanup-branch {build_branch} --apply --json"
    )
    return {
        "ok": False,
        "apply": True,
        "skipped_teardown": True,
        "kept_for_debug": True,
        "hub_project": hub_project,
        "project_deleted": False,
        "env_file": str(kept_env),
        "spec_file": str(kept_spec),
        "teardown_command": teardown,
    }


def run_wave(
    wave_num: int,
    *,
    env_file: Path | str,
    idle_soak_sec: int = 1800,
    drills: bool = False,
    timeout_sec: int = 3600,
    poll_interval_sec: float = 15.0,
    preset_path: Path | str | None = None,
    backup_root: Path | str | None = None,
    apply: bool = False,
    keep_on_failure: bool = False,
) -> dict[str, Any]:
    """Execute or dry-run a single-project `bananacast` GKE hillclimb wave.

    With `keep_on_failure`, a failed wave skips teardown: the GCP project, the GitHub wave issues and
    the wave branch stay, and the wave env and spec are copied out of the temp dir so the failure can
    be debugged in place. The operator must run the reported `teardown_command` afterwards.
    """
    if wave_num not in WAVE_CATALOG:
        raise ValueError(f"Unsupported wave_num {wave_num!r}; expected one of {sorted(WAVE_CATALOG)}")

    resolved_preset = Path(preset_path) if preset_path is not None else (ROOT_DIR / "presets" / "bananacast-gke.factory.yaml")
    preset_spec = scion_factory.load_spec(resolved_preset)
    effective_apply = bool(apply) and not gke_lifecycle._is_dry_run_env()
    resolved_env_file = Path(env_file).expanduser().resolve()

    work_dir = Path(tempfile.mkdtemp(prefix=f"scion-hc{wave_num}-"))
    os.chmod(work_dir, 0o700)
    stage_dir = work_dir / "live-stage"
    stage_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(stage_dir, 0o700)

    try:
        wave_spec_path, wave_env_path, project_slug, build_branch, issue_label, hub_project = _prepare_wave_files(
            wave_num,
            preset_spec=preset_spec,
            env_file=resolved_env_file,
            work_dir=work_dir,
            effective_apply=effective_apply,
        )
        eff_env = gke_lifecycle._load_lifecycle_env(wave_env_path)
        repo = gke_lifecycle._normalize_github_repo(
            str((preset_spec.get("vcs") or {}).get("repo") or eff_env.get("GITHUB_REPO") or "duboc/bananacast")
        )
        tasks = WAVE_CATALOG[wave_num]

        if not effective_apply:
            prev_hub_url = os.environ.pop("HUB_URL", None)
            try:
                up_dry = gke_lifecycle.gke_up(
                    wave_spec_path,
                    env_file=wave_env_path,
                    kit_dir=work_dir / f"kit-{project_slug}",
                    create_project=True,
                    apply=False,
                    send_go=False,
                )
            finally:
                if prev_hub_url is not None:
                    os.environ["HUB_URL"] = prev_hub_url
            down_dry = gke_lifecycle.gke_down(
                wave_spec_path,
                env_file=wave_env_path,
                backup_root=backup_root,
                cleanup_issue_label=issue_label,
                cleanup_branch=build_branch,
                delete_project=True,
                apply=False,
            )
            return {
                "ok": bool(up_dry.get("ok") and down_dry.get("ok")),
                "apply": False,
                "wave": wave_num,
                "repo": repo,
                "project_slug": project_slug,
                "hub_project": hub_project,
                "build_branch": build_branch,
                "issue_label": issue_label,
                "planned_issues": [t["title"] for t in tasks],
                "drills_enabled": bool(drills),
                "idle_soak_sec": idle_soak_sec,
                "gke_up": up_dry,
                "gke_down": down_dry,
            }

        gh_token = provision_factory._resolve_github_token(eff_env)
        api_base = _api_base(eff_env)
        secret_strings = gke_lifecycle._collect_secret_strings(eff_env)

        # 1. Snapshot protected branch SHAs before any remote mutation
        main_before = _get_branch_sha(repo, "main", github_token=gh_token, api_base=api_base, allow_missing=False)
        factory_main_before = _get_branch_sha(
            repo, "factory/main", github_token=gh_token, api_base=api_base, allow_missing=True
        )

        seeded_issues: list[int] = []
        up_report: dict[str, Any] = {}
        down_report: dict[str, Any] = {}
        drills_report: dict[str, Any] = {}
        soak_report: dict[str, Any] = {}
        verified_issues: list[int] = []
        operational_error = ""

        try:
            # 2. Create remote branch factory/gke-hc<wave_num> from main SHA + seed 3 wave issues
            _ensure_remote_branch(repo, build_branch, main_before, github_token=gh_token, api_base=api_base)
            _ensure_labels(
                repo,
                [
                    (issue_label, "1d76db"),
                    ("digest", "0052cc"),
                    ("status:ready", "0e8a16"),
                    ("status:in-progress", "fbca04"),
                    ("status:in-review", "5319e7"),
                    ("status:verified", "2ea44f"),
                    ("status:blocked", "d93f0b"),
                    ("status:po-decision", "b60205"),
                ],
                github_token=gh_token,
                api_base=api_base,
            )
            _seed_digest_issue(
                repo,
                project_slug,
                issue_label,
                github_token=gh_token,
                api_base=api_base,
            )
            seeded_issues = _seed_wave_issues(
                repo,
                wave_num,
                issue_label,
                tasks,
                github_token=gh_token,
                api_base=api_base,
            )

            # 3. Provision ephemeral GKE Hub + single-project delta factory
            kit_dir = ROOT_DIR / "build" / f"kit-{project_slug}"
            up_report = gke_lifecycle.gke_up(
                wave_spec_path,
                env_file=wave_env_path,
                kit_dir=kit_dir,
                create_project=True,
                apply=True,
                send_go=False,
            )
            eff_env = gke_lifecycle._load_lifecycle_env(wave_env_path)
            secret_strings = gke_lifecycle._collect_secret_strings(eff_env)
            if not up_report.get("ok"):
                raise RuntimeError(f"gke_up failed in phase {up_report.get('phase', 'provision')}")

            # 4. Poll wave issues until status:verified (+ drills when enabled)
            poll_res = _poll_issues_until_verified(
                repo=repo,
                issue_label=issue_label,
                target_numbers=seeded_issues,
                eff_env=eff_env,
                github_token=gh_token,
                api_base=api_base,
                timeout_sec=timeout_sec,
                poll_interval_sec=poll_interval_sec,
                enable_eviction_drill=bool(drills),
                stage_dir=stage_dir,
                secret_strings=secret_strings,
            )
            verified_issues = poll_res["verified_issues"]
            if drills:
                ev_res = poll_res["eviction_recovery"]
                if not ev_res.get("ok"):
                    raise RuntimeError("Eviction recovery drill did not observe and delete a running specialist pod")
                po_res = _run_po_decision_drill(
                    wave_num=wave_num,
                    project_slug=project_slug,
                    repo=repo,
                    issue_label=issue_label,
                    eff_env=eff_env,
                    github_token=gh_token,
                    api_base=api_base,
                    timeout_sec=timeout_sec,
                    poll_interval_sec=poll_interval_sec,
                    stage_dir=stage_dir,
                    secret_strings=secret_strings,
                )
                drills_report = {
                    "eviction_recovery": ev_res,
                    "po_decision_roundtrip": po_res,
                }

            # 5. Idle soak confirming 0 running agent pods and 0 additional lead steps/tokens after grace
            soak_report = _run_idle_soak(
                project_slug=project_slug,
                eff_env=eff_env,
                idle_soak_sec=idle_soak_sec,
                poll_interval_sec=poll_interval_sec,
                stage_dir=stage_dir,
                secret_strings=secret_strings,
            )
        except (TimeoutError, RuntimeError) as exc:
            operational_error = gke_lifecycle._redact_secrets(str(exc), secret_strings)
        finally:
            # 6. Archive, clean up GitHub wave artifacts, delete ephemeral GCP project, and verify main SHAs.
            #    Always, unless the wave failed and the operator asked to keep it for debugging.
            kept_alive = bool(keep_on_failure) and (bool(operational_error) or not up_report.get("ok"))
            if kept_alive:
                down_report = _keep_project_for_debug(
                    wave_spec_path=wave_spec_path,
                    wave_env_path=wave_env_path,
                    backup_root=backup_root,
                    hub_project=hub_project,
                    issue_label=issue_label,
                    build_branch=build_branch,
                )
            else:
                down_report = gke_lifecycle.gke_down(
                    wave_spec_path,
                    env_file=wave_env_path,
                    backup_root=backup_root,
                    cleanup_issue_label=issue_label,
                    cleanup_branch=build_branch,
                    delete_project=True,
                    apply=True,
                )
            backup_dir_str = str(down_report.get("backup_dir") or "")
            _merge_staged_pods_into_backup(
                stage_dir,
                backup_dir_str,
                secret_strings,
            )
            if backup_dir_str and Path(backup_dir_str).is_dir():
                bdir_path = Path(backup_dir_str)
                merged_metrics = gke_lifecycle.extract_pod_and_transcript_metrics(bdir_path)
                down_report["metrics"] = merged_metrics
                scorecard_path = bdir_path / "scorecard.json"
                base_sc: dict[str, Any] = dict(down_report)
                if scorecard_path.is_file():
                    try:
                        loaded_sc = json.loads(scorecard_path.read_text(encoding="utf-8"))
                        if isinstance(loaded_sc, dict):
                            base_sc = loaded_sc
                    except Exception:
                        pass
                base_sc["metrics"] = merged_metrics
                safe_sc = gke_lifecycle._redact_secrets(base_sc, secret_strings)
                gke_lifecycle._write_private_file(scorecard_path, json.dumps(safe_sc, indent=2) + "\n", mode=0o600)

            main_after = _get_branch_sha(repo, "main", github_token=gh_token, api_base=api_base, allow_missing=False)
            factory_main_after = _get_branch_sha(
                repo, "factory/main", github_token=gh_token, api_base=api_base, allow_missing=True
            )

        branches_unchanged = (main_after == main_before) and (factory_main_after == factory_main_before)
        if not branches_unchanged:
            raise RuntimeError(
                f"Protected branch SHA mutated during wave {wave_num}: "
                f"main {main_before} -> {main_after}, factory/main {factory_main_before} -> {factory_main_after}"
            )

        ok = not operational_error and bool(up_report.get("ok")) and bool(down_report.get("ok"))
        result: dict[str, Any] = {
            "ok": ok,
            "apply": True,
            "wave": wave_num,
            "repo": repo,
            "project_slug": project_slug,
            "hub_project": hub_project,
            "build_branch": build_branch,
            "issue_label": issue_label,
            "seeded_issues": seeded_issues,
            "verified_issues": verified_issues,
            "drills": drills_report,
            "idle_soak": soak_report,
            "branch_guard": {
                "unchanged": True,
                "main_before": main_before,
                "main_after": main_after,
                "factory_main_before": factory_main_before,
                "factory_main_after": factory_main_after,
            },
            "gke_up": up_report,
            "gke_down": down_report,
        }
        if operational_error:
            result["error"] = operational_error
        if down_report.get("kept_for_debug"):
            result["kept_for_debug"] = True
        return gke_lifecycle._redact_secrets(result, secret_strings)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run or preview an end-to-end bananacast GKE hillclimb wave (1, 2, or 3)."
    )
    parser.add_argument("--wave", type=int, choices=(1, 2, 3), required=True, help="Hillclimb wave number (1, 2, or 3)")
    parser.add_argument(
        "--env",
        type=str,
        default=str(ROOT_DIR / "config" / "factory.env"),
        help="Path to base factory.env (copied into an ephemeral wave env; never mutated in place)",
    )
    parser.add_argument(
        "--idle-soak-sec",
        type=int,
        default=1800,
        help="Post-wave idle soak duration in seconds (default: 1800)",
    )
    parser.add_argument(
        "--drills",
        action="store_true",
        help="Enable Wave 3 resilience drills (specialist pod deletion + status:po-decision wake roundtrip)",
    )
    parser.add_argument(
        "--timeout-sec",
        type=int,
        default=3600,
        help="Timeout in seconds per wave phase before triggering guaranteed gke-down cleanup (default: 3600)",
    )
    parser.add_argument(
        "--backup-root",
        type=str,
        default=None,
        help="Optional backup root directory for archived logs, transcripts, and scorecard.json",
    )
    parser.add_argument(
        "--keep-on-failure",
        action="store_true",
        help="If the wave fails, skip teardown and keep the GCP project, issues and branch for debugging "
        "(the report prints the gke-down command to run afterwards)",
    )
    parser.add_argument("--apply", action="store_true", help="Execute live cloud/GitHub wave (default: dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="Preview wave plan without mutating cloud/GitHub state")
    parser.add_argument("--json", action="store_true", help="Emit structured JSON report")
    args = parser.parse_args(argv)

    eff_apply = bool(args.apply) and not bool(args.dry_run) and not gke_lifecycle._is_dry_run_env()
    report = run_wave(
        args.wave,
        env_file=Path(args.env),
        idle_soak_sec=args.idle_soak_sec,
        drills=args.drills,
        timeout_sec=args.timeout_sec,
        backup_root=Path(args.backup_root) if args.backup_root else None,
        apply=eff_apply,
        keep_on_failure=args.keep_on_failure,
    )
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        mode_str = "apply" if report.get("apply") else "dry-run"
        print(
            f"hillclimb wave {args.wave} ({mode_str}): ok={report.get('ok')} "
            f"hub_project={report.get('hub_project')} branch={report.get('build_branch')}"
        )
        if report.get("error"):
            print(f"error: {report['error']}", file=sys.stderr)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
