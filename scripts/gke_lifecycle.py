#!/usr/bin/env python3
"""One-command GKE Scion software factory lifecycle (`gke-up`, `gke-down`, and scorecard metrics).

Orchestrates cold-start GKE Hub setup (`setup-gke-hub.sh`), kit compilation, and
single/two-project delta or scheduled factory provisioning (`gke_up`), as well as
forensic pod log / Antigravity transcript archiving, `scorecard.json` emission,
GitHub test cleanup, and guarded ephemeral GCP project deletion (`gke_down`).
"""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

os.environ.setdefault("CLOUDSDK_CORE_DISABLE_PROMPTS", "1")

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import provision_factory  # noqa: E402
import scion_factory  # noqa: E402
from hub_client import HubClient  # noqa: E402

PROTECTED_BRANCHES = frozenset({"main", "master", "factory/main", "factory/eval"})

_SENSITIVE_DICT_KEYS = frozenset(
    {
        "token",
        "access_token",
        "refresh_token",
        "transport_token",
        "github_token",
        "githubtoken",
        "hub_token",
        "scion_hub_token",
        "hub_transport_token",
        "secret",
        "password",
        "authorization",
        "proxy-authorization",
        "api_key",
        "private_key",
    }
)
_SECRET_PREFIX_ALT = "|".join(
    [
        "gh" + r"p_[A-Za-z0-9_]+",
        "github_" + r"pat_[A-Za-z0-9_]+",
        "scion_" + r"pat_[A-Za-z0-9_]+",
        "ya" + r"29\.[A-Za-z0-9._-]+",
        "AI" + r"za[A-Za-z0-9_-]+",
    ]
)
_SECRET_VALUE_RE = re.compile(f"({_SECRET_PREFIX_ALT})")


def _is_dry_run_env() -> bool:
    return os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")


def _parse_env_file(env_file: str | Path) -> dict[str, str]:
    path = Path(env_file)
    parsed: dict[str, str] = {}
    if not path.is_file():
        return parsed
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" in line:
            k, v = line.split("=", 1)
            parsed[k.strip()] = v.strip().strip("'\"")
    return parsed


def _load_lifecycle_env(env_file: str | Path) -> dict[str, str]:
    """Merge process env with `env_file`, letting explicit non-empty `env_file` keys win."""
    merged = {k: v for k, v in os.environ.items() if v}
    for k, v in _parse_env_file(env_file).items():
        if v:
            merged[k] = v
    merged["CLOUDSDK_CORE_DISABLE_PROMPTS"] = "1"
    return merged


def _collect_secret_strings(eff_env: dict[str, str]) -> list[str]:
    candidates: set[str] = set()
    for key in ("GITHUB_TOKEN", "GH_TOKEN", "SCION_HUB_TOKEN", "HUB_TOKEN", "HUB_TRANSPORT_TOKEN"):
        val = (eff_env.get(key) or "").strip()
        if len(val) >= 4:
            candidates.add(val)
    tok_resolved = provision_factory._resolve_github_token(eff_env)
    if len(tok_resolved) >= 4:
        candidates.add(tok_resolved)
    return sorted(candidates, key=len, reverse=True)


def _redact_secrets(value: Any, secret_strings: list[str]) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for k, v in value.items():
            if str(k).lower() in _SENSITIVE_DICT_KEYS and isinstance(v, str):
                cleaned[k] = "[REDACTED]" if v else ""
            else:
                cleaned[k] = _redact_secrets(v, secret_strings)
        return cleaned
    if isinstance(value, list):
        return [_redact_secrets(item, secret_strings) for item in value]
    if isinstance(value, str):
        out = value
        for s in secret_strings:
            if s and s in out:
                out = out.replace(s, "[REDACTED]")
        out = _SECRET_VALUE_RE.sub("[REDACTED]", out)
        return out
    return value


def _write_private_file(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
        os.chmod(path, mode)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def gke_up(
    spec_path: str | Path,
    *,
    env_file: str | Path,
    kit_dir: str | Path | None = None,
    create_project: bool = False,
    scion_repo: str | Path | None = None,
    apply: bool = False,
    send_go: bool = False,
) -> dict[str, Any]:
    """Run one-command GKE setup (`setup-gke-hub.sh`), kit compilation, and factory provisioning."""
    t_total = time.monotonic()
    effective_apply = bool(apply) and not _is_dry_run_env()

    spec = scion_factory.load_spec(spec_path)
    norm_spec, _ = scion_factory.normalize_team(spec)
    slug = str((spec.get("project") or {}).get("slug") or "factory")

    roles = list((norm_spec.get("team") or {}).get("build_roles") or [])
    if scion_factory.has_eval_project(norm_spec):
        roles.extend((norm_spec.get("team") or {}).get("eval_roles") or [])
    harnesses = ",".join(sorted({str(r.get("harness", "")).strip() for r in roles if r.get("harness")})) or "antigravity"

    env_path = Path(env_file).resolve()
    resolved_kit = (
        Path(kit_dir).resolve()
        if kit_dir is not None
        else (ROOT_DIR / "build" / f"kit-{slug}").resolve()
    )

    cmd_setup = [
        "bash",
        str(ROOT_DIR / "scripts" / "setup-gke-hub.sh"),
        "--env",
        str(env_path),
        "--harnesses",
        harnesses,
        "--write-env",
        str(env_path),
    ]
    if create_project:
        cmd_setup.append("--create-project")
    if scion_repo is not None and str(scion_repo).strip():
        cmd_setup.extend(["--scion-repo", str(Path(scion_repo).resolve())])
    if effective_apply:
        cmd_setup.append("--approve-apply")
    else:
        cmd_setup.append("--dry-run")

    setup_env = {
        **os.environ,
        "CLOUDSDK_CORE_DISABLE_PROMPTS": "1",
        "DRY_RUN": "0" if effective_apply else "1",
    }

    t0 = time.monotonic()
    r_setup = subprocess.run(cmd_setup, cwd=ROOT_DIR, env=setup_env, capture_output=True, text=True)
    setup_gke_sec = round(time.monotonic() - t0, 4)
    if r_setup.returncode != 0:
        total_sec = round(time.monotonic() - t_total, 4)
        return {
            "ok": False,
            "apply": effective_apply,
            "phase": "setup_gke",
            "harnesses": harnesses,
            "kit_dir": str(resolved_kit),
            "env_file": str(env_path),
            "stderr": r_setup.stderr,
            "setup_gke_sec": setup_gke_sec,
            "compile_sec": 0.0,
            "provision_sec": 0.0,
            "total_sec": total_sec,
        }

    t1 = time.monotonic()
    compile_res = scion_factory.compile_factory_boilerplate(spec, resolved_kit)
    compile_sec = round(time.monotonic() - t1, 4)

    # Ensure freshly written keys in env_path win even if caller had stale exports in os.environ
    updated_file_vals = _parse_env_file(env_path)
    saved_env: dict[str, str | None] = {}
    sync_keys = (
        "HUB_PROJECT",
        "SANDBOX_PROJECT",
        "SANDBOX_REGION",
        "FIRESTORE_PROJECT",
        "DATA_BUCKET",
        "HUB_URL",
        "HUB_NAME",
        "HUB_REGION",
        "HUB_IAP_AUDIENCE",
        "HUB_IMPERSONATE_SA",
        "AGENT_RUNTIME",
        "RUNTIME_PROFILE",
        "HUB_K8S_CLUSTER",
        "HUB_K8S_NAMESPACE",
        "IMAGE_REGISTRY",
        "VERTEX_LOCATION",
    )
    t2 = time.monotonic()
    try:
        for key in sync_keys:
            if key in updated_file_vals and updated_file_vals[key]:
                saved_env[key] = os.environ.get(key)
                os.environ[key] = updated_file_vals[key]
        prov_res = provision_factory.provision_factory(
            spec,
            resolved_kit,
            env_path,
            apply=effective_apply,
            send_go=send_go,
        )
    finally:
        for key, prev in saved_env.items():
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev
    provision_sec = round(time.monotonic() - t2, 4)
    total_sec = round(time.monotonic() - t_total, 4)

    return {
        "ok": bool(prov_res.get("ok", False)),
        "apply": effective_apply,
        "harnesses": harnesses,
        "kit_dir": str(resolved_kit),
        "env_file": str(env_path),
        "setup_gke_sec": setup_gke_sec,
        "compile_sec": compile_sec,
        "provision_sec": provision_sec,
        "total_sec": total_sec,
        "timers": {
            "setup_gke_sec": setup_gke_sec,
            "compile_sec": compile_sec,
            "provision_sec": provision_sec,
            "total_sec": total_sec,
        },
        "compile": compile_res,
        "provision": prov_res,
    }


def _infer_agent_name(backup_dir: Path, file_path: Path) -> str:
    rel = file_path.relative_to(backup_dir).parts
    if len(rel) >= 3 and rel[0] in ("pods", "agents", "transcripts", "logs"):
        return rel[1]
    if len(rel) >= 2:
        return rel[0]
    return file_path.stem


def _transcript_dedup_key(backup_dir: Path, file_path: Path, agent_name: str) -> tuple[str, str]:
    rel_parts = file_path.relative_to(backup_dir).parts
    if ".gemini" in rel_parts:
        idx = rel_parts.index(".gemini")
        return agent_name, "/".join(rel_parts[idx:])
    if len(rel_parts) >= 4 and rel_parts[0] == "pods":
        return agent_name, "/".join(rel_parts[3:])
    return agent_name, "/".join(rel_parts)


def _is_lead_agent(agent_name: str) -> bool:
    name = agent_name.strip().lower()
    if name.endswith("eval-lead"):
        return False
    return name == "lead" or name.endswith("-lead")


def _extract_step_tokens(step: dict[str, Any]) -> tuple[int, int, int]:
    usage = step.get("usage") if isinstance(step.get("usage"), dict) else {}
    in_tok = int(step.get("input_tokens", usage.get("input_tokens", 0)) or 0)
    cache_tok = int(
        step.get(
            "cache_read_tokens",
            usage.get("cache_read_tokens", usage.get("cached_tokens", 0)),
        )
        or 0
    )
    out_tok = int(step.get("output_tokens", usage.get("output_tokens", 0)) or 0)
    return in_tok, cache_tok, out_tok


def _is_lead_turn_active(turn_steps: list[dict[str, Any]]) -> bool:
    prompt_parts: list[str] = []
    resp_parts: list[str] = []
    tool_parts: list[str] = []

    for step in turn_steps:
        if step.get("idle") is True:
            return False
        stype = str(step.get("type") or "").upper()
        content = str(step.get("content") or "")
        if stype in ("USER_INPUT", "SYSTEM_MESSAGE"):
            prompt_parts.append(content)
        elif content:
            resp_parts.append(content)
        for tc in step.get("tool_calls") or []:
            if isinstance(tc, dict):
                tool_parts.append(json.dumps(tc, sort_keys=True))
            elif tc:
                tool_parts.append(str(tc))

    prompt_text = "\n".join(prompt_parts)
    resp_text = "\n".join(resp_parts)
    tool_text = "\n".join(tool_parts)

    tl = tool_text.lower()
    active_markers = (
        "scion start",
        "scion message",
        "scion stop",
        "scion delete",
        "git merge",
        "git push",
        "git commit",
        "gh issue edit",
        "gh issue create",
        "gh issue comment",
        "gh pr ",
        "/briefs/",
        "state.json",
    )
    if any(m in tl for m in active_markers):
        return True

    pl = prompt_text.lower()
    rl = resp_text.lower()
    if "[heartbeat]" in pl or "heartbeat:" in pl:
        return False
    if "state-change" in pl or "ignored bare state-change" in tl or "ignored bare state-change" in rl:
        return False
    if "stand by" in pl and "delta:" not in pl and "go:" not in pl:
        return False
    if not prompt_text.strip() and not tool_text.strip():
        return False
    return True


def extract_pod_and_transcript_metrics(backup_dir: Path | str) -> dict[str, Any]:
    """Parse archived `agent.log` and `transcript_full.jsonl` files into error and token metrics."""
    bdir = Path(backup_dir)
    iap_401_errors = 0
    heartbeat_errors = 0
    telemetry_boot_errors = 0
    per_agent: dict[str, dict[str, int]] = {}

    lead_active = {"turns": 0, "steps": 0, "input_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0}
    lead_idle = {"turns": 0, "steps": 0, "input_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0}

    if bdir.is_dir():
        seen_logs: set[Path] = set()
        for pattern in ("agent.log", "*.agent.log"):
            for log_path in sorted(bdir.rglob(pattern)):
                if not log_path.is_file() or log_path in seen_logs:
                    continue
                seen_logs.add(log_path)
                for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
                    ll = line.lower()
                    if "auth_lost" in ll or (
                        "401" in ll
                        and any(
                            m in ll
                            for m in (
                                "iap",
                                "token refresh failed",
                                "proxy-authorization",
                                "transport-token",
                                "invalid credentials",
                            )
                        )
                    ):
                        iap_401_errors += 1
                    if "heartbeat failed" in ll:
                        heartbeat_errors += 1
                    if (
                        "failed to start telemetry" in ll
                        or "cloud telemetry enabled but destination is not configured" in ll
                    ):
                        telemetry_boot_errors += 1

        deduped_transcripts: dict[tuple[str, str], tuple[tuple[int, int, str], Path]] = {}
        for cand in sorted(bdir.rglob("transcript_full.jsonl")):
            if not cand.is_file():
                continue
            ag_name = _infer_agent_name(bdir, cand)
            dkey = _transcript_dedup_key(bdir, cand, ag_name)
            st = cand.stat()
            rank = (int(st.st_size), int(st.st_mtime_ns), str(cand))
            prev = deduped_transcripts.get(dkey)
            if prev is None or rank > prev[0]:
                deduped_transcripts[dkey] = (rank, cand)

        for _, tpath in sorted(deduped_transcripts.values(), key=lambda pair: str(pair[1])):
            agent_name = _infer_agent_name(bdir, tpath)
            stats = per_agent.setdefault(
                agent_name,
                {
                    "steps": 0,
                    "user_inputs": 0,
                    "input_tokens": 0,
                    "cache_read_tokens": 0,
                    "output_tokens": 0,
                },
            )

            turns: list[list[dict[str, Any]]] = []
            current_turn: list[dict[str, Any]] = []

            for raw_line in tpath.read_text(encoding="utf-8", errors="replace").splitlines():
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    step = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(step, dict):
                    continue

                stype = str(step.get("type") or "").upper()
                if stype == "USER_INPUT":
                    stats["user_inputs"] += 1
                    if current_turn:
                        turns.append(current_turn)
                    current_turn = [step]
                else:
                    current_turn.append(step)

                in_tok, cache_tok, out_tok = _extract_step_tokens(step)
                stats["steps"] += 1
                stats["input_tokens"] += in_tok
                stats["cache_read_tokens"] += cache_tok
                stats["output_tokens"] += out_tok

            if current_turn:
                turns.append(current_turn)

            if _is_lead_agent(agent_name):
                for turn_steps in turns:
                    has_user_or_tokens = any(
                        str(s.get("type") or "").upper() == "USER_INPUT" or any(_extract_step_tokens(s))
                        for s in turn_steps
                    )
                    if not has_user_or_tokens:
                        continue
                    bucket = lead_active if _is_lead_turn_active(turn_steps) else lead_idle
                    bucket["turns"] += 1
                    bucket["steps"] += len(turn_steps)
                    for s in turn_steps:
                        in_tok, cache_tok, out_tok = _extract_step_tokens(s)
                        bucket["input_tokens"] += in_tok
                        bucket["cache_read_tokens"] += cache_tok
                        bucket["output_tokens"] += out_tok

    totals = {
        "steps": sum(v["steps"] for v in per_agent.values()),
        "user_inputs": sum(v["user_inputs"] for v in per_agent.values()),
        "input_tokens": sum(v["input_tokens"] for v in per_agent.values()),
        "cache_read_tokens": sum(v["cache_read_tokens"] for v in per_agent.values()),
        "output_tokens": sum(v["output_tokens"] for v in per_agent.values()),
    }

    return {
        "iap_401_errors": iap_401_errors,
        "heartbeat_errors": heartbeat_errors,
        "telemetry_boot_errors": telemetry_boot_errors,
        "per_agent": per_agent,
        "totals": totals,
        "lead_active_vs_idle": {
            "active": lead_active,
            "idle": lead_idle,
        },
    }


def _normalize_github_repo(repo: str) -> str:
    val = (repo or "").strip()
    if val.startswith("https://github.com/"):
        val = val.removeprefix("https://github.com/")
    elif val.startswith("http://github.com/"):
        val = val.removeprefix("http://github.com/")
    if val.endswith(".git"):
        val = val[:-4]
    return val.strip("/")


def _github_request(
    method: str,
    url: str,
    *,
    github_token: str = "",
    body: dict[str, Any] | None = None,
    timeout: int = 20,
) -> tuple[int, Any]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "scion-factory-lifecycle/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        parsed = json.loads(raw) if raw.strip() else {}
        return resp.status, parsed


def _archive_hub_agents(
    eff_env: dict[str, str],
    spec: dict[str, Any],
    backup_dir: Path,
    secret_strings: list[str],
    warnings: list[str],
) -> dict[str, list[dict[str, Any]]]:
    hub_agents: dict[str, list[dict[str, Any]]] = {}
    hub_url = (eff_env.get("HUB_URL") or "").strip()
    if not hub_url:
        return hub_agents

    try:
        client = HubClient.from_env(eff_env)
        _, proj_resp = client.request("GET", "/api/v1/projects", timeout=10)
        proj_items = (
            proj_resp.get("projects", proj_resp.get("items", []))
            if isinstance(proj_resp, dict)
            else (proj_resp if isinstance(proj_resp, list) else [])
        )
        by_slug = {
            str(p.get("slug")): str(p.get("id") or p.get("slug"))
            for p in proj_items
            if isinstance(p, dict) and p.get("slug")
        }

        slugs = [str((spec.get("project") or {}).get("slug") or "")]
        if scion_factory.has_eval_project(spec):
            slugs.append(str((spec.get("project") or {}).get("eval_slug") or ""))
        for slug in [s for s in slugs if s]:
            pid = by_slug.get(slug, slug)
            _, ag_resp = client.request("GET", f"/api/v1/projects/{pid}/agents", timeout=10)
            ag_list = (
                ag_resp.get("agents", ag_resp.get("items", []))
                if isinstance(ag_resp, dict)
                else (ag_resp if isinstance(ag_resp, list) else [])
            )
            sanitized = _redact_secrets([a for a in ag_list if isinstance(a, dict)], secret_strings)
            hub_agents[slug] = sanitized
        _write_private_file(backup_dir / "hub-agents.json", json.dumps(hub_agents, indent=2) + "\n", mode=0o600)
    except Exception as exc:
        warnings.append(_redact_secrets(f"Hub unreachable while archiving agent list: {exc}", secret_strings))
    return hub_agents


def _ensure_pod_dir(backup_dir: Path, agent_slug: str, pod_name: str) -> Path:
    pods_root = backup_dir / "pods"
    agent_dir = pods_root / agent_slug
    pod_dir = agent_dir / pod_name
    pod_dir.mkdir(parents=True, exist_ok=True)
    for d in (pods_root, agent_dir, pod_dir):
        os.chmod(d, 0o700)
    return pod_dir


def _write_pod_artifact_monotonic(dest_file: Path, content: str, mode: int = 0o600) -> None:
    encoded_len = len(content.encode("utf-8"))
    if dest_file.is_file() and dest_file.stat().st_size > encoded_len:
        return
    _write_private_file(dest_file, content, mode=mode)


def _archive_hub_scratchpad_telemetry(
    eff_env: dict[str, str],
    spec: dict[str, Any],
    backup_dir: Path,
    secret_strings: list[str],
    warnings: list[str],
) -> None:
    hub_url = (eff_env.get("HUB_URL") or "").strip()
    if not hub_url:
        return

    try:
        client = HubClient.from_env(eff_env)
        _, proj_resp = client.request("GET", "/api/v1/projects", timeout=10)
        proj_items = (
            proj_resp.get("projects", proj_resp.get("items", []))
            if isinstance(proj_resp, dict)
            else (proj_resp if isinstance(proj_resp, list) else [])
        )
        by_slug = {
            str(p.get("slug")): str(p.get("id") or p.get("slug"))
            for p in proj_items
            if isinstance(p, dict) and p.get("slug")
        }

        slugs = [str((spec.get("project") or {}).get("slug") or "")]
        if scion_factory.has_eval_project(spec):
            slugs.append(str((spec.get("project") or {}).get("eval_slug") or ""))

        for slug in [s for s in slugs if s]:
            pid = by_slug.get(slug, slug)
            try:
                qs = urllib.parse.urlencode({"q": r"^\.telemetry/", "limit": "2000"})
                _, list_resp = client.request(
                    "GET",
                    f"/api/v1/projects/{pid}/shared-dirs/scratchpad/files?{qs}",
                    timeout=20,
                )
            except Exception as exc:
                if getattr(exc, "status", None) == 404:
                    continue
                warnings.append(_redact_secrets(f"Could not list scratchpad .telemetry for {slug}: {exc}", secret_strings))
                continue

            file_items = (
                list_resp.get("files", list_resp.get("items", []))
                if isinstance(list_resp, dict)
                else (list_resp if isinstance(list_resp, list) else [])
            )
            for f_entry in file_items:
                if not isinstance(f_entry, dict):
                    continue
                rel_path = str(f_entry.get("path") or f_entry.get("name") or "").strip().lstrip("/")
                if not rel_path.startswith(".telemetry/"):
                    continue
                parts = Path(rel_path).parts
                if len(parts) < 4 or any(seg in ("", ".", "..") for seg in parts[1:]):
                    continue
                _, agent_slug, pod_name, *rest = parts
                quoted = urllib.parse.quote(rel_path, safe="/")
                try:
                    _, fetched = client.request(
                        "GET",
                        f"/api/v1/projects/{pid}/shared-dirs/scratchpad/files/{quoted}",
                        timeout=30,
                    )
                except Exception as exc:
                    if getattr(exc, "status", None) != 404:
                        warnings.append(
                            _redact_secrets(f"Failed downloading scratchpad {rel_path}: {exc}", secret_strings)
                        )
                    continue

                if isinstance(fetched, str):
                    raw_text = fetched
                elif isinstance(fetched, bytes):
                    raw_text = fetched.decode("utf-8", errors="replace")
                elif isinstance(fetched, dict):
                    if not fetched:
                        raw_text = ""
                    elif "content" in fetched and ("encoding" in fetched or "path" in fetched):
                        raw_text = str(fetched.get("content") or "")
                    else:
                        raw_text = json.dumps(fetched) + "\n"
                else:
                    raw_text = str(fetched)

                if not raw_text:
                    continue
                pod_dir = _ensure_pod_dir(backup_dir, agent_slug, pod_name)
                dest_file = pod_dir.joinpath(*rest)
                _write_pod_artifact_monotonic(
                    dest_file,
                    _redact_secrets(raw_text, secret_strings),
                    mode=0o600,
                )
    except Exception as exc:
        warnings.append(_redact_secrets(f"Hub unreachable while archiving scratchpad .telemetry: {exc}", secret_strings))


def _archive_k8s_logs_and_transcripts(
    eff_env: dict[str, str],
    backup_dir: Path,
    secret_strings: list[str],
    warnings: list[str],
) -> None:
    ns = (eff_env.get("HUB_K8S_NAMESPACE") or "").strip()
    if not ns:
        return

    cluster = (eff_env.get("HUB_K8S_CLUSTER") or "").strip()
    hub_project = (eff_env.get("HUB_PROJECT") or "").strip()
    hub_region = (eff_env.get("HUB_REGION") or "").strip()
    cmd_env = {**os.environ, "CLOUDSDK_CORE_DISABLE_PROMPTS": "1"}

    if cluster and hub_project and hub_region:
        try:
            subprocess.run(
                [
                    "gcloud",
                    "container",
                    "clusters",
                    "get-credentials",
                    cluster,
                    f"--project={hub_project}",
                    f"--region={hub_region}",
                ],
                env=cmd_env,
                capture_output=True,
                text=True,
                timeout=25,
            )
        except Exception as exc:
            warnings.append(_redact_secrets(f"Could not refresh GKE cluster credentials: {exc}", secret_strings))

    try:
        r_pods = subprocess.run(
            ["kubectl", "get", "pods", "-n", ns, "-o", "json"],
            env=cmd_env,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception as exc:
        warnings.append(_redact_secrets(f"kubectl unavailable while archiving pods: {exc}", secret_strings))
        return

    if r_pods.returncode != 0:
        msg = (r_pods.stderr or r_pods.stdout or f"exit {r_pods.returncode}").strip()
        warnings.append(_redact_secrets(f"Kubernetes cluster unreachable in namespace {ns}: {msg}", secret_strings))
        return

    try:
        pods_payload = json.loads(r_pods.stdout or "{}")
    except json.JSONDecodeError:
        pods_payload = {}

    items = pods_payload.get("items") if isinstance(pods_payload, dict) else []

    def _run_k8s(cmd: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(cmd, env=cmd_env, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            warnings.append(_redact_secrets(f"kubectl command failed ({cmd[1]}): {exc}", secret_strings))
            return None

    for item in items or []:
        if not isinstance(item, dict):
            continue
        meta = item.get("metadata") or {}
        pod_name = str(meta.get("name") or "").strip()
        if not pod_name:
            continue
        labels = meta.get("labels") if isinstance(meta.get("labels"), dict) else {}

        is_cron = pod_name.startswith("scion-factory-") or "job-name" in labels
        if is_cron:
            cron_dir = backup_dir / "cronjobs"
            cron_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(cron_dir, 0o700)
            r_log = _run_k8s(["kubectl", "logs", "-n", ns, pod_name, "--tail=2000"], timeout=15)
            if r_log is not None and r_log.returncode == 0 and r_log.stdout:
                _write_private_file(
                    cron_dir / f"{pod_name}.log",
                    _redact_secrets(r_log.stdout, secret_strings),
                    mode=0o600,
                )
            continue

        agent_slug = str(
            labels.get("scion.dev/agent")
            or labels.get("scion.name")
            or labels.get("app.kubernetes.io/instance")
            or pod_name
        )
        pod_dir = _ensure_pod_dir(backup_dir, agent_slug, pod_name)

        r_klog = _run_k8s(["kubectl", "logs", "-n", ns, pod_name], timeout=20)
        if r_klog is not None and r_klog.returncode == 0 and r_klog.stdout:
            _write_pod_artifact_monotonic(
                pod_dir / "container.log",
                _redact_secrets(r_klog.stdout, secret_strings),
                mode=0o600,
            )

        r_alog = _run_k8s(
            [
                "kubectl",
                "exec",
                "-n",
                ns,
                pod_name,
                "--",
                "sh",
                "-c",
                "cat /home/scion/.scion/agent.log 2>/dev/null || cat ~/.scion/agent.log 2>/dev/null",
            ],
            timeout=20,
        )
        if r_alog is not None and r_alog.returncode == 0 and r_alog.stdout:
            _write_pod_artifact_monotonic(
                pod_dir / "agent.log",
                _redact_secrets(r_alog.stdout, secret_strings),
                mode=0o600,
            )

        r_find = _run_k8s(
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
            timeout=20,
        )
        if r_find is not None and r_find.returncode == 0 and r_find.stdout.strip():
            for remote_path in [ln.strip() for ln in r_find.stdout.splitlines() if ln.strip()]:
                r_cat = _run_k8s(
                    ["kubectl", "exec", "-n", ns, pod_name, "--", "sh", "-c", f"cat {shlex.quote(remote_path)}"],
                    timeout=30,
                )
                if r_cat is not None and r_cat.returncode == 0 and r_cat.stdout:
                    rel_dest = remote_path.removeprefix("/home/scion/").lstrip("/")
                    dest_file = pod_dir / rel_dest
                    _write_pod_artifact_monotonic(dest_file, _redact_secrets(r_cat.stdout, secret_strings), mode=0o600)


def _cleanup_github_artifacts(
    repo: str,
    *,
    cleanup_issue_label: str,
    cleanup_branch: str,
    github_token: str,
    api_base: str,
    secret_strings: list[str],
    warnings: list[str],
) -> tuple[list[int], bool]:
    closed_issues: list[int] = []
    branch_deleted = False
    norm_repo = _normalize_github_repo(repo)
    if not norm_repo:
        return closed_issues, branch_deleted

    base = (api_base or "https://api.github.com").rstrip("/")

    if cleanup_issue_label:
        try:
            _github_request(
                "POST",
                f"{base}/repos/{norm_repo}/labels",
                github_token=github_token,
                body={"name": "hillclimb-archive", "color": "cfd3d7"},
            )
        except urllib.error.HTTPError as err:
            if err.code not in (409, 422):
                warnings.append(_redact_secrets(f"GitHub label ensure returned HTTP {err.code}", secret_strings))
        except Exception as exc:
            warnings.append(_redact_secrets(f"GitHub label ensure failed: {exc}", secret_strings))

        try:
            qs = urllib.parse.urlencode({"state": "open", "labels": cleanup_issue_label, "per_page": "100"})
            _, items = _github_request(
                "GET",
                f"{base}/repos/{norm_repo}/issues?{qs}",
                github_token=github_token,
            )
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict) or "pull_request" in item:
                        continue
                    num = int(item.get("number") or 0)
                    if not num:
                        continue
                    cur_labels = []
                    for lbl in item.get("labels") or []:
                        name = str(lbl.get("name") if isinstance(lbl, dict) else lbl).strip()
                        if name and name not in cur_labels:
                            cur_labels.append(name)
                    if "hillclimb-archive" not in cur_labels:
                        cur_labels.append("hillclimb-archive")
                    _github_request(
                        "PATCH",
                        f"{base}/repos/{norm_repo}/issues/{num}",
                        github_token=github_token,
                        body={"state": "closed", "labels": cur_labels},
                    )
                    closed_issues.append(num)
        except Exception as exc:
            warnings.append(_redact_secrets(f"GitHub issue cleanup failed: {exc}", secret_strings))

        issue_nums_to_prune: set[int] = set(closed_issues)
        try:
            _, ref_items = _github_request(
                "GET",
                f"{base}/repos/{norm_repo}/git/matching-refs/heads/factory/issue-",
                github_token=github_token,
            )
            if isinstance(ref_items, list):
                for ref_obj in ref_items:
                    if not isinstance(ref_obj, dict):
                        continue
                    ref_name = str(ref_obj.get("ref") or "")
                    m_iss = re.match(r"^refs/heads/factory/issue-(\d+)$", ref_name)
                    if not m_iss:
                        continue
                    cand_num = int(m_iss.group(1))
                    if cand_num in issue_nums_to_prune:
                        continue
                    try:
                        _, iss_obj = _github_request(
                            "GET",
                            f"{base}/repos/{norm_repo}/issues/{cand_num}",
                            github_token=github_token,
                        )
                        if isinstance(iss_obj, dict):
                            iss_labels = {
                                str(l.get("name") if isinstance(l, dict) else l).strip()
                                for l in (iss_obj.get("labels") or [])
                            }
                            if cleanup_issue_label in iss_labels:
                                issue_nums_to_prune.add(cand_num)
                    except Exception:
                        pass
        except Exception:
            pass

        for num in sorted(issue_nums_to_prune):
            issue_br = f"factory/issue-{num}"
            if issue_br in PROTECTED_BRANCHES:
                continue
            try:
                _github_request(
                    "DELETE",
                    f"{base}/repos/{norm_repo}/git/refs/heads/{issue_br}",
                    github_token=github_token,
                )
            except urllib.error.HTTPError as err:
                if err.code not in (404, 422):
                    warnings.append(
                        _redact_secrets(f"GitHub issue branch delete ({issue_br}) failed: HTTP {err.code}", secret_strings)
                    )
            except Exception as exc:
                warnings.append(_redact_secrets(f"GitHub issue branch delete ({issue_br}) failed: {exc}", secret_strings))

    if cleanup_branch:
        quoted_branch = urllib.parse.quote(cleanup_branch.strip(), safe="/")
        try:
            status, _ = _github_request(
                "DELETE",
                f"{base}/repos/{norm_repo}/git/refs/heads/{quoted_branch}",
                github_token=github_token,
            )
            branch_deleted = status in (200, 202, 204)
        except urllib.error.HTTPError as err:
            if err.code in (404, 422):
                warnings.append(f"GitHub branch {cleanup_branch!r} already absent (HTTP {err.code})")
            else:
                warnings.append(_redact_secrets(f"GitHub branch delete failed: HTTP {err.code}", secret_strings))
        except Exception as exc:
            warnings.append(_redact_secrets(f"GitHub branch delete failed: {exc}", secret_strings))

    return closed_issues, branch_deleted


def gke_down(
    spec_path: str | Path,
    *,
    env_file: str | Path,
    backup_root: str | Path | None = None,
    cleanup_issue_label: str = "",
    cleanup_branch: str = "",
    delete_project: bool = False,
    confirm_project: str = "",
    apply: bool = False,
) -> dict[str, Any]:
    """Archive pod logs/transcripts, write `scorecard.json`, clean GitHub test artifacts, and delete ephemeral GCP project."""
    spec = scion_factory.load_spec(spec_path)
    eff_env = _load_lifecycle_env(env_file)

    hub_project = (eff_env.get("HUB_PROJECT") or "").strip()
    sandbox_project = (eff_env.get("SANDBOX_PROJECT") or hub_project).strip()
    build_slug = str((spec.get("project") or {}).get("slug") or "factory")

    # 1. Safety guards (always enforced before any side effects)
    if delete_project:
        if not hub_project or (
            not hub_project.startswith("scion-gke-hc") and confirm_project != hub_project
        ):
            raise ValueError(
                f"Refusing to delete non-ephemeral GCP project {hub_project!r}: "
                "HUB_PROJECT must start with 'scion-gke-hc' or confirm_project must match HUB_PROJECT."
            )

    if cleanup_branch:
        raw_branch = cleanup_branch.strip()
        norm_branch = raw_branch.removeprefix("refs/heads/").removeprefix("origin/")
        if raw_branch in PROTECTED_BRANCHES or norm_branch in PROTECTED_BRANCHES:
            raise ValueError(
                f"Refusing to delete protected branch {cleanup_branch!r} "
                f"(protected: {', '.join(sorted(PROTECTED_BRANCHES))})."
            )

    effective_apply = bool(apply) and not _is_dry_run_env()
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_label = hub_project or build_slug
    resolved_backup_root = (
        Path(backup_root).expanduser().resolve()
        if backup_root is not None
        else Path(os.path.expanduser("~/scion-backups")).resolve()
    )
    backup_dir = resolved_backup_root / f"{archive_label}-{ts}"

    vcs = spec.get("vcs") or {}
    repo = str(vcs.get("repo") or eff_env.get("GITHUB_REPO") or "").strip()

    if not effective_apply:
        return {
            "ok": True,
            "apply": False,
            "project_slug": build_slug,
            "hub_project": hub_project,
            "backup_dir": str(backup_dir),
            "would_delete_project": bool(delete_project),
            "would_cleanup_issue_label": cleanup_issue_label,
            "would_cleanup_branch": cleanup_branch,
            "warnings": [],
        }

    # 2. Create 0700 backup directory & archive Hub/K8s logs and transcripts
    resolved_backup_root.mkdir(parents=True, exist_ok=True)
    os.chmod(resolved_backup_root, 0o700)
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)

    secret_strings = _collect_secret_strings(eff_env)
    warnings: list[str] = []

    hub_agents = _archive_hub_agents(eff_env, spec, backup_dir, secret_strings, warnings)
    _archive_hub_scratchpad_telemetry(eff_env, spec, backup_dir, secret_strings, warnings)
    _archive_k8s_logs_and_transcripts(eff_env, backup_dir, secret_strings, warnings)
    metrics = extract_pod_and_transcript_metrics(backup_dir)

    # 3. Clean up GitHub test issues and remote branch when configured
    closed_issues: list[int] = []
    branch_deleted = False
    if (cleanup_issue_label or cleanup_branch) and repo:
        gh_token = provision_factory._resolve_github_token(eff_env)
        api_base = (eff_env.get("GITHUB_API_URL") or os.environ.get("GITHUB_API_URL") or "https://api.github.com").strip()
        closed_issues, branch_deleted = _cleanup_github_artifacts(
            repo,
            cleanup_issue_label=cleanup_issue_label,
            cleanup_branch=cleanup_branch,
            github_token=gh_token,
            api_base=api_base,
            secret_strings=secret_strings,
            warnings=warnings,
        )

    # 4. Delete ephemeral GCP project when requested
    project_deleted = False
    ok = True
    if delete_project:
        r_del = subprocess.run(
            ["gcloud", "projects", "delete", hub_project, "--quiet"],
            env={**os.environ, "CLOUDSDK_CORE_DISABLE_PROMPTS": "1"},
            capture_output=True,
            text=True,
        )
        if r_del.returncode == 0:
            project_deleted = True
        else:
            ok = False
            warnings.append(
                _redact_secrets(
                    f"gcloud projects delete {hub_project} failed: {(r_del.stderr or r_del.stdout).strip()}",
                    secret_strings,
                )
            )

    scorecard_path = backup_dir / "scorecard.json"
    raw_scorecard = {
        "ok": ok,
        "apply": True,
        "timestamp": ts,
        "project_slug": build_slug,
        "hub_project": hub_project,
        "sandbox_project": sandbox_project,
        "backup_dir": str(backup_dir),
        "scorecard_path": str(scorecard_path),
        "metrics": metrics,
        "hub_agents": hub_agents,
        "github_cleanup": {
            "repo": repo,
            "cleanup_issue_label": cleanup_issue_label,
            "closed_issues": closed_issues,
            "cleanup_branch": cleanup_branch,
            "branch_deleted": branch_deleted,
        },
        "project_deleted": project_deleted,
        "warnings": warnings,
    }
    safe_scorecard = _redact_secrets(raw_scorecard, secret_strings)
    _write_private_file(scorecard_path, json.dumps(safe_scorecard, indent=2) + "\n", mode=0o600)
    os.chmod(backup_dir, 0o700)

    return safe_scorecard
