"""setup-sandbox.sh survives the GCP races seen on a fresh project (L28 Kubernetes run).

A fake `gcloud` on PATH simulates:
- IAM eventual consistency: the first add-iam-policy-binding for each service account says it "does not exist";
- a concurrent run: `firestore indexes composite create` returns ALREADY_EXISTS although `list` showed nothing.
"""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "setup-sandbox.sh"

FAKE_GCLOUD = r"""#!/usr/bin/env bash
state="$FAKE_STATE"
echo "$*" >> "$state/calls"
case "$*" in
  *"compute instances describe"*) echo "vm-sa@sandbox.iam.gserviceaccount.com" ;;
  *"projects describe"*) echo "123" ;;
  *"service-accounts describe"*) exit 1 ;;
  *"add-iam-policy-binding"*)
    key="$(echo "$*" | cksum | cut -d' ' -f1)"
    if [[ ! -e "$state/seen-$key" ]]; then
      touch "$state/seen-$key"
      echo "ERROR: (gcloud.projects.add-iam-policy-binding) INVALID_ARGUMENT: Service account x does not exist." >&2
      exit 1
    fi ;;
  *"indexes composite list"*) ;;
  *"indexes composite create"*)
    echo "ERROR: (gcloud.firestore.indexes.composite.create) ALREADY_EXISTS: index already exists" >&2
    exit 1 ;;
  *"firestore databases describe"*) ;;
  *"buckets describe"*) ;;
esac
exit 0
"""


def test_setup_sandbox_retries_iam_propagation_and_tolerates_existing_indexes(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gcloud").write_text(FAKE_GCLOUD)
    (bindir / "gcloud").chmod(0o755)
    (bindir / "sleep").write_text("#!/bin/sh\nexit 0\n")
    (bindir / "sleep").chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    env_file = tmp_path / "factory.env"
    env_file.write_text(
        "SANDBOX_PROJECT=sandbox\nSANDBOX_REGION=us-east1\nHUB_PROJECT=sandbox\nHUB_ZONE=us-east1-b\n"
        "HUB_VM=hub\nDEPLOYER_SA_NAME=deployer-sa\nRUNTIME_SA_NAME=svc\nDATA_BUCKET=sandbox-data\n"
    )
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "FACTORY_ENV": str(env_file),
           "FAKE_STATE": str(state), "DRY_RUN": "0"}
    env.pop("GITHUB_TOKEN", None)
    res = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    assert res.returncode == 0, res.stdout[-2000:] + res.stderr[-2000:]
    assert "index on eval_runs: exists" in res.stdout
    assert "done" in res.stdout


def _sandbox(tmp_path, env_text, dry_run):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "gcloud").write_text(FAKE_GCLOUD)
    (bindir / "gcloud").chmod(0o755)
    (bindir / "sleep").write_text("#!/bin/sh\nexit 0\n")
    (bindir / "sleep").chmod(0o755)
    state = tmp_path / f"state-{dry_run}"
    state.mkdir()
    env_file = tmp_path / "factory.env"
    env_file.write_text(env_text)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "FACTORY_ENV": str(env_file),
           "FAKE_STATE": str(state), "DRY_RUN": dry_run}
    for name in ("GITHUB_TOKEN", "DEPLOYER_SA_NAME", "RUNTIME_SA_NAME"):
        env.pop(name, None)
    res = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    calls = (state / "calls").read_text() if (state / "calls").exists() else ""
    return res, calls


NONE_ENV = ("SANDBOX_PROJECT=sandbox\nSANDBOX_REGION=us-east1\nHUB_PROJECT=sandbox\nHUB_ZONE=us-east1-b\n"
            "HUB_VM=hub\nDATA_BUCKET=sandbox-data\n"
            "# Nothing to deploy (infrastructure.deploy: none): no service, runtime or deployer variables.\n")


def test_setup_sandbox_with_nothing_to_deploy(tmp_path):
    """A deploy-none factory.env has no DEPLOYER_SA_NAME / RUNTIME_SA_NAME: skip the deploy identities, keep the rest."""
    for dry_run in ("1", "0"):
        res, calls = _sandbox(tmp_path, NONE_ENV, dry_run)
        out = res.stdout + res.stderr
        assert res.returncode == 0, out[-3000:]
        assert "iam service-accounts create" not in calls + out
        assert "service-accounts add-iam-policy-binding" not in calls + out
        assert "cloudbuild.builds.builder" not in calls + out
        assert "nothing to deploy" in res.stdout
        assert "services enable" in calls + out
        assert "indexes composite" in calls + out
        assert "roles/datastore.user" in calls + out and "serviceUsageConsumer" in calls + out
        assert "buckets" in calls + out
        assert "done" in res.stdout
        if dry_run == "0":
            assert "firestore databases describe" in calls and "indexes composite create" in calls


def test_setup_sandbox_rejects_half_a_deploy_config(tmp_path):
    res, _ = _sandbox(tmp_path, NONE_ENV + "DEPLOYER_SA_NAME=deployer-sa\n", "1")
    assert res.returncode != 0 and "RUNTIME_SA_NAME" in res.stderr


def _sandbox_with_kubectl(tmp_path, env_text, dry_run):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "gcloud").write_text(FAKE_GCLOUD)
    (bindir / "gcloud").chmod(0o755)
    (bindir / "sleep").write_text("#!/bin/sh\nexit 0\n")
    (bindir / "sleep").chmod(0o755)
    
    # Fake kubectl (also records stdin manifests for `kubectl apply -f -`)
    fake_kubectl = r"""#!/usr/bin/env bash
state="$FAKE_STATE"
echo "kubectl $*" >> "$state/calls"
if [[ "$*" == *"apply -f -"* ]]; then
  cat >> "$state/manifests"
  echo "---" >> "$state/manifests"
fi
if [[ "$1" == "get" && "$2" == "serviceaccount" ]]; then
  exit 1
fi
exit 0
"""
    (bindir / "kubectl").write_text(fake_kubectl)
    (bindir / "kubectl").chmod(0o755)

    state = tmp_path / f"state-{dry_run}"
    state.mkdir()
    env_file = tmp_path / "factory.env"
    env_file.write_text(env_text)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "FACTORY_ENV": str(env_file),
           "FAKE_STATE": str(state), "DRY_RUN": dry_run}
    for name in ("GITHUB_TOKEN", "DEPLOYER_SA_NAME", "RUNTIME_SA_NAME"):
        env.pop(name, None)
    res = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    calls = (state / "calls").read_text() if (state / "calls").exists() else ""
    manifests = (state / "manifests").read_text() if (state / "manifests").exists() else ""
    return res, calls + "\n" + manifests


def test_setup_sandbox_kubernetes_mode_creates_worker_sa_and_keeps_docker_deploy_none_clean(tmp_path):
    k8s_env = (
        "AGENT_RUNTIME=kubernetes\nSANDBOX_PROJECT=sandbox\nSANDBOX_REGION=us-central1\n"
        "HUB_PROJECT=hub-proj\nHUB_REGION=us-east1\nHUB_URL=https://scion-prod-123456.us-east1.run.app\n"
        "HUB_K8S_NAMESPACE=scion-prod\nHUB_SA=scion-prod-hub\nDATA_BUCKET=sandbox-data\n"
        "DEPLOYER_SA_NAME=agent-deployer\nRUNTIME_SA_NAME=app-serving\n"
    )
    res, calls = _sandbox_with_kubectl(tmp_path, k8s_env, "0")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "compute instances describe" not in calls
    assert "iam service-accounts create agent-worker" in calls
    assert "iam service-accounts create factory-control" in calls
    assert "iap web add-iam-policy-binding --project=hub-proj --region=us-east1 --resource-type=cloud-run --service=scion-prod --member=serviceAccount:agent-worker@sandbox.iam.gserviceaccount.com --role=roles/iap.httpsResourceAccessor" in calls
    assert "iap web add-iam-policy-binding --project=hub-proj --region=us-east1 --resource-type=cloud-run --service=scion-prod --member=serviceAccount:factory-control@sandbox.iam.gserviceaccount.com --role=roles/iap.httpsResourceAccessor" in calls
    assert "serviceAccount:hub-proj.svc.id.goog[scion-prod/scion-worker]" in calls
    assert "serviceAccount:hub-proj.svc.id.goog[scion-prod/scion-factory-control]" in calls
    assert "serviceAccount:hub-proj.svc.id.goog[scion-prod/scion-deployer]" in calls
    # scion-worker and scion-factory-control must NOT share the same GSA annotation (prevents AmbiguousGSAs 400)
    assert "annotate serviceaccount -n scion-prod scion-worker iam.gke.io/gcp-service-account=agent-worker@sandbox.iam.gserviceaccount.com" in calls
    assert "annotate serviceaccount -n scion-prod scion-factory-control iam.gke.io/gcp-service-account=factory-control@sandbox.iam.gserviceaccount.com" in calls
    assert "annotate serviceaccount -n scion-prod scion-worker iam.gke.io/gcp-service-account=factory-control@" not in calls
    assert "annotate serviceaccount -n scion-prod scion-factory-control iam.gke.io/gcp-service-account=agent-worker@" not in calls
    assert "annotate serviceaccount -n scion-prod scion-deployer iam.gke.io/gcp-service-account=agent-deployer@sandbox.iam.gserviceaccount.com" in calls
    # Hub SA normalized and granted token creator + user on WORKER_SA for /gcp-service-accounts VerifyImpersonation
    assert "service-accounts add-iam-policy-binding agent-worker@sandbox.iam.gserviceaccount.com --project=sandbox --member=serviceAccount:scion-prod-hub@hub-proj.iam.gserviceaccount.com --role=roles/iam.serviceAccountTokenCreator" in calls
    # K8s Role + RoleBinding allowing Hub SA to list/get serviceaccounts for discoverAssignKSA
    assert 'resources: ["serviceaccounts"]' in calls
    assert "name: scion-prod-hub@hub-proj.iam.gserviceaccount.com" in calls

def test_setup_sandbox_gke_mode_grants_cloud_telemetry_writer_roles(tmp_path):
    k8s_env = (
        "AGENT_RUNTIME=kubernetes\nSANDBOX_PROJECT=sandbox\nSANDBOX_REGION=us-central1\n"
        "HUB_PROJECT=hub-proj\nHUB_REGION=us-east1\nHUB_URL=https://scion-prod-123456.us-east1.run.app\n"
        "HUB_K8S_NAMESPACE=scion-prod\nHUB_SA=scion-prod-hub\nDATA_BUCKET=sandbox-data\n"
        "DEPLOYER_SA_NAME=agent-deployer\nRUNTIME_SA_NAME=app-serving\n"
    )
    res, calls = _sandbox_with_kubectl(tmp_path, k8s_env, "0")
    assert res.returncode == 0, res.stdout + res.stderr
    out = res.stdout + res.stderr
    for role in ("roles/logging.logWriter", "roles/cloudtrace.agent", "roles/monitoring.metricWriter"):
        assert f"--member=serviceAccount:agent-worker@sandbox.iam.gserviceaccount.com --role={role}" in calls
