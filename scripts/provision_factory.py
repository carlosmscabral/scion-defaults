import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from hub_client import HubClient

ROOT_DIR = Path(__file__).resolve().parent.parent


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
        default_ws = "per-agent" if runtime == "kubernetes" or vcs_enabled else "shared"
        ws_mode = eff_env.get("WORKSPACE_MODE", default_ws)
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
        if not eff_env.get("HUB_URL"):
            eff_env["HUB_LOCAL_URL"] = eff_env.get("HUB_LOCAL_URL", "http://127.0.0.1:8000")
        client = HubClient.from_env(eff_env)

        build_pid = spec.get("project", {}).get("slug", "")
        eval_pid = spec.get("project", {}).get("eval_slug", "")
        runtime = scion_factory.runtime_mode(spec)
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
                    "FIRESTORE_PROJECT",
                    "FIRESTORE_DATABASE",
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
        factory_projects = [p.strip() for p in eff_env.get("FACTORY_PROJECTS", "").split(",") if p.strip()]
        if eff_env.get("HUB_URL"):
            hub_preflight = _load_hub_preflight()
            report = hub_preflight.build_api_report(
                client,
                eff_env.get("PO_EMAIL", ""),
                [build_pid, eval_pid],
                None,
                factory_projects,
                factory_projects,
                github_token_present=bool(gh_token),
                runtime=runtime,
                k8s_ready=True,
            )
            if report.get("existing_projects"):
                factory_projects = list(dict.fromkeys(factory_projects + list(report["existing_projects"])))
            phases.append({"phase": "preflight", "ok": True})
        else:
            phases.append({"phase": "preflight", "ok": True, "local": True})

        # 2. sandbox (run when SANDBOX_PROJECT is configured and SKIP_SANDBOX is not set)
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
            eval_req = _build_project_payload(spec, eval_pid, "eval_branch", eff_env, gh_token, runtime=runtime)
            build_pid_id = client.create_project(build_req).get("id", build_pid)
            eval_pid_id = client.create_project(eval_req).get("id", eval_pid)
        phases.append({"phase": "projects", "ok": True})

        # 5. identity_and_profile
        sandbox_project = eff_env.get("SANDBOX_PROJECT", "")
        worker_sa_name = eff_env.get("WORKER_SA_NAME") or spec.get("infrastructure", {}).get("worker_sa_name") or "agent-worker"
        control_sa_name = eff_env.get("CONTROL_SA_NAME", "factory-control")
        deployer_sa_name = eff_env.get("DEPLOYER_SA_NAME", "")
        runtime_profile = eff_env.get("RUNTIME_PROFILE") or spec.get("infrastructure", {}).get("runtime_profile", "")

        worker_sa_email = eff_env.get("WORKER_SA_EMAIL") or (
            f"{worker_sa_name}@{sandbox_project}.iam.gserviceaccount.com" if sandbox_project else ""
        )
        control_sa_email = eff_env.get("CONTROL_SA_EMAIL") or (
            f"{control_sa_name}@{sandbox_project}.iam.gserviceaccount.com" if control_sa_name and sandbox_project else ""
        )
        deployer_sa_email = eff_env.get("DEPLOYER_SA_EMAIL") or (
            f"{deployer_sa_name}@{sandbox_project}.iam.gserviceaccount.com" if deployer_sa_name and sandbox_project else ""
        )
        sa_gcp_project = sandbox_project or eff_env.get("HUB_PROJECT", "")
        if not sa_gcp_project and ".iam.gserviceaccount.com" in worker_sa_email and "@" in worker_sa_email:
            sa_gcp_project = worker_sa_email.split("@", 1)[1].split(".iam.", 1)[0]

        id_mode = eff_env.get("GCP_IDENTITY_DEFAULT") or eff_env.get("GCP_IDENTITY_MODE") or "assign"
        docker_assign = runtime == "docker" and id_mode == "assign" and bool(
            eff_env.get("WORKER_SA_EMAIL") or eff_env.get("GCP_IDENTITY_DEFAULT") or eff_env.get("GCP_IDENTITY_MODE")
        ) and bool(worker_sa_email)

        if apply and (runtime == "kubernetes" or docker_assign):
            from hub_client import HubAPIError
            if control_sa_email and not client.find_user(control_sa_email):
                try:
                    client.request("POST", "/api/v1/users", body={"email": control_sa_email})
                except HubAPIError as exc:
                    if exc.status not in (403, 409, 422):
                        raise

            for pid in (build_pid_id, eval_pid_id):
                worker_sa_rec = client.register_project_service_account(pid, worker_sa_email, sa_gcp_project, name=worker_sa_name)
                worker_sa_uuid = worker_sa_rec.get("id", worker_sa_email)
                if scion_factory.deploy_mode(spec) != "none" and deployer_sa_email:
                    client.register_project_service_account(
                        pid, deployer_sa_email, sa_gcp_project, name=deployer_sa_name or deployer_sa_email.split("@", 1)[0]
                    )

                patch: dict = {"defaultGCPIdentityMode": id_mode}
                if id_mode == "assign":
                    patch["defaultGCPIdentityServiceAccountID"] = worker_sa_uuid
                if runtime_profile:
                    patch["activeProfile"] = runtime_profile
                    if id_mode == "assign":
                        patch["defaultGCPIdentityServiceAccountIDByProfile"] = {runtime_profile: worker_sa_uuid}
                client.put_project_settings(pid, patch)

                if control_sa_email:
                    try:
                        _, aroles_resp = client.request("GET", f"/api/v1/projects/{pid}/members/assignable-roles")
                        aroles = aroles_resp.get("items", []) if isinstance(aroles_resp, dict) else (aroles_resp or [])
                        role_id = next(
                            (r.get("id") for r in aroles if isinstance(r, dict) and r.get("name") == "project-admin"),
                            next((r.get("id") for r in aroles if isinstance(r, dict) and r.get("id")), ""),
                        )
                        if role_id:
                            _, cur_m = client.request("GET", f"/api/v1/projects/{pid}/members")
                            cur_items = cur_m.get("items", []) if isinstance(cur_m, dict) else (cur_m or [])
                            already = any(
                                isinstance(m, dict) and control_sa_email in (m.get("principalId"), m.get("email"), m.get("principalEmail"))
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
        phases.append({"phase": "identity_and_profile", "ok": True})

        # 6. shared_dirs_and_kp
        if apply:
            for pid in (build_pid_id, eval_pid_id):
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
                client.sync_shared_dir(build_pid_id, "knowledge-plane", str(kp_src_dir), apply=True)
                client.sync_shared_dir(eval_pid_id, "knowledge-plane", str(kp_src_dir), apply=True)
        phases.append({"phase": "shared_dirs_and_kp", "ok": True})

        # 7. harness_and_templates
        all_roles = [
            *spec.get("team", {}).get("build_roles", []),
            *spec.get("team", {}).get("eval_roles", []),
        ]
        used_harnesses = sorted({r.get("harness", "") for r in all_roles if r.get("harness")})

        if spec.get("monorepo", {}).get("subdir") or runtime == "kubernetes":
            cmd_harness = [sys.executable, str(ROOT_DIR / "scripts" / "make-factory-harness-configs.py")]
            if used_harnesses:
                cmd_harness.extend(["--harness", ",".join(used_harnesses)])
            if eff_env.get("HUB_URL"):
                cmd_harness.append("--via-api")
            if apply:
                cmd_harness.append("--apply")
            subprocess.run(cmd_harness, cwd=ROOT_DIR, env=sub_base_env, check=True, stdout=sys.stderr)

        b_roles = ",".join([r.get("role", "") for r in spec.get("team", {}).get("build_roles", []) if r.get("role")])
        e_roles = ",".join([r.get("role", "") for r in spec.get("team", {}).get("eval_roles", []) if r.get("role")])

        for pid, roles_str in [(build_pid, b_roles), (eval_pid, e_roles)]:
            if not roles_str:
                continue
            if not apply and pid not in factory_projects:
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

        # 8. control_plane
        merged_fp = ",".join(dict.fromkeys([*factory_projects, build_pid, eval_pid]))
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
            cmd_cp.append("--apply" if apply else "--dry-run")

            r_cp = subprocess.run(
                cmd_cp,
                cwd=ROOT_DIR,
                env={**sub_base_env, "FACTORY_PROJECTS": merged_fp},
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
            eval_lead_name = scion_factory._agent(spec, "eval-lead")
            build_kp_index = f"{scion_factory._ws(spec)}/knowledge-plane/INDEX.md"
            vcs = spec.get("vcs", {}) or {}
            vcs_enabled = vcs.get("enabled", True) is not False and bool(vcs.get("repo") or eff_env.get("GITHUB_REPO"))

            c_body: dict = {
                "name": lead_name,
                "template": "lead",
                "task": f"Read {build_kp_index} and stand by.",
            }
            if runtime == "kubernetes" and vcs_enabled:
                c_body["branch"] = _resolve_branch(spec, "build_branch", eff_env)
            if runtime_profile:
                c_body["profile"] = runtime_profile
            if harness_auth:
                c_body["harnessAuth"] = harness_auth
            client.ensure_agent_started(build_pid_id, lead_name, create_body=c_body, wait_sec=wait_sec, poll_sec=poll_sec)

            e_body: dict = {
                "name": eval_lead_name,
                "template": "eval-lead",
                "task": "Read /scion-volumes/knowledge-plane/INDEX.md and stand by for evaluation cycles.",
            }
            if runtime == "kubernetes" and vcs_enabled:
                e_body["branch"] = _resolve_branch(spec, "eval_branch", eff_env)
            if runtime_profile:
                e_body["profile"] = runtime_profile
            if harness_auth:
                e_body["harnessAuth"] = harness_auth
            client.ensure_agent_started(eval_pid_id, eval_lead_name, create_body=e_body, wait_sec=wait_sec, poll_sec=poll_sec)

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
            "factory_projects": [build_pid_id, eval_pid_id],
            "phases": phases,
        }
    except Exception as e:
        import traceback

        traceback.print_exc()
        return {"ok": False, "error": str(e), "phases": phases}
