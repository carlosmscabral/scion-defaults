import json
import os
import subprocess
import sys
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts')))
import scion_factory
from test_hub_client import fake_hub, CrossOriginHandler, get_free_port

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

def _starter():
    import yaml
    with open(os.path.join(ROOT, "presets", "starter-service.factory.yaml"), "r") as f:
        return yaml.safe_load(f)

def write_gke_factory_env(tmp_path, hub_url):
    env_file = tmp_path / "factory.env"
    env_file.write_text(f"""
HUB_URL={hub_url}
HUB_IAP_AUDIENCE=123-abc.apps.googleusercontent.com
HUB_TRANSPORT_TOKEN=fake-iap-oidc
SCION_HUB_TOKEN=fake-pat
SANDBOX_PROJECT=my-sandbox
SANDBOX_REGION=us-central1
FIRESTORE_PROJECT=my-sandbox
WORKER_SA_NAME=agent-worker
CONTROL_SA_NAME=factory-control
DEPLOYER_SA_NAME=deploy-sa
RUNTIME_SA_NAME=runtime-sa
HUB_PROJECT=my-hub
HUB_K8S_NAMESPACE=tfha-e2e
HUB_SA=my-hub-sa
DATA_BUCKET=my-bucket
AGENT_START_TIMEOUT_SEC=5
AGENT_START_POLL_SEC=0.05
""")
    return env_file

def fake_cli_env(tmp_path, hub_url):
    env = os.environ.copy()
    env["HUB_URL"] = hub_url
    env["GITHUB_TOKEN"] = "gh_fake_pat_for_test"
    
    # Fake binaries so scripts don't hit GCP or k8s, while logging kubectl args
    bin_dir = tmp_path / "fake_bin"
    bin_dir.mkdir(exist_ok=True)
    klog = tmp_path / "kubectl.log"
    for b in ("gcloud", "make", "git"):
        f = bin_dir / b
        f.write_text("#!/bin/bash\nexit 0\n")
        f.chmod(0o755)
    kf = bin_dir / "kubectl"
    kf.write_text(f"#!/bin/bash\necho \"$@\" >> \"{klog}\"\ncat >> \"{klog}\"\necho '' >> \"{klog}\"\nexit 0\n")
    kf.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    return env

def test_bad_infrastructure_runtime_fails_readiness_while_kubernetes_compiles_gke_env(tmp_path):
    spec = _starter()
    spec["infrastructure"]["runtime"] = "ecs"
    report = scion_factory.evaluate_spec_readiness(spec)
    assert report["ready"] is False
    assert any("infrastructure.runtime" in m for m in report["missing_or_weak"])

    spec["infrastructure"]["runtime"] = "kubernetes"
    spec["infrastructure"]["runtime_profile"] = "gke"
    for r in spec["team"]["build_roles"]:
        if r["role"] == "lead":
            r["harness"] = "antigravity"
            r["model"] = "Gemini 4 Argon (High)"
    assert scion_factory.evaluate_spec_readiness(spec)["ready"] is True
    scion_factory.compile_factory_boilerplate(spec, tmp_path / "kit")
    env_example = (tmp_path / "kit" / "config" / "factory.env.example").read_text()
    assert "AGENT_RUNTIME=kubernetes" in env_example
    assert "RUNTIME_PROFILE=gke" in env_example
    assert "WORKER_SA_NAME=agent-worker" in env_example
    lead_yaml = (tmp_path / "kit" / "templates" / "lead" / "scion-agent.yaml").read_text()
    assert 'GOOGLE_CLOUD_LOCATION: "global"' in lead_yaml
    assert "default_harness_config: antigravity-factory" in lead_yaml

def test_scion_factory_provision_dry_run_and_apply_against_gke_fake_hub(fake_hub, tmp_path):
    spec = _starter()
    spec["project"]["slug"] = "starter-service"
    spec["project"]["eval_slug"] = "starter-service-eval"
    spec["infrastructure"]["runtime"] = "kubernetes"
    spec["infrastructure"]["runtime_profile"] = "gke"
    spec_path = tmp_path / "factory.yaml"
    kit_dir = tmp_path / "kit"
    scion_factory.save_spec(spec, spec_path)
    scion_factory.compile_factory_boilerplate(spec, kit_dir)

    env_file = write_gke_factory_env(tmp_path, fake_hub.url)
    
    r_dry = subprocess.run(
        [sys.executable, "scripts/scion_factory.py", "provision", str(spec_path),
         "--kit", str(kit_dir), "--env", str(env_file), "--json"],
        cwd=ROOT, env=fake_cli_env(tmp_path, fake_hub.url), capture_output=True, text=True,
    )
    assert r_dry.returncode == 0, r_dry.stderr
    assert json.loads(r_dry.stdout)["apply"] is False
    assert "starter-service" not in fake_hub.projects_by_slug

    r_apply = subprocess.run(
        [sys.executable, "scripts/scion_factory.py", "provision", str(spec_path),
         "--kit", str(kit_dir), "--env", str(env_file), "--apply", "--go", "--json"],
        cwd=ROOT, env=fake_cli_env(tmp_path, fake_hub.url), capture_output=True, text=True,
    )
    assert r_apply.returncode == 0, r_apply.stderr + "\n" + r_apply.stdout
    out = json.loads(r_apply.stdout)
    assert out["ok"] is True, out
    assert out["apply"] is True
    assert {"starter-service", "starter-service-eval"} <= set(fake_hub.projects_by_slug)
    build_pid = fake_hub.projects_by_slug["starter-service"]["id"]

    # Verify full git-backed project creation payload (per-agent on GKE per k8s_runtime.go:4762)
    proj_posts = [r for r in fake_hub.requests if r["method"] == "POST" and r["path"] == "/api/v1/projects"]
    build_post = json.loads(proj_posts[0]["body"].decode("utf-8"))
    assert build_post["slug"] == "starter-service"
    assert build_post["workspaceMode"] == "per-agent"
    assert build_post["gitRemote"].endswith(".git")
    assert build_post["githubToken"] == "gh_fake_pat_for_test"
    assert "scion.dev/default-branch" in build_post["labels"]

    # Verify UUID service account ID (not email) in project settings
    assert fake_hub.project_settings[build_pid]["activeProfile"] == "gke"
    assert fake_hub.project_settings[build_pid]["defaultGCPIdentityMode"] == "assign"
    assert fake_hub.project_settings[build_pid]["defaultGCPIdentityServiceAccountID"] == "sa-uuid-1"
    assert fake_hub.project_settings[build_pid]["defaultGCPIdentityServiceAccountIDByProfile"] == {"gke": "sa-uuid-1"}

    # Verify dedicated deployer SA UUID and base64-encoded GitHub secrets are injected BEFORE lead starts
    import base64
    assert fake_hub.project_env[build_pid]["SCION_DEPLOYER_SA_ID"] == {
        "value": "sa-uuid-2",
        "injectionMode": "always",
    }
    assert base64.b64decode(fake_hub.project_secrets[build_pid]["GITHUB_TOKEN"]["value"]).decode("utf-8") == "gh_fake_pat_for_test"
    assert base64.b64decode(fake_hub.project_secrets[build_pid]["GH_TOKEN"]["value"]).decode("utf-8") == "gh_fake_pat_for_test"

    # Verify shared-dirs uses snake_case read_only and skips existing scratchpad
    sdirs = {d["name"]: d for d in fake_hub.shared_dirs[build_pid]}
    assert sdirs["knowledge-plane"]["read_only"] is True
    assert "INDEX.md" in fake_hub.shared_dir_files[(build_pid, "knowledge-plane")]

    # Verify K8s control plane CronJobs got namespace, IAP audience, and dedicated control SA
    klog_text = (tmp_path / "kubectl.log").read_text()
    assert '"namespace": "tfha-e2e"' in klog_text
    assert '"HUB_IAP_AUDIENCE"' in klog_text
    assert "factory-control@my-sandbox.iam.gserviceaccount.com" in klog_text

    # Single-repo GKE factories also publish <harness>-factory and invite/bind factory-control SA on both projects
    eval_pid = fake_hub.projects_by_slug["starter-service-eval"]["id"]
    hc_names = {hc["name"] for hc in getattr(fake_hub, "harness_configs_list", [])}
    assert any(n.endswith("-factory") for n in hc_names), hc_names
    invited_emails = {u["email"] for u in getattr(fake_hub, "provisioned_users", [])}
    assert "factory-control@my-sandbox.iam.gserviceaccount.com" in invited_emails
    for pid in (build_pid, eval_pid):
        members = getattr(fake_hub, "project_members", {}).get(pid, [])
        assert any(
            m.get("principalId") == "factory-control@my-sandbox.iam.gserviceaccount.com"
            and m.get("roleDefinitionId") == "rd-proj-admin"
            for m in members
        ), members

    # Verify lead started BEFORE schedule creation, and schedule uses upstream schema
    req_paths = [(r["method"], r["path"]) for r in fake_hub.requests]
    assert req_paths.index(("POST", f"/api/v1/projects/{build_pid}/agents")) < req_paths.index(
        ("POST", f"/api/v1/projects/{build_pid}/schedules")
    )
    assert "lead" in fake_hub.agents_by_project[build_pid]
    sched = next(s for s in fake_hub.schedules[build_pid] if s["name"] == "starter-service-lead-heartbeat")
    assert sched["eventType"] == "message"
    assert sched["agentName"] == "lead"
    assert sched["cronExpr"]


def test_scion_factory_provision_monorepo_prefixes_lead_eval_lead_and_schedule(fake_hub, tmp_path):
    spec = _starter()
    spec["project"]["slug"] = "k8s-version"
    spec["project"]["eval_slug"] = "k8s-version-eval"
    spec["monorepo"] = {
        "subdir": "staging/src/k8s.io/apimachinery/pkg/util/version",
        "agent_prefix": "k8s-version",
    }
    spec["infrastructure"]["runtime"] = "kubernetes"
    spec["infrastructure"]["deploy"] = "none"
    spec["team"]["build_roles"] = [r for r in spec["team"]["build_roles"] if r["role"] != "deployer"]
    spec["team"]["eval_roles"] = [r for r in spec["team"]["eval_roles"] if r["role"] != "eval-deployer"]
    for r in [*spec["team"]["build_roles"], *spec["team"]["eval_roles"]]:
        r["harness"] = "antigravity"
        r["model"] = "Gemini 4 Argon (High)"

    spec_path = tmp_path / "factory.yaml"
    kit_dir = tmp_path / "kit"
    scion_factory.save_spec(spec, spec_path)
    scion_factory.compile_factory_boilerplate(spec, kit_dir)
    env_file = write_gke_factory_env(tmp_path, fake_hub.url)

    r_apply = subprocess.run(
        [sys.executable, "scripts/scion_factory.py", "provision", str(spec_path),
         "--kit", str(kit_dir), "--env", str(env_file), "--apply", "--go", "--json"],
        cwd=ROOT, env=fake_cli_env(tmp_path, fake_hub.url), capture_output=True, text=True,
    )
    assert r_apply.returncode == 0, r_apply.stderr + "\n" + r_apply.stdout
    build_pid = fake_hub.projects_by_slug["k8s-version"]["id"]
    eval_pid = fake_hub.projects_by_slug["k8s-version-eval"]["id"]

    # Monorepo agents use <agent_prefix>-<role> names while keeping <role> templates
    assert "k8s-version-lead" in fake_hub.agents_by_project[build_pid]
    assert "k8s-version-eval-lead" in fake_hub.agents_by_project[eval_pid]
    agent_posts = [
        json.loads(r["body"].decode("utf-8"))
        for r in fake_hub.requests
        if r["method"] == "POST" and r["path"].endswith("/agents")
    ]
    assert any(p["name"] == "k8s-version-lead" and p["template"] == "lead" for p in agent_posts)
    assert any(p["name"] == "k8s-version-eval-lead" and p["template"] == "eval-lead" for p in agent_posts)

    sched = next(s for s in fake_hub.schedules[build_pid] if s["name"] == "k8s-version-lead-heartbeat")
    assert sched["agentName"] == "k8s-version-lead"
    msg_paths = [r["path"] for r in fake_hub.requests if r["method"] == "POST" and r["path"].endswith("/message")]
    assert f"/api/v1/projects/{build_pid}/agents/k8s-version-lead/message" in msg_paths

    # GKE clone-per-agent leads & spawned teammates pin trunk branches and document per-pod git clones
    assert any(p["name"] == "k8s-version-lead" and p.get("branch") == spec["vcs"]["build_branch"] for p in agent_posts)
    assert any(p["name"] == "k8s-version-eval-lead" and p.get("branch") == spec["vcs"]["eval_branch"] for p in agent_posts)
    lead_md = (kit_dir / "templates" / "lead" / "system-prompt.md").read_text()
    assert f"-b {spec['vcs']['build_branch']}" in lead_md
    shared_md = (kit_dir / "templates" / "_shared" / "agents.md").read_text()
    assert "HEAD:<branch>" in shared_md
    assert "isolated `/workspace` clone" in shared_md


def test_build_project_payload_defaults_shared_on_docker_and_per_agent_on_kubernetes():
    import provision_factory

    spec = _starter()
    docker_payload = provision_factory._build_project_payload(spec, "demo", "build_branch", {}, "tok", runtime="docker")
    k8s_payload = provision_factory._build_project_payload(spec, "demo", "build_branch", {}, "tok", runtime="kubernetes")
    override_payload = provision_factory._build_project_payload(
        spec, "demo", "build_branch", {"WORKSPACE_MODE": "shared"}, "tok", runtime="kubernetes"
    )
    assert docker_payload["workspaceMode"] == "shared"
    assert k8s_payload["workspaceMode"] == "per-agent"
    assert override_payload["workspaceMode"] == "shared"


def test_provision_go_waits_for_tmux_session_before_sending_go_message(fake_hub, tmp_path):
    spec = _starter()
    spec["project"]["slug"] = "k8s-version"
    spec["project"]["eval_slug"] = "k8s-version-eval"
    spec["monorepo"] = {
        "subdir": "staging/src/k8s.io/apimachinery/pkg/util/version",
        "agent_prefix": "k8s-version",
    }
    spec["infrastructure"]["runtime"] = "kubernetes"
    spec["infrastructure"]["deploy"] = "none"
    spec["team"]["build_roles"] = [r for r in spec["team"]["build_roles"] if r["role"] != "deployer"]
    spec["team"]["eval_roles"] = [r for r in spec["team"]["eval_roles"] if r["role"] != "eval-deployer"]
    for r in [*spec["team"]["build_roles"], *spec["team"]["eval_roles"]]:
        r["harness"] = "antigravity"
        r["model"] = "Gemini 4 Argon (High)"

    # Simulate large monorepo git checkout inside sciontool init: first 2 tmux checks exit 1, 3rd exits 0
    fake_hub.exec_exit_codes = {("pid-k8s-version", "k8s-version-lead"): [1, 1, 0]}

    spec_path = tmp_path / "factory.yaml"
    kit_dir = tmp_path / "kit"
    scion_factory.save_spec(spec, spec_path)
    scion_factory.compile_factory_boilerplate(spec, kit_dir)
    env_file = write_gke_factory_env(tmp_path, fake_hub.url)

    r_apply = subprocess.run(
        [
            sys.executable,
            "scripts/scion_factory.py",
            "provision",
            str(spec_path),
            "--kit",
            str(kit_dir),
            "--env",
            str(env_file),
            "--apply",
            "--go",
            "--json",
        ],
        cwd=ROOT,
        env=fake_cli_env(tmp_path, fake_hub.url),
        capture_output=True,
        text=True,
    )
    assert r_apply.returncode == 0, r_apply.stderr + "\n" + r_apply.stdout
    tmux_checks = fake_hub.exec_commands.get(("pid-k8s-version", "k8s-version-lead"), [])
    assert tmux_checks == [
        ["tmux", "has-session", "-t", "scion"],
        ["tmux", "has-session", "-t", "scion"],
        ["tmux", "has-session", "-t", "scion"],
    ]
    req_paths = [r["path"] for r in fake_hub.requests if r["method"] == "POST"]
    last_exec_idx = max(
        i for i, p in enumerate(req_paths) if p == "/api/v1/projects/pid-k8s-version/agents/k8s-version-lead/exec"
    )
    msg_idx = req_paths.index("/api/v1/projects/pid-k8s-version/agents/k8s-version-lead/message")
    assert last_exec_idx < msg_idx
    assert "SCION_DEPLOYER_SA_ID" not in getattr(fake_hub, "project_env", {}).get("pid-k8s-version", {})


def test_single_vm_docker_provision_injects_shared_deployer_sa_uuid_and_handles_dry_run_factory_projects(fake_hub, tmp_path):
    """Verify Single-VM Docker assign mode registers shared SA, injects SCION_DEPLOYER_SA_ID, and passes dry-run when FACTORY_PROJECTS pre-names new projects."""
    spec = _starter()
    spec["project"]["slug"] = "vm-app"
    spec["project"]["eval_slug"] = "vm-app-eval"
    spec["infrastructure"]["runtime"] = "docker"
    spec["infrastructure"]["gcp_identity_mode"] = "assign"
    spec["infrastructure"]["runtime_profile"] = ""
    spec_path = tmp_path / "factory.yaml"
    kit_dir = tmp_path / "kit"
    scion_factory.save_spec(spec, spec_path)
    scion_factory.compile_factory_boilerplate(spec, kit_dir)

    env_file = tmp_path / "vm.env"
    env_file.write_text(
        f"HUB_URL={fake_hub.url}\n"
        "SCION_HUB_TOKEN=fake-pat\n"
        "GCP_IDENTITY_DEFAULT=assign\n"
        "WORKER_SA_EMAIL=cabral-agent@single-vm-proj.iam.gserviceaccount.com\n"
        "FACTORY_PROJECTS=vm-app,vm-app-eval\n"
        "AGENT_START_TIMEOUT_SEC=5\n"
        "AGENT_START_POLL_SEC=0.05\n"
    )

    # Dry-run must succeed even when FACTORY_PROJECTS already lists uncreated projects
    r_dry = subprocess.run(
        [
            sys.executable,
            "scripts/scion_factory.py",
            "provision",
            str(spec_path),
            "--kit",
            str(kit_dir),
            "--env",
            str(env_file),
            "--json",
        ],
        cwd=ROOT,
        env=fake_cli_env(tmp_path, fake_hub.url),
        capture_output=True,
        text=True,
    )
    assert r_dry.returncode == 0, r_dry.stderr + "\n" + r_dry.stdout
    assert json.loads(r_dry.stdout)["ok"] is True

    r_apply = subprocess.run(
        [
            sys.executable,
            "scripts/scion_factory.py",
            "provision",
            str(spec_path),
            "--kit",
            str(kit_dir),
            "--env",
            str(env_file),
            "--apply",
            "--json",
        ],
        cwd=ROOT,
        env=fake_cli_env(tmp_path, fake_hub.url),
        capture_output=True,
        text=True,
    )
    assert r_apply.returncode == 0, r_apply.stderr + "\n" + r_apply.stdout
    build_pid = fake_hub.projects_by_slug["vm-app"]["id"]
    eval_pid = fake_hub.projects_by_slug["vm-app-eval"]["id"]
    assert fake_hub.project_settings[build_pid]["defaultGCPIdentityMode"] == "assign"
    assert fake_hub.project_settings[build_pid]["defaultGCPIdentityServiceAccountID"] == "sa-uuid-1"
    for pid in (build_pid, eval_pid):
        assert fake_hub.project_env[pid]["SCION_DEPLOYER_SA_ID"] == {
            "value": "sa-uuid-1",
            "injectionMode": "always",
        }
    put_env_idx = next(
        i for i, r in enumerate(fake_hub.requests)
        if r["method"] == "PUT" and r["path"].startswith("/api/v1/env/SCION_DEPLOYER_SA_ID") and build_pid in r["path"]
    )
    start_lead_idx = next(
        i for i, r in enumerate(fake_hub.requests)
        if r["method"] == "POST" and r["path"] == f"/api/v1/projects/{build_pid}/agents"
    )
    assert put_env_idx < start_lead_idx


def test_docker_passthrough_registers_explicit_deployer_sa_and_shell_expansion_handles_unset(fake_hub, tmp_path):
    """Verify Docker passthrough mode registers explicit DEPLOYER_SA_EMAIL and bash expansion omits empty --service-account."""
    spec = _starter()
    spec["project"]["slug"] = "pass-app"
    spec["project"]["eval_slug"] = "pass-app-eval"
    spec["infrastructure"]["runtime"] = "docker"
    spec["infrastructure"]["gcp_identity_mode"] = "passthrough"
    spec_path = tmp_path / "factory.yaml"
    kit_dir = tmp_path / "kit"
    scion_factory.save_spec(spec, spec_path)
    scion_factory.compile_factory_boilerplate(spec, kit_dir)

    # Verify POSIX shell expansion: unset -> no --service-account flag; set -> --service-account <id>
    unset_args = subprocess.check_output(
        ["bash", "-c", 'unset SCION_DEPLOYER_SA_ID; printf "%s\\n" ${SCION_DEPLOYER_SA_ID:+--service-account "$SCION_DEPLOYER_SA_ID"} "prompt"'],
        text=True,
    ).splitlines()
    assert unset_args == ["prompt"]
    set_args = subprocess.check_output(
        ["bash", "-c", 'SCION_DEPLOYER_SA_ID="sa-uuid-99"; printf "%s\\n" ${SCION_DEPLOYER_SA_ID:+--service-account "$SCION_DEPLOYER_SA_ID"} "prompt"'],
        text=True,
    ).splitlines()
    assert set_args == ["--service-account", "sa-uuid-99", "prompt"]

    env_file = tmp_path / "pass.env"
    env_file.write_text(
        f"HUB_URL={fake_hub.url}\n"
        "SCION_HUB_TOKEN=fake-pat\n"
        "GCP_IDENTITY_DEFAULT=passthrough\n"
        "DEPLOYER_SA_EMAIL=deploy-only@single-vm-proj.iam.gserviceaccount.com\n"
        "AGENT_START_TIMEOUT_SEC=5\n"
        "AGENT_START_POLL_SEC=0.05\n"
    )
    r_apply = subprocess.run(
        [
            sys.executable,
            "scripts/scion_factory.py",
            "provision",
            str(spec_path),
            "--kit",
            str(kit_dir),
            "--env",
            str(env_file),
            "--apply",
            "--json",
        ],
        cwd=ROOT,
        env=fake_cli_env(tmp_path, fake_hub.url),
        capture_output=True,
        text=True,
    )
    assert r_apply.returncode == 0, r_apply.stderr + "\n" + r_apply.stdout
    build_pid = fake_hub.projects_by_slug["pass-app"]["id"]
    assert fake_hub.project_env[build_pid]["SCION_DEPLOYER_SA_ID"] == {
        "value": "sa-uuid-1",
        "injectionMode": "always",
    }




