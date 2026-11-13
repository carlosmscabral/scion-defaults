"""Deterministic 9-phase Scion Hub provisioning orchestrator for compiled factory kits.

This module creates Hub projects, registers service accounts, and injects project secrets.
Then it syncs shared directories, pushes templates, and starts lead schedules.
"""
import importlib.util
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from hub_client import HubAPIError, HubClient

ROOT_DIR = Path(__file__).resolve().parent.parent

_TELEMETRY_BLOCK_RE = re.compile(r"(?m)^telemetry:[ \t]*\n(?:[ \t]+.*\n?)*")


def _inject_cloud_telemetry(tpl_root: Path, gcp_project: str) -> list[str]:
    """Point every kit template's `telemetry:` block at GCP-native export for `gcp_project`.

    Scion turns this block into SCION_TELEMETRY_* / SCION_GCP_PROJECT_ID inside the agent container.
    The Hub env API cannot carry them: it rejects SCION_* names as reserved. The project ID is only
    known at provision time, so the compiled kit (git-ignored) is rewritten in place, idempotently.
    """
    if not gcp_project or not tpl_root.is_dir():
        return []
    block = (
        "telemetry:\n"
        "  enabled: true\n"
        "  cloud:\n"
        "    enabled: true\n"
        "    provider: gcp\n"
        f'    gcp_project_id: "{gcp_project}"\n'
    )
    changed: list[str] = []
    for path in sorted(tpl_root.glob("*/scion-agent.yaml")):
        text = path.read_text(encoding="utf-8")
        updated, count = _TELEMETRY_BLOCK_RE.subn(lambda _m: block, text, count=1)
        if count == 0:
            updated = text.rstrip("\n") + "\n" + block
        if updated != text:
            path.write_text(updated, encoding="utf-8")
            changed.append(path.parent.name)
    return changed


CONTROL_PLANE_ROLE_PREFERENCE = ("project-owner", "project-admin")


def _pick_control_plane_role(assignable_roles: list) -> tuple[str, str]:
    """Return (role_id, role_name) for the control-plane identity: the first preferred role the Hub
    offers, else the first assignable role (named "" when it has no name)."""
    by_name = {
        str(r.get("name") or ""): str(r.get("id")) for r in assignable_roles if isinstance(r, dict) and r.get("id")
    }
    for name in CONTROL_PLANE_ROLE_PREFERENCE:
        if name in by_name:
            return by_name[name], name
    for name, role_id in by_name.items():
        return role_id, name
    return "", ""


VERTEX_ENV_KEYS = ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "GOOGLE_CLOUD_REGION")


def _ensure_hub_cloud_telemetry(client, gcp_project: str) -> list[str]:
    """Configure Hub-level GCP Cloud Telemetry defaults via `/api/v1/admin/server-config`.

    Current Scion ignores template `telemetry:` blocks on Hub dispatch (`ptone/scion#4524`); brokers
    receive `SCION_TELEMETRY_CLOUD_*` and `SCION_GCP_PROJECT_ID` from the Hub's `telemetry` operational
    settings section instead. Project-scoped callers without `hub.config.update` receive a warning and
    rely on the Hub's bootstrap `settings.yaml`.
    """
    from hub_client import HubAPIError

    if not gcp_project:
        return []
    try:
        client.request(
            "PUT",
            "/api/v1/admin/server-config",
            body={
                "telemetry": {
                    "enabled": True,
                    "cloud": {
                        "enabled": True,
                        "provider": "gcp",
                        "gcp_project_id": gcp_project,
                    },
                }
            },
        )
    except HubAPIError as exc:
        if exc.status in (403, 404, 405):
            return [f"hub server-config telemetry not updated (HTTP {exc.status}); relying on bootstrap settings.yaml"]
        raise
    return []


def _ensure_vertex_env(client, pids: list[str], gcp_project: str, location: str) -> list[str]:
    """Give every factory project the Vertex AI env the Hub requires before it starts an agent.

    With `--harness-auth vertex-ai` the Hub answers 422 `missing_env_vars` unless it can resolve
    GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION for the agent (hillclimb Run 1, fourth attempt).
    setup-gke-hub.sh writes them at hub scope, but that call needs a Hub admin and only warns on
    failure. Project scope is written here, by the caller that owns the projects, and read back so
    a missing value fails provisioning with a clear message instead of a 422 at agent start.
    Returns warnings for the optional steps (hub scope, read-back unavailable).
    """
    from hub_client import HubAPIError

    warnings: list[str] = []
    if not gcp_project:
        raise RuntimeError("Cannot set Vertex AI env: HUB_PROJECT and SANDBOX_PROJECT are both empty.")
    wanted = {
        "GOOGLE_CLOUD_PROJECT": gcp_project,
        "GOOGLE_CLOUD_LOCATION": location,
        "GOOGLE_CLOUD_REGION": location,
    }
    for key, val in wanted.items():
        try:
            client.request(
                "PUT",
                f"/api/v1/env/{key}",
                body={"value": val, "scope": "hub", "injectionMode": "always"},
            )
        except HubAPIError as exc:
            warnings.append(f"hub-scope {key} not set (HTTP {exc.status}); relying on project scope")
    for pid in pids:
        for key, val in wanted.items():
            client.request(
                "PUT",
                f"/api/v1/env/{key}",
                body={"value": val, "scope": "project", "scopeId": pid, "injectionMode": "always"},
            )
        try:
            _, listed = client.request("GET", f"/api/v1/env?scope=project&scopeId={pid}")
        except HubAPIError as exc:
            warnings.append(f"could not read back project env for {pid} (HTTP {exc.status})")
            continue
        if not isinstance(listed, dict) or not isinstance(listed.get("envVars"), list):
            warnings.append(f"project env read-back for {pid} returned no envVars list")
            continue
        have = {str(e.get("key")): str(e.get("value")) for e in listed["envVars"] if isinstance(e, dict)}
        missing = [k for k, v in wanted.items() if have.get(k) != v]
        if missing:
            raise RuntimeError(
                f"Hub project {pid} is missing Vertex AI env after write: {', '.join(missing)}. "
                "Agents would fail to start with 422 missing_env_vars."
            )
    return warnings


def _load_env(env_file) -> dict:
    env_file = Path(env_file)
    env = {}
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("'\"")
    for k, v in os.environ.items():
        if v:
            env[k] = v
    return env


def _load_hub_preflight():
    spec = importlib.util.spec_from_file_location("hub_preflight", Path(__file__).parent / "hub-preflight.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _resolve_github_token(eff_env: dict) -> str:
    tok = eff_env.get("GITHUB_TOKEN", "").strip()
    if tok:
        return tok
    token_file = Path(os.path.expanduser(eff_env.get("GITHUB_TOKEN_FILE", "~/.factory-github-token")))
    if token_file.exists():
        return token_file.read_text().strip()
    return ""


def _resolve_branch(spec: dict, branch_key: str, eff_env: dict) -> str:
    vcs = spec.get("vcs", {}) or {}
    return (
        vcs.get(branch_key)
        or eff_env.get("BUILD_BRANCH" if branch_key == "build_branch" else "EVAL_BRANCH")
        or "main"
    )


def _wait_for_agent_tmux(
    client: HubClient, project_id: str, slug: str, wait_sec: int = 420, poll_sec: float = 2.0
) -> bool:
    deadline = time.time() + max(wait_sec, 1)
    while time.time() < deadline:
        try:
            rc, _ = client.exec_agent(project_id, slug, ["tmux", "has-session", "-t", "scion"], timeout=10)
            if rc == 0:
                return True
        except Exception:
            pass
        time.sleep(poll_sec)
    return False


def _build_project_payload(
    spec: dict, slug: str, branch_key: str, eff_env: dict, gh_token: str, runtime: str = "docker"
) -> dict:
    vcs = spec.get("vcs", {}) or {}
    repo = (vcs.get("repo") or eff_env.get("GITHUB_REPO") or "").strip()
    vcs_enabled = vcs.get("enabled", True) is not False and bool(repo)
    body: dict = {"slug": slug, "name": slug}
    if vcs_enabled:
        git_url = repo if repo.startswith("http") else f"https://github.com/{repo}"
        if not git_url.endswith(".git"):
            git_url += ".git"
        branch = _resolve_branch(spec, branch_key, eff_env)
        # L35: Default to per-agent workspaces whenever Git VCS is enabled to avoid .git/index.lock conflicts
        # across concurrent Docker containers. Only fallback to shared for local non-Git Docker workflows.
        default_ws = "per-agent" if (runtime == "kubernetes" or vcs_enabled) else "shared"
        ws_mode = eff_env.get("WORKSPACE_MODE", "").lower().strip() or default_ws
        body["gitRemote"] = git_url
        body["workspaceMode"] = ws_mode
        body["labels"] = {
            "scion.dev/default-branch": branch,
            "scion.dev/clone-url": git_url,
            "scion.dev/source-url": git_url.removesuffix(".git"),
        }
        if gh_token:
            body["githubToken"] = gh_token
    else:
        body["workspaceMode"] = eff_env.get("WORKSPACE_MODE", "shared")
    return body


def _resolve_identity_config(spec: dict, eff_env: dict, runtime: str) -> dict:
    """Resolve service-account emails, target GCP project, and Scion identity mode."""
    infra = spec.get("infrastructure", {}) or {}
    sandbox_project = eff_env.get("SANDBOX_PROJECT", "").strip()
    worker_sa_name = eff_env.get("WORKER_SA_NAME") or infra.get("worker_sa_name") or "agent-worker"
    control_sa_name = eff_env.get("CONTROL_SA_NAME", "factory-control")
    deployer_sa_name = eff_env.get("DEPLOYER_SA_NAME", "").strip()
    runtime_profile = eff_env.get("RUNTIME_PROFILE") or infra.get("runtime_profile", "")
    lead_profile = eff_env.get("LEAD_PROFILE") or ("remote" if runtime_profile == "spot" else runtime_profile)

    worker_sa_email = eff_env.get("WORKER_SA_EMAIL", "").strip() or (
        f"{worker_sa_name}@{sandbox_project}.iam.gserviceaccount.com" if sandbox_project else ""
    )
    control_sa_email = eff_env.get("CONTROL_SA_EMAIL", "").strip() or (
        f"{control_sa_name}@{sandbox_project}.iam.gserviceaccount.com" if control_sa_name and sandbox_project else ""
    )
    deployer_sa_email = eff_env.get("DEPLOYER_SA_EMAIL", "").strip() or (
        f"{deployer_sa_name}@{sandbox_project}.iam.gserviceaccount.com" if deployer_sa_name and sandbox_project else ""
    )

    sa_gcp_project = sandbox_project or eff_env.get("HUB_PROJECT", "").strip()
    if not sa_gcp_project:
        for candidate_email in (worker_sa_email, deployer_sa_email, control_sa_email):
            if ".iam.gserviceaccount.com" in candidate_email and "@" in candidate_email:
                sa_gcp_project = candidate_email.split("@", 1)[1].split(".iam.", 1)[0]
                break

    import scion_factory
    id_mode = scion_factory.effective_gcp_identity_mode(spec, eff_env) or "assign"
    explicit_docker_assign = bool(
        eff_env.get("WORKER_SA_EMAIL")
        or eff_env.get("GCP_IDENTITY_DEFAULT")
        or eff_env.get("GCP_IDENTITY_MODE")
        or infra.get("gcp_identity_mode") == "assign"
    )
    register_sas = runtime == "kubernetes" or (
        runtime == "docker" and id_mode == "assign" and explicit_docker_assign and bool(worker_sa_email)
    )
    return {
        "worker_sa_name": worker_sa_name,
        "worker_sa_email": worker_sa_email,
        "control_sa_email": control_sa_email,
        "deployer_sa_name": deployer_sa_name,
        "deployer_sa_email": deployer_sa_email,
        "sa_gcp_project": sa_gcp_project,
        "runtime_profile": runtime_profile,
        "lead_profile": lead_profile,
        "id_mode": id_mode,
        "register_sas": register_sas,
        "explicit_deployer_sa_id": eff_env.get("FACTORY_DEPLOYER_SA_ID", "").strip(),
        "single_sa_mode": eff_env.get("SINGLE_SA_MODE", "").strip().lower() in ("1", "true", "yes"),
    }


def _has_gcp_identity_role(spec: dict) -> bool:
    """Return True if any build or eval role declares a dedicated gcp_identity."""
    roles = [
        *(spec.get("team", {}).get("build_roles") or []),
        *(spec.get("team", {}).get("eval_roles") or []),
    ]
    return any(bool(r.get("gcp_identity")) for r in roles if isinstance(r, dict))


def _provision_project_identity_and_secrets(
    client: HubClient,
    pid: str,
    id_cfg: dict,
    gh_token: str,
    vcs_enabled: bool,
    needs_deployer_sa: bool,
    control_role_warnings: list[str] | None = None,
) -> None:
    """Register project service accounts and inject FACTORY_DEPLOYER_SA_ID and GitHub secrets.

    Phase 5 runs before Phase 9 starts the lead containers.
    Both leads boot with FACTORY_DEPLOYER_SA_ID, GITHUB_TOKEN, and GH_TOKEN already set.
    """
    deployer_sa_uuid = id_cfg["explicit_deployer_sa_id"]
    if id_cfg["register_sas"]:
        worker_rec = client.register_project_service_account(
            pid,
            id_cfg["worker_sa_email"],
            id_cfg["sa_gcp_project"],
            name=id_cfg["worker_sa_name"],
        )
        worker_sa_uuid = worker_rec.get("id", id_cfg["worker_sa_email"])
        if not deployer_sa_uuid and (id_cfg.get("single_sa_mode") or id_cfg.get("deployer_sa_email") == id_cfg.get("worker_sa_email")):
            deployer_sa_uuid = worker_sa_uuid

        if needs_deployer_sa and id_cfg["deployer_sa_email"]:
            dep_name = id_cfg["deployer_sa_name"] or id_cfg["deployer_sa_email"].split("@", 1)[0]
            dep_rec = client.register_project_service_account(
                pid,
                id_cfg["deployer_sa_email"],
                id_cfg["sa_gcp_project"],
                name=dep_name,
            )
            if not id_cfg["explicit_deployer_sa_id"]:
                deployer_sa_uuid = dep_rec.get("id", id_cfg["deployer_sa_email"])

        id_mode = id_cfg["id_mode"]
        runtime_profile = id_cfg["runtime_profile"]
        patch: dict = {"defaultGCPIdentityMode": id_mode}
        if id_mode == "assign":
            patch["defaultGCPIdentityServiceAccountID"] = worker_sa_uuid
        if runtime_profile:
            patch["activeProfile"] = runtime_profile
            if id_mode == "assign":
                lead_prof = id_cfg.get("lead_profile", "")
                sa_by_profile = {runtime_profile: worker_sa_uuid}
                if lead_prof and lead_prof != runtime_profile:
                    sa_by_profile[lead_prof] = worker_sa_uuid
                patch["defaultGCPIdentityServiceAccountIDByProfile"] = sa_by_profile
        client.put_project_settings(pid, patch)

        control_sa_email = id_cfg["control_sa_email"]
        if control_sa_email:
            if id_cfg["register_sas"] and not client.find_user(control_sa_email):
                try:
                    client.request("POST", "/api/v1/users", body={"email": control_sa_email})
                except HubAPIError as exc:
                    if exc.status not in (403, 409, 422):
                        raise
            try:
                _, aroles_resp = client.request("GET", f"/api/v1/projects/{pid}/members/assignable-roles")
                aroles = aroles_resp.get("items", []) if isinstance(aroles_resp, dict) else (aroles_resp or [])
                # The Hub grants project-owner, which lets the control plane manage everything (start, stop, secret).
                # The next best is project-admin.
                role_id, role_name = _pick_control_plane_role(aroles)
                if role_name != CONTROL_PLANE_ROLE_PREFERENCE[0]:
                    warn_msg = (
                        f"control-plane identity got Hub role {role_name or 'unknown'} on {pid}, "
                        f"not {CONTROL_PLANE_ROLE_PREFERENCE[0]}: exec, agent delete and lead rotation may be denied"
                    )
                    if control_role_warnings is not None:
                        control_role_warnings.append(warn_msg)
                    print(f"Warning: {warn_msg}", file=sys.stderr)

                if role_id:
                    _, cur_m = client.request("GET", f"/api/v1/projects/{pid}/members")
                    cur_items = cur_m.get("items", []) if isinstance(cur_m, dict) else (cur_m or [])
                    already = any(
                        isinstance(m, dict)
                        and control_sa_email in (m.get("principalId"), m.get("email"), m.get("principalEmail"))
                        for m in cur_items
                    )
                    if not already:
                        client.request(
                            "POST",
                            f"/api/v1/projects/{pid}/members",
                            body={
                                "roleDefinitionId": role_id,
                                "principalType": "user",
                                "principalId": control_sa_email,
                            },
                        )
            except HubAPIError as exc:
                if exc.status not in (400, 403, 404, 409):
                    raise
    elif needs_deployer_sa and id_cfg["deployer_sa_email"] and id_cfg["sa_gcp_project"] and not deployer_sa_uuid:
        # In Docker passthrough or block mode, still register an explicit DEPLOYER_SA when configured.
        dep_name = id_cfg["deployer_sa_name"] or id_cfg["deployer_sa_email"].split("@", 1)[0]
        dep_rec = client.register_project_service_account(
            pid,
            id_cfg["deployer_sa_email"],
            id_cfg["sa_gcp_project"],
            name=dep_name,
        )
        deployer_sa_uuid = dep_rec.get("id", id_cfg["deployer_sa_email"])

    if needs_deployer_sa and deployer_sa_uuid:
        client.put_project_env(pid, "FACTORY_DEPLOYER_SA_ID", deployer_sa_uuid)

    if vcs_enabled and gh_token:
        client.put_project_secret(pid, "GITHUB_TOKEN", gh_token)
        client.put_project_secret(pid, "GH_TOKEN", gh_token)

def provision_factory(
    spec: dict,
    kit_dir,
    env_file,
    seed_repo=None,
    apply: bool = False,
    send_go: bool = False,
) -> dict:
    phases = []
    try:
        import scion_factory

        kit_dir = Path(kit_dir).resolve()
        env_file = Path(env_file).resolve()
        if seed_repo:
            seed_repo = Path(seed_repo).resolve()
        eff_env = _load_env(env_file)
        eff_env["CLOUDSDK_CORE_DISABLE_PROMPTS"] = "1"

        has_eval = scion_factory.has_eval_project(spec)
        is_delta = scion_factory.wake_mode(spec) == "delta"
        norm_spec, _ = scion_factory.normalize_team(spec)

        build_pid = spec.get("project", {}).get("slug", "")
        eval_pid = spec.get("project", {}).get("eval_slug", "") if has_eval else ""
        target_slugs = [s for s in ([build_pid, eval_pid] if has_eval else [build_pid]) if s]
        runtime = scion_factory.runtime_mode(spec)
        if runtime == "kubernetes":
            if eff_env.get("HUB_PROJECT") and not eff_env.get("SANDBOX_PROJECT"):
                eff_env["SANDBOX_PROJECT"] = eff_env["HUB_PROJECT"]
            if not eff_env.get("SANDBOX_REGION"):
                eff_env["SANDBOX_REGION"] = eff_env.get("HUB_REGION") or "us-central1"
            if eff_env.get("SANDBOX_PROJECT") and not eff_env.get("FIRESTORE_PROJECT"):
                eff_env["FIRESTORE_PROJECT"] = eff_env["SANDBOX_PROJECT"]
            if eff_env.get("SANDBOX_PROJECT") and not eff_env.get("DATA_BUCKET"):
                eff_env["DATA_BUCKET"] = f"{eff_env['SANDBOX_PROJECT']}-factory-data"
            eff_env.setdefault("AGENT_RUNTIME", "kubernetes")

        if not eff_env.get("HUB_URL"):
            eff_env["HUB_LOCAL_URL"] = eff_env.get("HUB_LOCAL_URL", "http://127.0.0.1:8000")
        client = HubClient.from_env(eff_env)
        gh_token = _resolve_github_token(eff_env)

        # Shared subprocess env forwarding Hub auth + IAP headers + factory.env
        sub_base_env = {
            **os.environ,
            **{
                k: eff_env[k]
                for k in (
                    "HUB_URL",
                    "SCION_HUB_TOKEN",
                    "HUB_TOKEN",
                    "HUB_IAP_AUDIENCE",
                    "SCION_TRANSPORT_AUDIENCE",
                    "HUB_TRANSPORT_MODE",
                    "HUB_TRANSPORT_TOKEN",
                    "HUB_TRANSPORT_TOKEN_TYPE",
                    "HUB_IMPERSONATE_SA",
                    "HUB_PROJECT",
                    "HUB_REGION",
                    "HUB_NAME",
                    "HUB_CLOUDRUN_SERVICE",
                    "SANDBOX_PROJECT",
                    "SANDBOX_REGION",
                    "FIRESTORE_PROJECT",
                    "FIRESTORE_DATABASE",
                    "DATA_BUCKET",
                    "AGENT_RUNTIME",
                    "PO_EMAIL",
                )
                if eff_env.get(k)
            },
            "FACTORY_ENV": str(env_file),
            "CLOUDSDK_CORE_DISABLE_PROMPTS": "1",
        }
        if eff_env.get("HUB_TOKEN") and not sub_base_env.get("SCION_HUB_TOKEN"):
            sub_base_env["SCION_HUB_TOKEN"] = eff_env["HUB_TOKEN"]

        # 1. preflight
        factory_projects = [
            p.strip()
            for p in eff_env.get("FACTORY_PROJECTS", "").split(",")
            if p.strip() and p.strip() != "global"
        ]
        existing_hub_projects: set[str] = set()
        if eff_env.get("HUB_URL"):
            hub_preflight = _load_hub_preflight()
            report = hub_preflight.build_api_report(
                client,
                eff_env.get("PO_EMAIL", ""),
                target_slugs,
                None,
                factory_projects,
                factory_projects,
                github_token_present=bool(gh_token),
                runtime=runtime,
                k8s_ready=True,
            )
            existing_non_global = [p for p in (report.get("existing_projects") or []) if p and p != "global"]
            existing_hub_projects = set(existing_non_global)
            if existing_non_global:
                factory_projects = list(dict.fromkeys(factory_projects + existing_non_global))
            phases.append({"phase": "preflight", "ok": True})
        else:
            existing_hub_projects = set(factory_projects)
            phases.append({"phase": "preflight", "ok": True, "local": True})

        # 2. sandbox (skip when SANDBOX_PROJECT is unset or SKIP_SANDBOX=1)
        if eff_env.get("SANDBOX_PROJECT") and eff_env.get("SKIP_SANDBOX") != "1":
            sb_env = {**sub_base_env, "DRY_RUN": "0" if apply else "1"}
            if gh_token:
                sb_env["GITHUB_TOKEN"] = gh_token
            r_sb = subprocess.run(
                ["bash", "scripts/setup-sandbox.sh"],
                cwd=ROOT_DIR,
                env=sb_env,
                capture_output=True,
                text=True,
            )
            if r_sb.returncode == 0:
                phases.append({"phase": "sandbox", "ok": True})
            else:
                phases.append({"phase": "sandbox", "ok": False, "stderr": r_sb.stderr})
                return {"ok": False, "apply": apply, "phases": phases}
        else:
            phases.append({"phase": "sandbox", "ok": True, "skipped": True})

        # 3. seed_repo
        seed_script = kit_dir / "scripts" / "seed-github-waves.sh"
        if not seed_script.exists():
            seed_script = ROOT_DIR / "scripts" / "seed-github-waves.sh"
        if seed_repo and seed_script.exists():
            seed_env = {**sub_base_env, "DRY_RUN": "0" if apply else "1"}
            if gh_token:
                seed_env["GH_TOKEN"] = gh_token
            r_seed = subprocess.run(
                ["bash", str(seed_script)],
                cwd=seed_repo,
                env=seed_env,
                capture_output=True,
                text=True,
            )
            if r_seed.returncode == 0:
                phases.append({"phase": "seed_repo", "ok": True})
            else:
                phases.append({"phase": "seed_repo", "ok": False, "stderr": r_seed.stderr})
                return {"ok": False, "apply": apply, "phases": phases}
        else:
            phases.append({"phase": "seed_repo", "ok": True, "skipped": True})

        build_pid_id = build_pid
        eval_pid_id = eval_pid
        # 4. projects
        if apply:
            build_req = _build_project_payload(spec, build_pid, "build_branch", eff_env, gh_token, runtime=runtime)
            build_pid_id = client.create_project(build_req).get("id", build_pid)
            if has_eval:
                eval_req = _build_project_payload(spec, eval_pid, "eval_branch", eff_env, gh_token, runtime=runtime)
                eval_pid_id = client.create_project(eval_req).get("id", eval_pid)
        target_pids = [build_pid_id, eval_pid_id] if has_eval else [build_pid_id]
        phases.append({"phase": "projects", "ok": True})

        # 5. identity_and_profile
        id_cfg = _resolve_identity_config(spec, eff_env, runtime)
        vcs = spec.get("vcs", {}) or {}
        vcs_enabled = vcs.get("enabled", True) is not False and bool(vcs.get("repo") or eff_env.get("GITHUB_REPO"))
        needs_deployer_sa = scion_factory.deploy_mode(spec) != "none" or _has_gcp_identity_role(spec)
        control_sa_email = id_cfg["control_sa_email"]
        lead_profile = id_cfg["lead_profile"]

        vertex_env_warnings: list[str] = []
        control_role_warnings: list[str] = []
        if apply:
            telemetry_gcp_project = eff_env.get("SANDBOX_PROJECT") or eff_env.get("HUB_PROJECT") or id_cfg["sa_gcp_project"]
            if runtime == "kubernetes":
                telemetry_warnings = _ensure_hub_cloud_telemetry(
                    client,
                    eff_env.get("HUB_PROJECT") or telemetry_gcp_project,
                )
                vertex_env_warnings = _ensure_vertex_env(
                    client,
                    list(target_pids),
                    eff_env.get("HUB_PROJECT") or id_cfg["sa_gcp_project"],
                    eff_env.get("VERTEX_LOCATION") or eff_env.get("GOOGLE_CLOUD_LOCATION") or "global",
                )
                vertex_env_warnings.extend(telemetry_warnings)
                for w in vertex_env_warnings:
                    print(f"Warning: {w}", file=sys.stderr)

            for pid in target_pids:
                _provision_project_identity_and_secrets(
                    client,
                    pid,
                    id_cfg,
                    gh_token,
                    vcs_enabled=vcs_enabled,
                    needs_deployer_sa=needs_deployer_sa,
                    control_role_warnings=control_role_warnings,
                )
        phase_identity: dict = {"phase": "identity_and_profile", "ok": True}
        if runtime == "kubernetes" and apply and (vertex_env_warnings or control_role_warnings):
            phase_identity["warnings"] = [*vertex_env_warnings, *control_role_warnings]
        phases.append(phase_identity)

        # 6. shared_dirs_and_kp
        if apply:
            for pid in target_pids:
                _st, sdirs_resp = client.request("GET", f"/api/v1/projects/{pid}/shared-dirs")
                sdirs_list = (
                    sdirs_resp.get("sharedDirs", [])
                    if isinstance(sdirs_resp, dict)
                    else (sdirs_resp if isinstance(sdirs_resp, list) else [])
                )
                existing_names = {d.get("name") for d in sdirs_list if isinstance(d, dict)}
                if "scratchpad" not in existing_names:
                    client.request("POST", f"/api/v1/projects/{pid}/shared-dirs", body={"name": "scratchpad"})
                if "knowledge-plane" not in existing_names:
                    client.request(
                        "POST",
                        f"/api/v1/projects/{pid}/shared-dirs",
                        body={"name": "knowledge-plane", "read_only": True},
                    )

            kp_src_dir = kit_dir / "knowledge-plane"
            if not kp_src_dir.exists() and seed_repo:
                sub = spec.get("monorepo", {}).get("subdir", "")
                cand = (seed_repo / sub / "knowledge-plane") if sub else (seed_repo / "knowledge-plane")
                kp_src_dir = cand if cand.exists() else (seed_repo / "knowledge-plane")
            if kp_src_dir.exists():
                for pid in target_pids:
                    client.sync_shared_dir(pid, "knowledge-plane", str(kp_src_dir), apply=True)
        phases.append({"phase": "shared_dirs_and_kp", "ok": True})

        # 7. harness_and_templates
        b_role_entries = norm_spec.get("team", {}).get("build_roles", [])
        e_role_entries = (norm_spec.get("team", {}).get("eval_roles") or []) if has_eval else []
        all_roles = [*b_role_entries, *e_role_entries]
        used_harnesses = sorted({r.get("harness", "") for r in all_roles if r.get("harness")})

        if spec.get("monorepo", {}).get("subdir") or runtime == "kubernetes":
            if not (runtime == "kubernetes" and not apply and not eff_env.get("HUB_URL")):
                cmd_harness = [sys.executable, str(ROOT_DIR / "scripts" / "make-factory-harness-configs.py")]
                if used_harnesses:
                    cmd_harness.extend(["--harness", ",".join(used_harnesses)])
                if eff_env.get("HUB_URL"):
                    cmd_harness.append("--via-api")
                if apply:
                    cmd_harness.append("--apply")
                subprocess.run(cmd_harness, cwd=ROOT_DIR, env=sub_base_env, check=True, stdout=sys.stderr)

        tpl_root = kit_dir / "templates"
        if runtime == "kubernetes" and apply:
            _inject_cloud_telemetry(tpl_root, eff_env.get("HUB_PROJECT") or id_cfg["sa_gcp_project"])

        def _roles_csv(entries: list[dict]) -> str:
            names = [r.get("role", "") for r in entries if r.get("role")]
            if tpl_root.is_dir():
                present = [n for n in names if (tpl_root / n).is_dir()]
                if present:
                    names = present
            return ",".join(names)

        b_roles = _roles_csv(b_role_entries)
        e_roles = _roles_csv(e_role_entries) if has_eval else ""

        push_targets = [(build_pid, b_roles)] + ([(eval_pid, e_roles)] if has_eval else [])
        for pid, roles_str in push_targets:
            if not pid or not roles_str:
                continue
            if not apply and pid not in existing_hub_projects:
                continue
            cmd_push = [
                sys.executable,
                str(ROOT_DIR / "scripts" / "push-templates.py"),
                "--project",
                pid,
                "--roles",
                roles_str,
                "--templates",
                str(kit_dir / "templates"),
            ]
            if eff_env.get("HUB_URL"):
                cmd_push.append("--direct")
            if apply:
                cmd_push.append("--apply")
            subprocess.run(cmd_push, cwd=ROOT_DIR, env=sub_base_env, check=True, stdout=sys.stderr)
        phases.append({"phase": "harness_and_templates", "ok": True})

        # 8. schedules_and_leads (start lead BEFORE installing K8s delta CronJob so cold Autopilot pod boot
        #    never races the 1-minute scion-factory-delta tick) & 9. control_plane
        lead_started = False
        if apply and is_delta and runtime == "kubernetes":
            wait_sec = int(eff_env.get("AGENT_START_TIMEOUT_SEC", "420"))
            poll_sec = float(eff_env.get("AGENT_START_POLL_SEC", "2.0"))
            harness_auth = eff_env.get("HARNESS_AUTH", "vertex-ai")
            lead_name = scion_factory._agent(spec, "lead")
            build_kp_index = "/scion-volumes/knowledge-plane/INDEX.md"
            vcs = spec.get("vcs", {}) or {}
            vcs_enabled = vcs.get("enabled", True) is not False and bool(vcs.get("repo") or eff_env.get("GITHUB_REPO"))
            c_body: dict = {
                "name": lead_name,
                "template": "lead",
                "task": f"Read {build_kp_index} and stand by.",
            }
            if vcs_enabled:
                c_body["branch"] = _resolve_branch(spec, "build_branch", eff_env)
            if lead_profile:
                c_body["profile"] = lead_profile
            if harness_auth:
                c_body["harnessAuth"] = harness_auth
            client.ensure_agent_started(build_pid_id, lead_name, create_body=c_body, wait_sec=wait_sec, poll_sec=poll_sec)
            _wait_for_agent_tmux(client, build_pid_id, lead_name, wait_sec=wait_sec, poll_sec=poll_sec)
            lead_started = True

        # 8. control_plane
        merged_fp = ",".join(dict.fromkeys([p for p in [*factory_projects, *target_slugs] if p and p != "global"]))
        if runtime == "kubernetes":
            cmd_cp = [sys.executable, str(ROOT_DIR / "scripts" / "install-k8s-control-plane.py")]
            if eff_env.get("HUB_K8S_NAMESPACE"):
                cmd_cp.extend(["--namespace", eff_env["HUB_K8S_NAMESPACE"]])
                cmd_cp.extend(["--gsa-email", control_sa_email])
            if eff_env.get("HUB_URL"):
                cmd_cp.extend(["--hub-url", eff_env["HUB_URL"]])
            if eff_env.get("SANDBOX_PROJECT"):
                cmd_cp.extend(["--sandbox-project", eff_env["SANDBOX_PROJECT"]])
            if eff_env.get("FIRESTORE_PROJECT") or eff_env.get("SANDBOX_PROJECT"):
                cmd_cp.extend(["--firestore-project", eff_env.get("FIRESTORE_PROJECT") or eff_env["SANDBOX_PROJECT"]])
            iap_aud = eff_env.get("HUB_IAP_AUDIENCE") or eff_env.get("SCION_TRANSPORT_AUDIENCE") or ""
            if iap_aud:
                cmd_cp.extend(["--iap-audience", iap_aud])
            cmd_cp.extend(["--factory-projects", merged_fp])
            if is_delta:
                vcs = spec.get("vcs", {}) or {}
                gh_repo = (vcs.get("repo") or eff_env.get("GITHUB_REPO") or "").strip()
                cmd_cp.append("--enable-delta")
                if gh_repo:
                    cmd_cp.extend(["--github-repo", gh_repo])
                cmd_cp.extend(["--issue-label", scion_factory._factory_label(spec)])
                cmd_cp.extend(["--lead-slug", scion_factory._agent(spec, "lead")])
                cmd_cp.extend(["--build-branch", _resolve_branch(spec, "build_branch", eff_env)])
                if lead_profile:
                    cmd_cp.extend(["--runtime-profile", lead_profile])
            cmd_cp.append("--apply" if apply else "--dry-run")

            cp_env = {**sub_base_env, "FACTORY_PROJECTS": merged_fp}
            if gh_token:
                cp_env["GITHUB_TOKEN"] = gh_token

            r_cp = subprocess.run(
                cmd_cp,
                cwd=ROOT_DIR,
                env=cp_env,
                capture_output=True,
                text=True,
            )
            if r_cp.returncode == 0:
                phases.append({"phase": "control_plane", "ok": True})
            elif "kubectl: command not found" in r_cp.stderr or not eff_env.get("HUB_K8S_NAMESPACE"):
                phases.append({"phase": "control_plane", "ok": True, "rendered_only": True})
            else:
                phases.append({"phase": "control_plane", "ok": False, "stderr": r_cp.stderr})
                return {"ok": False, "apply": apply, "phases": phases}
        else:
            phases.append({"phase": "control_plane", "ok": True})

        # 9. schedules_and_leads (start leads FIRST so schedule authoring finds target lead agent)
        if apply:
            wait_sec = int(eff_env.get("AGENT_START_TIMEOUT_SEC", "420"))
            poll_sec = float(eff_env.get("AGENT_START_POLL_SEC", "2.0"))
            harness_auth = eff_env.get("HARNESS_AUTH", "vertex-ai" if runtime == "kubernetes" else "")
            lead_name = scion_factory._agent(spec, "lead")
            build_kp_index = (
                "/scion-volumes/knowledge-plane/INDEX.md"
                if runtime == "kubernetes"
                else f"{scion_factory._ws(spec)}/knowledge-plane/INDEX.md"
            )
            vcs = spec.get("vcs", {}) or {}
            vcs_enabled = vcs.get("enabled", True) is not False and bool(vcs.get("repo") or eff_env.get("GITHUB_REPO"))

            if not lead_started:
                c_body = {
                    "name": lead_name,
                    "template": "lead",
                    "task": f"Read {build_kp_index} and stand by.",
                }
                if runtime == "kubernetes" and vcs_enabled:
                    c_body["branch"] = _resolve_branch(spec, "build_branch", eff_env)
                if lead_profile:
                    c_body["profile"] = lead_profile
                if harness_auth:
                    c_body["harnessAuth"] = harness_auth
                client.ensure_agent_started(build_pid_id, lead_name, create_body=c_body, wait_sec=wait_sec, poll_sec=poll_sec)

            if has_eval:
                eval_lead_name = scion_factory._agent(spec, "eval-lead")
                e_body: dict = {
                    "name": eval_lead_name,
                    "template": "eval-lead",
                    "task": "Read /scion-volumes/knowledge-plane/INDEX.md and stand by for evaluation cycles.",
                }
                if runtime == "kubernetes" and vcs_enabled:
                    e_body["branch"] = _resolve_branch(spec, "eval_branch", eff_env)
                if lead_profile:
                    e_body["profile"] = lead_profile
                if harness_auth:
                    e_body["harnessAuth"] = harness_auth
                client.ensure_agent_started(eval_pid_id, eval_lead_name, create_body=e_body, wait_sec=wait_sec, poll_sec=poll_sec)

            if not is_delta:
                scheds_resp = client.request("GET", f"/api/v1/projects/{build_pid_id}/schedules")[1]
                scheds = scheds_resp.get("schedules", []) if isinstance(scheds_resp, dict) else (scheds_resp or [])
                sched_name = f"{build_pid}-lead-heartbeat"
                hb_path = kit_dir / "config" / "heartbeat-prompt.txt"
                if not hb_path.exists():
                    hb_path = ROOT_DIR / "config" / "heartbeat-prompt.txt"
                hb_msg = hb_path.read_text().strip() if hb_path.exists() else "Heartbeat: check teammates, PRs, and Wave progress."
                cron_expr = spec.get("team", {}).get("heartbeat_cron") or eff_env.get("HEARTBEAT_CRON", "*/15 * * * *")
                if not any(isinstance(s, dict) and s.get("name") == sched_name for s in scheds):
                    client.request(
                        "POST",
                        f"/api/v1/projects/{build_pid_id}/schedules",
                        body={
                            "name": sched_name,
                            "cronExpr": cron_expr,
                            "eventType": "message",
                            "agentName": lead_name,
                            "message": hb_msg,
                        },
                    )

            eval_cron = (
                (spec.get("team") or {}).get("eval_heartbeat_cron")
                or eff_env.get("EVAL_HEARTBEAT_CRON", "")
            ).strip()
            if eval_pid and eval_pid_id and eval_cron:
                eval_scheds_resp = client.request("GET", f"/api/v1/projects/{eval_pid_id}/schedules")[1]
                eval_scheds = (
                    eval_scheds_resp.get("schedules", [])
                    if isinstance(eval_scheds_resp, dict)
                    else (eval_scheds_resp or [])
                )
                eval_sched_name = f"{eval_pid}-lead-heartbeat"
                eval_hb_msg = (
                    "Evaluation sweep check: inspect /scion-volumes/knowledge-plane/state/LIVE.md and "
                    "list-eval-runs, and run an L1-L5 sweep if a newer revision has shipped."
                )
                if not any(isinstance(s, dict) and s.get("name") == eval_sched_name for s in eval_scheds):
                    client.request(
                        "POST",
                        f"/api/v1/projects/{eval_pid_id}/schedules",
                        body={
                            "name": eval_sched_name,
                            "cronExpr": eval_cron,
                            "eventType": "message",
                            "agentName": eval_lead_name,
                            "message": eval_hb_msg,
                        },
                    )

            if send_go:
                _wait_for_agent_tmux(client, build_pid_id, lead_name, wait_sec=wait_sec, poll_sec=poll_sec)
                client.request(
                    "POST",
                    f"/api/v1/projects/{build_pid_id}/agents/{lead_name}/message",
                    body={"message": f"GO: start Wave 1 execution per {build_kp_index}."},
                )

        phases.append({"phase": "schedules_and_leads", "ok": True})

        return {
            "ok": True,
            "apply": apply,
            "runtime": runtime,
            "factory_projects": target_pids,
            "phases": phases,
        }
    except Exception as e:
        import traceback

        traceback.print_exc()
        return {"ok": False, "error": str(e), "phases": phases}
